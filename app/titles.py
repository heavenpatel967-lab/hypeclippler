"""Titles, hooks, descriptions and hashtags.

Uses a local Ollama model (free, unlimited). If Ollama isn't running, builds them
from the chat messages and transcript instead, so clipping never stops.
"""
import json
import logging
import re
from collections import Counter

import httpx

log = logging.getLogger("titles")

STOP = set("""a an the and or but is are was were be been to of in on at for with it its this that
i you he she we they me my your our their im u ur so just like what who how why when do does did
not no yes yeah oh ok okay lol get got go going have has had can cant dont will would could should
there here from up out about all if then than too very really one his her him them us chat""".split())

EMOTE_HOOKS = [
    (re.compile(r"\b(kekw|lul|lulw|lmao|lmfao|omegalul|icant|hahaha+|xd)\b", re.I), "Chat could not stop laughing"),
    (re.compile(r"\b(pog|poggers|pogu|pogchamp|letsgo|lets go+|w+)\b", re.I), "Chat went absolutely crazy"),
    (re.compile(r"\b(wtf|no way|noway|holy|omg|insane|what)\b", re.I), "Nobody expected this"),
    (re.compile(r"\b(monkas|scary|jumpscare)\b", re.I), "This got scary fast"),
    (re.compile(r"\b(l+|rip|sadge|f)\b", re.I), "This went so wrong"),
]


def _fallback(streamer, platform, topic, chat, transcript, score):
    joined = " ".join(chat)
    hook = "Chat went absolutely crazy"
    best = 0
    for rx, text in EMOTE_HOOKS:
        n = len(rx.findall(joined))
        if n > best:
            best, hook = n, text
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z0-9']{2,}", transcript.lower()) if w not in STOP]
    key = Counter(words).most_common(1)
    topic_part = f" in {topic}" if topic else ""
    title = f"{streamer} {hook.lower()}{topic_part}"
    if key:
        title = f"{streamer} {hook.lower()}{topic_part} ({key[0][0]})"
    tags = ["#" + re.sub(r"\W", "", streamer.lower()), f"#{platform}", "#shorts", "#clips", "#viral"]
    if topic:
        tags.insert(1, "#" + re.sub(r"\W", "", topic.lower()))
    return {
        "title": title[:95],
        "hook": hook,
        "description": f"{streamer}'s chat exploded at this moment{topic_part}! Hype score {score}x.\n"
                       f"Watch {streamer} live on {platform.title()}.",
        "hashtags": tags,
        "source": "template",
    }


def generate(settings, streamer, platform, topic, chat, transcript, score):
    prompt = f"""You write titles for viral YouTube Shorts and Instagram Reels made from live stream clips.
Streamer: {streamer} ({platform})
Game/topic: {topic or "unknown"}
What the streamer said in the clip: "{transcript[:1200]}"
Live chat messages at the peak moment: {json.dumps(chat[-40:], ensure_ascii=False)[:1500]}

Return JSON with keys:
"title": catchy Shorts title under 70 characters, includes the streamer name, no clickbait lies,
"hook": 2 to 6 word on-screen hook text that makes people keep watching, no emojis,
"description": 1-2 sentence description,
"hashtags": list of 5-8 hashtags with # (include #shorts and the streamer and game).
Only JSON."""
    try:
        r = httpx.post(f"{settings['ollama_url'].rstrip('/')}/api/generate", timeout=90, json={
            "model": settings["ollama_model"], "prompt": prompt, "format": "json",
            "stream": False, "options": {"temperature": 0.8}, "keep_alive": "10m"})
        r.raise_for_status()
        data = json.loads(r.json()["response"])
        tags = data.get("hashtags") or []
        if isinstance(tags, str):
            tags = tags.split()
        tags = [t if t.startswith("#") else "#" + t for t in map(str, tags)][:10]
        out = {
            "title": str(data.get("title", "")).strip()[:100],
            "hook": str(data.get("hook", "")).strip()[:60],
            "description": str(data.get("description", "")).strip(),
            "hashtags": tags,
            "source": "ollama",
        }
        if out["title"]:
            fb = _fallback(streamer, platform, topic, chat, transcript, score)
            return {k: v or fb[k] for k, v in out.items()}
    except Exception as e:  # noqa: BLE001
        log.info("Ollama not available (%s); using template titles", type(e).__name__)
    return _fallback(streamer, platform, topic, chat, transcript, score)
