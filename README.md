# AdiClipAI

Files: `index.html` (frontend), `server.py` + `clipper.py` (backend), `Dockerfile`, `requirements.txt`.

## Deploy on Render
1. Put all these files in the root of your GitHub repo (replace old ones).
2. Render -> New -> Web Service -> pick the repo -> Language: **Docker**.
3. Instance: Starter (512 MB+ RAM) is the minimum. Free works only for short videos.
4. (Optional) Environment: `WHISPER_MODEL=base` for better accuracy (needs ~1 GB RAM), `MAX_VIDEO_MIN=90`, `MAX_UPLOAD_MB=500`.
5. Deploy. Open your onrender.com URL.

## YouTube links blocked?
YouTube often blocks server IPs. Fix: Render -> Environment -> Secret Files -> add `cookies.txt`
(exported from a YouTube-logged-in browser). The server reads `/etc/secrets/cookies.txt` automatically.
If it still fails, use the **Upload a video** button, which never touches YouTube.
