"""
GrabitPro yt-dlp backend (YouTube-free).

Endpoints:
  GET  /            -> health check
  POST /info        -> { url } -> video metadata + downloadable formats
  POST /download    -> { url, format, name? } -> merged file stream
                        (supports video-only + bestaudio merging via ffmpeg)
  GET  /stream      -> ?u=<direct_url>&name=<filename> streams a raw URL

Auth: every request except `/` requires `Authorization: Bearer $API_SECRET`.

Note: YouTube (youtube.com, youtu.be, youtube-nocookie.com, music.youtube.com,
m.youtube.com) is explicitly rejected at every endpoint.
"""

import os
import re
import shutil
import tempfile
import subprocess
import logging
from urllib.parse import quote, urlparse

import requests
import yt_dlp
from flask import Flask, request, jsonify, Response, abort, stream_with_context
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("grabit")

API_SECRET = os.environ.get("API_SECRET", "")
if not API_SECRET:
    log.warning("API_SECRET is empty — endpoints are unprotected!")

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


# ---------- youtube guard ----------
_YT_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}


def is_youtube_url(url: str) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return bool(re.search(r"(?:^|//)(?:www\.|m\.|music\.)?youtu(?:\.be|be\.com|be-nocookie\.com)", url, re.I))
    if host in _YT_HOSTS:
        return True
    return host.endswith(".youtube.com") or host.endswith(".youtube-nocookie.com")


def reject_youtube(url: str):
    if is_youtube_url(url):
        return jsonify({
            "error": "YOUTUBE_UNSUPPORTED",
            "details": "YouTube is not supported. Try Instagram, TikTok, Facebook, Twitter/X, Reddit or another site.",
        }), 400
    return None


# ---------- helpers ----------
def build_ydl_options():
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        "format": "all",
        "ignore_no_formats_error": True,
        "extractor_retries": 3,
        "retries": 5,
        "impersonate": "chrome",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            )
        },
        **({"proxy": YTDLP_PROXY} if YTDLP_PROXY else {}),
    }


YDL_COMMON = build_ydl_options()


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


def extract_info(url: str):
    opts = build_ydl_options()
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = normalize_info(ydl.extract_info(url, download=False), url)
    return info


def pick_formats(info: dict):
    """Return every useful downloadable format the extractor exposed."""
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
            kind = "video" if is_audio else "video-only"
            label = base_label
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
        "youtube_supported": False,
        "yt_dlp_version": getattr(yt_dlp.version, "__version__", "unknown"),
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

    blocked = reject_youtube(url)
    if blocked:
        return blocked

    try:
        info_obj = extract_info(url)
    except yt_dlp.utils.DownloadError as e:
        log.info("yt-dlp failed for %s: %s", url, e)
        return jsonify({"error": f"Could not fetch video: {str(e)}"}), 400
    except Exception as e:
        log.exception("Unexpected error for %s", url)
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

    formats = pick_formats(info_obj)
    if not formats and not is_direct_media_info(info_obj):
        return jsonify({
            "error": "NO_FORMATS",
            "fallback": True,
            "details": "No downloadable formats returned for this link.",
            "formats": [],
            "webpage_url": info_obj.get("webpage_url") or url,
            "title": info_obj.get("title") or "Unavailable",
        }), 200

    return jsonify({
        "title": info_obj.get("title") or "Untitled",
        "thumbnail": info_obj.get("thumbnail"),
        "duration": info_obj.get("duration"),
        "uploader": info_obj.get("uploader") or info_obj.get("channel"),
        "webpage_url": info_obj.get("webpage_url") or url,
        "extractor": info_obj.get("extractor_key") or info_obj.get("extractor"),
        "formats": formats,
    })


# ---------- download (merged) ----------
def _safe_name(name: str) -> str:
    name = re.sub(r"[\r\n\"\\/]+", " ", name or "").strip()
    name = re.sub(r"[^\w.\-() ]+", "_", name)
    return (name[:120] or "video.mp4")


@app.route("/download", methods=["GET", "POST"])
def download():
    """Download a chosen format and, when needed, merge video+audio server-side."""
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
    name = _safe_name(data.get("name") or f"video.{container}")

    if not url:
        return jsonify({"error": "Missing 'url'"}), 400

    blocked = reject_youtube(url)
    if blocked:
        return blocked

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
        parts.append("bestvideo+bestaudio/best")
        fmt = "/".join(parts)
    else:
        if "bestvideo" not in fmt:
            fmt = f"{fmt}/{height_chain(h)}/bestvideo+bestaudio/best"

    tmpdir = tempfile.mkdtemp(prefix="grabit_")
    outtmpl = os.path.join(tmpdir, "out.%(ext)s")

    def build_cmd(target_fmt: str, out_template: str):
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
            "--extractor-retries", "3",
            "--retries", "5",
            "--fragment-retries", "5",
            "--impersonate", "chrome",
        ]
        if YTDLP_PROXY:
            c += ["--proxy", YTDLP_PROXY]
        c += [
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        ]
        c.append(url)
        return c

    fallback_fmt = height_chain(h) + "/bestvideo+bestaudio/best"
    attempts = [
        (fmt, outtmpl),
        (fallback_fmt, outtmpl + ".r1"),
    ]

    proc = None
    used_tmpdir = tmpdir
    last_err = ""
    for target_fmt, out_template in attempts:
        attempt_dir = tmpdir
        if out_template != outtmpl:
            attempt_dir = tempfile.mkdtemp(prefix="grabit_")
            out_template = os.path.join(attempt_dir, "out.%(ext)s")
        cmd = build_cmd(target_fmt, out_template)
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
        log.info("yt-dlp download attempt failed (fmt=%s): %s",
                 target_fmt[:80], last_err[:300])
        if attempt_dir != tmpdir:
            shutil.rmtree(attempt_dir, ignore_errors=True)
    else:
        shutil.rmtree(tmpdir, ignore_errors=True)
        friendly = last_err[:400] or "Download failed"
        return jsonify({"error": friendly}), 500

    tmpdir = used_tmpdir

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

    blocked = reject_youtube(src)
    if blocked:
        return blocked

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
