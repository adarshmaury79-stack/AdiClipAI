# AdiClipAI

AdiClipAI is a mobile- and tablet-friendly web app. It keeps the existing FastAPI + yt-dlp + faster-whisper workflow and serves the website from the same app.

## Included features

- YouTube URL input and optional start/end times
- Speech transcription with faster-whisper
- Suggested non-overlapping clips ranked by the existing scoring logic
- Clip rendering with captions and 9:16 export from the current interface
- Video/audio downloads and copyable title/description/hashtags
- Responsive landing page and clip workspace

## Run locally

Requirements: Python 3.11+, FFmpeg, Node.js (for yt-dlp's JavaScript challenge support).

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:8000`.

## Free Render deployment

1. Push this folder to a GitHub repository.
2. In Render, choose **New > Blueprint** and connect that repository.
3. Render reads `render.yaml`; the web service name is `adiclipai`.
4. If that service name is available, its URL will be `https://adiclipai.onrender.com`. Otherwise Render may require a different unique service name.
5. Wait for the Docker build and then open `/health`; it should return `{"status":"ok","app":"AdiClipAI"}`.

## Important free-hosting limitations

- Render's free service can sleep when idle and may take time to wake.
- Free instances have limited memory/CPU. Downloading a long video, loading Whisper, and rendering clips can exceed those limits or restart the service. This means a successful deployment does **not** guarantee every long-video job will complete on free hosting.
- Job state and downloaded/rendered files are stored locally and are not durable across service restarts/redeploys. The free deployment is suitable for testing, not guaranteed permanent production storage.
- YouTube may block automated downloads for some videos. If that happens, the existing `cookies.txt` option may be needed; never upload or commit personal cookies to a public repository.
- Optional `GEMINI_API_KEY` enables the existing optional Gemini metadata enhancement. The core fallback title/description/tags do not require it.

## Environment variables

- `PORT`: provided by the hosting platform
- `WHISPER_MODEL`: defaults to `base` (kept unchanged from the original project)
- `GEMINI_API_KEY`: optional
- `GEMINI_MODEL`: defaults to `gemini-2.0-flash`

## Safety and rights

Only process videos you own or have permission to use. Respect YouTube's terms and the rights of video creators.
