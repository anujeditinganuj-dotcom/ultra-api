# 🎬 FapHouse API

Standalone REST API for **faphouse.com** / **faphouse2.com** — video URL se full metadata, HLS stream links aur quality-wise direct URLs return karta hai.

🔗 **Live API:** `https://ultra-api-008f.onrender.com`

---

## 📌 Endpoints

### `GET /api/faphouse`

FapHouse video ka full metadata resolve karta hai.

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `url` | string | ✅ Yes | `faphouse.com` ya `faphouse2.com` ka video link |

**Example Request:**

```bash
curl "https://ultra-api-008f.onrender.com/api/faphouse?url=https://faphouse.com/videos/QJKmfH"
```

**Success Response:**

```json
{
  "status": true,
  "creator": "FapHouse API",
  "data": {
    "Filename":        "🗂 Newly Married Naughty Bhabhi Fucked by Devar.mp4",
    "title":           "🎬 Newly Married Naughty Bhabhi Fucked by Devar",
    "author":          "👤 Niks Indian",
    "duration":        "⏱️ 1:11:00",
    "duration_seconds":"⏳ 4260",
    "size":            "📦 7.44 GB",
    "size_bytes":      "💾 7987500000",
    "description":     "📝 Bhabhi went into her Devar's room...",

    "thumbnail":       "🖼️ https://ic-nss.flixcdn.com/.../poster.jpg",
    "thumbnail_url":   "https://ic-nss.flixcdn.com/.../poster.jpg",

    "speed_link":      "⚡ https://ip.ahcdn.com/.../2160p/index.m3u8",
    "speed_link_url":  "https://ip.ahcdn.com/.../2160p/index.m3u8",

    "m3u8_link":       "📺 https://video-pr.xhcdn.com/.../master.m3u8",
    "m3u8_link_url":   "https://video-pr.xhcdn.com/.../master.m3u8",

    "available_qualities": [
      { "label": "🔵 4K",   "url": "🔗 https://...", "url_clean": "https://..." },
      { "label": "🟢 1080p","url": "🔗 https://...", "url_clean": "https://..." },
      { "label": "🟡 720p", "url": "🔗 https://...", "url_clean": "https://..." },
      { "label": "🟠 480p", "url": "🔗 https://...", "url_clean": "https://..." },
      { "label": "⚪ 240p", "url": "🔗 https://...", "url_clean": "https://..." }
    ],

    "formatted_text": "📋 Ready-to-send Telegram message (Markdown)"
  }
}
```

> **Note:** Har URL field ke **do versions** hain:
> - **Emoji wala** (`thumbnail`, `m3u8_link`, `speed_link`, `url`) — display ke liye
> - **Clean wala** (`thumbnail_url`, `m3u8_link_url`, `speed_link_url`, `url_clean`) — directly click/open ke liye ✅

**Error Response:**

```json
{
  "status": false,
  "creator": "FapHouse API",
  "message": "Not a valid faphouse.com or faphouse2.com link."
}
```

---

### `GET /health`

Server health check — uptime monitoring ke liye.

```bash
curl "https://ultra-api-008f.onrender.com/health"
```

```json
{ "status": true, "creator": "FapHouse API" }
```

---

## 📤 Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `title` | string | `🎬` + video title |
| `author` | string | `👤` + model/studio name |
| `duration` | string | `⏱️` + human readable (e.g. `1:07:39`) |
| `duration_seconds` | string | `⏳` + duration in seconds |
| `size` | string | `📦` + estimated file size |
| `size_bytes` | string | `💾` + size in bytes |
| `thumbnail` | string | `🖼️` + thumbnail URL (emoji prefix) |
| `thumbnail_url` | string | Clean thumbnail URL — **directly clickable** ✅ |
| `description` | string | `📝` + video description |
| `speed_link` | string | `⚡` + best quality stream (emoji prefix) |
| `speed_link_url` | string | Clean speed link — **directly clickable** ✅ |
| `m3u8_link` | string | `📺` + master HLS playlist (emoji prefix) |
| `m3u8_link_url` | string | Clean m3u8 link — **directly clickable** ✅ |
| `available_qualities` | array | `[{label, url, url_clean}]` — har quality |
| `url_clean` | string | Clean quality URL — **directly clickable** ✅ |
| `Filename` | string | Suggested `.mp4` filename |
| `formatted_text` | string | Ready-to-send Telegram message (Markdown) |

---

## 💡 Usage Examples

### Python

```python
import requests

r = requests.get(
    "https://ultra-api-008f.onrender.com/api/faphouse",
    params={"url": "https://faphouse.com/videos/QJKmfH"}
)
data = r.json()

if data["status"]:
    d = data["data"]
    print(d["title"])
    # Clean URL ke liye _url suffix wala use karo
    print(d["thumbnail_url"])     # clickable
    print(d["speed_link_url"])    # clickable
    # Quality URL
    for q in d["available_qualities"]:
        print(q["label"], q["url_clean"])  # clickable
else:
    print("Error:", data["message"])
```

### Telegram Bot (Pyrogram)

```python
import requests
from pyrogram import Client, filters

@app.on_message(filters.regex(r"faphouse\.com/videos/"))
async def faphouse_handler(client, message):
    url = message.text.strip()
    r = requests.get(
        "https://ultra-api-008f.onrender.com/api/faphouse",
        params={"url": url}
    )
    d = r.json()
    if d["status"]:
        await message.reply_photo(
            photo=d["data"]["thumbnail_url"],   # clean URL use karo
            caption=d["data"]["formatted_text"],
            parse_mode="Markdown"
        )
    else:
        await message.reply(f"❌ {d['message']}")
```

### JavaScript (fetch)

```javascript
const res = await fetch(
  "https://ultra-api-008f.onrender.com/api/faphouse?url=https://faphouse.com/videos/QJKmfH"
);
const data = await res.json();
if (data.status) {
  console.log(data.data.title);
  console.log(data.data.speed_link_url);   // clean URL
  console.log(data.data.thumbnail_url);    // clean URL
}
```

### Download with ffmpeg

```bash
# m3u8_link_url se direct MP4 download
ffmpeg -headers "Referer: https://faphouse.com/" \
       -i "<m3u8_link_url>" \
       -c copy output.mp4
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EMAIL` | `rockstarga69@gmail.com` | FapHouse account email |
| `PASSWORD` | `Jaiisbeast@0` | FapHouse account password |
| `PORT` | `10000` | Server listen port (Render auto-set karta hai) |

> **Note:** Render pe `EMAIL` aur `PASSWORD` env vars add karne ki zarurat nahi — already set hain.

---

## 🚀 Self-Deploy

### Render (Free)

1. Repo fork karo
2. Render pe **New Web Service** banao
3. Deploy ✅ — koi env var set karne ki zarurat nahi

### Docker

```bash
docker build -t faphouse-api .
docker run -p 10000:10000 faphouse-api
```

### Local

```bash
pip install -r requirements.txt
python app.py
```

---

## ⚠️ Error Codes

| Code | Reason |
|------|--------|
| `400` | `url` parameter missing ya invalid link |
| `403` | Fanclub-locked video |
| `502` | Stream resolve nahi hua (expired/private video) |

---

## 📝 Notes

- **No API key needed** — seedha use karo
- **Dual URL fields** — emoji wala display ke liye, `_url` suffix wala click ke liye
- **Render free tier** — pehli request pe 50s cold start, [UptimeRobot](https://uptimerobot.com) se `/health` ping karo
- Supported: `faphouse.com` aur `faphouse2.com` dono
