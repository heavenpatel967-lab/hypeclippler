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


def build_ass(words, duration: float, hook: str, layout: str, font: str, burn_hook: bool) -> str:
    cap_y = 1480 if layout == "blur" else 1380
    hook_y = 420 if layout == "blur" else 260
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,7,3,5,60,60,0,1",
        f"Style: Hook,{font},64,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB4000000,-1,0,0,0,100,100,0,0,3,18,0,5,80,80,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if burn_hook and hook:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(duration)},Hook,,0,0,0,,"
                     f"{{\\pos(540,{hook_y})\\fad(200,0)}}{_clean(hook).upper()}")
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
            lines.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,"
                         f"{{\\pos(540,{cap_y})}}" + " ".join(parts))
    return "\n".join(lines) + "\n"
