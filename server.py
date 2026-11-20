"""AdiClipAI backend (FastAPI).
Endpoints used by index.html:
  GET  /api/info?url=            -> {title, duration}
  POST /api/analyze  (json)      -> {id}      link flow
  POST /api/upload   (multipart) -> {id}      upload flow
  GET  /api/job/{id}             -> job status + clips
  GET  /media/{id}               -> source video (range requests)
  GET  /api/clip/{id}/{i}?q=&captions=&ratio=[&prepare=1]
  GET  /api/audio/{id}/{i}
"""
import glob
import json
import os
import re
import shutil
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

import clipper

BASE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("WORK_DIR", "/tmp/adiclip")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
MAX_VIDEO_MIN = int(os.environ.get("MAX_VIDEO_MIN", "90"))
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "tiny")
COOKIES = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
KEEP_SECONDS = 2 * 3600

os.makedirs(WORK, exist_ok=True)
app = FastAPI()
JOBS = {}
RENDERS = {}
JOB_SLOT = threading.Semaphore(int(os.environ.get("MAX_JOBS", "1")))
RENDER_SLOT = threading.Semaphore(2)
_model = None
_model_lock = threading.Lock()


# ---------------- helpers ----------------
def clean_err(e):
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).strip()
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return ("YouTube blocked this server. Add cookies.txt, or upload the video file instead.")
    if "Private video" in msg or "Video unavailable" in msg:
        return "This video is private or unavailable."
    return msg[:400] or "Something went wrong."


def cleanup():
    now = time.time()
    for d in glob.glob(WORK + "/*"):
        try:
            if now - os.path.getmtime(d) > KEEP_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
                JOBS.pop(os.path.basename(d), None)
        except OSError:
            pass


def new_job():
    cleanup()
    jid = uuid.uuid4().hex[:12]
    d = f"{WORK}/{jid}"
    os.makedirs(d, exist_ok=True)
    JOBS[jid] = {"id": jid, "dir": d, "status": "running", "pct": 0, "stage": "Starting",
                 "title": "Processing video", "clips": [], "w": 16, "h": 9, "error": None}
    return JOBS[jid]


def upd(job, pct=None, stage=None, title=None):
    if pct is not None:
        job["pct"] = int(pct)
    if stage:
        job["stage"] = stage
    if title:
        job["title"] = title


def ydl_opts(extra=None):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "retries": 3,
         "js_runtimes": {"node": {}}}
    if os.path.exists(COOKIES):
        tmp = "/tmp/cookies_copy.txt"
        shutil.copyfile(COOKIES, tmp)  # yt-dlp wants a writable file
        o["cookiefile"] = tmp
    if extra:
        o.update(extra)
    return o


def get_model():
    global _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=os.cpu_count() or 2)
        return _model


def prepare_source(raw, out):
    """Make a browser friendly mp4 (h264/aac, max 720p). Copies if already fine."""
    w, h, dur, codec = clipper.probe(raw)
    ext = os.path.splitext(raw)[1].lower()
    if codec == "h264" and h <= 1080 and ext in (".mp4", ".mov", ".m4v"):
        clipper.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-c", "copy",
                     "-movflags", "+faststart", out])
    else:
        clipper.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw,
                     "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "ultrafast",
                     "-crf", "27", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                     "-movflags", "+faststart", out])


def analyze(job, src, req):
    """Transcribe -> pick clips. src is the prepared mp4."""
    w, h, dur, _ = clipper.probe(src)
    job["w"], job["h"] = w, h
    start = float(req.get("start") or 0)
    end = float(req.get("end") or 0) or dur
    end = min(end, dur)
    if start >= end:
        raise RuntimeError("Start time is after the end of the video.")
    upd(job, 40, "Loading speech model")
    t_load = time.time()
    get_model()
    print(f"[job {job['id']}] model load: {time.time() - t_load:.1f}s", flush=True)
    upd(job, 42, "Transcribing speech")
    wav = job["dir"] + "/audio.wav"
    clipper.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(start), "-t", str(end - start),
                 "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
    lang = req.get("lang") or "auto"
    segs_iter, _ = get_model().transcribe(
        wav, language=None if lang == "auto" else lang, vad_filter=True,
        word_timestamps=True, beam_size=1)
    segs = []
    for s in segs_iter:
        words = [{"w": x.word.strip(), "start": x.start + start, "end": x.end + start}
                 for x in (s.words or []) if x.word.strip()]
        segs.append({"start": s.start + start, "end": s.end + start, "text": s.text.strip(), "words": words})
        upd(job, 40 + 42 * min(1, (s.end) / max(end - start, 1)), "Transcribing speech")
    if not segs:
        raise RuntimeError("No speech found in this video.")
    upd(job, 85, "Finding viral moments")
    clips = clipper.select_clips(segs, float(req.get("minlen") or 30), float(req.get("maxlen") or 60),
                                 int(req.get("count") or 4), req.get("keywords") or "")
    if not clips:
        raise RuntimeError("Could not find good clips. Try a longer clip length or a longer video.")
    job["clips"] = clips
    try:
        os.remove(wav)
    except OSError:
        pass


def run_link_job(job, req):
    import yt_dlp
    JOB_SLOT_ACQUIRED = False
    try:
        upd(job, 1, "Waiting in queue")
        JOB_SLOT.acquire()
        JOB_SLOT_ACQUIRED = True
        d = job["dir"]
        upd(job, 3, "Downloading video")

        def hook(h):
            if h.get("status") == "downloading":
                tot = h.get("total_bytes") or h.get("total_bytes_estimate") or 0
                if tot:
                    upd(job, 3 + 30 * h.get("downloaded_bytes", 0) / tot)

        with yt_dlp.YoutubeDL(ydl_opts()) as y:
            info = y.extract_info(req["url"], download=False)
            if (info.get("duration") or 0) > MAX_VIDEO_MIN * 60:
                raise RuntimeError(f"Video is longer than {MAX_VIDEO_MIN} minutes.")
            upd(job, title=info.get("title") or "Processing video")
        fmt = ("bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/b[height<=720][ext=mp4]/"
               "bv*[height<=720]+ba/b[height<=720]/b")
        with yt_dlp.YoutubeDL(ydl_opts({"format": fmt, "merge_output_format": "mp4",
                                         "outtmpl": d + "/dl.%(ext)s", "progress_hooks": [hook]})) as y:
            y.download([req["url"]])
        raws = [p for p in glob.glob(d + "/dl.*") if not p.endswith((".part", ".ytdl"))]
        if not raws:
            raise RuntimeError("Download failed.")
        upd(job, 34, "Preparing video")
        prepare_source(raws[0], d + "/src.mp4")
        os.remove(raws[0]) if os.path.exists(raws[0]) else None
        analyze(job, d + "/src.mp4", req)
        job["status"], job["pct"], job["stage"] = "done", 100, "Ready"
    except Exception as e:
        job["status"], job["error"] = "error", clean_err(e)
    finally:
        if JOB_SLOT_ACQUIRED:
            JOB_SLOT.release()


def run_upload_job(job, raw, req):
    got = False
    try:
        upd(job, 1, "Waiting in queue")
        JOB_SLOT.acquire()
        got = True
        upd(job, 22, "Preparing video", job.get("title"))
        t0 = time.time()
        try:
            prepare_source(raw, job["dir"] + "/src.mp4")
        except RuntimeError:
            raise RuntimeError("Could not read this file. Please upload a normal video (mp4, mov, webm).")
        _, _, dur, _ = clipper.probe(job["dir"] + "/src.mp4")
        if dur > MAX_VIDEO_MIN * 60:
            raise RuntimeError(f"Video is longer than {MAX_VIDEO_MIN} minutes.")
        os.remove(raw)
        print(f"[job {job['id']}] prepare: {time.time() - t0:.1f}s", flush=True)
        t1 = time.time()
        analyze(job, job["dir"] + "/src.mp4", req)
        print(f"[job {job['id']}] transcribe+pick: {time.time() - t1:.1f}s", flush=True)
        job["status"], job["pct"], job["stage"] = "done", 100, "Ready"
    except Exception as e:
        job["status"], job["error"] = "error", clean_err(e)
    finally:
        if got:
            JOB_SLOT.release()


def range_response(request, path, media_type="video/mp4"):
    size = os.path.getsize(path)
    rng = request.headers.get("range")
    if not rng:
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    m = re.match(r"bytes=(\d*)-(\d*)", rng)
    a = int(m.group(1)) if m and m.group(1) else 0
    b = int(m.group(2)) if m and m.group(2) else size - 1
    b = min(b, size - 1)

    def it():
        with open(path, "rb") as f:
            f.seek(a)
            left = b - a + 1
            while left > 0:
                chunk = f.read(min(1024 * 256, left))
                if not chunk:
                    break
                left -= len(chunk)
                yield chunk

    return StreamingResponse(it(), status_code=206, media_type=media_type, headers={
        "Content-Range": f"bytes {a}-{b}/{size}", "Accept-Ranges": "bytes",
        "Content-Length": str(b - a + 1)})


def err(msg, code=400):
    return JSONResponse({"detail": msg}, status_code=code)


def get_done_job(jid):
    j = JOBS.get(jid)
    if not j or j["status"] != "done":
        return None
    return j


# ---------------- routes ----------------
@app.get("/")
def home():
    return FileResponse(BASE + "/index.html")


@app.get("/api/info")
def info(url: str = ""):
    if not re.match(r"^https?://", url or ""):
        return JSONResponse({})
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL(ydl_opts()) as y:
            i = y.extract_info(url, download=False)
        return {"title": i.get("title"), "duration": i.get("duration")}
    except Exception:
        return JSONResponse({})


@app.post("/api/analyze")
async def api_analyze(request: Request):
    try:
        req = await request.json()
    except Exception:
        return err("Bad request.")
    if not re.match(r"^https?://", (req.get("url") or "").strip()):
        return err("Paste a valid YouTube link.")
    job = new_job()
    threading.Thread(target=run_link_job, args=(job, req), daemon=True).start()
    return {"id": job["id"]}


@app.post("/api/upload")
async def api_upload(request: Request):
    form = await request.form()
    file = form.get("file")
    if file is None or not hasattr(file, "read"):
        return err("No file received.")
    try:
        req = json.loads(form.get("opts") or "{}")
    except Exception:
        req = {}
    for k in ("start", "end", "minlen", "maxlen", "count", "keywords", "lang"):
        if form.get(k) not in (None, ""):
            req[k] = form.get(k)
    job = new_job()
    raw = job["dir"] + "/upload" + (os.path.splitext(file.filename or "")[1].lower() or ".mp4")
    size = 0
    with open(raw, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1048576:
                f.close()
                shutil.rmtree(job["dir"], ignore_errors=True)
                JOBS.pop(job["id"], None)
                return err(f"File is too big. Max {MAX_UPLOAD_MB} MB.", 413)
            f.write(chunk)
    job["title"] = os.path.splitext(file.filename or "Uploaded video")[0][:80]
    threading.Thread(target=run_upload_job, args=(job, raw, req), daemon=True).start()
    return {"id": job["id"]}


@app.get("/api/job/{jid}")
def api_job(jid: str):
    j = JOBS.get(jid)
    if not j:
        return err("Job not found.", 404)
    out = {k: j[k] for k in ("id", "status", "pct", "stage", "title", "w", "h", "error")}
    out["clips"] = [dict({k: c[k] for k in ("start", "end", "score", "title", "description", "tags")},
                         words=[[w["w"], round(w["start"], 2), round(w["end"], 2)] for w in c["words"]])
                    for c in j["clips"]]
    return out


@app.get("/media/{jid}")
def media(jid: str, request: Request):
    p = f"{WORK}/{jid}/src.mp4"
    if not os.path.exists(p):
        return err("Not found.", 404)
    return range_response(request, p)


def _render_key(jid, i, q, caps, ratio):
    return f"{jid}-{i}-{q}-{caps}-{re.sub(r'[^0-9a-z]', '', ratio)}"


def _do_render(key, job, i, q, caps, ratio, out):
    with RENDER_SLOT:
        try:
            c = job["clips"][i]
            clipper.render_clip(job["dir"] + "/src.mp4", out, c["start"], c["end"], q, ratio,
                                c["words"], bool(caps), job["dir"])
            RENDERS[key] = "done"
        except Exception:
            RENDERS[key] = "error"


@app.get("/api/clip/{jid}/{i}")
def api_clip(jid: str, i: int, q: int = 720, captions: int = 1, ratio: str = "orig", prepare: int = 0):
    job = get_done_job(jid)
    if not job or i < 0 or i >= len(job["clips"]):
        return err("Not found.", 404)
    if ratio != "orig" and not re.match(r"^\d+:\d+$", ratio):
        ratio = "orig"
    q = max(240, min(q, 2160))
    key = _render_key(jid, i, q, captions, ratio)
    out = f"{job['dir']}/clip_{key.split('-', 1)[1]}.mp4"
    state = RENDERS.get(key)
    if state is None:
        RENDERS[key] = "running"
        threading.Thread(target=_do_render, args=(key, job, i, q, captions, ratio, out), daemon=True).start()
        state = "running"
    if state == "error":
        RENDERS.pop(key, None)
        return err("Render failed.", 500)
    if prepare:
        return {"ready": state == "done"}
    if state != "done":
        return err("Still rendering.", 409)
    return FileResponse(out, media_type="video/mp4", filename=f"clip{i + 1}.mp4")


@app.get("/api/audio/{jid}/{i}")
def api_audio(jid: str, i: int):
    job = get_done_job(jid)
    if not job or i < 0 or i >= len(job["clips"]):
        return err("Not found.", 404)
    out = f"{job['dir']}/audio_{i}.mp3"
    if not os.path.exists(out):
        c = job["clips"][i]
        clipper.render_audio(job["dir"] + "/src.mp4", out, c["start"], c["end"])
    return FileResponse(out, media_type="audio/mpeg", filename=f"clip{i + 1}.mp3")
