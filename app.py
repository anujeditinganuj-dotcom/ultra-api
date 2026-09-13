"""
FapHouse Resolver API — no auth needed, just hit the endpoint.

GET /api/faphouse?url=<link>              → full info + all qualities
GET /api/qualities?url=<link>             → quality list only
GET /api/stream?url=<link>[&quality=1080p]  → inline browser playback
GET /api/download?url=<link>[&quality=720p] → mp4 file download
GET /health                               → health check
"""

import logging
import os
import re
import subprocess
from urllib.parse import urlparse, urlencode

from flask import Flask, jsonify, request, Response, stream_with_context

import faphouse_downloader as faphouse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ak_api")

app = Flask(__name__)
CREATOR = "AK API"


# ── helpers ───────────────────────────────────────────────────────────

def _sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return re.sub(r"\s+", " ", clean)[:150] or "video"

def _slug_filename(video_url: str) -> str:
    path = urlparse(video_url).path.strip("/")
    slug = path.split("/")[-1] if path else "video"
    return (_sanitize_filename(slug.replace("-", " ")).replace(" ", "-") or "video")

def _error(msg: str, code: int = 400):
    return jsonify({"status": False, "creator": CREATOR, "message": msg}), code

def _make_filename(title: str | None, video_url: str, quality_label: str = None) -> str:
    """Build filename from already-fetched title — no extra network call."""
    name = _sanitize_filename(title) if title else _slug_filename(video_url)
    if quality_label and quality_label.lower() not in ("auto (best)", "auto"):
        name = f"{name}_{quality_label}"
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    return name

def _best_stream_url(video_url: str, quality_label: str = None,
                     qualities: list = None) -> str | None:
    """Pick the right HLS sub-playlist. Reuse already-fetched qualities list."""
    if qualities is None:
        try:
            qualities = faphouse.get_available_qualities(video_url)
        except Exception:
            qualities = []

    # Match requested quality
    if quality_label and qualities:
        for q in qualities:
            if q["label"].lower() == quality_label.lower() and q.get("url"):
                return q["url"]

    # Best available sub-playlist (not auto/None)
    for q in (qualities or []):
        if q.get("url"):
            return q["url"]

    # Last resort: master m3u8 (ffmpeg picks best quality itself)
    try:
        return faphouse.client.get_m3u8_url(video_url)
    except Exception:
        return None

def _stream_response(stream_url: str, filename: str, video_url: str, inline: bool):
    """ffmpeg HLS → mp4 piped to client."""
    # Use correct Referer for faphouse vs faphouse2
    referer = faphouse.get_base_url(video_url) or "https://faphouse.com"

    cmd = [
        "ffmpeg", "-y",
        "-headers", f"Referer: {referer}/\r\nUser-Agent: Mozilla/5.0\r\n",
        "-i", stream_url,
        "-c", "copy",
        "-movflags", "frag_keyframe+empty_moov+faststart",
        "-f", "mp4",
        "pipe:1",
    ]
    logger.info(f"[ffmpeg] {'stream' if inline else 'download'}: {filename}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        return _error("ffmpeg not installed on this server.", 500)

    def generate():
        try:
            while chunk := proc.stdout.read(65536):
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait()

    disposition = "inline" if inline else "attachment"
    return Response(
        stream_with_context(generate()),
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "Content-Type":        "video/mp4",
            "X-Filename":          filename,
            "Cache-Control":       "no-cache",
        },
        direct_passthrough=True,
    )


def _probe_size(stream_url: str) -> tuple[int | None, str | None]:
    """Use ffprobe to estimate size_bytes from duration * bitrate.
    Returns (size_bytes, human_size) or (None, None) if ffprobe unavailable."""
    import shutil, json as _json
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, None
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration,bit_rate,size",
                "-of", "json",
                "-i", stream_url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        data   = _json.loads(result.stdout or "{}").get("format", {})
        # For HLS: "size" is rarely set, estimate from duration * bitrate
        size_b = int(data["size"]) if data.get("size") and int(data.get("size", 0)) > 0 else None
        if not size_b:
            dur = float(data.get("duration") or 0)
            bps = int(data.get("bit_rate") or 0)
            if dur > 0 and bps > 0:
                size_b = int(dur * bps / 8)
        if not size_b:
            return None, None
        # Human readable
        for unit in ("B", "KB", "MB", "GB"):
            if size_b < 1024 or unit == "GB":
                val = size_b / (1024 ** ["B","KB","MB","GB"].index(unit))
                human = f"{val:.2f} {unit}"
                break
        return size_b, human
    except Exception as e:
        logger.debug(f"ffprobe size probe failed: {e}")
        return None, None


# ── routes ────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": True, "creator": CREATOR})


@app.route("/")
def index():
    host = request.host_url.rstrip("/")
    return jsonify({
        "status":  True,
        "creator": CREATOR,
        "usage": {
            "info":      f"{host}/api/faphouse?url=FAPHOUSE_URL",
            "qualities": f"{host}/api/qualities?url=FAPHOUSE_URL",
            "stream":    f"{host}/api/stream?url=FAPHOUSE_URL&quality=1080p",
            "download":  f"{host}/api/download?url=FAPHOUSE_URL&quality=720p",
        },
        "quality_options": ["1080p", "720p", "480p", "360p", "Auto (Best)"],
        "sites_supported": ["faphouse.com", "faphouse2.com"],
    })


@app.route("/api/faphouse")
def resolve_faphouse():
    url = (request.args.get("url") or "").strip()
    if not url:
        return _error("Missing ?url= parameter.")
    if not faphouse.is_faphouse_link(url):
        return _error("Not a faphouse.com or faphouse2.com link.")

    # 1. Metadata (title + poster)
    try:
        meta = faphouse.get_page_meta(url) or {}
    except Exception as e:
        logger.warning(f"get_page_meta failed: {e}")
        meta = {}

    # 2. Qualities (internally resolves m3u8 once)
    try:
        qualities = faphouse.get_available_qualities(url)
    except Exception as e:
        logger.warning(f"get_available_qualities failed: {e}")
        qualities = []

    # 3. Master m3u8 — reuse from qualities if possible, else resolve once
    m3u8 = None
    for q in qualities:
        if q.get("url"):
            # sub-playlist URL → derive master from qualities list
            break
    if not m3u8:
        try:
            m3u8 = faphouse.client.get_m3u8_url(url)
        except Exception as e:
            return _error(f"Stream resolve failed: {e}", 502)
    if not m3u8 and not qualities:
        return _error("Could not resolve stream URL.", 502)

    # Fallback quality list
    if not qualities:
        qualities = [{"label": "Auto (Best)", "height": None, "url": None}]

    host  = request.host_url.rstrip("/")
    title = meta.get("title")
    name  = _sanitize_filename(title) if title else _slug_filename(url)

    quality_data = []
    for q in qualities:
        params = {"url": url, "quality": q["label"]}
        quality_data.append({
            "label":        q["label"],
            "height":       q.get("height"),
            "m3u8_url":     q.get("url") or m3u8,
            "stream_link":  f"{host}/api/stream?"  + urlencode(params),
            "download_url": f"{host}/api/download?" + urlencode(params),
        })

    # Probe size from best quality stream (background-friendly, timeout 30s)
    best_url = next((q.get("url") for q in qualities if q.get("url")), m3u8)
    size_bytes, size_human = _probe_size(best_url) if best_url else (None, None)

    return jsonify({
        "status":  True,
        "creator": CREATOR,
        "data": {
            "title":        title,
            "filename":     name + ".mp4",
            "thumbnail":    meta.get("poster_url"),
            "size":         size_human,
            "size_bytes":   size_bytes,
            "m3u8_link":    m3u8 or (qualities[0].get("url") if qualities else None),
            "qualities":    quality_data,
            "stream_url":   f"{host}/api/stream?"  + urlencode({"url": url}),
            "download_url": f"{host}/api/download?" + urlencode({"url": url}),
        },
    })


@app.route("/api/qualities")
def get_qualities():
    url = (request.args.get("url") or "").strip()
    if not url:
        return _error("Missing ?url= parameter.")
    if not faphouse.is_faphouse_link(url):
        return _error("Not a faphouse.com or faphouse2.com link.")

    try:
        qualities = faphouse.get_available_qualities(url)
    except Exception as e:
        return _error(f"Failed to get qualities: {e}", 502)

    if not qualities:
        return _error("No qualities found.", 502)

    host = request.host_url.rstrip("/")
    return jsonify({
        "status":    True,
        "creator":   CREATOR,
        "qualities": [
            {
                "label":        q["label"],
                "height":       q.get("height"),
                "m3u8_url":     q.get("url"),
                "stream_link":  f"{host}/api/stream?"  + urlencode({"url": url, "quality": q["label"]}),
                "download_url": f"{host}/api/download?" + urlencode({"url": url, "quality": q["label"]}),
            }
            for q in qualities
        ],
    })


@app.route("/api/download")
def download_video():
    url     = (request.args.get("url") or "").strip()
    quality = (request.args.get("quality") or "").strip() or None
    if not url:
        return _error("Missing ?url= parameter.")
    if not faphouse.is_faphouse_link(url):
        return _error("Not a faphouse.com or faphouse2.com link.")

    # Fetch qualities once — reuse for both stream URL and filename avoids
    # calling get_page_meta() separately (saves one extra network round-trip)
    try:
        qualities = faphouse.get_available_qualities(url)
    except Exception:
        qualities = []

    stream_url = _best_stream_url(url, quality, qualities)
    if not stream_url:
        return _error("Could not resolve stream URL.", 502)

    # Get title for filename (uses cached session — fast)
    try:
        title = (faphouse.get_page_meta(url) or {}).get("title")
    except Exception:
        title = None

    filename = _make_filename(title, url, quality)
    return _stream_response(stream_url, filename, url, inline=False)


@app.route("/api/stream")
def stream_video():
    url     = (request.args.get("url") or "").strip()
    quality = (request.args.get("quality") or "").strip() or None
    if not url:
        return _error("Missing ?url= parameter.")
    if not faphouse.is_faphouse_link(url):
        return _error("Not a faphouse.com or faphouse2.com link.")

    try:
        qualities = faphouse.get_available_qualities(url)
    except Exception:
        qualities = []

    stream_url = _best_stream_url(url, quality, qualities)
    if not stream_url:
        return _error("Could not resolve stream URL.", 502)

    try:
        title = (faphouse.get_page_meta(url) or {}).get("title")
    except Exception:
        title = None

    filename = _make_filename(title, url, quality)
    return _stream_response(stream_url, filename, url, inline=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)


# Warm session on gunicorn startup (both sites)
for _base in ["https://faphouse.com", "https://faphouse2.com"]:
    try:
        faphouse.client.ensure_session(_base)
        logger.info(f"Session ready: {_base}")
    except Exception as e:
        logger.warning(f"Session warm-up failed for {_base}: {e}")
