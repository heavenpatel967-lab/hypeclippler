"""Speech-to-text with faster-whisper (local, free) and viral style .ass subtitles."""
import importlib.util
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("captions")
_model = None
_model_name = None
_lock = threading.Lock()


def _add_cuda_dlls():
    """On Windows, pip packages nvidia-cublas-cu12 / nvidia-cudnn-cu12 ship the CUDA DLLs
    faster-whisper needs. Make them findable so you don't have to install the CUDA toolkit."""
    if os.name != "nt":
        return
    spec = importlib.util.find_spec("nvidia")
    if not spec or not spec.submodule_search_locations:
        return
    for base in spec.submodule_search_locations:
        for sub in ("cublas", "cudnn", "cuda_nvrtc"):
            d = Path(base) / sub / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def _load(settings, force_cpu=False):
    global _model, _model_name
    from faster_whisper import WhisperModel
    name = settings["whisper_model"]
    device = settings["whisper_device"]
    key = (name, device, force_cpu)
    if _model is not None and _model_name == key:
        return _model
    _add_cuda_dlls()
    attempts = []
    if not force_cpu and device in ("auto", "cuda"):
        attempts.append((name, "cuda", "float16"))
    attempts += [(name, "cpu", "int8"), ("small", "cpu", "int8")]
    last = None
    for model_name, dev, ctype in attempts:
        try:
            log.info("Loading whisper %s on %s (%s)", model_name, dev, ctype)
            _model = WhisperModel(model_name, device=dev, compute_type=ctype)
            _model_name = key
            return _model
        except Exception as e:  # noqa: BLE001
            log.warning("whisper %s/%s failed: %s", model_name, dev, e)
            last = e
    raise RuntimeError(f"could not load whisper: {last}")


def transcribe(media: Path, settings) -> tuple[list[dict], str]:
    """Returns (words, language). words = [{"start", "end", "word"}]."""
    global _model
    with _lock:  # one transcription at a time keeps VRAM use predictable
        for force_cpu in (False, True):
            try:
                model = _load(settings, force_cpu)
                segments, info = model.transcribe(str(media), word_timestamps=True, vad_filter=True,
                                                  beam_size=5, condition_on_previous_text=False)
                words = []
                for seg in segments:
                    for w in seg.words or []:
                        text = w.word.strip()
                        if text:
                            words.append({"start": w.start, "end": w.end, "word": text})
                return words, info.language
            except Exception as e:  # noqa: BLE001 - e.g. missing CUDA DLL at run time
                if force_cpu:
                    raise
                log.warning("GPU transcription failed (%s), retrying on CPU", e)
                _model = None
    return [], "en"


# ------------------------------------------------------------------ subtitles
def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _clean(text: str) -> str:
    text = text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")
    return "".join(c for c in text if ord(c) <= 0xFFFF)  # emoji fonts don't burn well


def _chunks(words, max_words=3, max_len=1.6, gap=0.6):
    chunk = []
    for w in words:
        if chunk and (len(chunk) >= max_words or w["start"] - chunk[-1]["end"] > gap
                      or w["end"] - chunk[0]["start"] > max_len):
            yield chunk
            chunk = []
        chunk.append(w)
    if chunk:
        yield chunk


def geometry(layout: str, show_chat: bool) -> dict:
    """Where everything goes on the 1080x1920 frame.
    video_y = top of the 16:9 video in blur layout (None = centered)."""
    if layout == "crop":
        if show_chat:
            return {"video_y": None, "top_y": 230, "cap_y": 1060, "chat": (40, 1150, 1000, 720)}
        return {"video_y": None, "top_y": 260, "cap_y": 1380, "chat": None}
    if show_chat:
        # video moves up, captions go right under it, chat fills the bottom
        return {"video_y": 330, "top_y": 175, "cap_y": 1035, "chat": (40, 1115, 1000, 760)}
    return {"video_y": None, "top_y": 420, "cap_y": 1480, "chat": None}


PALETTE = ["&H5050FF&", "&H50D0FF&", "&H60FF60&", "&HFFB050&", "&HFF70D0&", "&H50FFFF&", "&HB0B0FF&", "&HFF9090&"]


def _ass_color(hex_color, name):
    """'#FF4500' (Twitch/Kick user color) -> ASS &HBBGGRR&. Random-but-stable color if missing."""
    if hex_color and len(hex_color.lstrip("#")) == 6:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        if 0.299 * r + 0.587 * g + 0.114 * b < 110:  # too dark to read on the black chat box
            r, g, b = min(255, r + 110), min(255, g + 110), min(255, b + 110)
        return f"&H{b:02X}{g:02X}{r:02X}&"
    return PALETTE[sum(map(ord, name or "?")) % len(PALETTE)]


def _chat_events(chat, duration, box, max_lines=11, per_sec=3.5):
    """Scrolling chat box like the Twitch sidebar, newest message at the bottom."""
    x, y, w, h = box
    out = [
        # box background (semi-transparent black) + header
        f"Dialogue: 2,{_ts(0)},{_ts(duration)},ChatBox,,0,0,0,,{{\\an7\\pos({x},{y})\\c&H000000&\\alpha&H48&\\p1}}"
        f"m 0 0 l {w} 0 l {w} {h} l 0 {h}{{\\p0}}",
        f"Dialogue: 3,{_ts(0)},{_ts(duration)},ChatHead,,0,0,0,,{{\\an7\\pos({x + 26},{y + 16})}}"
        "{\\c&H3C3CFF&}●{\\c&HFFFFFF&} LIVE CHAT",
    ]
    # thin out huge chats so it stays readable: at most `per_sec` new lines per second
    picked, last_t = [], -99.0
    for m in chat:
        text = _clean(m["text"]).strip()
        if not text:
            continue
        if m["t"] < 0 or m["t"] - last_t >= 1 / per_sec:
            picked.append({**m, "text": text[:70] + ("…" if len(text) > 70 else "")})
            last_t = max(m["t"], 0)
    if not picked:
        return out
    history = [m for m in picked if m["t"] < 0][-max_lines:]
    live = [m for m in picked if m["t"] >= 0]
    timeline = [(0.0, len(history))] if history else []
    shown = history[:]
    for i, m in enumerate(live):
        shown.append(m)
        timeline.append((m["t"], len(shown)))
    for i, (start, count) in enumerate(timeline):
        end = timeline[i + 1][0] if i + 1 < len(timeline) else duration
        if end - start < 0.02:
            continue
        visible = shown[max(0, count - max_lines):count]
        text = "\\N".join(f"{{\\b1\\c{_ass_color(m.get('color'), m['user'])}}}{_clean(m['user'])[:20]}"
                          f"{{\\b0\\c&HFFFFFF&}}: {m['text']}" for m in visible)
        out.append(f"Dialogue: 3,{_ts(start)},{_ts(end)},Chat,,0,0,0,,"
                   f"{{\\an1\\pos({x + 26},{y + h - 18})\\clip({x},{y + 64},{x + w},{y + h})}}{text}")
    return out


def build_ass(words, duration: float, top_captions: list, layout: str, font: str, burn_top: bool,
              chat=None, show_chat=False) -> str:
    g = geometry(layout, show_chat)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{84 if show_chat else 92},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,"
        "-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1",
        f"Style: Top,{font},62,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,18,0,5,70,70,0,1",
        "Style: ChatBox,Arial,20,&H00000000,&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1",
        "Style: ChatHead,Arial,34,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,7,0,0,0,1",
        "Style: Chat,Arial,38,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,2,1,1,66,66,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if burn_top and top_captions:
        caps = sorted(top_captions, key=lambda c: c["time"])
        for i, c in enumerate(caps):
            start = c["time"]
            end = caps[i + 1]["time"] if i + 1 < len(caps) else duration
            if end - start < 0.3:
                continue
            pop = "\\fscx70\\fscy70\\t(0,120,\\fscx106\\fscy106)\\t(120,200,\\fscx100\\fscy100)"
            lines.append(f"Dialogue: 4,{_ts(start)},{_ts(end)},Top,,0,0,0,,"
                         f"{{\\pos(540,{g['top_y']}){pop}}}{_clean(c['text']).upper()}")

    if show_chat and g["chat"] and chat is not None:
        lines += _chat_events(chat, duration, g["chat"])

    chunk_list = list(_chunks(words))
    for ci, chunk in enumerate(chunk_list):
        next_start = chunk_list[ci + 1][0]["start"] if ci + 1 < len(chunk_list) else duration
        for wi, w in enumerate(chunk):
            start = w["start"]
            end = chunk[wi + 1]["start"] if wi + 1 < len(chunk) else min(next_start, w["end"] + 0.5)
            if end <= start:
                end = start + 0.05
            parts = []
            for j, other in enumerate(chunk):
                word = _clean(other["word"]).upper()
                if j == wi:
                    parts.append("{\\c&H00E6FF&\\fscx112\\fscy112}" + word + "{\\c&HFFFFFF&\\fscx100\\fscy100}")
                else:
                    parts.append(word)
            lines.append(f"Dialogue: 5,{_ts(start)},{_ts(end)},Cap,,0,0,0,,"
                         f"{{\\pos(540,{g['cap_y']})}}" + " ".join(parts))
    return "\n".join(lines) + "\n"
