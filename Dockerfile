FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

# Shell form so ${PORT} is expanded by /bin/sh at runtime
CMD gunicorn app:app --bind "0.0.0.0:${PORT}" --workers 2 --timeout 120 --access-logfile -
