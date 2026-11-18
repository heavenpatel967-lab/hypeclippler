"""Works out *what happens when* inside a clip, so the top captions and sound
effects line up with the action.

Three signals are combined:
  1. chat reactions: bursts of laughing / hype / shock / fail / scared / bruh messages
  2. what the streamer says (Whisper words): "no way", "let's go", "bruh", "haha"...
  3. loud moments in the audio (screams, explosions, crashes)

Chat reacts a few seconds after the action, so chat bursts are moved back in time
and snapped to the nearest loud audio moment.
"""
import logging
import random
import re
import subprocess
from collections import Counter

from . import tools

log = logging.getLogger("story")

CHAT_DELAY = 4.0  # seconds chat usually lags behind the action

CATEGORIES = {
    "laugh": re.compile(r"\b(kekw|kekl|lul|lulw|lmao+|lmfao+|omegalul|icant|pepelaugh|haha\w*|xd+|dead|crying)\b|😂|🤣|💀", re.I),
    "hype": re.compile(r"\b(pog\w*|letsgo+|lets go+|let'?s go+|goat|sheesh|insane|clip (it|that)|banger|huge|gg)\b|🔥", re.I),
    "shock": re.compile(r"\b(wtf|no ?way|omg|holy|what+|huh+|wait|wow|bro what)\b|\?{2,}|😱|😳|😮", re.I),
    "fail": re.compile(r"\b(rip|sadge|unlucky|wasted|busted|ratio|copium|nooo+)\b|😭", re.I),
    "suspense": re.compile(r"\b(monkas|monkaw|scary|run+|hide|uh oh|careful|behind you)\b|😬|😰", re.I),
    "bruh": re.compile(r"\b(bruh+|erm|ummm*|why|cringe|weird|sus)\b|🤨|🙄", re.I),
}

SPEECH = [
    (re.compile(r"\b(no way|oh my god|what the|holy)\b", re.I), "shock"),
    (re.compile(r"\b(let'?s go+|yes+|let's f\w*)\b", re.I), "hype"),
    (re.compile(r"\b(bruh|bro what)\b", re.I), "bruh"),
    (re.compile(r"\b(oh no|i'?m dead|i died|wasted|busted|arrested|i'?m done|nooo+)\b", re.I), "fail"),
    (re.compile(r"\b(ha(ha)+|hehe)\b", re.I), "laugh"),
    (re.compile(r"\b(run|go go|hide|they'?re coming|behind me)\b", re.I), "suspense"),
]

# which sound folders fit each kind of moment, best first
SFX_FOR = {
    "laugh": ["laugh", "boom"],
    "hype": ["airhorn", "boom"],
    "shock": ["boom", "wow"],
    "fail": ["fail", "bruh"],
    "suspense": ["suspense"],
    "bruh": ["bruh", "boom"],
    "loud": ["boom"],
}

TOP_LINES = {
    "setup": ["Wait for it...", "Watch this", "He had no idea...", "Keep watching", "This is where it starts"],
    "setup_fail": ["This is about to go wrong", "He really thought he had it", "Nobody warned him..."],
    "laugh": ["Chat lost it", "He can't stop laughing", "I can't breathe", "Chat is crying", "This is too funny"],
    "hype": ["LET'S GOOO", "Chat went crazy", "Absolute W", "That was insane", "Chat is going nuts"],
    "shock": ["NO WAY", "Did that just happen?", "Nobody expected this", "Wait WHAT", "Everyone froze"],
    "fail": ["It all went wrong", "Instant regret", "Massive L", "That did NOT work", "Oh no..."],
    "suspense": ["Uh oh...", "Things got tense", "He's in trouble now", "RUN"],
    "bruh": ["Bruh moment", "Why would he do that", "Chat is confused", "What was that?"],
    "loud": ["WHAT JUST HAPPENED", "He's screaming", "That was loud"],
}


SPAM_TOKENS = [(re.compile(r"^w+$", re.I), "hype"), (re.compile(r"^l+$", re.I), "fail"),
               (re.compile(r"^f+$", re.I), "fail")]


def categorize(text: str):
    cats = [c for c, rx in CATEGORIES.items() if rx.search(text)]
    tokens = text.split()
    if tokens:  # messages that are only "W W W", "LLLL", "F"
        for rx, cat in SPAM_TOKENS:
            if all(rx.match(t) for t in tokens) and cat not in cats:
                cats.append(cat)
    return cats


# ----------------------------------------------------------------- signals
def chat_reactions(chat, duration):
    """Bursts of each reaction type, in chat time."""
    secs = int(duration) + 1
    counts = {c: [0] * secs for c in CATEGORIES}
    samples = {c: [[] for _ in range(secs)] for c in CATEGORIES}
    for m in chat:
        s = int(m["t"])
        if not 0 <= s < secs:
            continue
        for c in categorize(m["text"]):
            counts[c][s] += 1
            samples[c][s].append(m["text"])
    biggest = max([max(v) for v in counts.values()] + [1])
    out = []
    for c, series in counts.items():
        smooth = [sum(series[max(0, i - 1):i + 2]) for i in range(secs)]
        thresh = max(3, 0.35 * biggest * 2)
        last = -99
        for i, v in enumerate(smooth):
            if v < thresh or i - last < 5:
                continue
            if v >= max(smooth[max(0, i - 2):i + 3]):
                texts = [t for j in range(max(0, i - 1), min(secs, i + 2)) for t in samples[c][j]]
                out.append({"t": float(i), "cat": c, "strength": v / max(thresh, 1),
                            "sample": Counter(t.strip()[:30] for t in texts).most_common(1)[0][0] if texts else ""})
                last = i
    return out


def speech_triggers(words):
    out = []
    for i, w in enumerate(words):
        pair = " ".join(x["word"] for x in words[i:i + 3])
        for rx, cat in SPEECH:
            if rx.match(pair.lower().strip(" ,.!?")):
                out.append({"t": w["start"], "cat": cat, "strength": 1.2, "sample": pair})
                break
    return out


def audio_peaks(media, duration):
    """Loud moments (screams, explosions). Returns [(time, strength)]."""
    try:
        import numpy as np
        p = subprocess.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(media), "-vn",
                            "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
                           capture_output=True, timeout=120, creationflags=tools.NO_WINDOW)
        pcm = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)
        if pcm.size < 8000:
            return []
        hop = 400  # 50 ms
        n = pcm.size // hop
        rms = np.sqrt((pcm[: n * hop].reshape(n, hop) ** 2).mean(axis=1)) + 1.0
        med = float(np.median(rms))
        out, last = [], -99.0
        for i in range(2, n - 2):
            t = i * hop / 8000
            if rms[i] > 3.0 * med and rms[i] == rms[max(0, i - 20):i + 20].max() and t - last > 2:
                out.append((t, float(rms[i] / med)))
                last = t
        return out
    except Exception as e:  # noqa: BLE001
        log.debug("audio analysis failed: %s", e)
        return []


def _snap(t, peaks, lo, hi):
    near = [(abs(pt - t), pt) for pt, _ in peaks if t + lo <= pt <= t + hi]
    return min(near)[1] if near else None


def build_moments(chat, words, peaks, duration):
    """Merged, time-ordered list of moments: {t (action time), cat, strength, sample}."""
    moments = []
    for r in chat_reactions(chat, duration):
        guess = max(0.3, r["t"] - CHAT_DELAY)
        snapped = _snap(guess, peaks, -4, 3)
        moments.append({**r, "t": round(snapped if snapped is not None else guess, 2), "src": "chat"})
    for s in speech_triggers(words):
        moments.append({**s, "src": "speech"})
    used = {m["t"] for m in moments}
    for t, strength in peaks:
        if strength > 5 and all(abs(t - u) > 2 for u in used):
            moments.append({"t": round(t, 2), "cat": "loud", "strength": strength / 5, "sample": "", "src": "audio"})
    moments.sort(key=lambda m: m["t"])
    return [m for m in moments if 0.3 <= m["t"] <= duration - 0.5]


# -------------------------------------------------------------- top captions
def fallback_captions(moments, duration, main_cat, seed=None):
    rnd = random.Random(seed)
    setup = TOP_LINES["setup_fail" if main_cat == "fail" else "setup"]
    caps = [{"time": 0.0, "text": rnd.choice(setup)}]
    used_lines = set()
    for m in sorted(moments, key=lambda m: -m["strength"])[:8]:
        t = max(1.5, m["t"] - 0.3)
        if any(abs(t - c["time"]) < 3.5 for c in caps):
            continue
        sample = m.get("sample", "").strip()
        if m["src"] == "chat" and sample and len(sample) <= 18 and rnd.random() < 0.35:
            line = f'Chat: "{sample}"'
        else:
            pool = [x for x in TOP_LINES.get(m["cat"], TOP_LINES["shock"]) if x not in used_lines] or TOP_LINES["shock"]
            line = rnd.choice(pool)
        used_lines.add(line)
        caps.append({"time": round(t, 2), "text": line})
    caps.sort(key=lambda c: c["time"])
    # long gap with nothing happening: add a line so the top never feels frozen.
    # Before the biggest moment it teases, after it it keeps the reaction going.
    peak_t = max(moments, key=lambda m: m["strength"])["t"] if moments else duration / 2
    after_pool = [x for x in TOP_LINES.get(main_cat, TOP_LINES["hype"]) if x not in used_lines] \
        or ["Chat will never recover"]
    filled = []
    for i, c in enumerate(caps):
        filled.append(c)
        nxt = caps[i + 1]["time"] if i + 1 < len(caps) else duration
        if nxt - c["time"] > 12:
            t = round(c["time"] + (nxt - c["time"]) / 2, 2)
            text = rnd.choice(["Keep watching...", "It gets worse", "Wait for it..."]) if t < peak_t \
                else rnd.choice(after_pool)
            filled.append({"time": t, "text": text})
    return filled


def clean_captions(raw, duration, fallback):
    """Validates captions coming from the AI. Falls back if they're unusable."""
    caps = []
    for c in raw if isinstance(raw, list) else []:
        try:
            t = float(c.get("time", c.get("start", 0)))
            text = str(c.get("text", "")).strip().strip('"')
        except (AttributeError, TypeError, ValueError):
            continue
        if text and 0 <= t < duration - 1 and len(text) <= 45:
            caps.append({"time": round(t, 2), "text": text})
    caps.sort(key=lambda c: c["time"])
    spaced = []
    for c in caps:
        if not spaced or c["time"] - spaced[-1]["time"] >= 2.5:
            spaced.append(c)
    if len(spaced) < 3:
        return fallback
    spaced[0]["time"] = 0.0
    return spaced[:8]


def timeline_text(moments, words, duration):
    """Short timeline for the AI prompt."""
    lines = []
    chunk, start = [], None
    for w in words:
        if start is None:
            start = w["start"]
        chunk.append(w["word"])
        if w["end"] - start > 3.5:
            lines.append((start, f'streamer says: "{" ".join(chunk)}"'))
            chunk, start = [], None
    if chunk:
        lines.append((start, f'streamer says: "{" ".join(chunk)}"'))
    for m in moments:
        what = {"chat": "chat reacts", "speech": "streamer reacts", "audio": "very loud moment"}[m["src"]]
        lines.append((m["t"], f"{what}: {m['cat']}" + (f' ("{m["sample"]}")' if m.get("sample") else "")))
    lines.sort()
    return "\n".join(f"{t:5.1f}s  {text}" for t, text in lines[:60]) + f"\nclip length: {duration:.0f}s"


def main_category(moments):
    if not moments:
        return "hype"
    score = Counter()
    for m in moments:
        score[m["cat"]] += m["strength"]
    return score.most_common(1)[0][0]


# ------------------------------------------------------------- sound effects
def pick_sfx(moments, duration, available: dict, max_sfx: int, intro_whoosh=True):
    """available = {"boom": [paths], ...}. Returns [{time, sound, file, cat}]."""
    rnd = random.Random(int(duration * 1000))
    picks = []
    if intro_whoosh and available.get("whoosh"):
        picks.append({"time": 0.0, "sound": "whoosh", "file": rnd.choice(available["whoosh"]), "cat": "intro"})
    for m in sorted(moments, key=lambda m: -m["strength"]):
        if len([p for p in picks if p["cat"] != "intro"]) >= max_sfx:
            break
        if any(abs(m["t"] - p["time"]) < 5 for p in picks if p["cat"] != "intro") or m["t"] < 1:
            continue
        for sound in SFX_FOR.get(m["cat"], ["boom"]):
            if available.get(sound):
                lead = 1.6 if sound == "suspense" else 0.0   # risers should end on the action
                picks.append({"time": round(max(0.0, m["t"] - lead), 2), "sound": sound,
                              "file": rnd.choice(available[sound]), "cat": m["cat"]})
                break
    picks.sort(key=lambda p: p["time"])
    return picks
