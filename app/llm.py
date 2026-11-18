"""Talks to the local Ollama server.

Picks a model automatically from what you have installed, so a wrong model name
(Ollama answers "404 Not Found") never silently turns the AI off. Vision models
(they can look at video frames) are preferred.
"""
import json
import logging
import re
import threading
import time

import httpx

log = logging.getLogger("llm")

# best first. Vision models first: they can see what happens in the video.
PREFERRED = ["gemma4", "qwen3-vl", "gemma3", "qwen2.5vl", "llama3.2-vision", "minicpm-v", "llava",
             "qwen3", "llama3.2", "llama3.1", "qwen2.5", "mistral", "phi4", "phi3", "gemma2"]
_cache = {"at": 0.0, "url": None, "wanted": None, "info": None}
_lock = threading.Lock()


def _base(settings):
    return settings["ollama_url"].rstrip("/")


def _installed(settings):
    r = httpx.get(_base(settings) + "/api/tags", timeout=5)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]


def _capabilities(settings, model):
    try:
        r = httpx.post(_base(settings) + "/api/show", json={"model": model}, timeout=10)
        r.raise_for_status()
        data = r.json()
        caps = set(data.get("capabilities") or [])
        if not caps:  # older Ollama: guess from the model family
            fams = " ".join((data.get("details") or {}).get("families") or []).lower()
            if "clip" in fams or "mllama" in fams or "vision" in model:
                caps.add("vision")
        return caps
    except Exception:  # noqa: BLE001
        return set()


def resolve(settings, force=False):
    """{"model", "vision", "thinking", "installed", "error"} for the model to use."""
    wanted = settings["ollama_model"].strip()
    with _lock:
        c = _cache
        if (not force and c["info"] and time.time() - c["at"] < 60
                and c["url"] == settings["ollama_url"] and c["wanted"] == wanted):
            return c["info"]
        info = {"model": None, "vision": False, "thinking": False, "installed": [], "error": None}
        try:
            names = _installed(settings)
            info["installed"] = names
            chosen = None
            if wanted and wanted != "auto":
                for n in names:  # "llama3.2" matches "llama3.2:latest"
                    if n == wanted or n == wanted + ":latest" or n.split(":")[0] == wanted:
                        chosen = n
                        break
                if not chosen:  # same family, other size/tag ("llama3.2:3b" -> "llama3.2:latest")
                    chosen = next((n for n in names if n.split(":")[0] == wanted.split(":")[0]), None)
                if not chosen:
                    info["error"] = f"model '{wanted}' is not installed, using another one"
            if not chosen:
                for fam in PREFERRED:
                    chosen = next((n for n in names if n.split(":")[0] == fam), None)
                    if chosen:
                        break
                chosen = chosen or (names[0] if names else None)
            if not chosen:
                info["error"] = "no models installed (run: ollama pull qwen3-vl:4b)"
            else:
                caps = _capabilities(settings, chosen)
                info.update(model=chosen, vision="vision" in caps, thinking="thinking" in caps)
        except Exception as e:  # noqa: BLE001
            info["error"] = f"Ollama is not running ({type(e).__name__})"
        _cache.update(at=time.time(), url=settings["ollama_url"], wanted=wanted, info=info)
        return info


debug_log = threading.local()  # raw answers of the current clip, saved to ai_debug.txt


def _log_raw(label, text):
    if not hasattr(debug_log, "entries"):
        debug_log.entries = []
    debug_log.entries.append(f"===== {label} =====\n{text}\n")


def take_debug():
    entries = getattr(debug_log, "entries", [])
    debug_log.entries = []
    return "\n".join(entries)


def generate(settings, prompt, images=None, fmt=None, timeout=150, temperature=0.8, num_predict=1500):
    """Returns the model's answer text. Raises RuntimeError with a readable reason.
    fmt: None, "json" or a JSON schema dict (Ollama structured outputs)."""
    info = resolve(settings)
    if not info["model"]:
        raise RuntimeError(info["error"] or "no Ollama model")
    low = settings["low_memory"]
    body = {"model": info["model"], "prompt": prompt, "stream": False, "keep_alive": "2m" if low else "15m",
            "options": {"temperature": temperature, "num_ctx": 8192 if low else 12288, "num_predict": num_predict}}
    if fmt:
        body["format"] = fmt
    if images and info["vision"]:
        body["images"] = images
    if info["thinking"]:
        body["think"] = False  # we want the answer, not minutes of reasoning
    r = httpx.post(_base(settings) + "/api/generate", json=body, timeout=timeout)
    if r.status_code == 404:
        resolve(settings, force=True)
        raise RuntimeError(f"Ollama doesn't have model '{info['model']}'")
    if r.status_code == 400 and "think" in r.text and body.pop("think", None) is not None:
        r = httpx.post(_base(settings) + "/api/generate", json=body, timeout=timeout)  # model can't toggle thinking
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:150]}")
    data = r.json()
    text = data.get("response") or ""
    if not text.strip() and data.get("thinking"):
        text = data["thinking"]  # some thinking models put everything in the thinking field
    return text


def unload(settings):
    """Tells Ollama to free the model's memory now (GPU + RAM)."""
    model = (_cache.get("info") or {}).get("model")
    if not model:
        return
    try:
        httpx.post(_base(settings) + "/api/generate", json={"model": model, "keep_alive": 0}, timeout=15)
    except Exception:  # noqa: BLE001
        pass


def _extract_json(text):
    text = re.sub(r"```(?:json)?", "", text or "")
    try:
        return json.loads(text)
    except ValueError:
        pass
    start = text.find("{")
    while start >= 0:  # first balanced {...} that parses
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def generate_json(settings, prompt, images=None, schema=None, label="request", **kw):
    """Tries structured output (schema), then plain JSON mode, then free text with the
    JSON cut out of it. Small and 'thinking' models often fail one of these."""
    attempts = ([schema] if schema else []) + ["json", None]
    last = ""
    for fmt in attempts:
        p = prompt if fmt else prompt + "\n\nReply with ONLY the JSON object, nothing before or after it."
        try:
            text = generate(settings, p, images, fmt=fmt, **kw)
        except RuntimeError as e:
            if "doesn't have model" in str(e) or "not running" in str(e):
                raise
            last = str(e)
            continue
        _log_raw(f"{label} (format={'schema' if isinstance(fmt, dict) else fmt})", text[:4000])
        data = _extract_json(text)
        if isinstance(data, dict) and data:
            return data
        last = f"unreadable answer: {text.strip()[:80]!r}"
    raise RuntimeError(f"AI answer wasn't usable JSON ({last})")
