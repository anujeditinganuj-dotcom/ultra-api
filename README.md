# FapHouse API

Standalone resolver API for faphouse.com / faphouse2.com — given a video
link, returns its stream URL and basic metadata as JSON, in the same
shape as the reference "AK API" (Diskwala-style) response.

## Authentication

Access requires logging in with a real faphouse.com/faphouse2.com
account first — the same login flow the fbot Telegram bot itself uses
for account-gated videos, just with your own credentials instead of the
server's configured account. There's no more static API key.

```
POST /api/login
Content-Type: application/json

{"email": "you@example.com", "password": "yourpassword"}
```

```json
{
  "status": true,
  "creator": "FapHouse API",
  "data": {
    "token": "<random token>",
    "expires_in": 43200,
    "note": "Send this token as 'Authorization: Bearer <token>' (or ?token=<token>) on /api/faphouse."
  }
}
```

Tokens are kept in memory (not a database) and last `TOKEN_TTL_SECONDS`
(default 12 hours) — a restart, or the token simply expiring, just means
logging in again.

## Endpoint

```
GET /api/faphouse?url=<faphouse.com or faphouse2.com video link>
Authorization: Bearer <token from /api/login>
```

### Success response

```json
{
  "status": true,
  "creator": "FapHouse API",
  "data": {
    "Filename": "some-video-title.mp4",
    "size": null,
    "size_bytes": null,
    "thumbnail": "https://.../poster.jpg",
    "speed_link": null,
    "m3u8_link": "https://.../master.m3u8",
    "note": "faphouse serves HLS (m3u8) only — no direct progressive-download URL exists, and file size isn't known until the stream is actually downloaded/muxed (e.g. via ffmpeg)."
  }
}
```

**`size` / `size_bytes` / `speed_link` are honestly `null`, not guessed.**
Diskwala-style APIs can report these because they point at one direct
file with a known size. Faphouse serves HLS — a stream made of many
small segments — which has no fixed file size until it's actually
downloaded, and there's no equivalent of a single "fast direct link" to
hand back. Anything else would be a made-up number.

To actually get a downloadable .mp4 from `m3u8_link`, remux it with
ffmpeg:

```bash
ffmpeg -headers "Referer: https://faphouse2.com/\r\n" -i "<m3u8_link>" -c copy output.mp4
```

(The `Referer` header matters — faphouse's CDN checks it.)

### Error response

```json
{
  "status": false,
  "creator": "FapHouse API",
  "message": "Couldn't resolve a stream URL for that link — the page may have changed, or the video may be private/members-only."
}
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in values, all optional
python app.py          # dev server on :8080
```

Production (multiple workers, handles concurrent requests properly):

```bash
gunicorn --bind 0.0.0.0:8080 --workers 4 --timeout 60 app:app
```

Or just build/run the included `Dockerfile`.

## Environment variables (all optional)

| Variable | Purpose |
|---|---|
| `EMAIL` / `PASSWORD` | Faphouse account login, used server-side for account-gated videos when *resolving* a link. Same email/password logs in to both faphouse.com and faphouse2.com (separate sessions per domain, since cookies don't cross domains). Separate from the per-caller login in `/api/login` above — that one is what gates *access to the API itself*, this one is what the server uses while doing the actual resolving. |
| `BASE_URL` | Which site to log in to first. Default `https://faphouse2.com`. |
| `SESSION_MAX_AGE` | Force a fresh login after this many seconds. Default `1800`. |
| `TOKEN_TTL_SECONDS` | How long an `/api/login` token stays valid. Default `43200` (12 hours). |
| `PORT` | Listen port. Default `8080`. |

## Notes

- Guest (non-logged-in) resolution works for most non-gated videos with
  no configuration at all — but `/api/faphouse` still requires a valid
  login token regardless, since that's the API's own access gate now,
  separate from whether the *video itself* needs an account.
- This shares the exact same resolver (`faphouse_downloader.py`) used by
  the fbot Telegram bot project — same login/session handling, same
  m3u8-extraction logic (JSON-embedded and plain-HTML both), same
  brotli-decoding fix.
