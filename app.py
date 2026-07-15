"""
GrabitPro yt-dlp backend.

Endpoints:
  GET  /            -> health check
  POST /info        -> { url } -> video metadata + downloadable formats
  POST /download    -> { url, format, name? } -> merged mp4 stream (supports
                        video-only + bestaudio merging up to 4K via ffmpeg)
  GET  /stream      -> ?u=<direct_url>&name=<filename> streams a raw URL

Auth: every request except `/` requires `Authorization: Bearer $API_SECRET`.
"""

import os
import re
import base64
import shutil
import tempfile
import subprocess
import logging
import glob
from urllib.parse import quote

import requests
import yt_dlp
from flask import Flask, request, jsonify, Response, abort, stream_with_context
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("grabit")

API_SECRET = os.environ.get("API_SECRET", "")
if not API_SECRET:
    log.warning("API_SECRET is empty — endpoints are unprotected!")

# ---------- Load YouTube cookies from env (raw or base64) ----------
COOKIES_FILE = None
_raw = os.environ.get("YT_COOKIES", "").strip()
_b64 = os.environ.get("YT_COOKIES_B64", "").strip()
if _b64 and not _raw:
    try:
        _raw = base64.b64decode(_b64).decode("utf-8")
        log.info("Decoded YT_COOKIES_B64 (%d chars)", len(_raw))
    except Exception as e:
        log.warning("YT_COOKIES_B64 decode failed: %s", e)
if _raw:
    try:
        _f = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
        _f.write(_raw)
        _f.close()
        COOKIES_FILE = _f.name
        log.info("Loaded YouTube cookies -> %s (%d chars)", COOKIES_FILE, len(_raw))
    except Exception as e:
        log.warning("Failed to write cookies file: %s", e)
else:
    log.info("No YT_COOKIES / YT_COOKIES_B64 set — running without cookies")

YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()
if YTDLP_PROXY:
    log.info("Using configured yt-dlp proxy")

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


# ---------- auth ----------
def check_auth():
    if not API_SECRET:
        return
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or header[7:].strip() != API_SECRET:
        abort(401, description="Invalid or missing bearer token")


# ---------- helpers ----------
YOUTUBE_CLIENT_PROFILES = (
    None,  # let the installed yt-dlp release choose its current safest defaults
    ["android"],  # most reliable logged-out progressive fallback
    ["android_vr", "web", "web_safari"],
    ["web", "web_safari", "mweb"],
    ["tv_downgraded", "web"],
    ["web_embedded", "web"],
    ["android", "ios"],
)

# Railway images often do not have a local JS runtime for YouTube's changing
# signature/n-challenge code. Let yt-dlp fetch its official EJS solver bundle
# at runtime; without this many public YouTube links expose only storyboards.
YOUTUBE_REMOTE_COMPONENTS = ["ejs:github"]


def build_ydl_options(player_clients=None, skip_configs=False, use_cookies=True):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "format": "all",
        "ignore_no_formats_error": True,
        "remote_components": YOUTUBE_REMOTE_COMPONENTS,
        "extractor_retries": 3,
        "retries": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        },
    }
    if player_clients:
        yt_args = {"player_client": player_clients}
        if skip_configs:
            yt_args["player_skip"] = ["configs"]
        opts["extractor_args"] = {"youtube": yt_args}
    if COOKIES_FILE and use_cookies:
        opts["cookiefile"] = COOKIES_FILE
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    return opts


YDL_COMMON = build_ydl_options(["web", "mweb"], skip_configs=True)


def normalize_info(info: dict, fallback_url: str):
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]
    if not info.get("webpage_url"):
        info["webpage_url"] = fallback_url
    return info


def is_direct_media_info(info: dict):
    ext = (info.get("ext") or "").lower()
    proto = (info.get("protocol") or "").lower()
    if not info.get("url"):
        return False
    if ext in {"mhtml", "html", "json", "xml", "vtt", "srt", "srv1", "srv2", "srv3", "ttml"}:
        return False
    if proto in {"mhtml", "http_dash_segments"}:
        return False
    return True


def youtube_placeholder_formats(info: dict, url: str):
    """Expose selectable quality rows even when metadata extraction succeeds
    but YouTube withholds direct format URLs on the info endpoint. The download
    endpoint will run yt-dlp again with the height-scoped selector.
    """
    webpage_url = info.get("webpage_url") or url
    # If YouTube only returns metadata (common with SABR / PO-token /
    # challenge changes), reported height is often just the Android 360p
    # fallback. Still expose high-quality selectors; /download will ask yt-dlp
    # for the best available stream at or below the chosen height.
    max_height = 4320
    heights = [4320, 2160, 1440, 1080, 720, 480, 360]
    return [{
        "format_id": "",
        "ext": "mp4",
        "quality": f"{h}p",
        "kind": "video-only",
        "height": h,
        "fps": None,
        "abr": None,
        "vcodec": "unknown",
        "acodec": "none",
        "filesize": None,
        "url": webpage_url,
    } for h in heights if h <= max_height]


def format_score(info: dict):
    picked = pick_formats(info)
    max_height = max((f.get("height") or 0) for f in picked) if picked else 0
    adaptive_count = sum(1 for f in picked if f.get("kind") == "video-only")
    return (max_height, adaptive_count, len(picked))


def extract_info_with_fallback(url: str):
    attempts = []
    last_info = None
    best_info = None
    best_score = (-1, -1, -1)
    is_youtube = "youtube.com" in url.lower() or "youtu.be" in url.lower()

    # YouTube cookies often go stale and can suppress formats. Try the
    # currently reliable logged-out clients first, then fall back to cookies.
    if is_youtube:
        for clients in (None, ["android"], ["android", "ios"], ["android_vr", "web", "web_safari"]):
            if clients is None:
                attempts.append(("yt-dlp:default:nocookies", build_ydl_options(None, use_cookies=False)))
                continue
            attempts.append((
                "youtube:" + ",".join(clients) + ":nocookies",
                build_ydl_options(clients, use_cookies=False),
            ))

    for clients in YOUTUBE_CLIENT_PROFILES:
        if clients is None:
            attempts.append(("yt-dlp:default", build_ydl_options(None)))
            continue
        skip_configs = all(c in {"web", "web_safari", "mweb"} for c in clients)
        attempts.append((
            "youtube:" + ",".join(clients),
            build_ydl_options(clients, skip_configs=skip_configs),
        ))

    if not is_youtube:
        for clients in (None, ["android_vr", "web", "web_safari"], ["android"], ["android", "ios"]):
            if clients is None:
                attempts.append(("yt-dlp:default:nocookies", build_ydl_options(None, use_cookies=False)))
                continue
            attempts.append((
                "youtube:" + ",".join(clients) + ":nocookies",
                build_ydl_options(clients, use_cookies=False),
            ))

    last_error = None
    used_nocookies = False
    for label, opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = normalize_info(ydl.extract_info(url, download=False), url)
            last_info = info
            picked = pick_formats(info)
            if picked or is_direct_media_info(info):
                info["_grabit_client_profile"] = label
                info["_grabit_used_cookies"] = ":nocookies" not in label
                if not is_youtube:
                    log.info("yt-dlp succeeded for %s via %s", url, label)
                    return info

                score = format_score(info)
                log.info("yt-dlp candidate for %s via %s: max_height=%s adaptive=%s formats=%s",
                         url, label, score[0], score[1], score[2])
                if score > best_score:
                    best_info = info
                    best_score = score
                # Do not stop at Android's common 360p-only fallback. Keep
                # searching for adaptive 720p/1080p/4K formats.
                if score[0] >= 2160 or (score[0] >= 1080 and score[1] >= 2):
                    log.info("yt-dlp succeeded for %s via %s", url, label)
                    return info
                continue
            last_error = RuntimeError("No downloadable formats returned")
            log.info("yt-dlp returned no formats for %s via %s", url, label)
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            log.info("yt-dlp failed for %s via %s: %s", url, label, e)
        except Exception as e:
            last_error = e
            log.info("yt-dlp failed for %s via %s: %s", url, label, e)

    if is_youtube and isinstance(best_info, dict):
        log.info("yt-dlp using best available candidate for %s: max_height=%s adaptive=%s formats=%s",
                 url, best_score[0], best_score[1], best_score[2])
        return best_info

    if is_youtube and isinstance(last_info, dict) and (last_info.get("title") or last_info.get("id")):
        log.info("yt-dlp metadata-only fallback for %s; YouTube blocked formats", url)
        return {
            "error": "YOUTUBE_BLOCKED",
            "fallback": True,
            "details": (
                "YouTube returned metadata but no downloadable streams. "
                "Refresh backend cookies from a logged-in browser or configure YTDLP_PROXY."
            ),
            "formats": [],
            "webpage_url": last_info.get("webpage_url") or url,
            "title": last_info.get("title") or "YouTube video",
            "thumbnail": last_info.get("thumbnail"),
            "uploader": last_info.get("uploader") or last_info.get("channel"),
            "duration": last_info.get("duration"),
        }

    log.info("yt-dlp exhausted all fallbacks for %s: %s", url, last_error)
    details = str(last_error or "").strip()
    if is_youtube and (not details or "no downloadable formats" in details.lower() or "no formats" in details.lower()):
        return {
            "error": "YOUTUBE_BLOCKED",
            "fallback": True,
            "details": (
                "YouTube returned no downloadable streams from this server. "
                "Refresh backend cookies from a logged-in browser or configure YTDLP_PROXY."
            ),
            "formats": [],
            "webpage_url": url,
            "title": "YouTube video",
        }

    return {
        "error": "SERVICE_UNAVAILABLE",
        "fallback": True,
        "details": details or "No downloadable formats returned",
        "formats": [],
        "webpage_url": url,
        "title": "Unavailable",
    }


def pick_formats(info: dict):
    """Return every useful downloadable format the extractor exposed.

    We keep progressive (audio+video), video-only, and audio-only entries so the
    frontend can offer up to 4K by merging server-side on demand.
    """
    formats = info.get("formats") or []
    out = []
    seen = set()

    for f in formats:
        url = f.get("url")
        if not url:
            continue

        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = (f.get("ext") or "mp4").lower()
        protocol = (f.get("protocol") or "").lower()
        height = f.get("height")
        fps = f.get("fps")
        abr = f.get("abr")
        fmt_note = f.get("format_note") or ""

        if ext in {"mhtml", "html", "json", "xml", "vtt", "srt", "srv1", "srv2", "srv3", "ttml"}:
            continue
        if protocol in {"mhtml", "http_dash_segments"}:
            continue

        is_video = vcodec and vcodec != "none"
        is_audio = acodec and acodec != "none"

        if is_video:
            base_label = f"{height}p" if height else (fmt_note or ext.upper())
            if fps and fps >= 50 and height:
                base_label = f"{height}p{int(fps)}"
            if is_audio:
                kind = "video"
                label = base_label
            else:
                kind = "video-only"
                label = base_label  # UI will indicate "merged"
        elif is_audio:
            kind = "audio"
            if abr:
                label = f"{ext.upper()} · {int(abr)}kbps"
            else:
                label = f"{ext.upper()} audio"
        else:
            continue

        key = (kind, label, ext)
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "format_id": str(f.get("format_id") or ""),
            "ext": ext,
            "quality": label,
            "kind": kind,
            "height": height,
            "fps": fps,
            "abr": abr,
            "vcodec": vcodec,
            "acodec": acodec,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "url": url,
        })

    if not out:
        best = info.get("url")
        if best and is_direct_media_info(info):
            out.append({
                "format_id": "best",
                "ext": info.get("ext") or "mp4",
                "quality": "Best available",
                "kind": "video",
                "height": info.get("height"),
                "fps": info.get("fps"),
                "abr": info.get("abr"),
                "vcodec": info.get("vcodec"),
                "acodec": info.get("acodec"),
                "filesize": None,
                "url": best,
            })

    def sort_key(item):
        m = re.match(r"(\d+)p", item["quality"] or "")
        h = int(m.group(1)) if m else (item.get("height") or 0)
        rank = {"video": 0, "video-only": 1, "audio": 2}.get(item["kind"], 3)
        return (rank, -h)

    out.sort(key=sort_key)
    return out


# ---------- routes ----------
@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "grabit-ytdlp",
        "version": 13,
        "yt_dlp_version": getattr(yt_dlp.version, "__version__", "unknown"),
        "cookies_loaded": bool(COOKIES_FILE),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "proxy_configured": bool(YTDLP_PROXY),
    })


@app.post("/info")
def info():
    check_auth()
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing 'url' in body"}), 400

    try:
        info_obj = extract_info_with_fallback(url)
        if isinstance(info_obj, dict) and info_obj.get("fallback"):
            return jsonify(info_obj), 200
    except yt_dlp.utils.DownloadError as e:
        log.info("yt-dlp exhausted fallbacks for %s: %s", url, e)
        return jsonify({"error": f"Could not fetch video: {str(e)}"}), 400
    except Exception as e:
        log.exception("Unexpected error for %s", url)
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

    return jsonify({
        "title": info_obj.get("title") or "Untitled",
        "thumbnail": info_obj.get("thumbnail"),
        "duration": info_obj.get("duration"),
        "uploader": info_obj.get("uploader") or info_obj.get("channel"),
        "webpage_url": info_obj.get("webpage_url") or url,
        "extractor": info_obj.get("extractor_key") or info_obj.get("extractor"),
        "client_profile": info_obj.get("_grabit_client_profile"),
        "used_cookies": info_obj.get("_grabit_used_cookies"),
        "formats": pick_formats(info_obj),
    })


# ---------- download (merged) ----------
def _safe_name(name: str) -> str:
    name = re.sub(r"[\r\n\"\\/]+", " ", name or "").strip()
    name = re.sub(r"[^\w.\-() ]+", "_", name)
    return (name[:120] or "video.mp4")


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "ios"}


def _find_output_file(tmpdir: str):
    candidates = [
        p for p in glob.glob(os.path.join(tmpdir, "out.*"))
        if os.path.isfile(p) and not os.path.basename(p).startswith(".")
    ]
    if not candidates:
        candidates = [
            os.path.join(tmpdir, f)
            for f in os.listdir(tmpdir)
            if not f.startswith(".") and os.path.isfile(os.path.join(tmpdir, f))
        ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
    return candidates[0]


def _transcode_ios_mp4(source_path: str, tmpdir: str):
    """Create an iPhone-compatible MP4: H.264/AAC, yuv420p, faststart."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is missing on the server")

    final_path = os.path.join(tmpdir, "ios-transcoded.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", source_path,
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-profile:v", "main",
        "-level", "4.1",
        "-pix_fmt", "yuv420p",
        "-vf", "scale='min(1920,iw)':-2",
        "-c:a", "aac",
        "-b:a", "160k",
        "-movflags", "+faststart",
        final_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=900)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "ignore").strip()
        raise RuntimeError(err[:500] or "ffmpeg iPhone conversion failed")
    return final_path


@app.route("/download", methods=["GET", "POST"])
def download():
    """Download a chosen format and, when needed, merge video+audio server-side.

    Accepts JSON POST or query string GET with:
      url         (required)  page URL
      format      (optional)  yt-dlp format selector (default best up to 4K)
      format_id   (optional)  a single format id — will be merged with bestaudio
      name        (optional)  suggested filename
      container   (optional)  mp4 (default) or mkv
    """
    check_auth()
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
    else:
        data = request.args

    url = (data.get("url") or "").strip()
    fmt = (data.get("format") or "").strip()
    fmt_id = (data.get("format_id") or "").strip()
    try:
        height = int(data.get("height") or 0)
    except (TypeError, ValueError):
        height = 0
    container = (data.get("container") or "mp4").strip().lower()
    if container not in {"mp4", "mkv", "webm", "m4a", "mp3"}:
        container = "mp4"
    ios_compat = (
        _truthy(data.get("ios"))
        or str(data.get("compat") or "").strip().lower() == "ios"
        or str(data.get("transcode") or "").strip().lower() == "ios"
        or _truthy(data.get("force_transcode"))
    )
    if ios_compat:
        container = "mp4"
    name = _safe_name(data.get("name") or f"video.{container}")

    if not url:
        return jsonify({"error": "Missing 'url'"}), 400

    # Build a resilient yt-dlp -f selector. The chain tries the exact format_id
    # first, then progressively falls back to height-scoped video+audio pairs,
    # and finally to "best" — never to a bare "best" that YouTube maps to 360p.
    h = height if height and height > 0 else 2160

    def height_chain(h_cap: int) -> str:
        return (
            f"bestvideo[height<={h_cap}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={h_cap}]+bestaudio/"
            f"best[height<={h_cap}]"
        )

    if not fmt:
        parts = []
        if fmt_id:
            parts.append(f"{fmt_id}+bestaudio[ext=m4a]/{fmt_id}+bestaudio")
        parts.append(height_chain(h))
        # Absolute last resort: allow yt-dlp's own best merged selection.
        parts.append("bestvideo+bestaudio/best")
        fmt = "/".join(parts)
    else:
        # Frontend supplied a selector — still append a safety net so we never
        # collapse to a bare "best" (which yields 360p on YouTube).
        if "bestvideo" not in fmt:
            fmt = f"{fmt}/{height_chain(h)}/bestvideo+bestaudio/best"

    if ios_compat:
        # For Instagram/iOS, do not require a pre-existing AVC stream. Some
        # reels expose only VP9/AV1-ish MP4 variants; download the best usable
        # stream first, then force a real H.264/AAC faststart MP4 with ffmpeg.
        ios_height = f"[height<={h}]" if h else ""
        fmt = "/".join([
            f"bestvideo*{ios_height}+bestaudio/best{ios_height}",
            f"bestvideo{ios_height}+bestaudio/best{ios_height}",
            "bestvideo*+bestaudio/best",
            "best",
        ])

    tmpdir = tempfile.mkdtemp(prefix="grabit_")
    outtmpl = os.path.join(tmpdir, "out.%(ext)s")

    def build_cmd(target_fmt: str, out_template: str, with_cookies: bool, player_clients: str):
        c = [
            "yt-dlp",
            "-f", target_fmt,
            "--no-playlist",
            "--no-warnings",
            "--quiet",
            "--no-progress",
            "-o", out_template,
        ]
        if container in {"mp4", "mkv", "webm"}:
            c += ["--merge-output-format", container]
        if container in {"mp3", "m4a"}:
            c += ["-x", "--audio-format", container]
        c += [
            "--remote-components", "ejs:github",
            "--extractor-retries", "3",
            "--retries", "5",
            "--fragment-retries", "5",
        ]
        if YTDLP_PROXY:
            c += ["--proxy", YTDLP_PROXY]
        if with_cookies and COOKIES_FILE:
            c += ["--cookies", COOKIES_FILE]
        if player_clients:
            c += ["--extractor-args", f"youtube:player_client={player_clients}"]
        c += [
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        ]
        c.append(url)
        return c

    # Attempt sequence: try authenticated cookies across multiple YouTube
    # clients before logged-out attempts. Railway/datacenter IPs often hit
    # YouTube's bot wall when logged out, so cookie-backed attempts must cover
    # the same height-scoped fallbacks too. Never fall back to a bare "best" —
    # that maps to 360p on YouTube.
    fallback_fmt = fmt if ios_compat else height_chain(h) + "/bestvideo+bestaudio/best"
    attempts = []
    if COOKIES_FILE:
        attempts.extend([
            (fmt, outtmpl + ".c0", True, ""),
            (fallback_fmt, outtmpl + ".c1", True, ""),
            (fallback_fmt, outtmpl + ".c2", True, "web,mweb,android"),
            (fallback_fmt, outtmpl + ".c3", True, "android"),
            (fallback_fmt, outtmpl + ".c4", True, "android,ios"),
            (fallback_fmt, outtmpl + ".c5", True, "android_vr,web,web_safari"),
        ])
    attempts.extend([
        (fmt, outtmpl, False, ""),
        (fallback_fmt, outtmpl + ".r1", False, ""),
        (fallback_fmt, outtmpl + ".r2", False, "android"),
        (fallback_fmt, outtmpl + ".r3", False, "android,ios"),
        (fallback_fmt, outtmpl + ".r4", False, "android_vr,web,web_safari"),
    ])

    proc = None
    used_tmpdir = tmpdir
    last_err = ""
    for target_fmt, out_template, with_cookies, clients in attempts:
        attempt_dir = os.path.dirname(out_template) if out_template != outtmpl else tmpdir
        if attempt_dir != tmpdir:
            attempt_dir = tempfile.mkdtemp(prefix="grabit_")
            out_template = os.path.join(attempt_dir, "out.%(ext)s")
        cmd = build_cmd(target_fmt, out_template, with_cookies, clients)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=900)
        except subprocess.TimeoutExpired:
            shutil.rmtree(attempt_dir, ignore_errors=True)
            return jsonify({"error": "Download timed out"}), 504
        except FileNotFoundError:
            shutil.rmtree(attempt_dir, ignore_errors=True)
            return jsonify({"error": "yt-dlp binary is missing on the server"}), 500

        if proc.returncode == 0:
            used_tmpdir = attempt_dir
            break

        last_err = proc.stderr.decode("utf-8", "ignore").strip()
        log.info("yt-dlp download attempt failed (clients=%s cookies=%s fmt=%s): %s",
                 clients, with_cookies, target_fmt[:80], last_err[:300])
        if attempt_dir != tmpdir:
            shutil.rmtree(attempt_dir, ignore_errors=True)
    else:
        shutil.rmtree(tmpdir, ignore_errors=True)
        friendly = last_err[:400] or "Download failed"
        lower_err = last_err.lower()
        if (
            "sign in to confirm" in lower_err
            or "not a bot" in lower_err
            or "requested format is not available" in lower_err
        ):
            friendly = (
                "YouTube is blocking this server or the current cookies are stale. "
                "Refresh backend cookies from a logged-in browser, or configure YTDLP_PROXY "
                "with a residential/proxy IP and redeploy."
            )
        return jsonify({"error": friendly}), 500

    tmpdir = used_tmpdir


    # Find the produced file.
    path = _find_output_file(tmpdir)
    if not path:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "No output file produced"}), 500

    ios_transcoded = False
    if ios_compat and container == "mp4":
        try:
            original_path = path
            path = _transcode_ios_mp4(original_path, tmpdir)
            ios_transcoded = True
            log.info("iOS transcode succeeded for %s", url)
        except subprocess.TimeoutExpired:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return jsonify({"error": "iPhone conversion timed out"}), 504
        except Exception as e:
            log.info("iOS transcode failed for %s: %s", url, e)
            shutil.rmtree(tmpdir, ignore_errors=True)
            return jsonify({"error": f"iPhone conversion failed: {str(e)[:300]}"}), 500

    size = os.path.getsize(path)

    real_ext = "mp4" if ios_transcoded else (os.path.splitext(path)[1].lstrip(".") or container)
    if not name.lower().endswith("." + real_ext.lower()):
        name = os.path.splitext(name)[0] + "." + real_ext

    content_type = {
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "webm": "video/webm",
        "m4a": "audio/mp4",
        "mp3": "audio/mpeg",
    }.get(real_ext.lower(), "application/octet-stream")

    @stream_with_context
    def gen():
        try:
            with open(path, "rb") as fp:
                while True:
                    chunk = fp.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    headers = {
        "Content-Disposition": f'attachment; filename="{quote(name)}"',
        "Content-Length": str(size),
        "Cache-Control": "no-store",
    }
    if ios_transcoded:
        headers["X-IOS-Transcoded"] = "1"

    return Response(
        gen(),
        content_type=content_type,
        headers=headers,
    )


@app.get("/stream")
def stream():
    check_auth()
    src = request.args.get("u", "").strip()
    name = _safe_name(request.args.get("name", "video.mp4"))
    if not src:
        return jsonify({"error": "Missing 'u'"}), 400

    upstream = requests.get(src, stream=True, timeout=30, headers={
        "User-Agent": YDL_COMMON["http_headers"]["User-Agent"],
    })
    if upstream.status_code >= 400:
        return jsonify({"error": f"Upstream {upstream.status_code}"}), 502

    def generate():
        for chunk in upstream.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    disposition = f'attachment; filename="{quote(name)}"'
    return Response(
        generate(),
        content_type=upstream.headers.get("Content-Type", "application/octet-stream"),
        headers={
            "Content-Disposition": disposition,
            "Content-Length": upstream.headers.get("Content-Length", ""),
        },
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
