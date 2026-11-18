FROM python:3.11-slim

# ffmpeg (video cut/render) + ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# deno (yt-dlp needs it for YouTube)
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# app.py serves static/index.html, so this works even if index.html is in the repo root
RUN mkdir -p static && if [ -f index.html ]; then cp index.html static/index.html; fi

ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
