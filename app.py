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

from flask import Flask, jsonify, request, redirect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.exceptions import HTTPException

import faphouse_downloader as faphouse

try:
    from deep_translator import GoogleTranslator as _DeepGoogleTranslator
    _DEEP_TRANSLATOR_OK = True
except ImportError:
    _DEEP_TRANSLATOR_OK = False

try:
    from googletrans import Translator as _GoogletransTranslator
    _GOOGLETRANS_OK = True
except ImportError:
    _GOOGLETRANS_OK = False

_TRANSLATOR_OK = _DEEP_TRANSLATOR_OK or _GOOGLETRANS_OK

import html as _html_module

def _decode_html(text):
    """Decode HTML entities — &amp; → &, &#39; → ' etc."""
    return _html_module.unescape(text).strip() if isinstance(text, str) else text

def _is_mostly_english(text: str) -> bool:
    """True if >85% of chars are ASCII — heuristic for English text."""
    if not text or len(text) < 3:
        return False
    ascii_letters = sum(1 for c in text if c.isascii())
    return ascii_letters / len(text) > 0.85

def _translate_to_english(text: str) -> str | None:
    """Translate text to English using Google Translate — free, no API key.

    Strategy:
    1. deep-translator (GoogleTranslator) — most reliable in sync context
    2. googletrans fallback — has asyncio issues in some envs, used as last resort

    Runs in a daemon thread with 5s timeout so it never blocks the request.
    Returns None on any failure — caller uses original text as fallback.
    """
    if not _TRANSLATOR_OK or not text or not text.strip():
        return None

    # Don't translate if already English
    if _is_mostly_english(text):
        return text

    result = {"out": None}

    def _run():
        # Strategy 1: deep-translator — pure sync, no asyncio issues
        if _DEEP_TRANSLATOR_OK:
            try:
                out = _DeepGoogleTranslator(source="auto", target="en").translate(text)
                if out and isinstance(out, str) and out.strip():
                    result["out"] = out.strip()
                    return
            except Exception as e:
                logger.debug(f"[translate/deep] failed: {e}")

        # Strategy 2: googletrans — may have asyncio issues in Flask threads
        if _GOOGLETRANS_OK:
            try:
                import asyncio
                # Create a new event loop for this thread to avoid conflict
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    _gt = _GoogletransTranslator()
                    out = _gt.translate(text, dest="en")
                    if out and hasattr(out, "text") and out.text and out.text.strip():
                        result["out"] = out.text.strip()
                finally:
                    loop.close()
            except Exception as e:
                logger.debug(f"[translate/googletrans] failed: {e}")

    _thread = threading.Thread(target=_run, daemon=True)
    _thread.start()
    _thread.join(timeout=5)
    return result["out"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("faphouse_api")

app   = Flask(__name__)

@app.after_request
def _add_cors(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response
# Render (and any reverse proxy — Nginx/Caddy on a VPS too, if you put one
# in front) terminates TLS and forwards plain HTTP internally, only noting
# the original scheme/host in X-Forwarded-* headers. Without ProxyFix,
# Flask ignores those and request.url_root below would report "http://"
# even though the site is actually served over https — this trusts one
# hop of proxy (x_proto, x_host) so the dynamic base URL comes out right
# on Render, behind a VPS reverse proxy, or plain/unproxied either way.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.json.ensure_ascii = False  # Emojis as-is, not \uXXXX escape codes
# Flask's default JSON provider alphabetically re-sorts every dict's keys
# on output (sort_keys=True), regardless of insertion order — so despite
# the "Filename/Title first, links last" ordering built into `data` below
# (and available_qualities specifically being the very last key added),
# the actual JSON response was coming out alphabetized instead, putting
# available_qualities right after "author" near the top. Disabling this
# is what makes the intended field order actually take effect.
app.json.sort_keys = False
CREATOR = "Ultra API"

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
    _empty = {
        "title": None, "thumbnail": None, "duration_seconds": None,
        "duration": None, "views": None, "likes": None,
        "upload_date": None, "author": None, "description": None,
    }
    if not html:
        return _empty
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
    #
    # BUG FIX: previous regex used r'\{.*?\}' (non-greedy) which stopped at the
    # FIRST closing brace in the blob — capturing only `{}` or the outermost
    # shell of a shallow JSON, never the actual deeply-nested video object.
    # FapHouse's __NEXT_DATA__ is typically 50-500 KB of nested JSON, so the
    # non-greedy match always returned a truncated/invalid fragment.
    #
    # Fix: find the opening `{` and walk forward counting brace depth to find
    # the matching closing `}` — same approach yt-dlp and many scrapers use.
    next_data_m = re.search(
        r'<script\s+id=["\']__NEXT_DATA__["\'][^>]*>',
        html, re.IGNORECASE
    )
    if next_data_m:
        blob_start = html.find('{', next_data_m.end())
        if blob_start != -1:
            depth = 0
            blob_end = blob_start
            in_str = False
            escape_next = False
            for idx in range(blob_start, min(blob_start + 2_000_000, len(html))):
                ch = html[idx]
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\' and in_str:
                    escape_next = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        blob_end = idx + 1
                        break
            if blob_end > blob_start:
                try:
                    nd = _json.loads(html[blob_start:blob_end])
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

                    # English description
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
                    # upload_date from __NEXT_DATA__
                    if not meta["upload_date"]:
                        for date_key in ("createdAt", "uploadedAt", "publishedAt", "datePublished", "createdAt"):
                            d_val = video_obj.get(date_key)
                            if d_val and isinstance(d_val, str) and len(d_val) >= 10:
                                meta["upload_date"] = d_val[:10]
                                break
                    # views/likes from __NEXT_DATA__
                    if not meta["views"]:
                        for k in ("viewsCount", "views", "viewCount"):
                            v = video_obj.get(k)
                            if isinstance(v, (int, float)) and v > 0:
                                meta["views"] = int(v)
                                break
                    if not meta["likes"]:
                        for k in ("likesCount", "likes", "likeCount", "votesAmount"):
                            v = video_obj.get(k)
                            if isinstance(v, (int, float)) and v > 0:
                                meta["likes"] = int(v)
                                break
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
<title>Ultra API — FapHouse Resolver</title>
<!-- BUG FIX: no favicon at all before this — browsers show their own
     generic/blank default tab icon with nothing set, which is what was
     showing up instead of FapHouse's actual branding. Points straight at
     FapHouse's own official favicon (their CDN, not a re-hosted copy) so
     the tab icon matches the real site. -->
<link rel="icon" type="image/png" sizes="32x32" href="https://assets-nss.flixcdn.com/61d4c051c4a2edb8244d150bedbae1b061628206/fap-site/default/images/favicons/favicon-32x32.png">
<link rel="icon" type="image/png" sizes="96x96" href="https://assets-nss.flixcdn.com/61d4c051c4a2edb8244d150bedbae1b061628206/fap-site/default/images/favicons/favicon-96x96.png">
<link rel="shortcut icon" href="https://assets-nss.flixcdn.com/61d4c051c4a2edb8244d150bedbae1b061628206/fap-site/default/images/favicons/favicon.ico">
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

  /* Theme toggle — fixed beside the floating Library icon (top-right),
     same row, so the two sit together instead of theme-btn being left
     behind in the header while lib-btn floats on its own. */
  /* ── iOS-style pill toggle ─────────────────────────────────────────────── */
  .theme-toggle-wrap{
    position:fixed; top:12px; right:58px; z-index:50;
    display:flex; align-items:center; gap:6px;
    background:var(--panel); border:1px solid var(--line); border-radius:20px;
    padding:4px 8px 4px 6px; cursor:pointer;
    box-shadow:0 4px 14px rgba(0,0,0,.35);
    font-family:'Inter',sans-serif; font-size:0.68rem; color:var(--muted);
    transition:border-color .2s;
    user-select:none;
  }
  .theme-toggle-wrap:hover{ border-color:var(--amber); }
  .toggle-icon{ font-size:0.75rem; line-height:1; }
  .toggle-track{
    width:26px; height:14px; border-radius:7px;
    background:var(--line); position:relative;
    transition:background .25s; flex-shrink:0;
  }
  .toggle-thumb{
    position:absolute; top:2px; left:2px;
    width:10px; height:10px; border-radius:50%;
    background:#fff; transition:transform .25s, background .25s;
    box-shadow:0 1px 3px rgba(0,0,0,.3);
  }
  [data-theme="light"] .toggle-track{ background:var(--amber); }
  [data-theme="light"] .toggle-thumb{ transform:translateX(12px); background:#fff; }
  .toggle-label{ font-size:0.65rem; color:var(--muted); }
  /* legacy .theme-btn — kept so nothing 404s if cached page calls it */
  .theme-btn{ display:none; }
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
  .dl-btn{
    display:inline-flex; align-items:center; gap:6px; background:var(--amber);
    color:#0a0a0a; border:none; border-radius:7px; padding:8px 16px;
    font-family:'Inter',sans-serif; font-weight:600; font-size:0.82rem;
    text-decoration:none; cursor:pointer; transition:background .15s, transform .15s; margin-left:8px;
  }
  .dl-btn:hover{ background:#f5b45f; transform:translateY(-1px); }
  .dl-btn:active{ transform:translateY(0); }
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

  /* Library button — fixed floating icon pinned to the top-right of the
     viewport (stays put on scroll), rounded-square app-icon style with
     the count badge sitting on its corner, matching the reference image
     instead of competing for space as an inline text button in the
     header row. */
  .lib-btn{
    position:fixed; top:12px; right:12px; z-index:50;
    display:flex; align-items:center; justify-content:center;
    width:38px; height:38px; border-radius:12px;
    background:var(--panel); border:1px solid var(--line);
    color:var(--muted); font-size:1.05rem; cursor:pointer;
    transition:all .15s; box-shadow:0 4px 14px rgba(0,0,0,.35);
  }
  .lib-btn:hover{ border-color:var(--amber); color:var(--amber); }
  .lib-btn-count{
    background:var(--amber); color:#1a1206; border-radius:50%;
    min-width:16px; height:16px; padding:0 3px; font-size:0.62rem; font-weight:700;
    display:none; align-items:center; justify-content:center;
    position:absolute; top:-5px; right:-5px; border:2px solid var(--bg);
  }
  .lib-btn-count.show{ display:flex; }
  @keyframes fadein{ from{ opacity:0; transform:translateY(10px); } to{ opacity:1; transform:translateY(0); } }
  .page{ animation: fadein .45s ease both; }

  /* ── Header ── */
  /* Theme + Library buttons are position:fixed (top-right corner) now,
     so they're out of normal flow and don't push the title over on
     their own — this padding-right reserves their footprint (~2x48px
     button width + gaps + the 16px edge margin) so "FAPHOUSE" doesn't
     run underneath and get its last letter(s) covered, like it did. */
  .header-row{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-bottom:6px; padding-right:120px; }
  .reel{ display:flex; align-items:center; gap:14px; }
  .reel svg{ flex:none; width:34px; height:34px; transition:transform .6s cubic-bezier(.34,1.56,.64,1); }
  @media (max-width: 480px){
    /* Narrow phone screens: reserve a bit more (buttons sit closer to
       the text horizontally at this width) and shrink the title so the
       full word reliably clears them. */
    .header-row{ padding-right:96px; }
    .reel h1{ font-size:1.5rem !important; }
  }
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
  .code-block .copy-btn{
    position:absolute; top:10px; right:10px;
    /* Was opacity:0 + only shown via :hover — doesn't work on touch
       screens (no real hover state), so the button was invisible until
       tapped. Now always visible. Solid background (was transparent)
       so it reads as a clean pill sitting on top of the scrolling code
       line instead of the "Copy" label visually colliding/blending with
       whatever text happens to be scrolled underneath it. */
    background:var(--panel); opacity:1;
  }
  pre{
    margin:0; padding:16px; font-family:'JetBrains Mono',monospace; font-size:0.8rem;
    white-space:pre; overflow-x:auto; line-height:1.65;
    /* Reserves room so the fixed-position copy button (see .code-block
       .copy-btn above) never sits directly over the start of a scrolled
       line's text — previously it could land right on top of characters
       like "url=https:/" as seen when scrolling the cURL example. */
    padding-right:76px;
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

  /* ── Footer ───────────────────────────────────────────────────────────── */
  .site-footer{
    margin-top:64px; border-top:1px solid var(--line); padding-top:40px;
  }
  .footer-grid{
    display:grid; grid-template-columns:1fr 1fr; gap:32px; margin-bottom:36px;
  }
  @media(max-width:420px){ .footer-grid{ grid-template-columns:1fr; gap:24px; } }
  .footer-col-label{
    font-size:0.68rem; font-weight:700; letter-spacing:0.12em;
    color:var(--faint); text-transform:uppercase; margin:0 0 12px;
  }
  .tg-contact-btn{
    display:inline-flex; align-items:center; gap:9px;
    color:var(--text); text-decoration:none;
    background:var(--panel); border:1px solid var(--line);
    border-radius:10px; padding:9px 14px;
    font-size:0.88rem; font-weight:500;
    transition:border-color .2s, box-shadow .2s;
  }
  .tg-contact-btn:hover{
    border-color:var(--amber);
    box-shadow:0 2px 12px rgba(242,166,65,.12);
  }
  .support-link{
    display:flex; align-items:center; gap:7px;
    color:var(--muted); text-decoration:none; font-size:0.87rem;
    padding:6px 0; border-bottom:1px solid transparent;
    transition:color .2s, border-color .2s;
  }
  .support-link:hover{ color:var(--text); border-color:var(--line); }
  .support-link svg{ flex-shrink:0; opacity:.6; }
  .footer-bottom{
    display:flex; align-items:center; justify-content:space-between;
    flex-wrap:wrap; gap:8px;
    border-top:1px solid var(--line); padding-top:18px; margin-top:4px;
  }
  .footer-bottom-left{ display:flex; align-items:center; gap:14px; }
  .footer-brand{ font-size:0.8rem; color:var(--faint); font-weight:500; }
  .footer-health{
    font-size:0.75rem; color:var(--muted);
    text-decoration:none; padding:2px 8px;
    border:1px solid var(--line); border-radius:20px;
    transition:border-color .2s; display:inline-flex; align-items:center; gap:4px;
  }
  .footer-health:hover{ border-color:var(--teal); color:var(--teal); }
  .footer-health::before{ content:""; display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--teal); }
  .footer-copy{ font-size:0.75rem; color:var(--faint); }
  #uptime-txt{ font-size:0.75rem; color:var(--faint); }
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
      <!-- BUG FIX: this used to be a generic orange lightning-bolt SVG
           (an "Ultra" mark unrelated to FapHouse) — swapped for FapHouse's
           own official icon so the header actually shows their branding,
           same source as the favicon above. -->
      <img src="https://assets-nss.flixcdn.com/61d4c051c4a2edb8244d150bedbae1b061628206/fap-site/default/images/favicons/favicon-96x96.png"
           width="42" height="42" alt="FapHouse" style="border-radius:8px;flex-shrink:0;"
           onerror="this.style.display='none'">
      <div style="display:flex;flex-direction:column;gap:2px;">
        <h1 style="font-size:clamp(2.8rem,8vw,5.5rem);font-weight:900;letter-spacing:-0.03em;text-transform:uppercase;line-height:0.95;font-family:'Inter','Arial Black',sans-serif;">
          <span style="color:#f2a641;text-shadow:0 0 40px rgba(242,166,65,0.6);">FAP</span><span id="house-text" style="color:#f0ede6;">HOUSE</span>
          <span style="font-size:0.28em;color:#f2a641;font-family:'Inter',sans-serif;font-weight:700;letter-spacing:0.08em;vertical-align:middle;margin-left:8px;background:#1a1206;border:1px solid #f2a641;border-radius:4px;padding:2px 7px;">Ultra</span>
        </h1>

      </div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:-30px;">
      <div class="theme-toggle-wrap" id="themeBtn" role="button" tabindex="0" aria-label="Toggle theme">
      <span class="toggle-icon" id="toggleIcon">🌙</span>
      <div class="toggle-track"><div class="toggle-thumb"></div></div>
      <span class="toggle-label" id="toggleLabel">Dark</span>
    </div>
      <button class="lib-btn" id="libBtn" title="Library">
        📚
        <span class="lib-btn-count" id="libBtnCount">0</span>
      </button>
    </div>
  </div>
  <p class="tagline">A fast, reliable resolver for <strong style="color:var(--text)">faphouse.com</strong> and <strong style="color:var(--text)">faphouse2.com</strong> — turn any video link into clean metadata, an adaptive HLS playlist, and a direct download URL for every available quality, formatted and ready to drop straight into a Telegram reply.</p>

  <!-- Quick test buttons above fetch section -->
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 4px;">
    <a class="run" href="/api/faphouse?url=https://faphouse.com/videos/QJKmfH" target="_blank" style="text-decoration:none;">
      <svg viewBox="0 0 12 12" fill="currentColor" width="12" height="12"><path d="M1 0.5 11 6 1 11.5Z"/></svg>
      faphouse.com
    </a>
    <a class="run" href="/api/faphouse?url=https://faphouse2.com/videos/QJKmfH" target="_blank" style="background:var(--teal);color:#0a1a15;text-decoration:none;">
      <svg viewBox="0 0 12 12" fill="currentColor" width="12" height="12"><path d="M1 0.5 11 6 1 11.5Z"/></svg>
      faphouse2.com
    </a>
  </div>

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
    <span class="st">"__BASE_URL__/api/faphouse"</span>,
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
    <span class="st">"__BASE_URL__/api/faphouse"</span>,
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
            <pre id="code-curl">curl "__BASE_URL__/api/faphouse?url=https://faphouse.com/videos/QJKmfH"</pre>
          </div>
        </div>

      </div>
    </div>
  </section>

  <!-- Footer -->
  <div class="site-footer">
    <div class="footer-grid">

      <!-- Contact -->
      <div>
        <p class="footer-col-label">Contact</p>
        <a href="https://t.me/anujedits97" target="_blank" rel="noopener" class="tg-contact-btn">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="#2AABEE">
            <path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.562 8.248-2.018 9.51c-.145.658-.537.818-1.084.508l-3-2.21-1.447 1.394c-.16.16-.295.295-.605.295l.213-3.053 5.56-5.023c.242-.213-.054-.333-.373-.12L7.26 14.42l-2.95-.924c-.642-.2-.655-.643.136-.953l11.527-4.448c.535-.194 1.003.13.59.153z"/>
          </svg>
          @anujedits97
        </a>
      </div>

      <!-- Support -->
      <div>
        <p class="footer-col-label">Support</p>
        <a href="https://t.me/anujedits97" target="_blank" rel="noopener" class="support-link">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 8v4m0 4h.01"/></svg>
          Bug Report
        </a>
        <a href="https://t.me/anujedits97" target="_blank" rel="noopener" class="support-link" style="margin-top:4px;">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z"/></svg>
          Feature Request
        </a>
      </div>

    </div>

    <!-- Bottom bar -->
    <div class="footer-bottom">
      <div class="footer-bottom-left">
        <span class="footer-brand"><span style="color:var(--amber);font-weight:700;">FAP</span><span style="font-weight:700;">HOUSE</span> <span style="color:var(--muted);font-weight:400;">Ultra</span></span>

      </div>
      <span class="footer-copy">&copy; 2026 Ultra API</span>
    </div>
  </div>

</div>

<script>
// ── Theme toggle ───────────────────────────────────────────────────────
(function(){
  var btn = document.getElementById('themeBtn');
  var houseText = document.getElementById('house-text');
  var saved = localStorage.getItem('fh_theme') || 'dark';

  var iconEl  = document.getElementById('toggleIcon');
  var labelEl = document.getElementById('toggleLabel');

  function applyTheme(t){
    document.documentElement.setAttribute('data-theme', t);
    if(houseText) houseText.style.color = t === 'light' ? 'var(--text)' : '#fff';
    if(iconEl)  iconEl.textContent  = t === 'light' ? '☀️' : '🌙';
    if(labelEl) labelEl.textContent = t === 'light' ? 'Light' : 'Dark';
    localStorage.setItem('fh_theme', t);
  }

  applyTheme(saved);

  function _toggle(){
    var cur = document.documentElement.getAttribute('data-theme') || 'dark';
    applyTheme(cur === 'dark' ? 'light' : 'dark');
  }
  btn.addEventListener('click', _toggle);
  btn.addEventListener('keydown', function(e){ if(e.key===' '||e.key==='Enter') _toggle(); });
})();
(function(){
  // status-badge/status-text ("v1.0" + "Live • Nms" pill in the header)
  // were removed — this still pings /health and fills in the footer's
  // response-time line, just no longer touches the deleted badge.
  var start = Date.now();
  fetch('/health')
    .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function(){
      var ms = Date.now() - start;
      var el = document.getElementById('uptime-txt');
      if(el) el.textContent = 'Response time: ' + ms + ' ms';
    })
    .catch(function(){});
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

  // ── Global helpers ──────────────────────────────────────────────────
  function escHtml(s){
    return String(s||'')
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  }
  // BUG FIX: the Library section further down is a separate top-level
  // IIFE and calls escHtml() while rendering each saved item's title —
  // without this, that call throws "escHtml is not defined" (this
  // function was only in scope here), which happened AFTER the item
  // count badge was already set but BEFORE the list markup was written.
  // Net effect: the library badge showed the real count (e.g. "1") while
  // the list itself stayed stuck on the static "No videos yet" placeholder
  // forever, since the render call that would've replaced it always threw
  // partway through. cleanUrl() right below already gets this same
  // treatment for the same reason — this was just missed for escHtml.
  window.escHtml = escHtml;
  var btn      = document.getElementById('searchBtn');
  var input    = document.getElementById('searchInput');
  var loading  = document.getElementById('searchLoading');
  var videoCard = document.getElementById('videoCard');
  var lastFetchedUrl = '';
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
          fetchErr.innerHTML = '<div class="fetch-error">' + escHtml(String(d.message || 'Unknown error')) + '</div>';
          switchToTab('response');
          return;
        }

        // ── Build video card ──
        var data = d.data;
        var html = '';

        // BUG FIX: lastFetchedUrl must be set BEFORE building the button HTML
        // so the onclick captures the current URL, not the previous one.
        // Previously it was set after html was built — first fetch always got
        // an empty string, subsequent fetches got the *previous* video's URL.
        lastFetchedUrl = url;

        if(data.thumbnail)
          html += '<img class="video-thumb" src="' + cleanUrl(data.thumbnail) + '" onerror="this.style.display=\\'none\\'">';

        if(data.title)
          html += '<div class="video-title">' + escHtml(data.title) + '</div>';

        var meta = '';
        if(data.author)   meta += '<span class="meta-pill">' + escHtml(data.author) + '</span>';
        if(data.duration) meta += '<span class="meta-pill">⏱ ' + escHtml(data.duration) + '</span>';
        if(data.size)     meta += '<span class="meta-pill">' + escHtml(data.size) + '</span>';
        if(meta) html += '<div class="video-meta">' + meta + '</div>';

        var quals = data.available_qualities || [];
        if(quals.length){
          html += '<div class="quality-row">';
          quals.forEach(function(q){
            if(q.url) html += '<a class="quality-btn" href="' + cleanUrl(q.url) + '" target="_blank">' + escHtml(q.label||'') + '</a>';
          });
          html += '</div>';
        }

        if(data.m3u8_link){
          html += '<a class="m3u8-btn" href="' + cleanUrl(data.m3u8_link) + '" target="_blank">▶ Stream (Auto Best)</a>';
          // FIX: onclick="startDownload(\'URL\')" kaam nahi karta — backslash
          // string ke bahar valid JS nahi hota → SyntaxError → poora card crash.
          // data-url attribute use karo, event listener baad mein lagao.
          html += '<button class="dl-btn">⬇ Download</button>';
        }
        videoCard.innerHTML = html;
        videoCard.className = 'video-card show';

        // Wire download button via event listener (not inline onclick)
        // lastFetchedUrl closure mein hai — no escaping needed at all.
        var dlBtn = videoCard.querySelector('.dl-btn');
        if(dlBtn) dlBtn.addEventListener('click', function(){
          startDownload(lastFetchedUrl);
        });

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

  // ── Download handler ─────────────────────────────────────────────
  // FIX: was fetch()-ing the whole response and buffering it as a Blob
  // in JS memory before triggering the save — for a multi-GB video
  // (these commonly run several GB) that either exhausts the mobile
  // browser's memory outright or just sits there with no visible
  // progress until the entire blob is built. A plain navigation to the
  // download URL lets the browser's own native download manager stream
  // it straight to disk with real progress, exactly like clicking a
  // normal download link — which is also what actually works with the
  // backend's new streamed (unknown-length, chunked) response; a
  // Content-Length-less body doesn't play well with manual blob-based
  // progress tracking anyway.
  window.startDownload = function(videoUrl){
    var dlBtn = videoCard.querySelector('.dl-btn');
    if(dlBtn){
      dlBtn.disabled = true; dlBtn.textContent = 'Starting…';
      setTimeout(function(){ dlBtn.disabled = false; dlBtn.textContent = 'Download'; }, 3000);
    }
    var a = document.createElement('a');
    a.href = '/api/download?url=' + encodeURIComponent(videoUrl);
    // target="_blank" matters for the FAILURE case specifically: a
    // successful download (Content-Disposition: attachment) triggers a
    // save regardless of target, but an error response is plain JSON
    // with no attachment header — without _blank that would navigate
    // this whole docs page away to show the raw JSON instead of it.
    a.target = '_blank';
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };
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
          '<div class="lib-name">' + escHtml(item.title || item.url) + '</div>' +
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
    # The 3 example snippets above (Python/Telegram-bot/cURL tabs) had this
    # bot's onrender.com URL hardcoded — broke the moment anyone ran the
    # same code on a VPS with its own domain, or even just Render's own
    # auto-generated URL for a fresh deploy.
    #
    # request.url_root alone isn't reliable on Render: it depends on
    # X-Forwarded-Proto being forwarded correctly through Render's edge to
    # waitress and picked up by ProxyFix, and in practice that came out as
    # "http://" instead of "https://" — which Render then redirects
    # (301 Moved Permanently), so the copy-pasted curl example didn't work
    # as given. Render sets RENDER_EXTERNAL_URL itself, though — the
    # service's real public https URL, no header-forwarding involved —
    # so that's checked first and is what actually runs on Render;
    # request.url_root stays as the fallback for a VPS (or anywhere else)
    # where that env var won't be set.
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/") or request.url_root.rstrip("/")
    html = html.replace("__BASE_URL__", base_url)
    return html


@app.route("/health")
def health():
    return jsonify({"status": True, "creator": CREATOR, "message": "OK"})


@app.route("/api/download")
def api_download():
    """
    Stream a real, single .mp4 file to the browser as a download —
    ffmpeg remuxes the CDN's HLS stream on the server and pipes its
    output straight into the HTTP response as it's produced, so the
    browser starts receiving (and showing download progress for) the
    file within a second or two instead of waiting for the whole thing
    to download+remux on the server first.

    Query params:
      url      — FapHouse video page URL
      quality  — optional, e.g. "1080p" / "480p" (must match a label from
                 /api/resolve's available_qualities). Defaults to "480p"
                 if omitted; pass "auto" explicitly for best-available
                 instead.

    NOTE: this does route video bytes through the server (ffmpeg pulls
    from the CDN, server re-streams to the browser) — that's the
    unavoidable cost of turning an HLS playlist into one real .mp4 file
    a browser can save with a plain click. The CDN-direct redirect
    (what "Stream" uses / what this endpoint did briefly) skips that
    cost but only ever opens/streams the .m3u8 playlist, it can't
    trigger an actual file save.
    """
    import subprocess, shutil
    from flask import Response, stream_with_context

    if not shutil.which("ffmpeg"):
        return jsonify({"error": "ffmpeg not found on server — download unavailable"}), 503

    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "url parameter required"}), 400
    if not faphouse.is_faphouse_link(url):
        return jsonify({"error": "Not a valid faphouse.com or faphouse2.com link."}), 400

    # Default quality is now 480p (not "auto/best") — per your last request.
    # Pass ?quality=1080p / ?quality=720p / etc. to override, or
    # ?quality=auto to explicitly get the old best-available behavior.
    quality = request.args.get("quality", "").strip() or "480p"

    qualities = []
    try:
        qualities = faphouse.get_available_qualities(url) or []
    except Exception as e:
        logger.warning(f"api_download qualities fetch failed: {e}")

    try:
        stream_url = None
        matched_q = None
        if quality.lower() != "auto":
            matched_q = next((q for q in qualities if q["label"].lower() == quality.lower()), None)
            if not matched_q and qualities:
                # This exact video doesn't have a 480p rendition (some don't).
                # Falling straight through to "auto" could silently jump to
                # 1080p or 240p depending on what's available — instead pick
                # whichever available resolution is numerically closest to
                # what was requested, so the result stays as close to 480p
                # as that video actually offers.
                numeric_q = [q for q in qualities if isinstance(q.get("height"), int)]
                if numeric_q:
                    target_h = int(re.sub(r"[^0-9]", "", quality) or 0)
                    matched_q = (
                        min(numeric_q, key=lambda q: abs(q["height"] - target_h))
                        if target_h else numeric_q[0]
                    )
            if matched_q and matched_q.get("url"):
                stream_url = matched_q["url"]
        if not stream_url:
            stream_url = faphouse.client.get_m3u8_url(url)
        if not stream_url:
            return jsonify({"error": "Could not resolve m3u8 URL"}), 502
    except Exception as e:
        logger.exception(f"api_download resolve error: {e}")
        return jsonify({"error": str(e)}), 500

    # Get a clean filename from page meta (best-effort — a title lookup
    # failure here shouldn't block the download itself).
    meta = {}
    try:
        meta = faphouse.get_page_meta(url) or {}
    except Exception:
        pass
    title = (meta.get("title") or "video").replace("/", "-").replace("\\", "-")
    safe_name = re.sub(r'[^\w\s\.\-]', '', title)[:80].strip() or "video"
    out_filename = safe_name + ".mp4"

    # ── Estimated Content-Length (user-requested) ────────────────────
    # Browsers only show a "X MB / TOTAL MB" progress readout when the
    # response declares Content-Length up front — a live-piped fragmented
    # mp4 has no true length until ffmpeg finishes, so this is a bitrate×
    # duration ESTIMATE, not the real byte count. If ffmpeg's actual
    # output ends up smaller/larger than this estimate, some browsers
    # flag the finished file as incomplete/corrupt — that's the trade-off
    # that was explicitly accepted for getting a total shown at all.
    # Bounded to a short probe (3s) so it can't meaningfully delay the
    # download from starting; on any failure this just silently falls
    # back to no Content-Length (today's chunked-transfer behavior).
    est_size_bytes = None
    try:
        duration_result = {"secs": None}
        def _probe_duration_for_download():
            try:
                duration_result["secs"] = faphouse.get_video_duration(stream_url)
            except Exception:
                pass
        t = threading.Thread(target=_probe_duration_for_download, daemon=True)
        t.start()
        t.join(timeout=3)
        duration_secs = duration_result["secs"]
        if duration_secs and duration_secs > 0:
            selected_q = matched_q or (qualities[0] if qualities else None)
            est_size_bytes, _ = _size_from_bandwidth([selected_q] if selected_q else [], int(duration_secs))
    except Exception as e:
        logger.debug(f"api_download size estimate skipped: {e}")

    referer = "https://faphouse2.com/" if "faphouse2" in url else "https://faphouse.com/"
    cmd = [
        "ffmpeg", "-y",
        # FIX: this command had no timeout/reconnect flags at all — if the
        # CDN connection stalled mid-stream (seen for real against
        # es.faphouse.com elsewhere in this same app), ffmpeg just hung
        # forever with zero output, and the browser showed a download
        # frozen at a fixed byte count indefinitely (confirmed via a
        # screen recording: stuck at the same MB figure for 35s+ straight,
        # 0 KB/s). download_video() already had these same flags for the
        # exact same reason — this endpoint was just missing them.
        "-rw_timeout", "20000000",  # microseconds = 20s
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-headers", f"Referer: {referer}\r\nUser-Agent: Mozilla/5.0\r\n",
        "-i", stream_url,
        "-c", "copy",
        # Piping ffmpeg's stdout straight into the HTTP response (via
        # "-f mp4 pipe:1" + frag_keyframe/empty_moov, the streamable-mp4
        # equivalent of faststart that doesn't require seeking back) means
        # the browser gets its first bytes in a second or two and shows
        # real download progress throughout, and nothing is ever written
        # to disk on the server at all — no temp-file/disk-space issue on
        # Render's small ephemeral disk, and no risk of the edge proxy's
        # request timeout killing the connection while waiting for a
        # multi-GB file to finish before the response even starts.
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as e:
        logger.exception(f"api_download ffmpeg spawn error: {e}")
        return jsonify({"error": f"Couldn't start ffmpeg: {e}"}), 500

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(1024 * 256)
                if not chunk:
                    break
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait(timeout=10)
            if proc.returncode not in (0, None):
                stderr_tail = (proc.stderr.read() or b"").decode(errors="replace")[-800:]
                logger.warning(f"api_download: ffmpeg exited {proc.returncode} for {url}: {stderr_tail}")

    resp = Response(stream_with_context(generate()), mimetype="video/mp4")
    resp.headers["Content-Disposition"] = f'attachment; filename="{out_filename}"'
    if est_size_bytes:
        # Estimate only (see comment above) — not the real, final byte
        # count. Deliberately not setting this by default; only present
        # when the estimate was actually computable.
        resp.headers["Content-Length"] = str(est_size_bytes)
    return resp


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
                filename_url = candidate if candidate.startswith("http") else f"{faphouse.get_base_url(url) or faphouse.DEFAULT_BASE_URL}{candidate}"
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
        # Skip "Auto (Best)" — m3u8_link field already covers this,
        # no need to duplicate it inside available_qualities.
        if label == "Auto (Best)":
            if m3u8_link and not m3u8_link:  # never true — just skip
                pass
            continue
        if url_q:
            quality_list.append({"label": label, "url": url_q})
    # If no explicit quality entries AND we have an m3u8, parse resolutions
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
    # 1. __NEXT_DATA__ title — always English (extracted above in _scrape_full_meta)
    # 2. JSON-LD / og:title — usually English
    # 3. URL slug — always English, good fallback
    # 4. Translation — only when title is clearly non-English (non-ASCII heavy)
    slug_title = _slug_from_url(filename_url)

    if meta["title"]:
        if _is_mostly_english(meta["title"]):
            # Already English — no translation needed (most common case when
            # __NEXT_DATA__ parsing now works correctly)
            english_title = meta["title"]
        else:
            # Non-English title (Hindi, Japanese, Spanish etc.) — translate
            translated = _translate_to_english(meta["title"])
            if translated and translated.strip():
                english_title = translated
                logger.info(f"[translate] '{meta['title'][:50]}' → '{translated[:50]}'")
            elif slug_title:
                # Translation failed — slug is always English
                english_title = slug_title
                logger.info(f"[translate] failed, using slug: '{slug_title[:50]}'")
            else:
                english_title = meta["title"]
    else:
        # No title from HTML at all — use slug
        english_title = slug_title or "Unknown"

    # ── Filename ──────────────────────────────────────────────────────
    filename = _make_filename(english_title, filename_url)

    # ── HTML entity decode ─────────────────────────────────────────────
    english_title       = _decode_html(english_title)
    filename            = _make_filename(english_title, filename_url)
    meta["description"] = _decode_html(meta.get("description"))
    meta["author"]      = _decode_html(meta.get("author"))

    # ── URL fixer ────────────────────────────────────────────────────
    def _fix_url(u):
        if not u:
            return None
        return u.replace(",", "%2C").replace("+", "%2B")

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
    #   Best-quality link only (full list lives in available_qualities,
    #   not duplicated here)

    flines = []
    # title, author, duration, size already exist as separate data fields —
    # not repeated here to avoid duplication in the response.
    if meta.get("description"):
        flines.append(meta["description"])
    if quality_list:
        # Every quality link is already in data["available_qualities"]
        # separately — repeating the full list here too just duplicated
        # it. This is meant as a human-readable caption (it's literally
        # sent as-is via `caption=` in the Telegram-bot example above), so
        # one link — the best available quality, quality_list is already
        # ordered highest→lowest — is what's actually useful here; anyone
        # wanting the rest can read them off available_qualities.
        pass  # quality links are in available_qualities — not repeated in formatted_text

    description_text = "\n".join(flines)

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
    data["description"]        = description_text
    data["available_qualities"] = [
        {"label": q.get("label", "?"), "url": _fix_url(q.get("url"))}
        for q in (quality_list or [])
        if q.get("url")
    ]

    return jsonify({
        "status":  True,
        "creator": CREATOR,
        "data":    data,
    })


@app.route("/debug-html")
def debug_html():
    """Serve the last saved M3U8 fail HTML for inspection.
    Only accessible — remove after debugging."""
    import glob
    paths = [
        "downloads/debug_last_m3u8_fail.html",
        "/tmp/debug_last_m3u8_fail.html",
    ]
    # Also check any debug file in downloads/
    paths += glob.glob("downloads/debug_*.html")
    for path in paths:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            # Return first 60KB so browser doesn't hang
            snippet = content[:60000]
            size = len(content)
            return f"""<html><body>
<h3>Debug HTML ({size:,} chars) — {path}</h3>
<p>Showing first 60,000 chars</p>
<hr>
<h4>All script tags:</h4>
<pre style="font-size:11px;white-space:pre-wrap">""" + \
                "\n\n---SCRIPT---\n".join(
                    __import__('re').findall(r'<script[^>]*>.*?</script>', snippet, __import__('re').DOTALL | __import__('re').IGNORECASE)[:20]
                ) + \
                f"""</pre>
<hr>
<h4>Raw HTML (first 60KB):</h4>
<pre style="font-size:10px;white-space:pre-wrap">{__import__('html').escape(snippet)}</pre>
</body></html>"""
    return "No debug HTML file found yet — trigger a failing request first.", 404


@app.errorhandler(Exception)
def handle_exception(e):
    # HTTPException covers Werkzeug's own routing/HTTP-level responses —
    # 404 Not Found (bad URL, favicon.ico probes, bots), 405 Method Not
    # Allowed, etc. These aren't bugs, so let Werkzeug return its real
    # status code and body as normal instead of stamping every one of
    # them as a scary "Unhandled exception" 500 with a full traceback in
    # the logs — that was flooding the log with noise for routine 404s
    # and, worse, actually changing their response status to 500 instead
    # of the correct 404/405/etc.
    if isinstance(e, HTTPException):
        return e
    logger.error(f"Unhandled exception: {e}", exc_info=True)
    return jsonify({
        "status":  False,
        "creator": CREATOR,
        "message": f"Internal server error: {type(e).__name__}: {e}",
    }), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
