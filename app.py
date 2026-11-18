import os, re, json, uuid, subprocess, threading, urllib.request
from collections import Counter
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp

ROOT = Path(__file__).parent
JOBS_DIR = ROOT / "jobs"; JOBS_DIR.mkdir(exist_ok=True)
from fastapi.middleware.cors import CORSMiddleware
app = FastAPI(); app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*']); jobs = {}; _model = None
MINL, MAXL, TOP = 25, 55, 7

HOOK = set("""secret never always worst best shocking crazy truth mistake why how nobody everyone money free stop
biggest viral insane unbelievable warning hidden real fake exposed sach raaz kabhi nahi sabse paisa galti kaise kyun
kyu dhoka pagal jhooth sachai khatra""".split())
STOP = set("""this that with have from they them what when where which your about there their would could should been
were will just like also into than then very more some only know really going want right yeah okay hai hain tha thi
kar karna kya ke ki ka ko se mein par aur ek yeh woh toh bhi nahi""".split())

def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        name = os.getenv("WHISPER_MODEL", "base")
        try:
            _model = WhisperModel(name, compute_type="auto")
        except Exception:
            _model = WhisperModel(name, device="cpu", compute_type="int8")
    return _model

def score(text, dur):
    w = re.findall(r"\w+", text.lower()); n = max(len(w), 1)
    s = sum(x in HOOK for x in w) * 3 + text.count("?") * 2 + text.count("!") * 2 + len(re.findall(r"\d+", text))
    s += min(n / dur, 3.5) * 2
    s += 4 * any(x in HOOK for x in w[:12]) + (3 if "?" in text[:120] else 0)
    return s

def pick(segs, minl=MINL, maxl=MAXL, top=TOP, kw=()):
    best = []
    for i, s in enumerate(segs):
        t0, txt, top = s["s"], [], None
        for k in range(i, len(segs)):
            if segs[k]["e"] - t0 > maxl: break
            txt.append(segs[k]["t"]); d = segs[k]["e"] - t0
            if d >= minl:
                sc = score(" ".join(txt), d) + 4 * sum(k in " ".join(txt).lower() for k in kw)
                if not top or sc > top[0]: top = (sc, t0, segs[k]["e"], " ".join(txt))
        if top: best.append(top)
    best.sort(reverse=True); out = []
    for c in best:
        if all(c[2] <= o[1] or c[1] >= o[2] for o in out): out.append(c)
        if len(out) == top: break
    return out

def tags_for(text):
    w = [x for x in re.findall(r"[^\W\d_]{4,}", text.lower()) if x not in STOP]
    return ["#" + x for x, _ in Counter(w).most_common(5)] + ["#shorts"]

def gemini(clips):
    key = os.getenv("GEMINI_API_KEY")
    if not key: return
    try:
        prompt = ("For each transcript return JSON list of objects {title (max 60 chars, catchy), description (1-2 sentences), "
                  "tags (5 hashtags), score (1-99 virality)} in the same language as the transcript, same order. Transcripts:\n"
                  + json.dumps([c["text"][:900] for c in clips], ensure_ascii=False))
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"responseMimeType": "application/json"}}).encode()
        req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                     body, {"Content-Type": "application/json"})
        res = json.load(urllib.request.urlopen(req, timeout=60))
        data = json.loads(res["candidates"][0]["content"]["parts"][0]["text"])
        for c, g in zip(clips, data):
            c.update(title=g["title"], description=g["description"], tags=g["tags"], score=int(g["score"]))
    except Exception as e:
        print("gemini skipped:", e)

def ydl_base():
    o = {"quiet": True, "noplaylist": True, "remote_components": ["ejs:github"]}
    if (ROOT / "cookies.txt").exists(): o["cookiefile"] = str(ROOT / "cookies.txt")
    return o

def run(jid, req):
    url = req.url.strip(); off = max(req.start, 0)
    j = jobs[jid]; d = JOBS_DIR / jid; d.mkdir(exist_ok=True)
    def hook(h):
        t = h.get("total_bytes") or h.get("total_bytes_estimate") or 0
        if t: j["pct"] = 3 + int(27 * h.get("downloaded_bytes", 0) / t)
    try:
        j.update(stage="Video download ho raha hai", pct=3)
        opts = {**ydl_base(), "format": "bestvideo[height<=1440]+bestaudio/best[height<=1440]/best", "merge_output_format": "mp4",
                "outtmpl": str(d / "source.%(ext)s"), "progress_hooks": [hook]}
        with yt_dlp.YoutubeDL(opts) as y: info = y.extract_info(url)
        j["title"] = info.get("title", "")
        src = d / "source.mp4"
        pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                             "-of", "csv=p=0", str(src)], capture_output=True, text=True).stdout.strip().split(",")
        j["w"], j["h"] = int(pr[0]), int(pr[1])
        j.update(stage="Awaaz se transcript ban raha hai", pct=32)
        subprocess.run(["ffmpeg", "-y", "-ss", str(off)] + (["-t", str(req.end - off)] if req.end > off else []) + ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", str(d / "a.wav")],
                       capture_output=True, check=True)
        segs, words = [], []
        it, meta = get_model().transcribe(str(d / "a.wav"), word_timestamps=True, vad_filter=True, language=None if req.lang == "auto" else req.lang)
        for s in it:
            segs.append({"s": s.start + off, "e": s.end + off, "t": s.text.strip()})
            words += [[w.word.strip(), round(w.start + off, 2), round(w.end + off, 2)] for w in (s.words or [])]
            j["pct"] = 32 + int(55 * min(s.end / max(meta.duration, 1), 1))
        j.update(stage="Viral moments dhoondh rahe hain", pct=90)
        picks = pick(segs, max(10, req.minlen), max(req.minlen + 5, req.maxlen), min(max(req.count, 1), 10), [k.strip().lower() for k in req.keywords.split(",") if k.strip()])
        if not picks: raise RuntimeError("Is video mein bolne wala content nahi mila.")
        hi, lo = picks[0][0], picks[-1][0]
        clips = []
        for sc, a, b, text in picks:
            first = re.split(r"(?<=[.!?।])\s", text)[0][:70]
            cw = [w for w in words if w[1] >= a - 0.05 and w[2] <= b + 0.3]
            clips.append({"start": round(a, 2), "end": round(b, 2), "text": text, "title": first,
                          "description": text[:180] + ("…" if len(text) > 180 else ""), "tags": tags_for(text),
                          "score": 60 + int(39 * (sc - lo) / max(hi - lo, 1e-6)), "words": cw})
        gemini(clips)
        clips.sort(key=lambda c: -c["score"])
        j.update(clips=clips, stage="Ready", pct=100, status="done")
    except Exception as e:
        m = str(e)
        if "Sign in" in m or "bot" in m: m = "YouTube blocked this server. Add a cookies.txt file (see README). " + m
        j.update(status="error", error=m[:300])

class Req(BaseModel):
    url: str
    start: float = 0
    end: float = 0
    minlen: int = 30
    maxlen: int = 60
    count: int = 7
    keywords: str = ""
    lang: str = "auto"

@app.post("/api/analyze")
def analyze(r: Req):
    if not re.match(r"https?://(www\.|m\.)?(youtube\.com|youtu\.be)/", r.url.strip()):
        raise HTTPException(400, "Valid YouTube link daalo.")
    jid = uuid.uuid4().hex[:10]
    jobs[jid] = {"status": "running", "stage": "Shuru", "pct": 0}
    threading.Thread(target=run, args=(jid, r), daemon=True).start()
    return {"id": jid}

@app.get("/api/job/{jid}")
def job(jid: str):
    if jid not in jobs: raise HTTPException(404)
    return jobs[jid]

@app.get("/api/info")
def info(url: str):
    try:
        with yt_dlp.YoutubeDL({**ydl_base(), "skip_download": True}) as y:
            i = y.extract_info(url, download=False)
        return {"title": i.get("title"), "duration": i.get("duration")}
    except Exception:
        raise HTTPException(400, "Could not read this link.")

@app.get("/media/{jid}")
def media(jid: str):
    return FileResponse(JOBS_DIR / jid / "source.mp4", media_type="video/mp4")

def ts(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"

def make_ass(path, clip, W, H, vertical):
    fs = int(H * 0.042); mv = int(H * (0.2 if vertical else 0.08))
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "", "[V4+ Styles]",
             "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
             f"Style: D,Arial,{fs},&H0000E5FF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(fs // 8, 2)},0,2,40,40,{mv},1",
             "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"]
    ws = [[w[0], w[1] - clip["start"], w[2] - clip["start"]] for w in clip["words"]]
    for i in range(0, len(ws), 3):
        g = ws[i:i + 3]; end = ws[i + 3][1] if i + 3 < len(ws) else g[-1][2]
        txt = " ".join(x[0] for x in g).upper().replace("{", "").replace("}", "")
        lines.append(f"Dialogue: 0,{ts(max(g[0][1], 0))},{ts(max(end, g[0][1] + .2))},D,,0,0,0,,{txt}")
    Path(path).write_text("\n".join(lines), encoding="utf-8")

rendering, failed = set(), set()

def render(jid, i, q, captions, ratio):
    j = jobs[jid]; c = j["clips"][i]; d = JOBS_DIR / jid
    out = f"clip{i}_{q}_{captions}_{ratio.replace(':', 'x')}.mp4"
    if (d / out).exists(): return out
    sw, sh = j["w"], j["h"]
    if ratio == "orig": cw, ch = sw, sh
    else:
        a_, b_ = map(int, ratio.split(":")); r = a_ / b_
        cw, ch = (int(sh * r), sh) if r < sw / sh else (sw, int(sw / r))
    cw, ch = cw // 2 * 2, ch // 2 * 2
    f = min(1, q / ch); W, H = int(cw * f) // 2 * 2, int(ch * f) // 2 * 2
    vf = [f"crop={cw}:{ch}", f"scale={W}:{H}"]
    if captions:
        make_ass(d / f"c{i}.ass", c, W, H, W < H); vf.append(f"ass=c{i}.ass")
    tmp = "t_" + out
    cmd = ["ffmpeg", "-y", "-ss", str(c["start"]), "-i", "source.mp4", "-t", str(c["end"] - c["start"]),
           "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac",
           "-movflags", "+faststart", tmp]
    if subprocess.run(cmd, cwd=d, capture_output=True).returncode: raise RuntimeError("Render failed")
    os.replace(d / tmp, d / out)
    return out

def _bg(key, args):
    try: render(*args)
    except Exception: failed.add(key)
    finally: rendering.discard(key)

@app.get("/api/clip/{jid}/{i}")
def clip(jid: str, i: int, q: int = 1440, captions: int = 1, ratio: str = "orig", prepare: int = 0):
    j = jobs.get(jid)
    if not j or j.get("status") != "done": raise HTTPException(404)
    if ratio != "orig" and not re.fullmatch(r"\d{1,2}:\d{1,2}", ratio): raise HTTPException(400, "Bad ratio")
    q = min(q, 1440); args = (jid, i, q, captions, ratio)
    name = f"clip{i}_{q}_{captions}_{ratio.replace(':', 'x')}.mp4"; d = JOBS_DIR / jid
    if prepare:
        if (d / name).exists(): return {"ready": True}
        key = (jid, name)
        if key in failed: failed.discard(key); raise HTTPException(500, "Render failed")
        if key not in rendering:
            rendering.add(key); threading.Thread(target=_bg, args=(key, args), daemon=True).start()
        return {"ready": False}
    try: out = render(*args)
    except Exception: raise HTTPException(500, "Render failed")
    return FileResponse(d / out, media_type="video/mp4", filename=f"adiclipai-clip-{i + 1}-{q}p.mp4")

@app.get("/api/audio/{jid}/{i}")
def audio(jid: str, i: int):
    j = jobs.get(jid)
    if not j or j.get("status") != "done": raise HTTPException(404)
    c = j["clips"][i]; d = JOBS_DIR / jid; out = f"aud{i}.mp3"
    if not (d / out).exists():
        subprocess.run(["ffmpeg", "-y", "-ss", str(c["start"]), "-i", "source.mp4", "-t", str(c["end"] - c["start"]),
                        "-vn", "-c:a", "libmp3lame", "-q:a", "2", out], cwd=d, capture_output=True, check=True)
    return FileResponse(d / out, media_type="audio/mpeg", filename=f"viral-audio-{i + 1}.mp3")

@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
