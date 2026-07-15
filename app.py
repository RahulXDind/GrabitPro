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
    ["android"],
    ["android", "ios"],
    ["web", "mweb"],
    ["tv_embedded", "web"],
)


def build_ydl_options(player_clients=None, skip_configs=False, use_cookies=True):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "format": "all",
        "ignore_no_formats_error": True,
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


def extract_info_with_fallback(url: str):
    attempts = []
    for clients in YOUTUBE_CLIENT_PROFILES:
        skip_configs = all(c in {"web", "mweb"} for c in clients)
        attempts.append((
            "youtube:" + ",".join(clients),
            build_ydl_options(clients, skip_configs=skip_configs),
        ))
    for clients in (["android"], ["android", "ios"]):
        attempts.append((
            "youtube:" + ",".join(clients) + ":nocookies",
            build_ydl_options(clients, use_cookies=False),
        ))
    attempts.append(("default", build_ydl_options(None)))

    last_error = None
    used_nocookies = False
    for label, opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = normalize_info(ydl.extract_info(url, download=False), url)
            if pick_formats(info) or is_direct_media_info(info):
                log.info("yt-dlp succeeded for %s via %s", url, label)
                info["_grabit_client_profile"] = label
                info["_grabit_used_cookies"] = ":nocookies" not in label
                return info
            last_error = RuntimeError("No downloadable formats returned")
            log.info("yt-dlp returned no formats for %s via %s", url, label)
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            log.info("yt-dlp failed for %s via %s: %s", url, label, e)
        except Exception as e:
            last_error = e
            log.info("yt-dlp failed for %s via %s: %s", url, label, e)

    log.info("yt-dlp exhausted all fallbacks for %s: %s", url, last_error)
    return {
        "error": "SERVICE_UNAVAILABLE",
        "fallback": True,
        "details": str(last_error or "No downloadable formats returned"),
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
        "version": 4,
        "cookies_loaded": bool(COOKIES_FILE),
        "ffmpeg": bool(shutil.which("ffmpeg")),
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
    container = (data.get("container") or "mp4").strip().lower()
    if container not in {"mp4", "mkv", "webm", "m4a", "mp3"}:
        container = "mp4"
    name = _safe_name(data.get("name") or f"video.{container}")

    if not url:
        return jsonify({"error": "Missing 'url'"}), 400

    if not fmt:
        if fmt_id:
            # Merge chosen video with best audio (fallback to just the id).
            fmt = f"{fmt_id}+bestaudio/best/{fmt_id}"
        else:
            fmt = "bestvideo[height<=2160]+bestaudio/best[height<=2160]/best"

    tmpdir = tempfile.mkdtemp(prefix="grabit_")
    outtmpl = os.path.join(tmpdir, "out.%(ext)s")

    cmd = [
        "yt-dlp",
        "-f", fmt,
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--no-progress",
        "-o", outtmpl,
    ]
    if container in {"mp4", "mkv", "webm"}:
        cmd += ["--merge-output-format", container]
    if container in {"mp3", "m4a"}:
        cmd += ["-x", "--audio-format", container]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]
    # Broad extractor coverage for YouTube.
    cmd += ["--extractor-args", "youtube:player_client=web,mweb,android"]
    cmd += [
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    ]
    cmd.append(url)

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "Download timed out"}), 504
    except FileNotFoundError:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "yt-dlp binary is missing on the server"}), 500

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "ignore").strip()
        log.info("yt-dlp download failed for %s: %s", url, stderr[:500])
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Retry once without cookies — helps YouTube age-gate weirdness.
        if COOKIES_FILE:
            log.info("retrying %s without cookies", url)
            tmpdir2 = tempfile.mkdtemp(prefix="grabit_")
            outtmpl2 = os.path.join(tmpdir2, "out.%(ext)s")
            cmd2 = [c for c in cmd if c != COOKIES_FILE and c != "--cookies"]
            # Rebuild with new outtmpl
            cmd2 = [
                "yt-dlp", "-f", fmt, "--no-playlist", "--no-warnings", "--quiet",
                "--no-progress", "-o", outtmpl2,
            ]
            if container in {"mp4", "mkv", "webm"}:
                cmd2 += ["--merge-output-format", container]
            if container in {"mp3", "m4a"}:
                cmd2 += ["-x", "--audio-format", container]
            cmd2 += ["--extractor-args", "youtube:player_client=android,ios"]
            cmd2.append(url)
            try:
                proc = subprocess.run(cmd2, capture_output=True, timeout=900)
            except subprocess.TimeoutExpired:
                shutil.rmtree(tmpdir2, ignore_errors=True)
                return jsonify({"error": "Download timed out"}), 504
            if proc.returncode != 0:
                shutil.rmtree(tmpdir2, ignore_errors=True)
                return jsonify({
                    "error": (proc.stderr.decode("utf-8", "ignore").strip()[:400]
                              or "Download failed"),
                }), 500
            tmpdir = tmpdir2
        else:
            return jsonify({"error": stderr[:400] or "Download failed"}), 500

    # Find the produced file.
    produced = [f for f in os.listdir(tmpdir) if not f.startswith(".")]
    if not produced:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify({"error": "No output file produced"}), 500
    path = os.path.join(tmpdir, produced[0])
    size = os.path.getsize(path)

    real_ext = os.path.splitext(produced[0])[1].lstrip(".") or container
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

    return Response(
        gen(),
        content_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{quote(name)}"',
            "Content-Length": str(size),
            "Cache-Control": "no-store",
        },
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
