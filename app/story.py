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
import os
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

TOP_LINES = {
    "setup": ["He had no idea...", "Watch his face", "This was supposed to be easy", "He thought he was safe",
              "It started so normal", "Look at the corner", "Chat saw it first", "He was so confident",
              "Just a normal day in {topic}", "Everything was fine...", "One wrong move"],
    "setup_fail": ["This is about to go wrong", "He really thought he had it", "Nobody warned him...",
                   "Confidence: 100%", "What could go wrong?", "He was SO sure"],
    "laugh": ["He can't breathe", "Chat is in tears", "I'm crying", "This is too funny", "He's dying laughing",
              "Not the laugh", "Chat can't recover", "Funniest thing today"],
    "hype": ["LET'S GOOO", "Absolute W", "He actually did it", "Clean", "Chat is going nuts",
             "Built different", "He's HIM", "Nobody does it like him"],
    "shock": ["NO WAY", "Did that just happen?", "Wait WHAT", "Everyone froze", "He's speechless",
              "Look at his face", "Chat is shaking", "This can't be real"],
    "fail": ["Instant regret", "Massive L", "That did NOT work", "Oh no...", "It's over", "Pain",
             "He's never living this down", "Worst timing ever", "Plan: failed"],
    "suspense": ["Uh oh...", "Don't look behind you", "He's in trouble now", "RUN", "Heart rate: 200",
                 "Too close"],
    "bruh": ["Bruh", "Why would he do that", "Chat is confused", "What was that?", "Make it make sense",
             "Explain this"],
    "loud": ["His ears", "Volume warning", "He lost it", "Chat felt that", "He's yelling"],
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
FILLERS = {"um", "uh", "like", "the", "a", "and", "so", "yeah", "okay", "ok", "i", "you", "it"}


def quote_near(words, t, before=2.5, after=1.5, max_words=6):
    """The streamer's own words around time t, cleaned up for a caption."""
    near = [w["word"] for w in words if t - before <= w["start"] <= t + after]
    text = re.sub(r"[^\w' ?!]", "", " ".join(near)).strip()
    parts = text.split()
    while parts and parts[0].lower() in FILLERS:
        parts.pop(0)
    if len(parts) < 2:
        return None
    return " ".join(parts[-max_words:])


def best_quote(words, moments):
    """The streamer's most dramatic line in the clip (for titles)."""
    for m in sorted(moments, key=lambda m: -m["strength"]):
        q = quote_near(words, m["t"])
        if q:
            return q
    return None


def fallback_captions(moments, duration, main_cat, seed=None, words=None, avoid=(), topic=""):
    rnd = random.Random(seed)
    words = words or []
    avoid = {a.lower() for a in avoid}

    def pick(pool):
        fresh = [x for x in pool if x.lower() not in avoid and x.lower() not in used]
        line = rnd.choice(fresh or pool)
        used.add(line.lower())
        return line.replace("{topic}", topic or "this game")

    used = set()
    setup = TOP_LINES["setup_fail" if main_cat == "fail" else "setup"]
    caps = [{"time": 0.0, "text": pick(setup), "cat": "setup"}]
    for m in sorted(moments, key=lambda m: -m["strength"])[:8]:
        t = max(1.5, m["t"] - 0.3)
        if any(abs(t - c["time"]) < 3.5 for c in caps):
            continue
        quote = quote_near(words, m["t"]) if m["src"] in ("speech", "audio") or rnd.random() < 0.5 else None
        sample = m.get("sample", "").strip()
        if quote and len(quote) <= 32 and quote.lower() not in used:
            line = f'"{quote}"'
            used.add(quote.lower())
        elif m["src"] == "chat" and sample and len(sample) <= 18 and rnd.random() < 0.3:
            line = f'Chat: "{sample}"'
        else:
            line = pick(TOP_LINES.get(m["cat"], TOP_LINES["shock"]))
        caps.append({"time": round(t, 2), "text": line, "cat": m["cat"]})
    caps.sort(key=lambda c: c["time"])
    # long gap with nothing happening: add a line so the top never feels frozen.
    peak_t = max(moments, key=lambda m: m["strength"])["t"] if moments else duration / 2
    filled = []
    for i, c in enumerate(caps):
        filled.append(c)
        nxt = caps[i + 1]["time"] if i + 1 < len(caps) else duration
        if nxt - c["time"] > 12:
            t = round(c["time"] + (nxt - c["time"]) / 2, 2)
            pool = ["Keep watching...", "It gets worse", "Here it comes", "Watch closely"] if t < peak_t \
                else TOP_LINES.get(main_cat, TOP_LINES["hype"])
            filled.append({"time": t, "text": pick(pool), "cat": main_cat})
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


def timeline_text(moments, words, duration, scenes=None):
    """Short timeline for the AI prompt."""
    lines = [(sc["time"], f"on screen: {sc['what']}") for sc in scenes or []]
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
    return "\n".join(f"{t:5.1f}s  {text}" for t, text in lines[:70]) + f"\nclip length: {duration:.0f}s"


def main_category(moments):
    if not moments:
        return "hype"
    score = Counter()
    for m in moments:
        score[m["cat"]] += m["strength"]
    return score.most_common(1)[0][0]


# ------------------------------------------------------------- sound effects
def _choose_file(files, recent_files, rnd):
    fresh = [f for f in files if os.path.basename(f) not in recent_files]
    return rnd.choice(fresh or files)


def pick_sfx(moments, duration, available: dict, max_sfx: int, intro_whoosh=True, sfx_for=None, seed=None,
             recent_kinds=(), recent_files=(), ai_picks=None):
    """available = {"boom": {"files": [...], "own": bool}, ...}. Returns [{time, sound, file, cat}].

    If the AI chose sounds (ai_picks = [{time, sound}]), those are used. Otherwise a fitting
    sound is picked per moment. Either way: no kind twice in one clip, kinds and files used
    in the last clips are avoided, and folders with your own sounds are preferred."""
    from .sfx import SFX_FOR
    sfx_for = sfx_for or SFX_FOR
    rnd = random.Random(seed if seed is not None else int(duration * 1000))
    recent_kinds, recent_files = list(recent_kinds), set(recent_files)
    picks, used = [], set()
    if intro_whoosh and available.get("whoosh"):
        picks.append({"time": 0.0, "sound": "whoosh", "cat": "intro",
                      "file": _choose_file(available["whoosh"]["files"], recent_files, rnd)})
    if ai_picks:
        for p in ai_picks:
            if p["sound"] in available and p["sound"] not in used:
                used.add(p["sound"])
                lead = 1.6 if p["sound"] == "suspense" else 0.0
                picks.append({"time": round(max(0.0, p["time"] - lead), 2), "sound": p["sound"], "cat": "ai",
                              "why": p.get("why", ""),
                              "file": _choose_file(available[p["sound"]]["files"], recent_files, rnd)})
        if len(picks) > (1 if intro_whoosh else 0):
            picks.sort(key=lambda p: p["time"])
            return picks
    for m in sorted(moments, key=lambda m: -m["strength"]):
        if len([p for p in picks if p["cat"] != "intro"]) >= max_sfx:
            break
        if any(abs(m["t"] - p["time"]) < 5 for p in picks if p["cat"] != "intro") or m["t"] < 1:
            continue
        options = [k for k in sfx_for.get(m["cat"], ["boom"]) if k in available and k != "whoosh"]
        options = [k for k in options if k not in used] or options
        if not options:
            continue
        # weight: own sounds x3, kinds used in recent clips count less (the more recent, the less)
        weights = []
        for k in options:
            w = 3.0 if available[k]["own"] else 1.0
            if k in recent_kinds:
                w *= 0.15 + 0.85 * (recent_kinds[::-1].index(k) / max(1, len(recent_kinds)))
            weights.append(w)
        sound = rnd.choices(options, weights=weights, k=1)[0]
        used.add(sound)
        lead = 1.6 if sound == "suspense" else 0.0   # risers should end on the action
        picks.append({"time": round(max(0.0, m["t"] - lead), 2), "sound": sound, "cat": m["cat"],
                      "file": _choose_file(available[sound]["files"], recent_files, rnd)})
    picks.sort(key=lambda p: p["time"])
    return picks


# ------------------------------------------------------------- chat analysis
def _norm(text):
    t = re.sub(r"\s+", " ", text.strip().lower())
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)          # "lmaooooo" -> "lmaoo"
    words = t.split()
    if len(words) > 1 and len(set(words)) == 1:    # "KEKW KEKW KEKW" -> "kekw"
        t = words[0]
    return t[:40]


def chat_digest(chat, moments, duration):
    """What chat did during the clip, as text for the AI plus numbers for scoring."""
    if not chat:
        return "(no chat messages)", {"messages": 0, "unique": 0, "peak_unique_5s": 0, "spread": 0.0}
    users = {m["user"] for m in chat}
    counts = Counter(_norm(m["text"]) for m in chat if m["text"].strip())
    # chat speed per 2 seconds as a tiny graph the AI can read
    buckets = [0] * (int(duration // 2) + 1)
    for m in chat:
        i = int(m["t"] // 2)
        if 0 <= i < len(buckets):
            buckets[i] += 1
    top = max(buckets) or 1
    bars = " ".join(f"{i * 2}s:{'#' * max(1, round(8 * b / top)) if b else '.'}" for i, b in enumerate(buckets))
    peak_unique = 0
    for s in range(0, int(duration)):
        peak_unique = max(peak_unique, len({m["user"] for m in chat if s <= m["t"] < s + 5}))
    # what different people wrote: one line per distinct idea, longest/most "telling" first
    telling = []
    for m in chat:
        t = m["text"].strip()
        if 12 <= len(t) <= 120 and not re.fullmatch(r"(\S+)( \1)*", t):
            telling.append((m["t"], m["user"], t))
    seen, notable = set(), []
    for t, u, text in telling:
        key = _norm(text)[:25]
        if key not in seen:
            seen.add(key)
            notable.append(f"{t:5.1f}s {u}: {text}")
    step = max(1, len(notable) // 30)
    around = []
    for m in sorted(moments, key=lambda m: -m["strength"])[:3]:
        lines = [f"{c['t']:5.1f}s {c['user']}: {c['text'][:80]}" for c in chat if m["t"] <= c["t"] <= m["t"] + 7]
        around.append(f"right after {m['t']:.1f}s: " + " | ".join(lines[:12]))
    text = (f"{len(chat)} messages from {len(users)} different people; up to {peak_unique} people in 5 seconds.\n"
            f"chat speed: {bars}\n"
            f"most repeated: " + ", ".join(f'"{k}" x{v}' for k, v in counts.most_common(8)) + "\n"
            "what chatters said (sample):\n  " + "\n  ".join(notable[::step][:30]) + "\n"
            + "\n".join(around))
    spread = len(users) / max(1, len(chat))
    return text, {"messages": len(chat), "unique": len(users), "peak_unique_5s": peak_unique, "spread": spread}


def heuristic_score(hype_score, metrics, moments, words):
    """0-10 'is this worth posting' score when no AI is available."""
    try:
        hype = float(hype_score)
    except (TypeError, ValueError):
        hype = 4.0
    score = min(4.5, hype * 0.8)
    score += min(2.0, metrics.get("peak_unique_5s", 0) / 12)
    cats = {m["cat"] for m in moments}
    score += 1.0 if any(m["src"] == "speech" for m in moments) else 0
    score += 1.0 if any(m["src"] == "audio" for m in moments) else 0
    score += 0.5 if len(cats) >= 2 else 0
    score += 0.5 if len(words) > 15 else 0
    return round(max(1.0, min(10.0, score)), 1)


def smart_window(moments, words, duration, ai=None, min_len=15.0, max_len=55.0):
    """Best part of the recorded window: (start, end) in seconds.
    Uses the AI's choice when it makes sense, otherwise starts ~10 s before the first strong
    moment and ends ~7 s after the last one. Never cuts in the middle of a word."""
    strong = [m for m in moments if m["strength"] >= 1.0] or moments
    if ai:
        try:
            s, e = float(ai[0]), float(ai[1])
            if 0 <= s < e <= duration + 1 and min_len <= e - s <= max_len + 5 and \
                    any(s <= m["t"] <= e for m in strong or [{"t": (s + e) / 2}]):
                start, end = s, min(e, duration)
            else:
                ai = None
        except (TypeError, ValueError, IndexError):
            ai = None
    if not ai:
        if not strong:
            return 0.0, duration
        start = max(0.0, strong[0]["t"] - 10.0)
        end = min(duration, strong[-1]["t"] + 7.0)
    if end - start < min_len:  # grow evenly
        pad = (min_len - (end - start)) / 2
        start, end = max(0.0, start - pad), min(duration, end + pad)
        if end - start < min_len:
            start, end = (0.0, min(duration, min_len)) if start == 0 else (max(0.0, duration - min_len), duration)
    if end - start > max_len:
        end = start + max_len
    # don't start or end in the middle of a word
    for w in words:
        if w["start"] < start < w["end"]:
            start = w["start"] - 0.1
        if w["start"] < end < w["end"]:
            end = w["end"] + 0.15
    return round(max(0.0, start), 2), round(min(duration, end), 2)


def shift(items, offset, end, key="t"):
    """Moves timed items by -offset and drops the ones outside [0, end]."""
    out = []
    for it in items:
        t = it[key] - offset
        if 0 <= t <= end:
            out.append({**it, key: round(t, 2)})
    return out
