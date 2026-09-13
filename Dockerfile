FROM python:3.12-slim

WORKDIR /app

# BUG FIX: /api/stream and /api/download both shell out to ffmpeg
# (_stream_response() in app.py) — python:3.12-slim doesn't include it,
# so every stream/download request was failing with "ffmpeg not
# installed on this server." (the /api/faphouse and /api/qualities
# endpoints don't need it, which is why only those two were broken).
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000

# Render sets $PORT dynamically — gunicorn reads it at runtime
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --workers 4 --timeout 600 --keep-alive 5 app:app"]
