import os, json, shutil, subprocess, threading, time, uuid, datetime
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse

BASE = Path(__file__).parent
WORK = Path(os.environ.get("WORK_DIR", "/tmp/adiclip")); WORK.mkdir(parents=True, exist_ok=True)
MAX_MB = int(os.environ.get("MAX_MB", "100"))          # max upload size
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "5"))   # jobs per IP per day
MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3-flash-preview", "gemini-2.5-flash"] if m]

app = FastAPI()
jobs, usage = {}, {}
gate = threading.Semaphore(1)  # one video at a time (small server)


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode:
        raise RuntimeError(r.stderr[-500:])
    return r.stdout


def srt_time(t):
    t = max(0, t); h, m, s = int(t // 3600), int(t % 3600 // 60), t % 60
    return f"{h:02}:{m:02}:{int(s):02},{int((s % 1) * 1000):03}"


def write_srt(path, caps, length):
    lines, n = [], 1
    for c in caps or []:
        try:
            a, b, txt = float(c["start"]), float(c["end"]), str(c["text"]).strip()
        except Exception:
            continue
        if not txt or a >= length:
            continue
        lines.append(f"{n}\n{srt_time(a)} --> {srt_time(min(b, length))}\n{txt}\n"); n += 1
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return n > 1


def ask_gemini(proxy, duration, seconds, count, want_caps):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    f = client.files.upload(file=str(proxy))
    while not f.state or f.state.name != "ACTIVE":
        if f.state and f.state.name == "FAILED":
            raise RuntimeError("Gemini could not process this video")
        time.sleep(3); f = client.files.get(name=f.name)
    prompt = f"""You are a short-form video editor. The video is {duration:.0f} seconds long.
Pick up to {count} of the best standalone moments for Shorts/Reels, best first.
Rules: each clip about {seconds} seconds (between {int(seconds*0.7)} and {int(seconds*1.3)}); start at the beginning of a sentence and end at the end of one; strong hook in the first 3 seconds; clips must not overlap; timestamps are seconds from the start of the video.
{"For each clip also give captions of the speech, in the language spoken, max 7 words each, with start/end seconds RELATIVE to the clip start." if want_caps else "Leave captions as an empty list."}
Return ONLY JSON: {{"clips":[{{"start":12.0,"end":42.0,"title":"short catchy title","reason":"why this works","captions":[{{"start":0.0,"end":2.4,"text":"..."}}]}}]}}"""
    last, data = None, None
    for m in MODELS:
        try:
            r = client.models.generate_content(model=m, contents=[f, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"))
            data = json.loads(r.text); break
        except Exception as e:
            last = e
    try: client.files.delete(name=f.name)
    except Exception: pass
    if data is None:
        raise RuntimeError(f"Gemini error: {last}")
    return data.get("clips", []) if isinstance(data, dict) else data


def vf_chain(w, h, fit, srt):
    if fit:
        v = (f"split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5[bg];"
             f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        v = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if srt:
        v += f",subtitles={srt}:force_style='FontName=DejaVu Sans,FontSize=13,Bold=1,Outline=2,Shadow=0,Alignment=2,MarginV=40'"
    return v


def run_job(jid, src, fmt, seconds, fit, want_caps):
    j, d = jobs[jid], WORK / jid
    try:
        j.update(status="queued", message="Waiting in queue…")
        with gate:
            j.update(status="working", progress=5, message="Preparing video…")
            dur = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)]).strip())
            proxy = d / "proxy.mp4"
            sh(["ffmpeg", "-y", "-threads", "2", "-i", str(src), "-vf", "scale=-2:360", "-r", "5", "-c:v", "libx264",
                "-preset", "veryfast", "-crf", "32", "-c:a", "aac", "-b:a", "64k", str(proxy)])
            j.update(progress=20, message="AI is watching your video…")
            raw = ask_gemini(proxy, dur, seconds, 3, want_caps)
            clips = []
            for c in raw:
                try:
                    a = max(0.0, float(c["start"])); b = min(dur, float(c["end"]))
                except Exception:
                    continue
                if b - a > seconds * 1.6: b = a + seconds
                if b - a >= 5: clips.append((a, b, c))
            if not clips:
                raise RuntimeError("AI could not find a good moment in this video.")
            w, h = (720, 1280) if fmt == "shorts" else (1280, 720)
            out = []
            for i, (a, b, c) in enumerate(clips):
                j.update(progress=40 + int(55 * i / len(clips)), message=f"Rendering clip {i+1} of {len(clips)}…")
                srt = None
                if want_caps and write_srt(d / f"c{i}.srt", c.get("captions"), b - a): srt = f"c{i}.srt"
                sh(["ffmpeg", "-y", "-threads", "2", "-ss", f"{a:.2f}", "-i", str(src), "-t", f"{b-a:.2f}",
                    "-vf", vf_chain(w, h, fit, srt), "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", f"clip{i}.mp4"], cwd=d)
                out.append({"title": str(c.get("title", f"Clip {i+1}"))[:80], "reason": str(c.get("reason", ""))[:200],
                            "start": a, "end": b, "url": f"/api/clip/{jid}/{i}"})
            src.unlink(missing_ok=True); proxy.unlink(missing_ok=True)
            j.update(status="done", progress=100, message="Done!", clips=out)
    except Exception as e:
        j.update(status="error", message=f"Failed: {str(e)[:300]}")
        shutil.rmtree(d, ignore_errors=True)


@app.post("/api/process")
async def process(request: Request, file: UploadFile = File(...), format: str = Form("shorts"),
                  seconds: int = Form(30), framing: str = Form("fill"), captions: str = Form("off")):
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(500, "Server is missing GEMINI_API_KEY")
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    key = (ip, datetime.date.today())
    if usage.get(key, 0) >= DAILY_LIMIT:
        raise HTTPException(429, f"Daily limit reached ({DAILY_LIMIT} videos). Try tomorrow.")
    for p in WORK.iterdir():  # delete old jobs (>1h)
        if time.time() - p.stat().st_mtime > 3600: shutil.rmtree(p, ignore_errors=True)
    jid = uuid.uuid4().hex[:12]; d = WORK / jid; d.mkdir()
    src, size = d / "source", 0
    with open(src, "wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_MB * 1024 * 1024:
                out.close(); shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(413, f"Video too large (max {MAX_MB} MB)")
            out.write(chunk)
    usage[key] = usage.get(key, 0) + 1
    jobs[jid] = {"status": "queued", "progress": 0, "message": "Uploaded", "clips": []}
    threading.Thread(target=run_job, daemon=True, args=(
        jid, src, "shorts" if format == "shorts" else "long", max(10, min(90, seconds)),
        framing == "fit", captions == "on")).start()
    return {"id": jid}


@app.get("/api/status/{jid}")
def status(jid: str):
    if jid not in jobs: raise HTTPException(404, "Unknown job")
    return jobs[jid]


@app.get("/api/clip/{jid}/{i}")
def clip(jid: str, i: int):
    p = WORK / jid / f"clip{i}.mp4"
    if jid not in jobs or not p.exists(): raise HTTPException(404, "Clip not found")
    return FileResponse(p, media_type="video/mp4", filename=f"AdiclipAI-clip-{i+1}.mp4")


@app.get("/")
def home():
    return FileResponse(BASE / "index.html")
