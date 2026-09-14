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

  /* ── Page fade-in on load ── */
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

  <!-- Header -->
  <div class="header-row">
    <div class="reel">
      <svg viewBox="0 0 34 34" fill="none">
        <circle cx="17" cy="17" r="15.5" stroke="#f2a641" stroke-width="2"/>
        <circle cx="17" cy="17" r="4" fill="#f2a641"/>
        <circle cx="17" cy="6" r="2.4" fill="#262b38"/>
        <circle cx="27" cy="22" r="2.4" fill="#262b38"/>
        <circle cx="7" cy="22" r="2.4" fill="#262b38"/>
      </svg>
      <h1>FapHouse API</h1>
    </div>
    <div class="badges">
      <span class="badge badge-ver">v1.0</span>
      <span class="badge badge-status" id="status-badge">
        <span class="dot"></span><span id="status-text">checking…</span>
      </span>
    </div>
  </div>
  <p class="tagline">Resolves <strong style="color:var(--text)">faphouse.com</strong> and <strong style="color:var(--text)">faphouse2.com</strong> video links into title, duration, HLS master playlist, and one direct URL per quality — ready to forward straight into a Telegram reply.</p>

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
      <div class="spec-row"><div class="spec-key">data.formatted_text<span class="type">string</span></div><div class="spec-desc">Ready-to-send Telegram message (Markdown)</div></div>
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
        <span class="pl">parse_mode</span><span class="op">=</span><span class="st">"Markdown"</span>,
    )
<span class="kw">else</span>:
    <span class="kw">await</span> <span class="pl">message</span>.<span class="fn">reply</span>(<span class="st">f"❌ {d['message']}"</span>)</pre>
          </div>
        </div>

        <div class="tab-panel panel-curl">
          <div class="code-block">
            <button class="copy-btn" data-copy-pre="code-curl">
              <svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="4" y="4" width="8" height="8" rx="1.5"/><path d="M2 10V2h8"/></svg>Copy
            </button>
            <pre id="code-curl">curl "https://ultra-api-008f.onrender.com/api/faphouse\\
  ?url=https://faphouse.com/videos/QJKmfH"</pre>
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
// ── Live status check ──────────────────────────────────────────────────
(function(){
  var badge = document.getElementById('status-badge');
  var txt   = document.getElementById('status-text');
  var start = Date.now();
  fetch('/health')
    .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function(d){
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
document.querySelectorAll('.copy-btn').forEach(function(btn){
  btn.addEventListener('click', function(){
    var text = '';
    if(btn.dataset.copy){
      text = location.origin + btn.dataset.copy;
    } else if(btn.dataset.copyPre){
      var pre = document.getElementById(btn.dataset.copyPre);
      text = pre ? pre.innerText : '';
    }
    if(!text) return;
    navigator.clipboard.writeText(text).then(function(){
      btn.classList.add('copied');
      var orig = btn.innerHTML;
      btn.innerHTML = '<svg viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5"><polyline points="2,7 5,10 12,3"/></svg>Copied';
      setTimeout(function(){
        btn.innerHTML = orig;
        btn.classList.remove('copied');
      }, 1800);
    });
  });
});

// ── Tab switching (JS fallback for better UX) ──────────────────────────
document.querySelectorAll('.tab-labels label').forEach(function(label){
  label.addEventListener('click', function(){
    var forId = label.getAttribute('for');
    var radio = document.getElementById(forId);
    if(radio) radio.checked = true;
  });
});
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

    # ── Fetch page HTML once (title + all metadata) ───────────────────
    html, final_url = _fetch_page_html(url)
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
    # FapHouse stores the canonical URL which contains the English title slug.
    #
    # IMPORTANT: this can't just take the first regex match — the same
    # __NEXT_DATA__ blob commonly also embeds a "related videos" list
    # using these exact same field names (url/permalink/slug/
    # canonicalUrl), so a naive re.search() sometimes picked up a
    # DIFFERENT (related) video's slug instead of the one actually
    # requested. Faphouse video URLs end in a short id suffix after the
    # last hyphen (e.g. "...-QJKmfH") — every candidate match is checked
    # against that same id from the requested URL, and only a candidate
    # that actually ends in it gets used. If none do (id extraction
    # failed, or genuinely no match carries it), this falls back to the
    # original url/final_url untouched rather than guessing wrong.
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
                continue  # belongs to a different (likely related) video
            if len(candidate) > len(filename_url):  # longer = has full slug
                filename_url = candidate if candidate.startswith("http") else f"https://faphouse.com{candidate}"
            break

    # ── M3U8 master URL ───────────────────────────────────────────────
    m3u8_link = None
    try:
        m3u8_link = faphouse.client.get_m3u8_url(url)
    except faphouse.FanclubLockedError as e:
        return _error(f"Fanclub locked: {e}", 403)
    except Exception as e:
        logger.warning(f"get_m3u8_url failed: {e}")

    # ── Quality variants ───────────────────────────────────────────────
    qualities = []
    try:
        qualities = faphouse.get_available_qualities(url) or []
    except faphouse.FanclubLockedError as e:
        return _error(f"Fanclub locked: {e}", 403)
    except Exception as e:
        logger.warning(f"get_available_qualities failed: {e}")

    if not m3u8_link and not qualities:
        return _error("Could not resolve any stream URL — link may be expired or private.", 502)

    # ── Quality variants with direct URLs ────────────────────────────
    quality_list = []
    for q in qualities:
        if q.get("label") == "Auto (Best)":
            continue
        entry = {"label": q.get("label"), "url": q.get("url")}
        if entry["url"]:
            quality_list.append(entry)
    # Fallback: parse labels from m3u8 URL if no direct URLs
    if not quality_list and m3u8_link:
        # FIX: was "url": None here. When get_available_qualities() comes
        # back empty and this multi= parsing is the only source of
        # per-quality info, there IS no separate per-quality URL — every
        # resolution named in a multi= master URL is just an adaptive-
        # bitrate rendition served by that SAME master m3u8 (the player
        # switches between them itself), not a separate file. With
        # url=None, the Markdown-link loop above (`if q.get("label") and
        # q.get("url")`) always skipped every one of these entries — so
        # "Qualities:" printed with a label for each resolution and no
        # link ever attached to any of them. Pointing every label at
        # m3u8_link isn't a workaround: it's the correct, only-available
        # URL for that stream — clicking any of them opens the real
        # adaptive playlist, same as before, just now actually clickable.
        for label in _parse_multi_from_m3u8_url(m3u8_link):
            quality_list.append({"label": label, "url": m3u8_link})

    # ── Duration (ffprobe fallback if HTML parse didn't get it) ───────
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
            t.join(timeout=8)  # max 8s wait — don't block request forever
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

    # ── Formatted text response ───────────────────────────────────────
    def _fix_url(url: str | None) -> str | None:
        """Encode comma and plus in URLs so text parsers don't truncate them."""
        if not url:
            return url
        return url.replace(",", "%2C").replace("+", "%2B")

    lines = ["📹 *Video Information*"]
    if english_title:
        lines.append(f"🎬 *Title:* {english_title}")
    if meta["author"]:
        lines.append(f"👤 *Author:* {meta['author']}")
    if meta["duration"]:
        lines.append(f"⏱️ *Duration:* {meta['duration']}")
    if size_human:
        lines.append(f"💾 *Size:* {size_human}")
    if quality_list:
        lines.append("🎞️ *Qualities:*")
        for q in quality_list:
            if q.get("label") and q.get("url"):
                # FIX: was using the raw q['url'] here — every other place
                # in this function that emits a URL (thumbnail, speed_link,
                # m3u8_link, and the JSON quality_list below) already runs
                # it through _fix_url() first, but this Markdown-link loop
                # was skipped. The CDN links themselves contain literal
                # unescaped commas (`key=...,s=,end=...`), and Telegram's
                # Markdown link parser treats the first `)` OR a raw comma
                # inside certain clients' auto-link detection as the end of
                # the URL — so the link rendered but silently truncated at
                # the first comma, making it unclickable/broken instead of
                # opening the real (much longer) signed URL.
                lines.append(f"  ┣ [{q['label']}]({_fix_url(q['url'])})")
        # Fix last item arrow
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("  ┣"):
                lines[i] = lines[i].replace("  ┣", "  ┗", 1)
                break
    formatted_text = "\n".join(lines)

    # ── Build response with emoji prefixes ───────────────────────────────
    def _e(emoji, val):
        return f"{emoji} {val}" if val is not None else None

    def _quality_label(label: str) -> str:
        l = (label or "").lower()
        if "2160" in l or "4k" in l:  return "🔵 4K"
        if "1440" in l or "2k" in l:  return "🟣 2K"
        if "1080" in l:                return "🟢 1080p"
        if "720"  in l:                return "🟡 720p"
        if "480"  in l:                return "🟠 480p"
        if "360"  in l:                return "🔴 360p"
        if "240"  in l:                return "⚪ 240p"
        if "144"  in l:                return "⚫ 144p"
        return label or "?"

    data: dict = {
        "Filename":            _e("🗂",  filename),
        "title":               _e("🎬",  english_title),
        "author":              _e("👤",  meta["author"]),
        "duration":            _e("⏱️", meta["duration"]),
        "duration_seconds":    _e("⏳",  meta["duration_seconds"]),
        "size":                _e("📦",  size_human),
        "size_bytes":          _e("💾",  size_bytes),
        "🖼️ thumbnail":      _fix_url(meta.get("thumbnail")),
        "📺 m3u8_link":      _fix_url(m3u8_link),
        "available_qualities": [
            {
                "label": _quality_label(q.get("label", "")),
                "🔗 url":      _fix_url(q.get("url")),
            }
            for q in (quality_list or [])
            if q.get("url")
        ],
        "description":    _e("📝", meta["description"]),
        "formatted_text": _e("📋", formatted_text),
    }

    if meta["views"] is not None:
        data["views"]       = _e("👁",  meta["views"])
    if meta["likes"] is not None:
        data["likes"]       = _e("👍",  meta["likes"])
    if meta["upload_date"] is not None:
        data["upload_date"] = _e("📅",  meta["upload_date"])

    return jsonify({
        "status":  True,
        "creator": CREATOR,
        "data":    data,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
