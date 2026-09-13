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
    "Filename": "Newly Married Naughty Bhabhi Fucked by Devar.mp4",
    "title": "Newly Married Naughty Bhabhi Fucked by Devar",
    "author": "Niks Indian",
    "duration": "1:11:00",
    "duration_seconds": 4260,
    "size": "7.44 GB",
    "size_bytes": 7987634176,
    "thumbnail": "https://ic-nss.flixcdn.com/.../poster.jpg",
    "description": "Team India may have disappointed...",
    "speed_link": "https://ip.ahcdn.com/.../2160p/index.m3u8",
    "m3u8_link": "https://video-pr.xhcdn.com/.../master.m3u8",
    "available_qualities": [
      { "label": "2160p", "url": "https://..." },
      { "label": "1080p", "url": "https://..." },
      { "label": "720p",  "url": "https://..." },
      { "label": "480p",  "url": "https://..." },
      { "label": "240p",  "url": "https://..." }
    ],
    "formatted_text": "📹 *Video Information*\n🎬 *Title:* Newly Married...\n..."
  }
}
```

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
| `title` | string | English video title |
| `author` | string | Model / studio name |
| `duration` | string | Human readable (e.g. `1:07:39`) |
| `duration_seconds` | int | Duration in seconds |
| `size` | string | Estimated file size (e.g. `7.09 GB`) |
| `size_bytes` | int | Size in bytes |
| `thumbnail` | string | Thumbnail image URL |
| `description` | string | English video description |
| `speed_link` | string | Best quality direct stream URL |
| `m3u8_link` | string | Master HLS playlist URL |
| `available_qualities` | array | `[{label, url}]` — har quality ka direct link |
| `Filename` | string | Suggested `.mp4` filename |
| `formatted_text` | string | Ready-to-send Telegram message (Markdown) |
| `views` | int | View count *(only if available)* |
| `likes` | int | Like count *(only if available)* |
| `upload_date` | string | Upload date `YYYY-MM-DD` *(only if available)* |

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
    print(d["speed_link"])
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
            photo=d["data"]["thumbnail"],
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
  console.log(data.data.speed_link);
}
```

### Download with ffmpeg

```bash
# M3U8 se MP4 mein convert karo
ffmpeg -headers "Referer: https://faphouse2.com/" \
       -i "<m3u8_link>" \
       -c copy output.mp4
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EMAIL` | — | FapHouse account email (premium videos ke liye) |
| `PASSWORD` | — | FapHouse account password |
| `BASE_URL` | `https://faphouse2.com` | Primary domain |
| `SESSION_MAX_AGE` | `1800` | Session refresh interval (seconds) |
| `PORT` | `8080` | Server listen port |

---

## 🚀 Self-Deploy

### Render (Free)

1. Repo fork karo
2. Render pe **New Web Service** banao
3. Environment variables set karo (`EMAIL`, `PASSWORD`)
4. Deploy ✅

### Docker

```bash
docker build -t faphouse-api .
docker run -p 8080:8080 \
  -e EMAIL=your@email.com \
  -e PASSWORD=yourpassword \
  faphouse-api
```

### Local

```bash
pip install -r requirements.txt
EMAIL=your@email.com PASSWORD=yourpass python app.py
```

---

## ⚠️ Error Codes

| Code | Reason |
|------|--------|
| `400` | `url` parameter missing ya invalid link |
| `403` | Fanclub-locked video (alag subscription chahiye) |
| `502` | Stream resolve nahi hua (expired/private video) |

---

## 📝 Notes

- **No API key needed** — seedha use karo
- **Title always English** — `__NEXT_DATA__` + yt-dlp se extract hota hai
- **Render free tier** — pehli request pe 50s cold start ho sakta hai, [UptimeRobot](https://uptimerobot.com) se `/health` ping karo
- Supported: `faphouse.com` aur `faphouse2.com` dono
