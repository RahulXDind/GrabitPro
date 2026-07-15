FROM python:3.11-slim

# ffmpeg is required by yt-dlp for many extractors / merging
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

# 2 workers, long timeout because extract_info can be slow
CMD gunicorn app:app --bind 0.0.0.0:${PORT} --workers 2 --timeout 120 --access-logfile -
