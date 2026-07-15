"""
GrabitPro yt-dlp backend.

Endpoints:
  GET  /            -> health check
  POST /info        -> { url } -> video metadata + downloadable formats
  GET  /stream      -> ?u=<direct_url>&name=<filename> streams the file
                        (optional: lets the frontend proxy through here if
                         the CDN blocks hotlinking)

Auth: every request except `/` requires `Authorization: Bearer $API_SECRET`.
"""

import os
import re
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
YDL_COMMON = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "noplaylist": True,
    "extract_flat": False,
    # Pretend to be a normal desktop browser — helps IG / TikTok
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    },
    # YouTube: use android + tv clients to bypass "Sign in to confirm you're
    # not a bot" checks that hit cloud IPs on the default web client.
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "tv_embedded", "web"],
            "player_skip": ["configs"],
        }
    },
}


def pick_formats(info: dict):
    """Return a slim list of the useful downloadable formats."""
    formats = info.get("formats") or []
    out = []
    seen = set()

    for f in formats:
        url = f.get("url")
        if not url:
            continue

        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = f.get("ext") or "mp4"
        height = f.get("height")
        fmt_note = f.get("format_note") or ""

        # Only keep progressive video (has both audio + video) OR audio-only mp3/m4a
        is_video = vcodec and vcodec != "none"
        is_audio = acodec and acodec != "none"

        if is_video and not is_audio:
            # skip video-only streams (would need ffmpeg merge on client)
            continue

        if is_video:
            label = f"{height}p" if height else (fmt_note or ext.upper())
            kind = "video"
        else:
            label = f"Audio ({ext})"
            kind = "audio"

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

    # If nothing progressive was found (common on YouTube), fall back to best mp4
    if not out:
        best = info.get("url")
        if best:
            out.append({
                "format_id": "best",
                "ext": info.get("ext") or "mp4",
                "quality": "Best available",
                "kind": "video",
                "filesize": None,
                "url": best,
            })

    # Sort: video first (by height desc), then audio
    def sort_key(item):
        m = re.match(r"(\d+)p", item["quality"] or "")
        h = int(m.group(1)) if m else 0
        return (0 if item["kind"] == "video" else 1, -h)

    out.sort(key=sort_key)
    return out


# ---------- routes ----------
@app.get("/")
def health():
    return jsonify({"ok": True, "service": "grabit-ytdlp", "version": 1})


@app.post("/info")
def info():
    check_auth()
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Missing 'url' in body"}), 400

    try:
        with yt_dlp.YoutubeDL(YDL_COMMON) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        log.info("yt-dlp failed for %s: %s", url, e)
        return jsonify({"error": f"Could not fetch video: {str(e)}"}), 400
    except Exception as e:
        log.exception("Unexpected error for %s", url)
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

    # For playlists we already set noplaylist, but guard anyway
    if info.get("_type") == "playlist" and info.get("entries"):
        info = info["entries"][0]

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
    """Optional passthrough: stream a direct media URL back to the client.
    Useful when the CDN blocks hotlink referers from the browser."""
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
