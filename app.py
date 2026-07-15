"""
GrabitPro yt-dlp backend.

Endpoints:
  GET  /            -> health check
  POST /info        -> { url } -> video metadata + downloadable formats
  GET  /stream      -> ?u=<direct_url>&name=<filename> streams the file

Auth: every request except `/` requires `Authorization: Bearer $API_SECRET`.
"""

import os
import re
import base64
import tempfile
import logging
from urllib.parse import quote

import requests
import yt_dlp
from flask import Flask, request, jsonify, Response, abort
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("grabit")

API_SECRET = os.environ.get("API_SECRET", "")
if not API_SECRET:
    log.warning("API_SECRET is empty — /info and /stream are unprotected!")

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
    ["web", "mweb"],
    ["android", "ios"],
    ["tv_embedded", "web"],
)


def build_ydl_options(player_clients=None, skip_configs=False):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
        # We only need metadata + direct URLs here. yt-dlp's default selector can
        # throw "Requested format is not available" when YouTube exposes only
        # DASH/adaptive formats for a client, even though useful formats exist.
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
    if COOKIES_FILE:
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
    direct_url = info.get("url")
    if not direct_url:
        return False
    if ext in {"mhtml", "html", "json", "xml", "vtt", "srt", "srv1", "srv2", "srv3", "ttml"}:
        return False
    if proto in {"mhtml", "http_dash_segments"}:
        return False
    return True


def extract_info_with_fallback(url: str):
    attempts = []
    # First try browser-like clients (cookies usually work best here), then
    # mobile/embedded clients because YouTube availability differs by video.
    for clients in YOUTUBE_CLIENT_PROFILES:
        skip_configs = all(c in {"web", "mweb"} for c in clients)
        attempts.append(("youtube:" + ",".join(clients), build_ydl_options(clients, skip_configs=skip_configs)))
    attempts.append(("default", build_ydl_options(None)))

    last_error = None
    for label, opts in attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = normalize_info(ydl.extract_info(url, download=False), url)
            if pick_formats(info) or is_direct_media_info(info):
                log.info("yt-dlp succeeded for %s via %s", url, label)
                return info
            last_error = RuntimeError("No downloadable formats returned")
            log.info("yt-dlp returned no formats for %s via %s", url, label)
        except yt_dlp.utils.DownloadError as e:
            last_error = e
            log.info("yt-dlp failed for %s via %s: %s", url, label, e)
        except Exception as e:
            last_error = e
            log.info("yt-dlp failed for %s via %s: %s", url, label, e)

    raise last_error or RuntimeError("Could not fetch video")


def pick_formats(info: dict):
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
        fmt_note = f.get("format_note") or ""

        if ext in {"mhtml", "html", "json", "xml", "vtt", "srt", "srv1", "srv2", "srv3", "ttml"}:
            continue
        if protocol in {"mhtml", "http_dash_segments"}:
            continue

        is_video = vcodec and vcodec != "none"
        is_audio = acodec and acodec != "none"

        if is_video:
            label = f"{height}p" if height else (fmt_note or ext.upper())
            kind = "video" if is_audio else "video-only"
        elif is_audio:
            label = f"Audio ({ext})"
            kind = "audio"
        else:
            continue

        # Prefer playable progressive video when available, but keep adaptive
        # video-only/audio-only as fallback instead of returning nothing.
        key = (kind, label, ext)
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "format_id": str(f.get("format_id") or ""),
            "ext": ext,
            "quality": label,
            "kind": kind,
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
                "filesize": None,
                "url": best,
            })

    def sort_key(item):
        m = re.match(r"(\d+)p", item["quality"] or "")
        h = int(m.group(1)) if m else 0
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
        "version": 3,
        "cookies_loaded": bool(COOKIES_FILE),
    })


@app.post("/info")
def info():
    check_auth()
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing 'url' in body"}), 400

    try:
        info = extract_info_with_fallback(url)
    except yt_dlp.utils.DownloadError as e:
        log.info("yt-dlp exhausted fallbacks for %s: %s", url, e)
        return jsonify({"error": f"Could not fetch video: {str(e)}"}), 400
    except Exception as e:
        log.exception("Unexpected error for %s", url)
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

    return jsonify({
        "title": info.get("title") or "Untitled",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "formats": pick_formats(info),
    })


@app.get("/stream")
def stream():
    check_auth()
    src = request.args.get("u", "").strip()
    name = request.args.get("name", "video.mp4").strip() or "video.mp4"
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
