"""Talks to the local Ollama server.

Picks a model automatically from what you have installed, so a wrong model name
(Ollama answers "404 Not Found") never silently turns the AI off. Vision models
(they can look at video frames) are preferred.
"""
import json
import logging
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


def generate(settings, prompt, images=None, as_json=False, timeout=150, temperature=0.8):
    """Returns the model's text. Raises RuntimeError with a readable reason."""
    info = resolve(settings)
    if not info["model"]:
        raise RuntimeError(info["error"] or "no Ollama model")
    body = {"model": info["model"], "prompt": prompt, "stream": False, "keep_alive": "15m",
            "options": {"temperature": temperature, "num_ctx": 8192}}
    if as_json:
        body["format"] = "json"
    if images and info["vision"]:
        body["images"] = images
    if info["thinking"]:
        body["think"] = False  # we want the answer, not minutes of reasoning
    r = httpx.post(_base(settings) + "/api/generate", json=body, timeout=timeout)
    if r.status_code == 404:
        resolve(settings, force=True)
        raise RuntimeError(f"Ollama doesn't have model '{info['model']}'")
    if r.status_code >= 400:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:150]}")
    return r.json().get("response", "")


def generate_json(settings, prompt, images=None, **kw):
    text = generate(settings, prompt, images, as_json=True, **kw)
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise RuntimeError("Ollama didn't return JSON")
