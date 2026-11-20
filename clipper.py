"""Clip picking + ffmpeg rendering. Uses only the standard library and ffmpeg."""
import json
import re
import subprocess
import uuid
from collections import Counter

STOP = set("""a an and are as at be been but by can could did do does for from had has have he her his i if in into is it its just
me my no not of on or our she so than that the their them then there these they this to too us was we were what when where which
who will with would you your about also very really like get got one all some more other out up how why""".split())
STOP |= set("है हैं का की के में से को और यह वो तो पर कि एक ही भी नहीं था थी थे हो कर रहा रही रहे जो इस उस ने तक कोई बहुत मैं हम आप तुम".split())

HOOKS = ("how", "why", "what", "secret", "never", "nobody", "stop", "truth", "mistake", "biggest", "best", "worst",
         "money", "free", "fail", "success", "problem", "listen", "imagine", "remember", "important", "reason",
         "क्यों", "कैसे", "क्या", "सच", "राज", "कभी", "सबसे", "पैसा", "गलती", "ध्यान")


def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "ffmpeg failed")[-600:])
    return p.stdout


def probe(path):
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,codec_name:format=duration",
               "-of", "json", path])
    d = json.loads(out)
    st = (d.get("streams") or [None])[0]
    if not st:
        raise RuntimeError("No video stream found.")
    return int(st["width"]), int(st["height"]), float(d["format"].get("duration") or 0), st.get("codec_name", "")


def words_of(seg):
    """Return word list for a segment. Falls back to evenly spread words."""
    if seg.get("words"):
        return seg["words"]
    toks = seg["text"].split()
    if not toks:
        return []
    step = (seg["end"] - seg["start"]) / len(toks)
    return [{"w": t, "start": seg["start"] + i * step, "end": seg["start"] + (i + 1) * step} for i, t in enumerate(toks)]


def _sentences(text):
    parts = re.split(r"(?<=[.?!।])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def _clip_text(text, n):
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:- ") + "..."


def _score(text, dur, first_text, kws):
    words = text.split()
    n = len(words)
    low = text.lower()
    s = 42.0
    wps = n / max(dur, 1)
    s += max(-8, min(14, (wps - 1.6) * 8))
    fl = first_text.lower()
    if "?" in first_text or any(fl.startswith(h) for h in HOOKS):
        s += 10
    s += min(8, 2 * low.count("?")) + min(5, low.count("!"))
    if re.search(r"\d", text):
        s += 4
    s += min(10, 2 * sum(1 for h in HOOKS if h in low))
    s += min(24, 12 * sum(1 for k in kws if k and k in low))
    if re.search(r"[.?!।]\s*$", text):
        s += 4
    return int(max(50, min(99, round(s))))


def select_clips(segs, minlen, maxlen, count, keywords=""):
    segs = [s for s in segs if s["text"].strip()]
    if not segs:
        return []
    kws = [k.strip().lower() for k in re.split(r"[,\n]", keywords or "") if k.strip()]
    cands = []
    n = len(segs)
    for i in range(n):
        shortest = longest = None
        for k in range(i, n):
            dur = segs[k]["end"] - segs[i]["start"]
            if dur > maxlen:
                break
            if dur >= minlen:
                if shortest is None:
                    shortest = k
                longest = k
        picks = {shortest, longest} - {None}
        for k in picks:
            cands.append((i, k))
    if not cands:  # video/speech shorter than min length
        total = segs[-1]["end"] - segs[0]["start"]
        if total < 5:
            return []
        k = n - 1
        while k > 0 and segs[k]["end"] - segs[0]["start"] > maxlen:
            k -= 1
        cands = [(0, k)]
    scored = []
    for i, k in cands:
        text = " ".join(s["text"].strip() for s in segs[i:k + 1])
        st, en = segs[i]["start"], segs[k]["end"]
        scored.append((_score(text, en - st, segs[i]["text"], kws), st, en, i, k, text))
    scored.sort(key=lambda x: -x[0])
    chosen = []
    for sc, st, en, i, k, text in scored:
        if len(chosen) >= count:
            break
        if any(st < c["end"] and en > c["start"] for c in chosen):
            continue
        chosen.append({"start": st, "end": en, "score": sc, "i": i, "k": k, "text": text})
    out = []
    for c in chosen:
        sents = _sentences(c["text"])
        title = sents[0] if sents else c["text"]
        if len(title) < 18 and len(sents) > 1:
            title = title + " " + sents[1]
        title = _clip_text(title.strip(), 70).rstrip(".")
        title = title[:1].upper() + title[1:]
        rest = " ".join(sents[1:]) if len(sents) > 1 else c["text"]
        desc = _clip_text(rest, 160)
        toks = [w.strip(".,!?;:\"'()[]।").lower() for w in c["text"].split()]
        toks = [w for w in toks if w and w not in STOP and (len(w) >= 4 or (not w.isascii() and len(w) >= 3))]
        tags = ["#" + re.sub(r"\W+", "", w) for w, _ in Counter(toks).most_common(3)]
        tags = [t for t in tags if len(t) > 1] or ["#shorts"]
        words = []
        for sg in segs[c["i"]:c["k"] + 1]:
            words += words_of(sg)
        out.append({"start": round(max(0, c["start"] - 0.2), 2), "end": round(c["end"] + 0.3, 2),
                    "score": c["score"], "title": title, "description": desc, "tags": tags, "words": words})
    out.sort(key=lambda c: -c["score"])
    return out


# ---------- captions ----------
def _t(x):
    x = max(0, x)
    h, m, s = int(x // 3600), int(x % 3600 // 60), x % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def make_ass(words, clip_start, clip_end, out_w, out_h, path):
    size = max(18, int(min(out_w, out_h * 0.9) * 0.075))
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {out_w}
PlayResY: {out_h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,DejaVu Sans,{size},&H0000E5FF,&H0000E5FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{max(2, size // 9)},0,2,{int(out_w * 0.06)},{int(out_w * 0.06)},{int(out_h * 0.16)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ws = [w for w in words if clip_start - 0.05 <= w["start"] <= clip_end]
    chunks, cur = [], []
    for w in ws:
        if cur and (len(cur) >= 3 or w["start"] - cur[-1]["end"] > 0.7 or re.search(r"[.?!।,]$", cur[-1]["w"])):
            chunks.append(cur)
            cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    lines = []
    for idx, ch in enumerate(chunks):
        st = ch[0]["start"] - clip_start
        en = ch[-1]["end"] - clip_start
        if idx + 1 < len(chunks):
            en = min(max(en, st + 0.3), chunks[idx + 1][0]["start"] - clip_start)
        text = " ".join(x["w"].strip() for x in ch).upper().replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{_t(st)},{_t(en)},Cap,,0,0,0,,{text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")


# ---------- rendering ----------
def _even(x):
    return max(2, int(x) // 2 * 2)


def out_size(src_w, src_h, ratio, q):
    if ratio and ratio != "orig":
        a, b = [float(x) for x in ratio.split(":")]
        r = a / b
        if src_w / src_h > r:
            cw, ch = src_h * r, src_h
        else:
            cw, ch = src_w, src_w / r
    else:
        cw, ch = src_w, src_h
    cw, ch = _even(cw), _even(ch)
    oh = _even(min(int(q), ch))
    ow = _even(cw * oh / ch)
    return cw, ch, ow, oh


def render_clip(src, out, start, end, q, ratio, words, captions, workdir):
    sw, sh, _, _ = probe(src)
    cw, ch, ow, oh = out_size(sw, sh, ratio, q)
    vf = []
    if ratio and ratio != "orig":
        vf.append(f"crop={cw}:{ch}")
    if (ow, oh) != (cw, ch):
        vf.append(f"scale={ow}:{oh}")
    if captions and words:
        ass = f"cap_{uuid.uuid4().hex[:8]}.ass"
        make_ass(words, start, end, ow, oh, f"{workdir}/{ass}")
        vf.append(f"subtitles={ass}")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", src, "-t", f"{end - start:.2f}"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    run(cmd, cwd=workdir)


def render_audio(src, out, start, end):
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", src, "-t", f"{end - start:.2f}",
         "-vn", "-c:a", "libmp3lame", "-q:a", "2", out])
