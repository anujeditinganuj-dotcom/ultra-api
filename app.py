"""
FapHouse Resolver API — minimal, one endpoint, one response shape.

GET /api/faphouse?url=<link>
→
{
  "status": true,
  "creator": "FapHouse API",
  "data": {
    "Filename":   "...",
    "size":       "45.2 MB",
    "size_bytes": 47399936,
    "thumbnail":  "https://...",
    "speed_link": "https://...mp4",
    "m3u8_link":  "https://...m3u8"
  }
}

GET /health → {"status": true, "creator": "FapHouse API"}
"""

import logging
import os
import re
import subprocess
import shutil
import json as _json
from urllib.parse import urlparse

from flask import Flask, jsonify, request

import faphouse_downloader as faphouse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("faphouse_api")

app = Flask(__name__)
CREATOR = "FapHouse API"


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _error(msg: str, code: int = 400):
    return jsonify({"status": False, "creator": CREATOR, "message": msg}), code


def _sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "", name or "").strip()
    return re.sub(r"\s+", " ", clean)[:150] or "video"


def _slug_filename(video_url: str) -> str:
    path = urlparse(video_url).path.strip("/")
    slug = path.split("/")[-1] if path else "video"
    return _sanitize_filename(slug.replace("-", " ")).replace(" ", "-") or "video"


def _make_filename(title: str | None, video_url: str) -> str:
    name = _sanitize_filename(title) if title else _slug_filename(video_url)
    if not name.lower().endswith(".mp4"):
        name += ".mp4"
    return name


def _probe_size(stream_url: str) -> tuple[int | None, str | None]:
    """ffprobe se size_bytes + human size nikalo (HLS: duration * bitrate estimate)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None, None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "format=duration,bit_rate,size",
             "-of", "json", "-i", stream_url],
            capture_output=True, text=True, timeout=30,
        )
        data = _json.loads(result.stdout or "{}").get("format", {})
        size_b = int(data["size"]) if data.get("size") and int(data.get("size", 0)) > 0 else None
        if not size_b:
            dur = float(data.get("duration") or 0)
            bps = int(data.get("bit_rate") or 0)
            if dur > 0 and bps > 0:
                size_b = int(dur * bps / 8)
        if not size_b:
            return None, None
        units = ["B", "KB", "MB", "GB"]
        for i, unit in enumerate(units):
            if size_b < 1024 or unit == "GB":
                human = f"{size_b / (1024 ** i):.2f} {unit}"
                return size_b, human
    except Exception as e:
        logger.debug(f"ffprobe failed: {e}")
    return None, None


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": True, "creator": CREATOR})


@app.route("/api/faphouse")
def resolve():
    """
    GET /api/faphouse?url=<faphouse_link>

    Returns:
    {
      "status": true,
      "creator": "FapHouse API",
      "data": {
        "Filename":   "video-title.mp4",
        "size":       "45.2 MB",
        "size_bytes": 47399936,
        "thumbnail":  "https://...",
        "speed_link": "https://...mp4",
        "m3u8_link":  "https://...m3u8"
      }
    }
    """
    url = (request.args.get("url") or "").strip()
    if not url:
        return _error("Missing ?url= parameter.")
    if not faphouse.is_faphouse_link(url):
        return _error("Not a valid faphouse.com or faphouse2.com link.")

    # ── Meta (title + thumbnail) ──────────────────────────────────────
    try:
        meta = faphouse.get_page_meta(url) or {}
    except Exception as e:
        logger.warning(f"get_page_meta failed: {e}")
        meta = {}

    title     = meta.get("title")
    thumbnail = meta.get("poster_url")
    filename  = _make_filename(title, url)

    # ── M3U8 (master HLS stream) ───────────────────────────────────────
    m3u8_link = None
    try:
        m3u8_link = faphouse.client.get_m3u8_url(url)
    except Exception as e:
        logger.warning(f"get_m3u8_url failed: {e}")

    # ── Speed link (best direct MP4 quality) ─────────────────────────
    speed_link = None
    try:
        qualities = faphouse.get_available_qualities(url)
        for q in (qualities or []):
            if q.get("url"):
                speed_link = q["url"]
                break
    except faphouse.FanclubLockedError as e:
        return _error(f"Fanclub locked: {e}", 403)
    except Exception as e:
        logger.warning(f"get_available_qualities failed: {e}")

    # Fallback: m3u8 as speed_link if no direct MP4
    if not speed_link:
        speed_link = m3u8_link

    if not speed_link and not m3u8_link:
        return _error("Could not resolve any stream URL. Link may be expired or private.", 502)

    # ── Size ───────────────────────────────────────────────────────────
    probe_url  = speed_link or m3u8_link
    size_bytes, size_human = _probe_size(probe_url) if probe_url else (None, None)

    return jsonify({
        "status":  True,
        "creator": CREATOR,
        "data": {
            "Filename":   filename,
            "size":       size_human,
            "size_bytes": size_bytes,
            "thumbnail":  thumbnail,
            "speed_link": speed_link,
            "m3u8_link":  m3u8_link,
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
