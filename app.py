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
    # Prefer URL slug (always English) over scraped title (may be localized)
    slug_name = _slug_from_url(url)
    if slug_name:
        name = slug_name
    elif title:
        name = _sanitize_filename(title)
    else:
        name = _sanitize_filename(urlparse(url).path.strip("/").split("/")[-1]) or "video"
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
            "User-Agent": _UA,
            "Referer": base_url,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        }, allow_redirects=True)
        final_url = r.url or video_url  # capture redirect destination
        if r.status_code != 200:
            return None, final_url
        return faphouse.client._decode_response(r), final_url
    except Exception:
        try:
            r = _req.get(video_url, timeout=12, headers={"User-Agent": _UA, "Referer": base_url}, allow_redirects=True)
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
    height_bps = {2160: 15_000_000, 1080: 8_000_000, 720: 5_000_000,
                  480: 2_500_000, 360: 1_500_000, 240: 1_000_000}

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


def _ytdlp_title(url: str, timeout: int = 10) -> str | None:
    """yt-dlp se FapHouse video ka English title nikalo.
    yt-dlp FapHouse extractor hamesha English title return karta hai
    (site ke original title se, localized scraping se nahi).
    Timeout ke baad None return karta hai — caller fallback use kare."""
    result = {"title": None}

    def _run():
        try:
            import yt_dlp
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extract_flat": False,
                "socket_timeout": timeout,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                result["title"] = (info or {}).get("title")
        except Exception as e:
            logger.debug(f"[ytdlp_title] failed: {e}")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=timeout + 2)
    return result["title"]
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

@app.route("/health")
def health():
    return jsonify({"status": True, "creator": CREATOR})


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
    # Use final_url (after redirect) for filename — short URLs like /videos/G76XIj
    # redirect to full English slug URLs like /videos/indian-guy-creampies-teen-G76XIj
    filename_url = final_url if final_url != url else url

    # ── M3U8 master URL ───────────────────────────────────────────────
    m3u8_link = None
    try:
        m3u8_link = faphouse.client.get_m3u8_url(url)
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

    # Speed link = best direct quality URL
    speed_link = None
    for q in qualities:
        if q.get("url"):
            speed_link = q["url"]
            break
    if not speed_link:
        speed_link = m3u8_link

    if not speed_link and not m3u8_link:
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
        for label in _parse_multi_from_m3u8_url(m3u8_link):
            quality_list.append({"label": label, "url": None})

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

    # ── Title + Description: yt-dlp se (always English) ─────────────
    # __NEXT_DATA__ already tried above — yt-dlp as final override/fallback.
    # yt-dlp FapHouse extractor returns English title AND description.
    ytdlp_info = _ytdlp_extract_info(url) if not locals().get("ytdlp_info") else ytdlp_info
    if ytdlp_info:
        if not meta["title"] or True:  # always prefer yt-dlp English title
            meta["title"] = ytdlp_info.get("title") or meta["title"]
        if not meta["description"]:
            meta["description"] = ytdlp_info.get("description")
    english_title = meta["title"] or _slug_from_url(filename_url)

    # ── Filename ──────────────────────────────────────────────────────
    filename = _make_filename(english_title, filename_url)

    # ── Formatted text response ───────────────────────────────────────
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
                lines.append(f"  ┣ [{q['label']}]({q['url']})")
        # Fix last item arrow
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("  ┣"):
                lines[i] = lines[i].replace("  ┣", "  ┗", 1)
                break
    formatted_text = "\n".join(lines)

    # ── Build response — omit null fields ─────────────────────────────
    data: dict = {
        "Filename":            filename,
        "title":               english_title,
        "author":              meta["author"],
        "available_qualities": quality_list,
        "description":         meta["description"],
        "duration":            meta["duration"],
        "duration_seconds":    meta["duration_seconds"],
        "m3u8_link":           m3u8_link,
        "size":                size_human,
        "size_bytes":          size_bytes,
        "speed_link":          speed_link,
        "thumbnail":           meta["thumbnail"],
        "formatted_text":      formatted_text,
    }
    # Optional fields — only include if not null
    if meta["views"] is not None:
        data["views"] = meta["views"]
    if meta["likes"] is not None:
        data["likes"] = meta["likes"]
    if meta["upload_date"] is not None:
        data["upload_date"] = meta["upload_date"]

    return jsonify({
        "status":  True,
        "creator": CREATOR,
        "data":    data,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True)
