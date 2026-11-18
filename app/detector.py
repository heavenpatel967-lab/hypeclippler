"""Finds hype moments from chat speed.

Every message gets a score (1 for normal chat, more for hype words, emotes,
CAPS, subs, bits, super chats). Each second we compare the score of the last
5 seconds to the streamer's normal level (a slow moving average). When chat runs
`sensitivity` times faster than normal, a hype moment starts. When it calms down,
the second with the highest chat speed is the peak, and a clip is made around it.
"""
import re
import time
from collections import deque

HYPE_WORDS = {
    "pog", "poggers", "pogchamp", "pogu", "kekw", "lul", "lmao", "lmfao", "lol", "omegalul",
    "w", "ww", "www", "l", "clip", "clipit", "clip it", "omg", "wtf", "holy", "insane", "no way",
    "noway", "yooo", "letsgo", "goat", "monkas", "pepelaugh", "omegaroll", "lulw",
    "aware", "sadge", "hahaha", "hahahaha", "xd", "wow", "crazy", "wild", "rip", "gg", "ez", "huh",
    "icant", "classic", "sheesh", "kappa", "pepega", "5head", "kekl", "pogcrazy", "erm",
}
HYPE_PHRASE_RE = re.compile(r"\b(clip (it|that)|no way|let'?s go+|holy (shit|moly)|oh my god)\b", re.I)
REPEAT_RE = re.compile(r"(.)\1{3,}")          # "LMAOOOOO", "WWWWW"
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
WORD_RE = re.compile(r"[a-z0-9']+")


def message_score(text: str, kind: str = "chat") -> float:
    if kind == "event":
        return 3.0
    score = 1.0
    low = text.lower()
    words = WORD_RE.findall(low)
    if any(w in HYPE_WORDS for w in words) or HYPE_PHRASE_RE.search(low):
        score += 1.0
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        score += 0.5
    if REPEAT_RE.search(text):
        score += 0.5
    if EMOJI_RE.search(text):
        score += 0.3
    return min(score, 3.0)


class HypeDetector:
    WINDOW = 5.0

    def __init__(self, settings):
        self.settings = settings
        self.started = time.time()
        self.events = deque()            # (ts, score, user, text)
        self.recent_text = deque()       # (ts, user, text, color, kind) kept ~3 minutes for clips
        self.baseline = None             # normal score per second
        self.history = deque(maxlen=180) # (ts, rate) for the dashboard graph
        self.in_hype = False
        self.hype_start = 0.0
        self.peak_ts = 0.0
        self.peak_rate = 0.0
        self.last_clip = 0.0
        self.peak_unique, self.peak_unique_ratio = 0, 0.0
        self.total_messages = 0

    def add(self, user: str, text: str, kind: str = "chat", ts: float | None = None, color: str | None = None):
        ts = ts or time.time()
        self.events.append((ts, message_score(text, kind), user, text))
        self.recent_text.append((ts, user, text, color, kind))
        self.total_messages += 1

    def rate(self, now: float) -> float:
        while self.events and self.events[0][0] < now - self.WINDOW:
            self.events.popleft()
        while self.recent_text and self.recent_text[0][0] < now - 180:
            self.recent_text.popleft()
        return sum(e[1] for e in self.events) / self.WINDOW

    def messages_between(self, start: float, end: float, limit: int = 60):
        """Plain message texts (for titles)."""
        msgs = [m[2] for m in list(self.recent_text) if start <= m[0] <= end]
        return msgs[-limit:]

    def chat_log(self, start: float, end: float):
        """Full messages with times relative to `start`, for overlays and sound effects."""
        return [{"t": round(m[0] - start, 2), "user": m[1], "text": m[2], "color": m[3], "kind": m[4]}
                for m in list(self.recent_text) if start <= m[0] <= end]

    def tick(self, now: float | None = None):
        """Call once per second. Returns a moment dict when a hype moment finished."""
        now = now or time.time()
        rate = self.rate(now)
        self.history.append((now, round(rate, 2)))
        s = self.settings
        if self.baseline is None:
            self.baseline = rate
        warm = now - self.started >= s["warmup_seconds"]
        floor = max(self.baseline, 0.2)
        threshold = max(floor * s["sensitivity"], s["min_msgs_per_sec"])

        if not self.in_hype:
            # learn the normal level only while chat is calm
            alpha = 0.05 if not warm else 0.01
            self.baseline += alpha * (rate - self.baseline)
            if warm and rate >= threshold and now - self.last_clip >= s["cooldown_seconds"]:
                self.in_hype = True
                self.hype_start = now
                self.peak_ts, self.peak_rate = now, rate
                self.peak_unique, self.peak_unique_ratio = 0, 0.0
            return None

        users = {e[2] for e in self.events}
        self.peak_unique = max(self.peak_unique, len(users))
        if rate > self.peak_rate:
            self.peak_ts, self.peak_rate = now, rate
            # share of different people at the loudest second (1.0 = everyone wrote once)
            self.peak_unique_ratio = len(users) / max(1, len(self.events))
        calm = rate < max(floor * 1.4, threshold * 0.55)
        too_long = now - self.hype_start > 45
        if calm or too_long:
            self.in_hype = False
            # a few people spamming isn't a hype moment; small chats need fewer different people
            need = min(s["min_unique_chatters"], max(3, int(self.baseline * 2)))
            if self.peak_unique < need:
                return {"peak_ts": self.peak_ts, "score": round(self.peak_rate / floor, 1),
                        "weak": f"only {self.peak_unique} different chatters (needs {need})"}
            self.last_clip = now
            # many different people reacting counts more than the same people spamming
            crowd = 0.6 + 0.4 * min(1.0, self.peak_unique_ratio / 0.6)
            return {
                "peak_ts": self.peak_ts,
                "hype_start": self.hype_start,
                "score": round(self.peak_rate / floor * crowd, 1),
                "unique_chatters": self.peak_unique,
                "peak_rate": round(self.peak_rate, 1),
                "baseline": round(self.baseline, 2),
            }
        return None

    def snapshot(self):
        now = time.time()
        rate = self.history[-1][1] if self.history else 0.0
        base = max(self.baseline or 0.0, 0.2)
        return {
            "rate": rate,
            "baseline": round(self.baseline or 0.0, 2),
            "hype_level": round(rate / base, 2),
            "in_hype": self.in_hype,
            "warming_up": now - self.started < self.settings["warmup_seconds"],
            "messages": self.total_messages,
            "graph": [r for _, r in list(self.history)[-90:]],
        }
