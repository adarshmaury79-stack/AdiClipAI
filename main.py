import os, re, json, shutil, subprocess, threading, time, uuid, datetime, traceback, socket, ipaddress, urllib.request, urllib.parse
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import FileResponse

BASE = Path(__file__).parent
WORK = Path(os.environ.get("WORK_DIR", "/tmp/adiclip")); WORK.mkdir(parents=True, exist_ok=True)
MAX_MB = int(os.environ.get("MAX_MB", "100"))
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "8"))
MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3-flash-preview", "gemini-2.5-flash"] if m]
YT = re.compile(r"^https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", re.I)

app = FastAPI()
jobs, usage = {}, {}
gate = threading.Semaphore(1)


def sh(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode:
        raise RuntimeError("ffmpeg: " + r.stderr[-400:])
    return r.stdout


def ass_time(t):
    t = max(0, t); return f"{int(t // 3600)}:{int(t % 3600 // 60):02}:{t % 60:05.2f}"


def write_ass(path, caps, length, w, h):
    """Karaoke captions: spoken words change colour (white -> yellow)."""
    size = int(min(w, h) * 0.085); mv = int(h * 0.13)
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Default,Noto Sans,{size},&H004DE1FF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{max(3, size // 14)},1,2,40,40,{mv},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for c in caps or []:
        try: a, b, txt = float(c["start"]), float(c["end"]), str(c["text"]).strip()
        except Exception: continue
        words = txt.replace("{", "").replace("}", "").split()
        if not words or a >= length: continue
        b = min(b, length)
        if b <= a: b = min(length, a + 1)
        total = sum(len(x) for x in words); cs = max(1, int((b - a) * 100))
        parts = [f"{{\\k{max(1, int(cs * len(x) / total))}}}{x}" for x in words]
        lines.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Default,,0,0,0,,{' '.join(parts)}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return bool(lines)


def ask_gemini(src, url, seconds, count, caps, dur, key=""):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=key or os.environ["GEMINI_API_KEY"])
    f = None
    if url:
        part = types.Part(file_data=types.FileData(file_uri=url))
    else:
        f = client.files.upload(file=str(src))
        while not f.state or f.state.name != "ACTIVE":
            if f.state and f.state.name == "FAILED":
                raise RuntimeError("Gemini could not process this video")
            time.sleep(3); f = client.files.get(name=f.name)
        part = f
    prompt = f"""You are a short-form video editor. {"The video is %d seconds long." % dur if dur else ""}
Pick up to {count} of the best standalone moments for Shorts/Reels, best first. Give each a virality score 1-100 (hook, emotion, value, shareability).
Rules: each clip about {seconds} seconds (between {int(seconds*0.7)} and {int(seconds*1.3)}); start at the beginning of a sentence and end at the end of one; strong hook in the first 3 seconds; no overlap; timestamps are seconds from the start of the video.
{"For each clip give captions of the speech in the exact language spoken (Hindi speech in Devanagari script, English speech in English), max 5 words each, exact spoken words, start/end seconds RELATIVE to the clip start." if caps else "Leave captions as an empty list."}
Return ONLY JSON: {{"clips":[{{"start":12.0,"end":42.0,"title":"short catchy title","reason":"why this works","captions":[{{"start":0.0,"end":2.4,"text":"..."}}]}}]}}"""
    last, data = None, None
    for m in MODELS:
        try:
            r = client.models.generate_content(model=m, contents=[part, prompt],
                config=types.GenerateContentConfig(response_mime_type="application/json"))
            data = json.loads(r.text); break
        except Exception as e:
            last = e; print("gemini", m, "failed:", e, flush=True)
    if f:
        try: client.files.delete(name=f.name)
        except Exception: pass
    if data is None:
        raise RuntimeError(f"Gemini error: {str(last)[:200]}")
    return data.get("clips", []) if isinstance(data, dict) else data


def sc(c):
    try: return int(float(c.get("score", 0) or 0))
    except Exception: return 0


def safe_download(url, dest):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise RuntimeError("Invalid link")
    for info in socket.getaddrinfo(p.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            raise RuntimeError("This link is not allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    size = 0
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as out:
        ctype = r.headers.get("Content-Type", "")
        if not (ctype.startswith("video/") or re.search(r"\.(mp4|mov|webm|mkv)(\?|$)", url, re.I)):
            raise RuntimeError("This link is not a direct video file. Use a YouTube link or upload the video.")
        while chunk := r.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_MB * 1024 * 1024:
                raise RuntimeError(f"Video too large (max {MAX_MB} MB)")
            out.write(chunk)


def vf_chain(w, h, fit, srt):
    if fit:
        v = (f"split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:5[bg];"
             f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        v = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    if srt:
        v += f",ass={srt}"
    return v


def size_for(ratio, q):
    q = int(q); even = lambda x: int(round(x / 2) * 2)
    if ratio == "1:1": return q, q
    if ratio == "4:5": return q, even(q * 5 / 4)
    if ratio == "16:9": return even(q * 16 / 9), q
    return q, even(q * 16 / 9)


def run_job(jid, src, url, o):
    j, d = jobs[jid], WORK / jid
    try:
        with gate:
            is_yt = bool(url and YT.match(url))
            dur = 0
            if url and not is_yt:
                j.update(status="working", progress=4, message="Downloading video…")
                safe_download(url, src); url = None
            if not is_yt:
                dur = float(sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)]).strip())
            j.update(status="working", progress=15, message="AI is watching your video…")
            raw = ask_gemini(src, url if is_yt else None, o["seconds"], o["count"], o["captions"], dur, o["key"])
            clips = []
            for c in raw:
                try: a = max(0.0, float(c["start"])); b = float(c["end"])
                except Exception: continue
                if dur: b = min(dur, b)
                if b - a > o["seconds"] * 1.6: b = a + o["seconds"]
                if b - a >= 5: clips.append((a, b, c))
            clips.sort(key=lambda x: -sc(x[2]))
            clips = clips[:o["count"]]
            if not clips:
                raise RuntimeError("AI could not find a good moment in this video.")
            if is_yt:
                for a, b, c in clips:
                    t = " ".join(str(x.get("text", "")) for x in (c.get("captions") or []))
                    j["clips"].append({"title": str(c.get("title", "Moment"))[:80], "reason": str(c.get("reason", ""))[:240],
                        "start": a, "end": b, "score": sc(c), "url": None, "link": f"{url}{'&' if '?' in url else '?'}t={int(a)}s", "text": t[:600]})
                j.update(status="done", progress=100, message="Best moments found!"); return
            w, h = size_for(o["ratio"], o["quality"])
            for i, (a, b, c) in enumerate(clips):
                j.update(progress=30 + int(65 * i / len(clips)), message=f"Rendering clip {i+1} of {len(clips)}… (free server is slow, please wait)")
                srt = f"c{i}.ass" if o["captions"] and write_ass(d / f"c{i}.ass", c.get("captions"), b - a, w, h) else None
                sh(["ffmpeg", "-y", "-threads", "2", "-ss", f"{a:.2f}", "-i", str(src), "-t", f"{b-a:.2f}",
                    "-vf", vf_chain(w, h, o["fit"], srt), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", f"clip{i}.mp4"], cwd=d)
                j["clips"].append({"title": str(c.get("title", f"Clip {i+1}"))[:80], "reason": str(c.get("reason", ""))[:240],
                                   "start": a, "end": b, "score": sc(c), "url": f"/api/clip/{jid}/{i}", "link": None, "text": ""})
            src.unlink(missing_ok=True)
            j.update(status="done", progress=100, message="Done!")
    except Exception as e:
        traceback.print_exc()
        j.update(status="error", message=f"Failed: {str(e)[:300]}")


@app.post("/api/process")
async def process(request: Request, file: UploadFile = File(None), url: str = Form(""), ratio: str = Form("9:16"),
                  quality: str = Form("720"), framing: str = Form("fill"), captions: str = Form("on"),
                  seconds: int = Form(30), count: int = Form(2), api_key: str = Form("")):
    api_key = api_key.strip()
    if not api_key and not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(400, "Paste your Gemini API key in Settings")
    url = url.strip()
    if not file and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "Upload a video or paste a valid link")
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    key = (ip, datetime.date.today())
    if not api_key and usage.get(key, 0) >= DAILY_LIMIT:
        raise HTTPException(429, f"Daily limit reached ({DAILY_LIMIT}). Try tomorrow.")
    for p in WORK.iterdir():
        if time.time() - p.stat().st_mtime > 3600: shutil.rmtree(p, ignore_errors=True)
    jid = uuid.uuid4().hex[:12]; d = WORK / jid; d.mkdir()
    ext = Path(file.filename or "v.mp4").suffix.lower() if file else ".mp4"
    src = d / f"source{ext if ext in ('.mp4', '.mov', '.webm', '.mkv', '.m4v') else '.mp4'}"
    if file:
        size = 0
        with open(src, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_MB * 1024 * 1024:
                    out.close(); shutil.rmtree(d, ignore_errors=True)
                    raise HTTPException(413, f"Video too large (max {MAX_MB} MB)")
                out.write(chunk)
    if not api_key: usage[key] = usage.get(key, 0) + 1
    jobs[jid] = {"status": "working", "progress": 2, "message": "Queued…", "clips": []}
    o = dict(ratio=ratio if ratio in ("9:16", "1:1", "4:5", "16:9") else "9:16", quality=quality if quality in ("480", "720", "1080") else "720",
             key=api_key, fit=framing == "fit", captions=captions == "on", seconds=max(10, min(90, seconds)), count=max(1, min(10, count)))
    threading.Thread(target=run_job, daemon=True, args=(jid, src, None if file else url, o)).start()
    return {"id": jid}


@app.get("/api/status/{jid}")
def status(jid: str):
    if jid not in jobs: raise HTTPException(404, "Server restarted. Please try again.")
    return jobs[jid]


@app.get("/api/clip/{jid}/{i}")
def clip(jid: str, i: int):
    p = WORK / jid / f"clip{i}.mp4"
    if jid not in jobs or not p.exists(): raise HTTPException(404, "Clip not found")
    return FileResponse(p, media_type="video/mp4", filename=f"AdiClipAI-clip-{i+1}.mp4")


@app.get("/api/health")
def health():
    return {"ffmpeg": bool(shutil.which("ffmpeg")), "gemini_key": bool(os.environ.get("GEMINI_API_KEY")), "models": MODELS}


@app.get("/api/check")
def check():
    out = {"key_set": bool(os.environ.get("GEMINI_API_KEY")), "models": {}}
    if out["key_set"]:
        from google import genai
        c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        for m in MODELS:
            try: c.models.generate_content(model=m, contents="Say OK"); out["models"][m] = "works"
            except Exception as e: out["models"][m] = str(e)[:200]
    return out


@app.get("/")
def home():
    return FileResponse(BASE / "index.html")
