"""
FapHouse Resolver API — one endpoint, full metadata.

GET /api/faphouse?url=<link>
GET /health
"""

import json as _json
import logging
import os
import re
import subprocess
import shutil
import threading
from urllib.parse import urlparse, urljoin

from flask import Flask, jsonify, request

import faphouse_downloader as faphouse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("faphouse_api")

app   = Flask(__name__)
app.json.ensure_ascii = False  # Emojis as-is, not \uXXXX escape codes
CREATOR = "FapHouse API"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def _error(msg: str, code: int = 400):
    return jsonify({"status": False, "creator": CREATOR, "message": msg}), code


def _sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "", name or "").strip()
    return re.sub(r"\s+", " ", clean)[:150] or "video"


def _slug_from_url(url: str) -> str | None:
    """Extract descriptive English slug from URL path.
    FapHouse URLs: /videos/shared-bed-stepsister-fuck-C6Qi1u
    Slug part before the ID (last hyphen-separated alphanumeric ID) is always English.
    Returns None if URL only has a short ID (e.g. /videos/G76XIj)."""
    path = urlparse(url).path.strip("/")
    slug = path.split("/")[-1] if path else ""
    if not slug:
        return None
    # Remove trailing ID (last segment after last hyphen that looks like an ID)
    # e.g. "shared-bed-stepsister-fuck-C6Qi1u" → "shared-bed-stepsister-fuck"
    parts = slug.split("-")
    if len(parts) > 1 and re.match(r'^[A-Za-z0-9]{4,10}$', parts[-1]):
        slug = "-".join(parts[:-1])
    # Only use slug if it's descriptive (has multiple words / hyphens)
    if len(slug) > 8 and "-" in slug:
        return _sanitize_filename(slug.replace("-", " "))
    return None


def _make_filename(title: str | None, url: str) -> str:
    # Title ko prefer karo — ye full aur correct hota hai
    # Slug sirf tab use karo jab title bilkul nahi mila
    if title:
        name = _sanitize_filename(title)
    else:
        name = _slug_from_url(url) or _sanitize_filename(
            urlparse(url).path.strip("/").split("/")[-1]
        ) or "video"
    return name if name.lower().endswith(".mp4") else name + ".mp4"


def _iso_duration_to_seconds(iso: str) -> int | None:
    """PT1H27M32S → 5252 seconds"""
    if not iso or not iso.startswith("PT"):
        return None
    try:
        h = int((re.search(r"(\d+)H", iso) or [None, 0])[1])
        m = int((re.search(r"(\d+)M", iso) or [None, 0])[1])
        s = int((re.search(r"(\d+)S", iso) or [None, 0])[1])
        total = h * 3600 + m * 60 + s
        return total if total > 0 else None
    except Exception:
        return None


def _seconds_to_human(secs: int | None) -> str | None:
    if not secs or secs <= 0:
        return None
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _scrape_full_meta(html: str) -> dict:
    """
    Page HTML se sab metadata nikalo:
      title, thumbnail, duration_seconds, duration, views, likes,
      upload_date, author, description
    Strategy:
      1. JSON-LD (application/ld+json) — most structured
      2. og: meta tags — fallback
      3. Regex on raw HTML — last resort
    """
    meta = {
        "title":            None,
        "thumbnail":        None,
        "duration_seconds": None,
        "duration":         None,
        "views":            None,
        "likes":            None,
        "upload_date":      None,
        "author":           None,
        "description":      None,
    }

    # ── 0. __NEXT_DATA__ — FapHouse English fields (highest priority) ────
    # FapHouse is a Next.js app. __NEXT_DATA__ mein `pageProps.video` object
    # hota hai jisme English title aur description hote hain — ye og/JSON-LD
    # se behtar hain kyunki wo localized nahi hote.
    next_data_m = re.search(
        r'<script\s+id=["\']__NEXT_DATA__["\'][^>]*>\s*(\{.*?\})\s*</script>',
        html, re.DOTALL | re.IGNORECASE
    )
    if next_data_m:
        try:
            nd = _json.loads(next_data_m.group(1))
            # Walk: props → pageProps → video (or data.video)
            pp = (nd.get("props") or {}).get("pageProps") or {}
            video_obj = (
                pp.get("video")
                or pp.get("data", {}).get("video")
                or pp.get("videoData")
                or {}
            )
            if not isinstance(video_obj, dict):
                video_obj = {}

            # English title — FapHouse stores it as "title" in the video object
            nd_title = video_obj.get("title") or video_obj.get("name")
            if nd_title and isinstance(nd_title, str) and nd_title.strip():
                meta["title"] = nd_title.strip()

            # English description — "description" field in video object
            nd_desc = video_obj.get("description")
            if nd_desc and isinstance(nd_desc, str) and nd_desc.strip():
                meta["description"] = nd_desc.strip()

            # Bonus: thumbnail, duration, author from __NEXT_DATA__ too
            if not meta["thumbnail"]:
                meta["thumbnail"] = (
                    video_obj.get("thumbnailUrl")
                    or video_obj.get("thumbnail")
                    or video_obj.get("coverUrl")
                    or video_obj.get("previewUrl")
                )
            if not meta["duration_seconds"]:
                dur = video_obj.get("duration") or video_obj.get("durationSeconds")
                if isinstance(dur, (int, float)) and dur > 0:
                    meta["duration_seconds"] = int(dur)
                elif isinstance(dur, str) and dur.startswith("PT"):
                    meta["duration_seconds"] = _iso_duration_to_seconds(dur)
            if not meta["author"]:
                producer = (
                    video_obj.get("producer")
                    or video_obj.get("studio")
                    or video_obj.get("channel")
                    or {}
                )
                if isinstance(producer, dict):
                    meta["author"] = producer.get("name") or producer.get("title")
                models = video_obj.get("models") or video_obj.get("performers") or []
                if not meta["author"] and models and isinstance(models, list):
                    names = [m.get("name") or m.get("title") for m in models if isinstance(m, dict)]
                    names = [n for n in names if n]
                    if names:
                        meta["author"] = ", ".join(names[:2])
        except Exception as e:
            logger.debug(f"__NEXT_DATA__ parse failed: {e}")
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         html, re.DOTALL | re.IGNORECASE):
        try:
            data = _json.loads(m.group(1))
        except Exception:
            continue

        # Handle both bare object and @graph array
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and item.get("@type") in ("VideoObject", "Movie", "Video"):
                if not meta["title"]:
                    meta["title"] = item.get("name")
                if not meta["thumbnail"]:
                    t = item.get("thumbnailUrl") or item.get("thumbnail")
                    meta["thumbnail"] = t[0] if isinstance(t, list) and t else t
                if not meta["upload_date"]:
                    meta["upload_date"] = (item.get("uploadDate") or item.get("datePublished") or "")[:10] or None
                if not meta["description"]:
                    meta["description"] = item.get("description")
                if not meta["author"]:
                    p = item.get("author") or item.get("creator") or item.get("publisher")
                    if isinstance(p, dict):
                        meta["author"] = p.get("name")
                    elif isinstance(p, str):
                        meta["author"] = p
                if not meta["duration_seconds"]:
                    iso = item.get("duration")
                    meta["duration_seconds"] = _iso_duration_to_seconds(iso)
                # interactionStatistic → views / likes
                stats = item.get("interactionStatistic") or []
                if isinstance(stats, dict):
                    stats = [stats]
                for stat in stats:
                    t = stat.get("interactionType", "")
                    c = stat.get("userInteractionCount")
                    if c is None:
                        continue
                    try:
                        c = int(c)
                    except Exception:
                        continue
                    if "Watch" in t or "View" in t:
                        meta["views"] = c
                    elif "Like" in t or "React" in t or "Vote" in t or "Agree" in t:
                        meta["likes"] = c
                # Also check aggregateRating for likes/votes
                if not meta["likes"]:
                    ar = item.get("aggregateRating") or {}
                    if isinstance(ar, dict):
                        rc = ar.get("ratingCount") or ar.get("voteCount")
                        if rc is not None:
                            try:
                                meta["likes"] = int(rc)
                            except Exception:
                                pass

    # ── 2. og: meta tags (fallback) ──────────────────────────────────────
    def _og(prop: str) -> str | None:
        m = re.search(rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']',
                      html, re.IGNORECASE)
        if not m:
            m = re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:{prop}["\']',
                          html, re.IGNORECASE)
        return m.group(1).strip() if m else None

    if not meta["title"]:
        meta["title"] = _og("title")
    if not meta["thumbnail"]:
        meta["thumbnail"] = _og("image")
    if not meta["description"]:
        meta["description"] = _og("description")

    # ── 3. Regex fallbacks ────────────────────────────────────────────────
    # Duration in various HTML patterns: data-duration="1652", PT27M32S, etc.
    if not meta["duration_seconds"]:
        for pat in [
            r'data-duration=["\'](\d+)["\']',
            r'"duration"\s*:\s*(\d+)',
            r'duration["\s:]+(\d+)',
        ]:
            dm = re.search(pat, html, re.IGNORECASE)
            if dm:
                try:
                    meta["duration_seconds"] = int(dm.group(1))
                    break
                except Exception:
                    pass

    # Views
    if not meta["views"]:
        for pat in [
            r'(\d[\d,]+)\s*(?:views|viewCount|watch)',
            r'"viewCount"\s*:\s*(\d+)',
            r'data-views=["\'](\d+)["\']',
        ]:
            vm = re.search(pat, html, re.IGNORECASE)
            if vm:
                try:
                    meta["views"] = int(vm.group(1).replace(",", ""))
                    break
                except Exception:
                    pass

    # Likes — FapHouse (xHamster backend) uses several field names
    if not meta["likes"]:
        for pat in [
            # xHamster / FapHouse JSON fields
            r'"(?:likeCount|likesCount|likes_count|votesAmount|votes_amount|ratingCount|rating_count|positiveVotes|positive_votes|thumbsUp|thumbs_up)"\s*:\s*(\d+)',
            r'"rating"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)',
            r'"rating"\s*:\s*\{[^}]*"likes"\s*:\s*(\d+)',
            r'"likes"\s*:\s*(\d+)',
            # HTML data attributes
            r'data-(?:likes|like-count|votes|thumbs-up)=["\'](\d+)["\']',
            r'data-rating=["\'][^"\']*["\'][^>]*data-count=["\'](\d+)["\']',
            # __NEXT_DATA__ nested
            r'"votesAmount"\s*:\s*(\d+)',
            r'"likesCount"\s*:\s*(\d+)',
            # Inline text patterns
            r'(\d[\d,]+)\s*(?:likes?|thumbsUp|votes?)',
        ]:
            lm = re.search(pat, html, re.IGNORECASE)
            if lm:
                try:
                    val = int(lm.group(1).replace(",", ""))
                    if val >= 0:
                        meta["likes"] = val
                        break
                except Exception:
                    pass

    # Upload date
    if not meta["upload_date"]:
        for pat in [
            r'"(?:uploadDate|datePublished|dateCreated)"\s*:\s*"(\d{4}-\d{2}-\d{2})',
            r'datetime=["\'](\d{4}-\d{2}-\d{2})',
            r'data-date=["\'](\d{4}-\d{2}-\d{2})',
        ]:
            udm = re.search(pat, html, re.IGNORECASE)
            if udm:
                meta["upload_date"] = udm.group(1)
                break

    # Author — FapHouse stores model/studio name in multiple places
    if not meta["author"]:
        for pat in [
            r'"(?:modelName|model_name|performerName|performer_name|studioName|studio_name|channelName|channel_name)"\s*:\s*"([^"]{2,80})"',
            r'data-(?:model|performer|studio|channel)-name=["\']([^"\']{2,80})["\']',
            r'"author"\s*:\s*\{\s*"@type"\s*:[^}]+"name"\s*:\s*"([^"]{2,80})"',
            r'class="[^"]*(?:model|performer|studio|channel)[^"]*"[^>]*>\s*<[^>]+>([A-Za-z][^<]{1,60})</a>',
            r'class="[^"]*(?:model|performer|studio|author)[^"]*"[^>]*>([A-Za-z][^<]{1,60})</(?:a|span|div)',
        ]:
            am = re.search(pat, html, re.IGNORECASE)
            if am:
                candidate = am.group(1).strip()
                if candidate and candidate.lower() not in ("null", "undefined", "unknown", "faphouse", "faphouse2"):
                    meta["author"] = candidate
                    break

    # Duration human-readable
    if meta["duration_seconds"]:
        meta["duration"] = _seconds_to_human(meta["duration_seconds"])

    return meta


def _fetch_page_html(video_url: str) -> tuple[str | None, str]:
    """Faphouse page HTML fetch using authenticated session.
    Returns (html, final_url) — final_url is the redirect destination
    which contains the English slug even for short URLs like /videos/G76XIj."""
    import requests as _req
    base_url = faphouse.get_base_url(video_url) or faphouse.DEFAULT_BASE_URL
    final_url = video_url
    try:
        session = faphouse.client.ensure_session(base_url)
        r = session.get(video_url, timeout=12, headers={
            "User-Agent":      _UA,
            "Referer":         base_url,
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }, allow_redirects=True)
        final_url = r.url or video_url  # capture redirect destination
        if r.status_code != 200:
            return None, final_url
        return faphouse.client._decode_response(r), final_url
    except Exception:
        try:
            r = _req.get(video_url, timeout=12, headers={
                "User-Agent":      _UA,
                "Referer":         base_url,
                "Accept-Language": "en-US,en;q=0.9",
            }, allow_redirects=True)
            final_url = r.url or video_url
            return (r.text if r.status_code == 200 else None), final_url
        except Exception:
            return None, video_url


def _size_from_bandwidth(qualities: list, duration_secs: int | None) -> tuple[int | None, str | None]:
    """
    HLS bandwidth × duration → estimated file size.
    Qualities list se best bandwidth nikalo.
    """
    if not duration_secs or duration_secs <= 0:
        return None, None

    # Get bandwidth from quality URL (it's encoded in the URL path like key=...,end=...)
    # Or use highest quality index
    # Faphouse m3u8 qualities don't carry bandwidth directly in the dict,
    # but we can estimate from resolution:
    # ~2160p ≈ 15Mbps, 1080p ≈ 8Mbps, 720p ≈ 5Mbps, 480p ≈ 2.5Mbps, 240p ≈ 1Mbps
    height_bps = {2160: 15_000_000, 1440: 10_000_000, 1080: 8_000_000,
                  720: 5_000_000, 480: 2_500_000, 360: 1_500_000, 240: 1_000_000}

    bps = None
    for q in (qualities or []):
        h = q.get("height")
        if h and h in height_bps:
            bps = height_bps[h]
            break  # highest quality first

    if not bps:
        return None, None

    size_b = int(duration_secs * bps / 8)
    units = ["B", "KB", "MB", "GB"]
    for i, unit in enumerate(units):
        if size_b < 1024 or unit == "GB":
            human = f"{size_b / (1024 ** i):.2f} {unit}"
            return size_b, human
    return size_b, None


def _parse_multi_from_m3u8_url(m3u8_url: str) -> list:
    """
    Faphouse M3U8 URL mein 'multi=WxH:H,...' hota hai.
    Example: multi=426x240:240,854x480:480,1280x720:720,1400x1080:1080,3840x2160:2160
    → ["240p", "480p", "720p", "1080p", "2160p"]
    """
    m = re.search(r'multi=([^/\s]+)', m3u8_url)
    if not m:
        return []
    parts = m.group(1).split(",")
    heights = []
    for p in parts:
        hm = re.search(r':(\d+)$', p)
        if hm:
            heights.append(f"{hm.group(1)}p")
    return heights


# ─────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────

@app.route("/")
def homepage():
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FapHouse API</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root{
    --ink:#0d0f14; --panel:#14171f; --panel-2:#0f1218; --line:#262b38;
    --text:#dfe3ec; --muted:#8189a0; --faint:#565d70;
    --amber:#f2a641; --teal:#33c6a3; --rose:#ef6461;
  }
  [data-theme="light"]{
    --ink:#f5f5f0; --panel:#ffffff; --panel-2:#f0f0eb; --line:#dde0e8;
    --text:#1a1d26; --muted:#5a6078; --faint:#9aa0b4;
    --amber:#d4880a; --teal:#1a9e7e; --rose:#d94040;
  }
  [data-theme="light"] .lib-icon-placeholder{ border-color:var(--line); }
  [data-theme="light"] h1 span[style*="color:#fff"]{ color:var(--text) !important; }
  [data-theme="light"] .resp-json{ color:#2a2d3a; }
  [data-theme="light"] .jk{ color:#b07000; }
  [data-theme="light"] .js{ color:#2d7a2d; }
  [data-theme="light"] .jn{ color:#c04000; }
  [data-theme="light"] .jb{ color:#7020a0; }
  [data-theme="light"] .kw{ color:#9c3fc9; }
  [data-theme="light"] .fn{ color:#1a5fd4; }
  [data-theme="light"] .st{ color:#2d7a2d; }
  [data-theme="light"] .cm{ color:#7a8291; }
  [data-theme="light"] .nb{ color:#c25a15; }
  [data-theme="light"] .pl{ color:#a67a00; }
  [data-theme="light"] .badge-ver{ background:#fff3d6; border-color:#f0d896; }
  [data-theme="light"] .badge-status{ background:#e3f7ef; border-color:#a8dfc4; }
  [data-theme="light"] .badge-status.err{ background:#fbe4e4; border-color:#f0b4b4; }
  .crown-cut{ fill:var(--ink); transition:fill .2s; }

  /* Theme toggle */
  .theme-btn{
    background:var(--panel); border:1px solid var(--line); border-radius:20px;
    padding:5px 12px; font-size:0.8rem; cursor:pointer; color:var(--muted);
    font-family:'Inter',sans-serif; transition:all .2s; display:flex; align-items:center; gap:6px;
  }
  .theme-btn:hover{ border-color:var(--amber); color:var(--amber); }
  *{ box-sizing:border-box; }
  html{ scroll-behavior:smooth; }
  body{
    margin:0; background:var(--ink); color:var(--text);
    font-family:'Inter',sans-serif; line-height:1.55; padding:32px 20px 64px;
    -webkit-font-smoothing:antialiased;
  }
  a{ color:inherit; }
  :focus-visible{ outline:2px solid var(--amber); outline-offset:2px; }
  @media(prefers-reduced-motion:reduce){ *{ animation:none!important; transition:none!important; } }
  .page{ max-width:640px; margin:0 auto; }

  /* ── Two-panel Try-it section ── */
  .try-section{ margin:28px 0 0; }
  .try-tabs{ display:flex; gap:0; border-bottom:1px solid var(--line); margin-bottom:0; }
  .try-tab{
    padding:10px 20px; font-size:0.85rem; font-weight:600; color:var(--faint);
    cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px;
    transition:color .15s, border-color .15s; user-select:none;
  }
  .try-tab.active{ color:var(--amber); border-bottom-color:var(--amber); }
  .try-panels{ background:var(--panel); border:1px solid var(--line); border-top:none; border-radius:0 0 10px 10px; overflow:hidden; }
  .try-panel{ display:none; padding:16px; }
  .try-panel.active{ display:block; }

  /* Fetch panel */
  .fetch-row{ display:flex; gap:8px; align-items:center; margin-bottom:10px; }
  .search-input{
    flex:1; background:var(--panel-2); border:1px solid var(--line); border-radius:8px;
    padding:10px 14px; font-family:'JetBrains Mono',monospace; font-size:0.83rem;
    color:var(--text); outline:none; transition:border-color .2s, box-shadow .2s; min-width:0;
  }
  .search-input::placeholder{ color:var(--faint); }
  .search-input:focus{ border-color:var(--amber); box-shadow:0 0 0 3px rgba(242,166,65,.08); }
  .search-btn{
    background:var(--amber); color:#1a1206; border:none; border-radius:8px;
    padding:10px 20px; font-family:'Inter',sans-serif; font-weight:600; font-size:0.85rem;
    cursor:pointer; white-space:nowrap; transition:background .15s, transform .15s;
  }
  .search-btn:hover{ background:#f5b45f; transform:translateY(-1px); }
  .search-btn:active{ transform:translateY(0); }
  .search-loading{ display:none; color:var(--faint); font-family:'JetBrains Mono',monospace; font-size:0.8rem; padding:4px 0 8px; }
  .search-loading.show{ display:block; }

  /* Video info card */
  .video-card{ display:none; }
  .video-card.show{ display:block; }
  .video-thumb{ width:100%; max-height:170px; object-fit:cover; border-radius:8px; margin-bottom:10px; }
  .video-title{ font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:0.97rem; color:var(--text); margin-bottom:8px; }
  .video-meta{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
  .meta-pill{ font-family:'JetBrains Mono',monospace; font-size:0.72rem; color:var(--faint); background:var(--panel-2); padding:3px 9px; border-radius:4px; border:1px solid var(--line); }
  .quality-row{ display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
  .quality-btn{
    background:transparent; border:1px solid var(--line); border-radius:6px;
    padding:5px 12px; font-family:'JetBrains Mono',monospace; font-size:0.78rem;
    color:var(--muted); cursor:pointer; text-decoration:none; transition:all .15s; display:inline-block;
  }
  .quality-btn:hover{ border-color:var(--amber); color:var(--amber); background:rgba(242,166,65,.05); }
  .m3u8-btn{
    display:inline-flex; align-items:center; gap:6px; background:var(--teal);
    color:#0a1a15; border:none; border-radius:7px; padding:8px 16px;
    font-family:'Inter',sans-serif; font-weight:600; font-size:0.82rem;
    text-decoration:none; cursor:pointer; transition:background .15s, transform .15s;
  }
  .m3u8-btn:hover{ background:#4dd9b2; transform:translateY(-1px); }
  .fetch-error{ color:var(--rose); font-family:'JetBrains Mono',monospace; font-size:0.82rem; padding:8px 0; }

  /* Response panel */
  .resp-bar{
    display:flex; align-items:center; justify-content:space-between; gap:8px;
    padding:8px 12px; background:var(--panel-2); border-bottom:1px solid var(--line);
    border-radius:8px 8px 0 0; font-family:'JetBrains Mono',monospace; font-size:0.72rem;
  }
  .resp-status{ }
  .status-ok{ color:var(--teal); }
  .status-err{ color:var(--rose); }
  .resp-json{
    background:var(--panel-2); border:1px solid var(--line); border-top:none;
    border-radius:0 0 8px 8px; padding:14px; font-family:'JetBrains Mono',monospace;
    font-size:0.75rem; color:#c9cee0; white-space:pre; overflow-x:auto;
    max-height:420px; overflow-y:auto; line-height:1.6;
  }
  .resp-placeholder{
    color:var(--faint); font-family:'JetBrains Mono',monospace; font-size:0.8rem;
    padding:20px 0; text-align:center;
  }
  /* JSON syntax colors */
  .jk{ color:var(--amber); }
  .js{ color:#c3e88d; }
  .jn{ color:#f78c6c; }
  .jb{ color:#c792ea; }

  /* ── Library overlay ── */
  .lib-overlay{
    display:none; position:fixed; inset:0; background:rgba(0,0,0,.7);
    z-index:100; backdrop-filter:blur(4px);
  }
  .lib-overlay.open{ display:flex; align-items:flex-start; justify-content:center; padding:20px 16px; }
  .lib-sheet{
    background:var(--ink); border:1px solid var(--line); border-radius:14px;
    width:100%; max-width:500px; max-height:85vh; display:flex; flex-direction:column;
    animation: fadein .2s ease both;
  }
  .lib-header{
    display:flex; align-items:center; justify-content:space-between;
    padding:16px 18px; border-bottom:1px solid var(--line);
  }
  .lib-title{
    font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:1rem;
    display:flex; align-items:center; gap:8px;
  }
  .lib-count{
    background:var(--panel-2); color:var(--amber); border:1px solid var(--line);
    border-radius:20px; padding:1px 8px; font-size:0.72rem;
    font-family:'JetBrains Mono',monospace;
  }
  .lib-close{
    background:none; border:none; color:var(--faint); cursor:pointer; font-size:1.2rem;
    padding:4px 8px; border-radius:6px; transition:color .15s;
  }
  .lib-close:hover{ color:var(--text); }
  .lib-actions{
    display:flex; gap:8px; padding:10px 18px; border-bottom:1px solid var(--line);
  }
  .lib-action-btn{
    background:none; border:1px solid var(--line); border-radius:6px;
    color:var(--muted); font-size:0.78rem; font-family:'JetBrains Mono',monospace;
    padding:5px 12px; cursor:pointer; transition:all .15s;
  }
  .lib-action-btn:hover{ border-color:var(--amber); color:var(--amber); }
  .lib-action-btn.danger:hover{ border-color:var(--rose); color:var(--rose); }
  .lib-list{ overflow-y:auto; flex:1; padding:8px 0; }
  .lib-empty{
    text-align:center; padding:32px 20px;
    color:var(--faint); font-family:'JetBrains Mono',monospace; font-size:0.82rem;
  }
  .lib-item{
    display:flex; align-items:center; gap:12px; padding:10px 18px;
    border-bottom:1px solid var(--panel-2); cursor:pointer;
    transition:background .15s;
  }
  .lib-item:hover{ background:var(--panel); }
  .lib-item:last-child{ border-bottom:none; }
  .lib-icon{
    width:36px; height:36px; border-radius:6px; object-fit:cover; flex:none;
    background:var(--panel-2);
  }
  .lib-icon-placeholder{
    width:36px; height:36px; border-radius:6px; background:var(--panel-2);
    border:1px solid var(--line); flex:none; display:flex; align-items:center;
    justify-content:center; font-size:1rem;
  }
  .lib-info{ flex:1; min-width:0; }
  .lib-name{
    font-family:'JetBrains Mono',monospace; font-size:0.78rem; color:var(--text);
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:2px;
  }
  .lib-badge{
    display:inline-block; background:#1a1206; color:var(--amber);
    border:1px solid #2e2006; border-radius:4px; font-size:0.65rem;
    font-family:'JetBrains Mono',monospace; padding:1px 6px;
  }
  .lib-del{
    background:none; border:none; color:var(--faint); cursor:pointer;
    font-size:1rem; padding:4px 8px; border-radius:4px; flex:none;
    transition:color .15s;
  }
  .lib-del:hover{ color:var(--rose); }

  /* Library button in header */
  .lib-btn{
    display:inline-flex; align-items:center; gap:6px;
    background:var(--panel); border:1px solid var(--line); border-radius:8px;
    color:var(--muted); font-size:0.82rem; font-family:'Inter',sans-serif;
    padding:7px 14px; cursor:pointer; transition:all .15s; position:relative;
  }
  .lib-btn:hover{ border-color:var(--amber); color:var(--amber); }
  .lib-btn-count{
    background:var(--amber); color:#1a1206; border-radius:50%;
    width:16px; height:16px; font-size:0.62rem; font-weight:700;
    display:none; align-items:center; justify-content:center;
    position:absolute; top:-6px; right:-6px;
  }
  .lib-btn-count.show{ display:flex; }
  @keyframes fadein{ from{ opacity:0; transform:translateY(10px); } to{ opacity:1; transform:translateY(0); } }
  .page{ animation: fadein .45s ease both; }

  /* ── Header ── */
  .header-row{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:6px; }
  .reel{ display:flex; align-items:center; gap:14px; }
  .reel svg{ flex:none; width:34px; height:34px; transition:transform .6s cubic-bezier(.34,1.56,.64,1); }
  .reel:hover svg{ transform: rotate(18deg) scale(1.08); }
  h1{ font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:1.65rem; letter-spacing:-0.01em; margin:0; }
  .badges{ display:flex; gap:7px; flex-wrap:wrap; margin-top:4px; }
  .badge{
    display:inline-flex; align-items:center; gap:5px;
    font-size:0.72rem; font-weight:500; padding:3px 9px; border-radius:20px;
    font-family:'JetBrains Mono',monospace; letter-spacing:0.01em;
  }
  .badge-ver{ background:#1e2230; color:var(--amber); border:1px solid #2e3245; }
  .badge-status{ background:#0f1f1a; color:var(--teal); border:1px solid #1a3328; }
  .badge-status .dot{
    width:6px; height:6px; border-radius:50%; background:var(--teal);
    box-shadow:0 0 0 2px rgba(51,198,163,0.2);
    animation: pulse 2s infinite;
  }
  @keyframes pulse{ 0%,100%{ box-shadow:0 0 0 2px rgba(51,198,163,.2); } 50%{ box-shadow:0 0 0 5px rgba(51,198,163,.04); } }
  .badge-status.err{ background:#1f0f10; color:var(--rose); border:1px solid #3a1a1c; }
  .badge-status.err .dot{ background:var(--rose); box-shadow:none; animation:none; }
  .tagline{ color:var(--muted); font-size:0.95rem; margin:12px 0 0; max-width:52ch; }

  /* ── Sections staggered fade-in ── */
  section{ margin-top:44px; animation: fadein .5s ease both; }
  section:nth-of-type(2){ animation-delay:.07s; }
  section:nth-of-type(3){ animation-delay:.14s; }
  section:nth-of-type(4){ animation-delay:.21s; }
  h2{ font-family:'Space Grotesk',sans-serif; font-weight:700; font-size:1.05rem; margin:0 0 16px; }

  /* ── Terminal window ── */
  .term{
    background:var(--panel); border:1px solid var(--line); border-radius:10px;
    overflow:hidden; margin-bottom:20px;
    transition: border-color .2s, box-shadow .2s;
  }
  .term:hover{
    border-color:#3a4155;
    box-shadow: 0 0 0 1px #3a4155, 0 8px 32px rgba(0,0,0,.35);
  }
  .term-bar{ display:flex; align-items:center; gap:7px; padding:10px 14px; background:var(--panel-2); border-bottom:1px solid var(--line); }
  /* Traffic-light dots get colour on hover */
  .term:hover .term-bar i:nth-child(1){ background:#ef6461; }
  .term:hover .term-bar i:nth-child(2){ background:#f2a641; }
  .term:hover .term-bar i:nth-child(3){ background:#33c6a3; }
  .term-bar i{ width:9px; height:9px; border-radius:50%; background:var(--line); transition:background .2s; }
  .term-route{ margin-left:6px; font-family:'JetBrains Mono',monospace; font-size:0.86rem; color:var(--text); overflow-x:auto; white-space:nowrap; flex:1; }
  .term-route .get{ color:var(--teal); font-weight:500; margin-right:10px; }
  .term-body{ padding:18px; }
  .desc{ color:var(--muted); font-size:0.92rem; margin:0 0 16px; }

  /* Copy button */
  .copy-btn{
    display:inline-flex; align-items:center; gap:5px; padding:3px 10px;
    background:transparent; border:1px solid var(--line); border-radius:5px;
    color:var(--faint); font-size:0.75rem; font-family:'Inter',sans-serif;
    cursor:pointer; white-space:nowrap; transition:color .15s, border-color .15s, background .15s;
    flex-none;
  }
  .copy-btn:hover{ color:var(--amber); border-color:var(--amber); background:rgba(242,166,65,.05); }
  .copy-btn.copied{ color:var(--teal); border-color:var(--teal); background:rgba(51,198,163,.06); }
  .copy-btn svg{ width:11px; height:11px; flex:none; }

  table.params{ width:100%; border-collapse:collapse; font-size:0.85rem; margin-bottom:16px; }
  table.params th{ text-align:left; font-weight:500; color:var(--faint); font-size:0.75rem; padding:0 10px 8px 0; border-bottom:1px solid var(--line); }
  table.params td{ padding:9px 10px 9px 0; border-bottom:1px solid var(--panel-2); vertical-align:top; }
  table.params td:first-child{ font-family:'JetBrains Mono',monospace; color:var(--amber); white-space:nowrap; }
  table.params td.req{ color:var(--teal); white-space:nowrap; }
  table.params tr:last-child td{ border-bottom:none; }

  /* Run button — subtle lift on hover */
  .run{
    display:inline-flex; align-items:center; gap:8px; background:var(--amber);
    color:#1a1206; border:none; border-radius:7px; padding:9px 16px;
    font-family:'Inter',sans-serif; font-weight:600; font-size:0.85rem;
    text-decoration:none; cursor:pointer;
    transition: background .15s, transform .15s, box-shadow .15s;
  }
  .run:hover{ background:#f5b45f; transform:translateY(-1px); box-shadow:0 4px 14px rgba(242,166,65,.25); }
  .run:active{ transform:translateY(0); box-shadow:none; }
  .run svg{ width:11px; height:11px; }

  /* ── Spec sheet — row hover highlight ── */
  .spec{ border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .spec-row{
    padding:12px 16px; border-bottom:1px solid var(--panel-2); background:var(--panel);
    transition:background .15s;
  }
  .spec-row:hover{ background:#191d28; }
  .spec-row:last-child{ border-bottom:none; }
  .spec-key{ font-family:'JetBrains Mono',monospace; font-size:0.85rem; color:var(--amber); display:flex; align-items:baseline; gap:9px; flex-wrap:wrap; }
  .spec-key .type{ color:var(--faint); font-size:0.72rem; font-family:'Inter',sans-serif; }
  .spec-key .opt{ color:#3a4055; font-size:0.7rem; font-family:'Inter',sans-serif; }
  .spec-desc{ color:var(--muted); font-size:0.85rem; margin-top:4px; }
  .spec-row.err .spec-key{ color:var(--rose); }

  /* ── Code tabs ── */
  .tabs{ border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .tabs input{ display:none; }
  .tab-labels{ display:flex; background:var(--panel-2); border-bottom:1px solid var(--line); }
  .tab-labels label{
    padding:11px 16px; font-size:0.82rem; color:var(--faint); cursor:pointer;
    border-right:1px solid var(--line); user-select:none;
    transition:color .15s, background .15s;
  }
  .tab-labels label:hover{ color:var(--text); }
  .tab-labels label:last-child{ border-right:none; }
  .tab-panel-wrap{ position:relative; background:var(--panel); }
  .tab-panel{ display:none; padding:0; }
  .tab-panel.active{ display:block; }

  /* Code block with copy button */
  .code-block{ position:relative; }
  .code-block .copy-btn{ position:absolute; top:10px; right:10px; opacity:0; transition:opacity .15s; }
  .code-block:hover .copy-btn{ opacity:1; }
  pre{
    margin:0; padding:16px; font-family:'JetBrains Mono',monospace; font-size:0.8rem;
    white-space:pre; overflow-x:auto; line-height:1.65;
  }
  /* Syntax colours */
  .kw{ color:#c792ea; }
  .fn{ color:#82aaff; }
  .st{ color:#c3e88d; }
  .cm{ color:#546e7a; font-style:italic; }
  .nb{ color:#f78c6c; }
  .op{ color:var(--faint); }
  .pl{ color:#ffcb6b; }

  #tab-py:checked ~ .tab-labels label[for="tab-py"],
  #tab-tg:checked ~ .tab-labels label[for="tab-tg"],
  #tab-curl:checked ~ .tab-labels label[for="tab-curl"]{ color:var(--amber); }
  #tab-py:checked ~ .tab-panel-wrap .panel-py,
  #tab-tg:checked ~ .tab-panel-wrap .panel-tg,
  #tab-curl:checked ~ .tab-panel-wrap .panel-curl{ display:block; }

  footer{ margin-top:52px; padding-top:20px; border-top:1px solid var(--line); display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px; }
  footer p{ color:var(--faint); font-size:0.8rem; margin:0; }
  footer a{ color:var(--muted); text-decoration:underline; text-underline-offset:2px; }
</style>
</head>
<body>
<div class="page">

  <!-- Library overlay -->
  <div class="lib-overlay" id="libOverlay">
    <div class="lib-sheet">
      <div class="lib-header">
        <div class="lib-title">
          📚 LIBRARY <span class="lib-count" id="libCount">0</span>
        </div>
        <button class="lib-close" id="libClose">✕</button>
      </div>
      <div class="lib-actions">
        <button class="lib-action-btn" id="libRefresh">↺ REFRESH</button>
        <button class="lib-action-btn danger" id="libClearAll">CLEAR ALL</button>
      </div>
      <div class="lib-list" id="libList">
        <div class="lib-empty">No videos yet — fetch one to save it here.</div>
      </div>
    </div>
  </div>

  <!-- Header -->
  <div class="header-row">
    <div class="reel">
      <svg width="44" height="38" viewBox="0 0 44 38" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M4 34 L4 14 L12 24 L22 4 L32 24 L40 14 L40 34 Z" fill="#f2a641"/>
        <circle cx="4" cy="13" r="3.5" fill="#f2a641"/>
        <circle cx="22" cy="3.5" r="3.5" fill="#f2a641"/>
        <circle cx="40" cy="13" r="3.5" fill="#f2a641"/>
        <path d="M17 34 L17 26 Q17 22 22 22 Q27 22 27 26 L27 34 Z" class="crown-cut"/>
      </svg>
      <h1 style="font-size:2rem;letter-spacing:-0.02em;">
        <span style="color:#f2a641;">fap</span><span id="house-text" style="color:#fff;">house</span>
        <span style="font-size:0.55rem;color:#f2a641;font-family:'Inter',sans-serif;font-weight:600;letter-spacing:0.08em;vertical-align:middle;margin-left:6px;background:#1a1206;border:1px solid #2e2006;border-radius:4px;padding:2px 6px;">Ultra</span>
      </h1>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <button class="theme-btn" id="themeBtn">🌙 Dark</button>
      <button class="lib-btn" id="libBtn">
        📚 Library
        <span class="lib-btn-count" id="libBtnCount">0</span>
      </button>
      <div class="badges">
        <span class="badge badge-ver">v1.0</span>
        <span class="badge badge-status" id="status-badge">
          <span class="dot"></span><span id="status-text">checking…</span>
        </span>
      </div>
    </div>
  </div>
  <p class="tagline">Resolves <strong style="color:var(--text)">faphouse.com</strong> and <strong style="color:var(--text)">faphouse2.com</strong> video links into title, duration, HLS master playlist, and one direct URL per quality — ready to forward straight into a Telegram reply.</p>

  <!-- Two-panel Try-it section -->
  <div class="try-section">
    <div class="try-tabs">
      <div class="try-tab active" data-panel="fetch">Fetch Video</div>
      <div class="try-tab" data-panel="response">Response</div>
    </div>
    <div class="try-panels">

      <!-- Panel 1: Fetch -->
      <div class="try-panel active" id="panel-fetch">
        <div class="fetch-row">
          <input class="search-input" id="searchInput" type="text"
            placeholder="https://faphouse.com/videos/... or faphouse2.com/..."
            autocomplete="off" spellcheck="false">
          <button class="search-btn" id="searchBtn">Fetch</button>
        </div>
        <div class="search-loading" id="searchLoading">Fetching...</div>
        <div class="video-card" id="videoCard"></div>
        <div id="fetchError"></div>
      </div>

      <!-- Panel 2: Response -->
      <div class="try-panel" id="panel-response">
        <div id="respContent">
          <div class="resp-placeholder">← Fetch a video first to see the raw JSON response here.</div>
        </div>
      </div>

    </div>
  </div>

  <!-- Endpoints -->
  <section>
    <h2>Endpoints</h2>

    <div class="term">
      <div class="term-bar">
        <i></i><i></i><i></i>
        <span class="term-route"><span class="get">GET</span>/api/faphouse?url=&lt;faphouse.com or faphouse2.com link&gt;</span>
        <button class="copy-btn" data-copy="/api/faphouse?url=https://faphouse.com/videos/QJKmfH">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <rect x="4" y="4" width="8" height="8" rx="1.5"/><path d="M2 10V2h8"/>
          </svg>Copy
        </button>
      </div>
      <div class="term-body">
        <p class="desc">Takes a FapHouse video URL and returns full metadata plus M3U8 link and one direct URL per quality.</p>
        <table class="params">
          <tr><th>Parameter</th><th>Type</th><th>Required</th><th>Description</th></tr>
          <tr><td>url</td><td>string</td><td class="req">Yes</td><td>faphouse.com <span style="color:var(--faint)">or</span> faphouse2.com video link</td></tr>
        </table>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <a class="run" href="/api/faphouse?url=https://faphouse.com/videos/QJKmfH" target="_blank">
            <svg viewBox="0 0 12 12" fill="currentColor"><path d="M1 0.5 11 6 1 11.5Z"/></svg>faphouse.com
          </a>
          <a class="run" style="background:var(--teal);color:#0a1a15;" href="/api/faphouse?url=https://faphouse2.com/videos/QJKmfH" target="_blank">
            <svg viewBox="0 0 12 12" fill="currentColor"><path d="M1 0.5 11 6 1 11.5Z"/></svg>faphouse2.com
          </a>
        </div>
      </div>
    </div>

    <div class="term">
      <div class="term-bar">
        <i></i><i></i><i></i>
        <span class="term-route"><span class="get">GET</span>/health</span>
        <button class="copy-btn" data-copy="/health">
          <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5">
            <rect x="4" y="4" width="8" height="8" rx="1.5"/><path d="M2 10V2h8"/>
          </svg>Copy
        </button>
      </div>
      <div class="term-body">
        <p class="desc">Plain health check for uptime monitors (UptimeRobot, Freshping, etc.).</p>
        <a class="run" href="/health" target="_blank">
          <svg viewBox="0 0 12 12" fill="currentColor"><path d="M1 0.5 11 6 1 11.5Z"/></svg>Run
        </a>
      </div>
    </div>
  </section>

  <!-- Response fields -->
  <section>
    <h2>Response fields</h2>
    <div class="spec">
      <div class="spec-row"><div class="spec-key">status<span class="type">bool</span></div><div class="spec-desc">true on success, false on error</div></div>
      <div class="spec-row"><div class="spec-key">data.title<span class="type">string</span></div><div class="spec-desc">English title extracted via yt-dlp + __NEXT_DATA__</div></div>
      <div class="spec-row"><div class="spec-key">data.author<span class="type">string</span></div><div class="spec-desc">Model or studio name</div></div>
      <div class="spec-row"><div class="spec-key">data.duration<span class="type">string</span></div><div class="spec-desc">Human-readable duration, e.g. 1:07:39</div></div>
      <div class="spec-row"><div class="spec-key">data.duration_seconds<span class="type">int</span></div><div class="spec-desc">Duration in seconds</div></div>
      <div class="spec-row"><div class="spec-key">data.size<span class="type">string</span></div><div class="spec-desc">Estimated file size, e.g. 7.09 GB</div></div>
      <div class="spec-row"><div class="spec-key">data.thumbnail<span class="type">string</span></div><div class="spec-desc">Thumbnail image URL</div></div>
      <div class="spec-row"><div class="spec-key">data.description<span class="type">string</span></div><div class="spec-desc">English video description</div></div>
      <div class="spec-row"><div class="spec-key">data.m3u8_link<span class="type">string</span></div><div class="spec-desc">Master HLS playlist (all qualities adaptive)</div></div>
      <div class="spec-row"><div class="spec-key">data.available_qualities<span class="type">array</span></div><div class="spec-desc">[{label, url}] — one entry per quality (2160p → 240p)</div></div>
      <div class="spec-row"><div class="spec-key">data.Filename<span class="type">string</span></div><div class="spec-desc">Suggested download filename (.mp4)</div></div>
      <div class="spec-row"><div class="spec-key">data.formatted_text<span class="type">string</span></div><div class="spec-desc">Ready-to-send plain-text summary (no Markdown formatting)</div></div>
      <div class="spec-row"><div class="spec-key">data.views<span class="type">int</span><span class="opt">optional</span></div><div class="spec-desc">View count — omitted when null</div></div>
      <div class="spec-row"><div class="spec-key">data.likes<span class="type">int</span><span class="opt">optional</span></div><div class="spec-desc">Like count — omitted when null</div></div>
      <div class="spec-row"><div class="spec-key">data.upload_date<span class="type">string</span><span class="opt">optional</span></div><div class="spec-desc">Upload date YYYY-MM-DD — omitted when null</div></div>
    </div>
  </section>

  <!-- Code samples -->
  <section>
    <h2>Example usage</h2>
    <div class="tabs">
      <input type="radio" name="tabs" id="tab-py" checked>
      <input type="radio" name="tabs" id="tab-tg">
      <input type="radio" name="tabs" id="tab-curl">
      <div class="tab-labels">
        <label for="tab-py">Python</label>
        <label for="tab-tg">Telegram bot</label>
        <label for="tab-curl">cURL</label>
      </div>
      <div class="tab-panel-wrap">

        <div class="tab-panel panel-py">
          <div class="code-block">
            <button class="copy-btn" data-copy-pre="code-py">
              <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="8" height="8" rx="1.5"/><path d="M2 10V2h8"/></svg>Copy
            </button>
            <pre id="code-py"><span class="kw">import</span> <span class="pl">requests</span>

<span class="pl">r</span> <span class="op">=</span> <span class="pl">requests</span>.<span class="fn">get</span>(
    <span class="st">"https://ultra-api-008f.onrender.com/api/faphouse"</span>,
    <span class="pl">params</span><span class="op">=</span>{<span class="st">"url"</span>: <span class="st">"https://faphouse.com/videos/QJKmfH"</span>},
)
<span class="pl">data</span> <span class="op">=</span> <span class="pl">r</span>.<span class="fn">json</span>()[<span class="st">"data"</span>]
<span class="fn">print</span>(<span class="pl">data</span>[<span class="st">"title"</span>])
<span class="fn">print</span>(<span class="pl">data</span>[<span class="st">"m3u8_link"</span>])</pre>
          </div>
        </div>

        <div class="tab-panel panel-tg">
          <div class="code-block">
            <button class="copy-btn" data-copy-pre="code-tg">
              <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="8" height="8" rx="1.5"/><path d="M2 10V2h8"/></svg>Copy
            </button>
            <pre id="code-tg"><span class="kw">import</span> <span class="pl">requests</span>

<span class="pl">r</span> <span class="op">=</span> <span class="pl">requests</span>.<span class="fn">get</span>(
    <span class="st">"https://ultra-api-008f.onrender.com/api/faphouse"</span>,
    <span class="pl">params</span><span class="op">=</span>{<span class="st">"url"</span>: <span class="pl">url</span>},
)
<span class="pl">d</span> <span class="op">=</span> <span class="pl">r</span>.<span class="fn">json</span>()
<span class="kw">if</span> <span class="pl">d</span>[<span class="st">"status"</span>]:
    <span class="kw">await</span> <span class="pl">message</span>.<span class="fn">reply_photo</span>(
        <span class="pl">photo</span><span class="op">=</span><span class="pl">d</span>[<span class="st">"data"</span>][<span class="st">"thumbnail"</span>],
        <span class="pl">caption</span><span class="op">=</span><span class="pl">d</span>[<span class="st">"data"</span>][<span class="st">"formatted_text"</span>],
    )
<span class="kw">else</span>:
    <span class="kw">await</span> <span class="pl">message</span>.<span class="fn">reply</span>(<span class="st">f"{d['message']}"</span>)</pre>
          </div>
        </div>

        <div class="tab-panel panel-curl">
          <div class="code-block">
            <button class="copy-btn" data-copy-pre="code-curl">
              <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="8" height="8" rx="1.5"/><path d="M2 10V2h8"/></svg>Copy
            </button>
            <pre id="code-curl">curl "https://ultra-api-008f.onrender.com/api/faphouse?url=https://faphouse.com/videos/QJKmfH"</pre>
          </div>
        </div>

      </div>
    </div>
  </section>

  <!-- Errors -->
  <section>
    <h2>Error responses</h2>
    <div class="spec">
      <div class="spec-row err"><div class="spec-key">400<span class="type">Bad Request</span></div><div class="spec-desc">url parameter missing, or not a valid faphouse link</div></div>
      <div class="spec-row err"><div class="spec-key">403<span class="type">Forbidden</span></div><div class="spec-desc">Fanclub-locked video — needs a separate subscription</div></div>
      <div class="spec-row err"><div class="spec-key">502<span class="type">Bad Gateway</span></div><div class="spec-desc">Stream URL could not be resolved — link may be expired or private</div></div>
    </div>
  </section>

  <footer>
    <p>FapHouse API &mdash; <a href="/health">health</a></p>
    <p id="uptime-txt" style="color:var(--faint);font-size:0.8rem;"></p>
  </footer>

</div>

<script>
// ── Theme toggle ───────────────────────────────────────────────────────
(function(){
  var btn = document.getElementById('themeBtn');
  var houseText = document.getElementById('house-text');
  var saved = localStorage.getItem('fh_theme') || 'dark';

  function applyTheme(t){
    document.documentElement.setAttribute('data-theme', t);
    if(houseText) houseText.style.color = t === 'light' ? 'var(--text)' : '#fff';
    btn.textContent = t === 'light' ? '🌙 Dark' : '☀️ Light';
    localStorage.setItem('fh_theme', t);
  }

  applyTheme(saved);
  btn.addEventListener('click', function(){
    var cur = document.documentElement.getAttribute('data-theme') || 'dark';
    applyTheme(cur === 'dark' ? 'light' : 'dark');
  });
})();
(function(){
  var badge = document.getElementById('status-badge');
  var txt   = document.getElementById('status-text');
  var start = Date.now();
  fetch('/health')
    .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function(){
      var ms = Date.now() - start;
      txt.textContent = 'Live \u2022 ' + ms + ' ms';
      var el = document.getElementById('uptime-txt');
      if(el) el.textContent = 'Response time: ' + ms + ' ms';
    })
    .catch(function(){
      badge.classList.add('err');
      badge.querySelector('.dot').style.animation = 'none';
      txt.textContent = 'Unreachable';
    });
})();

// ── Copy buttons ───────────────────────────────────────────────────────
function doCopy(text, btn){
  var origText = btn.textContent;
  var origHTML = btn.innerHTML;

  function onSuccess(){
    btn.classList.add('copied');
    btn.innerHTML = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="2,7 5,10 12,3"/></svg>Copied!';
    setTimeout(function(){
      btn.innerHTML = origHTML;
      btn.classList.remove('copied');
    }, 1800);
  }

  function onFail(){
    // execCommand fallback for HTTP or older browsers
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0;';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      if(ok) onSuccess();
      else { btn.textContent = 'Failed'; setTimeout(function(){ btn.innerHTML = origHTML; }, 1500); }
    } catch(e){
      btn.textContent = 'Failed';
      setTimeout(function(){ btn.innerHTML = origHTML; }, 1500);
    }
  }

  if(navigator.clipboard && window.isSecureContext){
    navigator.clipboard.writeText(text).then(onSuccess).catch(onFail);
  } else {
    onFail();
  }
}

document.querySelectorAll('.copy-btn').forEach(function(btn){
  btn.addEventListener('click', function(){
    var text = '';
    if(btn.dataset.copy){
      text = location.origin + btn.dataset.copy;
    } else if(btn.dataset.copyPre){
      var pre = document.getElementById(btn.dataset.copyPre);
      text = pre ? pre.innerText : '';
    }
    if(text) doCopy(text, btn);
  });
});

// ── Code example tabs ──────────────────────────────────────────────────
document.querySelectorAll('.tab-labels label').forEach(function(label){
  label.addEventListener('click', function(){
    var radio = document.getElementById(label.getAttribute('for'));
    if(radio) radio.checked = true;
  });
});

// ── Try-it: two-panel tabs ─────────────────────────────────────────────
(function(){
  var tabs    = document.querySelectorAll('.try-tab');
  var panels  = document.querySelectorAll('.try-panel');

  tabs.forEach(function(tab){
    tab.addEventListener('click', function(){
      tabs.forEach(function(t){ t.classList.remove('active'); });
      panels.forEach(function(p){ p.classList.remove('active'); });
      tab.classList.add('active');
      var pid = 'panel-' + tab.dataset.panel;
      var panel = document.getElementById(pid);
      if(panel) panel.classList.add('active');
    });
  });

  // ── Fetch logic ────────────────────────────────────────────────────
  var input    = document.getElementById('searchInput');
  var btn      = document.getElementById('searchBtn');
  var loading  = document.getElementById('searchLoading');
  var videoCard = document.getElementById('videoCard');
  var fetchErr = document.getElementById('fetchError');
  var respContent = document.getElementById('respContent');

  function switchToTab(name){
    tabs.forEach(function(t){ t.classList.remove('active'); });
    panels.forEach(function(p){ p.classList.remove('active'); });
    var tab = document.querySelector('.try-tab[data-panel="' + name + '"]');
    var panel = document.getElementById('panel-' + name);
    if(tab) tab.classList.add('active');
    if(panel) panel.classList.add('active');
  }

  function syntaxJSON(obj){
    var s = JSON.stringify(obj, null, 2);
    return s
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"([\\w@.:/\\-_]+)"(\\s*:)/g, '<span class="jk">"$1"</span>$2')
      .replace(/:\\s*"([^"]*)"/g, function(m,v){
        // Long multi-line string values (formatted_text in particular —
        // a Telegram-Markdown caption with its own \\n line breaks and
        // *bold*/[label](url) syntax) used to show up here as one dense
        // line with literal "\\n" and no visual structure at all — that's
        // what looked like garbled clutter in the response viewer. This
        // only reformats the *display*; the actual JSON text used by the
        // Copy JSON button below still comes from JSON.stringify(d, null, 2)
        // untouched, so nothing here changes what a real API caller gets.
        var display = v.replace(/\\\\n/g, '<br>').replace(/\\\\t/g, '&nbsp;&nbsp;&nbsp;&nbsp;');
        return ': <span class="js">"' + display + '"</span>';
      })
      .replace(/:\\s*(\\d+(\\.\\d+)?)/g, ': <span class="jn">$1</span>')
      .replace(/:\\s*(true|false|null)/g, ': <span class="jb">$1</span>');
  }

  // BUG FIX: every URL field in the API's JSON response (thumbnail,
  // m3u8_link, available_qualities[].url) comes prefixed with an emoji +
  // space — e.g. "https://..." — by design, for readability when
  // looking at the raw JSON (see _e() in app.py). This page was using
  // those values directly as href/src, so the browser saw a literal
  // emoji character at the start of the URL and treated the whole thing
  // as invalid — broken thumbnail, dead quality buttons, dead stream
  // link. cleanUrl() strips anything before the URL itself by jumping to
  // the first "http" it finds, rather than trying to match the emoji
  // specifically (some of these are multi-codepoint emoji+variation-
  // selector sequences, which a naive single-codepoint regex would miss
  // half of) — works regardless of what the prefix actually is.
  function cleanUrl(v){
    if(!v) return v;
    var i = v.indexOf('http');
    return i === -1 ? v.trim() : v.slice(i);
  }
  window.cleanUrl = cleanUrl;   // Library section below is a separate IIFE — needs this exposed to reach it

  function doFetch(){
    var url = (input.value || '').trim();
    if(!url){ input.focus(); return; }
    if(!url.includes('faphouse')){
      videoCard.className = 'video-card';
      fetchErr.innerHTML = '<div class="fetch-error">Enter a faphouse.com or faphouse2.com video URL.</div>';
      return;
    }
    fetchErr.innerHTML = '';
    videoCard.className = 'video-card';
    loading.classList.add('show');
    btn.disabled = true; btn.textContent = '...';

    fetch('/api/faphouse?url=' + encodeURIComponent(url))
      .then(function(r){ return r.json(); })
      .then(function(d){
        loading.classList.remove('show');
        btn.disabled = false; btn.textContent = 'Fetch';

        // ── Update Response tab ──
        if(d.status){
          respContent.innerHTML =
            '<div class="resp-bar">' +
              '<span class="resp-status status-ok">200 OK</span>' +
              '<button class="copy-btn" id="copyResp"><svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="8" height="8" rx="1.5"/><path d="M2 10V2h8"/></svg>Copy JSON</button>' +
            '</div>' +
            '<div class="resp-json" id="respJson">' + syntaxJSON(d) + '</div>';
          // wire copy button for response
          var copyBtn = document.getElementById('copyResp');
          if(copyBtn) copyBtn.addEventListener('click', function(){
            doCopy(JSON.stringify(d, null, 2), copyBtn);
          });
        } else {
          respContent.innerHTML =
            '<div class="resp-bar"><span class="resp-status status-err">Error</span></div>' +
            '<div class="resp-json">' + syntaxJSON(d) + '</div>';
        }

        if(!d.status){
          fetchErr.innerHTML = '<div class="fetch-error">' + (d.message || 'Unknown error') + '</div>';
          switchToTab('response');
          return;
        }

        // ── Build video card ──
        var data = d.data;
        var html = '';

        if(data.thumbnail)
          html += '<img class="video-thumb" src="' + cleanUrl(data.thumbnail) + '" onerror="this.style.display=\\'none\\'">';

        if(data.title)
          html += '<div class="video-title">' + data.title + '</div>';

        var meta = '';
        if(data.author)   meta += '<span class="meta-pill">' + data.author + '</span>';
        if(data.duration) meta += '<span class="meta-pill">⏱ ' + data.duration + '</span>';
        if(data.size)     meta += '<span class="meta-pill">' + data.size + '</span>';
        if(meta) html += '<div class="video-meta">' + meta + '</div>';

        var quals = data.available_qualities || [];
        if(quals.length){
          html += '<div class="quality-row">';
          quals.forEach(function(q){
            if(q.url) html += '<a class="quality-btn" href="' + cleanUrl(q.url) + '" target="_blank">' + (q.label||'') + '</a>';
          });
          html += '</div>';
        }

        if(data.m3u8_link)
          html += '<a class="m3u8-btn" href="' + cleanUrl(data.m3u8_link) + '" target="_blank">▶ Stream (Auto Best)</a>';

        videoCard.innerHTML = html;
        videoCard.className = 'video-card show';
        switchToTab('fetch');

        // Save to library
        if(window._libAdd){
          window._libAdd({
            url: url,
            title: data.title || url,
            thumbnail: cleanUrl(data.thumbnail) || null,
          });
        }
      })
      .catch(function(e){
        loading.classList.remove('show');
        btn.disabled = false; btn.textContent = 'Fetch';
        fetchErr.innerHTML = '<div class="fetch-error">Network error: ' + e.message + '</div>';
      });
  }

  btn.addEventListener('click', doFetch);
  input.addEventListener('keydown', function(e){ if(e.key==='Enter') doFetch(); });
})();

// ── Library (localStorage) ─────────────────────────────────────────────
(function(){
  var LIB_KEY = 'faphouse_library';

  function loadLib(){ try{ return JSON.parse(localStorage.getItem(LIB_KEY)||'[]'); }catch(e){ return []; } }
  function saveLib(lib){ localStorage.setItem(LIB_KEY, JSON.stringify(lib)); }

  function renderLib(){
    var lib = loadLib();
    var list = document.getElementById('libList');
    var count = document.getElementById('libCount');
    var btnCount = document.getElementById('libBtnCount');

    count.textContent = lib.length;
    btnCount.textContent = lib.length;
    btnCount.className = 'lib-btn-count' + (lib.length ? ' show' : '');

    if(!lib.length){
      list.innerHTML = '<div class="lib-empty">No videos yet — fetch one to save it here.</div>';
      return;
    }
    list.innerHTML = lib.map(function(item, i){
      var thumb = item.thumbnail
        ? '<img class="lib-icon" src="' + window.cleanUrl(item.thumbnail) + '" onerror="this.style.display=\\'none\\'">'
        : '<div class="lib-icon-placeholder"></div>';
      var domain = item.url.includes('faphouse2') ? 'FAPHOUSE2' : 'FAPHOUSE';
      return '<div class="lib-item" data-idx="' + i + '">' +
        thumb +
        '<div class="lib-info">' +
          '<div class="lib-name">' + (item.title || item.url) + '</div>' +
          '<span class="lib-badge">' + domain + '</span>' +
        '</div>' +
        '<button class="lib-del" data-idx="' + i + '" title="Remove">✕</button>' +
      '</div>';
    }).join('');

    // Click item → load into fetch input and auto-fetch
    list.querySelectorAll('.lib-item').forEach(function(el){
      el.addEventListener('click', function(e){
        if(e.target.classList.contains('lib-del')) return;
        var idx = parseInt(el.dataset.idx);
        var item = loadLib()[idx];
        if(!item) return;
        var input = document.getElementById('searchInput');
        if(input){ input.value = item.url; }
        closeLib();
        var fetchBtn = document.getElementById('searchBtn');
        if(fetchBtn) fetchBtn.click();
      });
    });

    // Delete buttons
    list.querySelectorAll('.lib-del').forEach(function(btn){
      btn.addEventListener('click', function(e){
        e.stopPropagation();
        var lib = loadLib();
        lib.splice(parseInt(btn.dataset.idx), 1);
        saveLib(lib); renderLib();
      });
    });
  }

  function addToLib(item){
    var lib = loadLib();
    if(lib.find(function(x){ return x.url === item.url; })) return; // no duplicates
    lib.unshift(item);
    if(lib.length > 50) lib = lib.slice(0, 50);
    saveLib(lib); renderLib();
  }

  function openLib(){ document.getElementById('libOverlay').classList.add('open'); renderLib(); }
  function closeLib(){ document.getElementById('libOverlay').classList.remove('open'); }

  document.getElementById('libBtn').addEventListener('click', openLib);
  document.getElementById('libClose').addEventListener('click', closeLib);
  document.getElementById('libOverlay').addEventListener('click', function(e){ if(e.target===this) closeLib(); });
  document.getElementById('libRefresh').addEventListener('click', renderLib);
  document.getElementById('libClearAll').addEventListener('click', function(){
    if(confirm('Clear all saved videos?')){ saveLib([]); renderLib(); }
  });
  document.addEventListener('keydown', function(e){ if(e.key==='Escape') closeLib(); });

  renderLib();
  window._libAdd = addToLib;
})();
</script>
</body>
</html>"""
    return html


@app.route("/health")
def health():
    return jsonify({"status": True, "creator": CREATOR, "message": "OK"})


@app.route("/api/faphouse")
def resolve():
    """
    GET /api/faphouse?url=<faphouse_link>
    """
    url = (request.args.get("url") or "").strip()
    if not url:
        return _error("Missing ?url= parameter.")
    if not faphouse.is_faphouse_link(url):
        return _error("Not a valid faphouse.com or faphouse2.com link.")

    # ── Parallel fetch: HTML + M3U8 + Qualities ────────────────────────
    # BUG FIX: Previously _task_m3u8 and _task_qualities BOTH called
    # client.get_m3u8_url() simultaneously — on a cache miss, this caused
    # two concurrent login+page-fetch attempts, a race on the unprotected
    # _m3u8_cache dict, and the qualities task timing out at 15s because
    # it had to wait for its own full resolve (10-15s) + master playlist
    # fetch (10s) = 25s+ total, always exceeding the timeout.
    #
    # Fix: fetch m3u8 first (with enough timeout), then derive qualities
    # FROM the already-resolved URL so get_available_qualities() only needs
    # to do the master-playlist fetch (~1-2s), not another full resolve.
    from concurrent.futures import ThreadPoolExecutor

    results = {
        "html":        None,
        "final_url":   url,
        "m3u8":        None,
        "qualities":   [],
        "fanclub_err": None,
    }

    def _task_html():
        return _fetch_page_html(url)

    def _task_m3u8():
        return faphouse.client.get_m3u8_url(url)

    # Phase 1: HTML + M3U8 in parallel (both independent)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_html = pool.submit(_task_html)
        fut_m3u8 = pool.submit(_task_m3u8)

        try:
            results["html"], results["final_url"] = fut_html.result(timeout=18)
        except Exception as e:
            logger.warning(f"HTML fetch failed: {e}")

        try:
            results["m3u8"] = fut_m3u8.result(timeout=25)
        except faphouse.FanclubLockedError as e:
            results["fanclub_err"] = str(e)
        except Exception as e:
            logger.warning(f"get_m3u8_url failed: {e}")

    if results["fanclub_err"]:
        return _error(f"Fanclub locked: {results['fanclub_err']}", 403)

    # Phase 2: Qualities — only if m3u8 resolved, uses the cached URL
    # so get_available_qualities only needs ~1-2s for the master playlist
    # fetch instead of another full 15-25s resolution cycle.
    if results["m3u8"]:
        try:
            results["qualities"] = faphouse.get_available_qualities(url) or []
        except faphouse.FanclubLockedError as e:
            results["fanclub_err"] = str(e)
        except Exception as e:
            logger.warning(f"get_available_qualities failed: {e}")

    html       = results["html"]
    final_url  = results["final_url"]
    m3u8_link  = results["m3u8"]
    qualities  = results["qualities"]

    meta = _scrape_full_meta(html) if html else {
        "title": None, "thumbnail": None, "duration_seconds": None,
        "duration": None, "views": None, "likes": None,
        "upload_date": None, "author": None, "description": None,
    }

    # Use final_url (after redirect) for slug extraction.
    # Short URLs like /videos/QJKmfH redirect to full English slug URLs
    # like /videos/newly-married-naughty-bhabhi-fucked-by-devar-QJKmfH
    filename_url = final_url if final_url != url else url

    # Also try to get English slug from __NEXT_DATA__ url/permalink/slug field.
    if html:
        current_id_match = re.search(r"-([A-Za-z0-9]+)/?$", filename_url.split("?")[0].rstrip("/"))
        current_id = current_id_match.group(1) if current_id_match else None

        candidates = re.findall(
            r'"(?:url|permalink|slug|canonicalUrl)"\s*:\s*"([^"]*videos/[^"]+)"',
            html
        )
        for raw_candidate in candidates:
            candidate = raw_candidate.replace("\\u002F", "/").replace("\\/", "/")
            if current_id and not candidate.rstrip("/").endswith(current_id):
                continue
            if len(candidate) > len(filename_url):
                filename_url = candidate if candidate.startswith("http") else f"https://faphouse.com{candidate}"
            break

    if not m3u8_link and not qualities:
        return _error("Could not resolve any stream URL — link may be expired or private.", 502)

    # ── Quality variants with direct URLs ────────────────────────────
    # BUG FIX: previously filtered out "Auto (Best)" entries, but that's
    # the ONLY entry returned when qualities can't be parsed (e.g. non-master
    # playlist, CDN fetch fails). Filtering it silently emptied quality_list,
    # meaning speed_link fell through to m3u8_link — that part was fine, but
    # quality_list=[] + no m3u8 = 502. Now keep Auto (Best) as a real entry
    # with url=m3u8_link so it's always useful to the caller.
    quality_list = []
    for q in qualities:
        label = q.get("label")
        url_q = q.get("url")
        if label == "Auto (Best)":
            # Use master m3u8 as the URL for "Auto" so it's always non-null
            if m3u8_link:
                quality_list.append({"label": "Auto (Best)", "url": m3u8_link})
            continue
        if url_q:
            quality_list.append({"label": label, "url": url_q})
    # If no explicit quality entries AND we have an m3u8, add Auto as fallback
    if not quality_list and m3u8_link:
        for label in _parse_multi_from_m3u8_url(m3u8_link):
            quality_list.append({"label": label, "url": m3u8_link})
        if not quality_list:
            quality_list.append({"label": "Auto (Best)", "url": m3u8_link})

    # ── Duration (ffprobe fallback — 3s timeout, not 8s) ─────────────
    if not meta["duration_seconds"] and m3u8_link:
        try:
            result = {"secs": None}
            def _probe():
                try:
                    result["secs"] = faphouse.get_video_duration(m3u8_link)
                except Exception:
                    pass
            t = threading.Thread(target=_probe, daemon=True)
            t.start()
            t.join(timeout=3)  # 8s → 3s: duration mostly comes from HTML now
            secs = result["secs"]
            if secs and secs > 0:
                meta["duration_seconds"] = int(secs)
                meta["duration"]         = _seconds_to_human(int(secs))
        except Exception:
            pass

    # ── Size estimation ───────────────────────────────────────────────
    size_bytes, size_human = _size_from_bandwidth(qualities, meta["duration_seconds"])

    # ── Title resolution (priority order) ────────────────────────────
    # 1. URL slug — ALWAYS English, taken from the FapHouse video URL itself
    #    e.g. /videos/indian-bhabhi-devar-hot-scene-QJKmfH → "Indian Bhabhi Devar Hot Scene"
    # 2. Scraped title from __NEXT_DATA__ / JSON-LD / og:title — may be
    #    localized (Hindi/Indonesian etc.) based on server IP, but usually
    #    English. _scrape_full_meta() already tried __NEXT_DATA__ first.
    #
    # NOTE: _ytdlp_extract_info() was previously called here as a "bonus try"
    # for title/description. Removed because:
    #   1. yt-dlp has NO FapHouse extractor — always fails with
    #      "ERROR: Unsupported URL: ..." printed to stderr on every request.
    #   2. t.join(timeout=14) blocked the Flask thread for up to 14 seconds.
    #   3. ytdlp_title was always None — zero benefit, pure cost.

    slug_title = _slug_from_url(filename_url)   # always English, but only ever as long as the URL slug itself

    def _is_mostly_english(text: str) -> bool:
        if not text:
            return False
        ascii_letters = sum(1 for c in text if c.isascii())
        return ascii_letters / len(text) > 0.85

    # Title priority: real scraped title (full, correctly cased) if it's
    # actually in English > slug (always English, but only as complete as
    # the URL slug) > scraped title even if not confirmed English > "Unknown"
    if meta["title"] and _is_mostly_english(meta["title"]):
        english_title = meta["title"]
    else:
        english_title = slug_title or meta["title"] or "Unknown"

    # ── Filename ──────────────────────────────────────────────────────
    filename = _make_filename(english_title, filename_url)

    # ── HTML entity decode ────────────────────────────────────────────
    # Titles scraped from HTML carry &amp; &quot; &#39; etc. — decode them
    # so the JSON response has clean readable text, not HTML markup.
    import html as _html
    def _decode(text):
        return _html.unescape(text).strip() if isinstance(text, str) else text

    english_title           = _decode(english_title)
    filename                = _make_filename(english_title, filename_url)
    meta["description"]     = _decode(meta.get("description"))
    meta["author"]          = _decode(meta.get("author"))

    # ── URL fixer ────────────────────────────────────────────────────
    def _fix_url(u):
        if not u:
            return None
        return u.replace(",", "%2C").replace("+", "%2B")

    # ── Speed link = best available quality URL ───────────────────────
    speed_link = next(
        (q["url"] for q in (quality_list or []) if q.get("url")),
        m3u8_link
    )

    # ── Formatted text ────────────────────────────────────────────────
    # Order:
    #   Filename
    #   Title
    #   Author
    #   Duration
    #   Size
    #   Description
    #   ━━━━━━━━━━━━
    #   Thumbnail link
    #   2160p link
    #   1080p link
    #   ... (all quality links)

    flines = []

    if filename:
        flines.append(f"Filename: {filename}")
    if english_title:
        flines.append(f"Title: {english_title}")
    if meta.get("author"):
        flines.append(f"Author: {meta['author']}")
    if meta.get("duration"):
        flines.append(f"Duration: {meta['duration']}")
    if size_human:
        flines.append(f"Size: {size_human}")
    if meta.get("description"):
        flines.append(f"Description: {meta['description']}")

    if meta.get("thumbnail"):
        flines.append(f"Thumbnail: {_fix_url(meta['thumbnail'])}")
    for q in (quality_list or []):
        if q.get("label") and q.get("url") and q.get("label") != "Auto (Best)":
            flines.append(f"{q['label']}: {_fix_url(q['url'])}")

    formatted_text = "\n".join(flines)

    # ── Build response — Filename + Title sabse upar, links neeche ────
    data: dict = {}

    # 1. Info fields (top) — Filename aur Title pehle
    data["Filename"]         = filename
    data["title"]            = english_title
    data["author"]           = meta["author"]
    data["duration"]         = meta["duration"]
    data["duration_seconds"] = meta["duration_seconds"]
    if meta["views"] is not None:
        data["views"]        = meta["views"]
    if meta["likes"] is not None:
        data["likes"]        = meta["likes"]
    if meta["upload_date"] is not None:
        data["upload_date"]  = meta["upload_date"]
    data["description"]      = meta["description"]
    data["size"]             = size_human
    data["size_bytes"]       = size_bytes

    # 2. Links (bottom)
    data["thumbnail"]          = _fix_url(meta.get("thumbnail"))
    data["m3u8_link"]          = _fix_url(m3u8_link)
    data["available_qualities"] = [
        {"label": q.get("label", "?"), "url": _fix_url(q.get("url"))}
        for q in (quality_list or [])
        if q.get("url")
    ]
    data["formatted_text"]     = formatted_text

    return jsonify({
        "status":  True,
        "creator": CREATOR,
        "data":    data,
    })


@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {e}", exc_info=True)
    return jsonify({
        "status":  False,
        "creator": CREATOR,
        "message": f"Internal server error: {type(e).__name__}: {e}",
    }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
