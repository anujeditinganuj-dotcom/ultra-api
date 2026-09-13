FROM python:3.12-slim

WORKDIR /app

# ffmpeg needed for /api/stream and /api/download endpoints
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000
EXPOSE 10000

# waitress replaces gunicorn — compatible with Python 3.12+ and no C deps
CMD ["sh", "-c", "waitress-serve --port=$PORT --threads=4 --channel-timeout=600 app:app"]
