"""One worker per streamer: chat reader + rolling recorder + hype detector."""
import asyncio
import logging
import re
import time

from . import clipper
from .chat import READERS
from .detector import HypeDetector
from .recorder import Recorder

log = logging.getLogger("manager")


def parse_channel(text: str, platform: str | None = None):
    """'xqc', 'twitch.tv/xqc', 'https://kick.com/adinross', '@MrBeast', youtube URLs..."""
    text = text.strip()
    low = text.lower()
    if "twitch.tv/" in low:
        return "twitch", re.split(r"twitch\.tv/", text, flags=re.I)[1].split("/")[0].split("?")[0].lower()
    if "kick.com/" in low:
        return "kick", re.split(r"kick\.com/", text, flags=re.I)[1].split("/")[0].split("?")[0].lower()
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube", text
    if text.startswith("@") and platform in ("twitch", "kick"):
        text = text[1:]
    elif text.startswith("@"):
        return "youtube", text
    if not platform:
        raise ValueError("pick a platform or paste the full channel URL")
    return platform, (text.lower() if platform in ("twitch", "kick") else text)


def make_key(platform, channel):
    return f"{platform}_" + re.sub(r"[^a-zA-Z0-9_-]+", "_", channel)[-60:].strip("_")


def display_name(platform, channel):
    if platform == "youtube":
        m = re.search(r"@([\w.-]+)", channel)
        return m.group(1) if m else channel.rsplit("/", 1)[-1][:30]
    return channel


class StreamerWorker:
    def __init__(self, platform, channel, settings, topic="", auto_clip=True, show_chat=False, facecam=None):
        self.platform, self.channel, self.settings = platform, channel, settings
        self.key = make_key(platform, channel)
        self.channel_name = display_name(platform, channel)
        self.topic = topic
        self.auto_clip = auto_clip
        self.show_chat = show_chat  # draw the live chat box under the video in clips
        self.facecam = facecam      # webcam box [x, y, w, h] (0-1) for the facecam layout
        self.detector = HypeDetector(settings)
        self.chat_status = "starting"
        self.video_status = "starting"
        self.recorder = Recorder(self.key, platform, channel, settings, self._video_status)
        self.stop_event = asyncio.Event()
        self.tasks = []
        self.clips_made = 0
        self.hype_scores = []

    def _video_status(self, s):
        self.video_status = s

    def _chat_status(self, s):
        self.chat_status = s

    def _on_message(self, user, text, kind, color=None):
        self.detector.add(user, text, kind, color=color)

    def start(self):
        self.recorder.start()
        reader = READERS[self.platform]
        self.tasks = [asyncio.create_task(reader(self.channel, self._on_message, self.stop_event, self._chat_status)),
                      asyncio.create_task(self._tick_loop())]

    async def stop(self):
        self.stop_event.set()
        self.recorder.stop()
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.recorder.clear()

    async def _tick_loop(self):
        while not self.stop_event.is_set():
            await asyncio.sleep(1)
            moment = self.detector.tick()
            if moment and self.auto_clip and not self._worth_it(moment):
                continue
            if moment and self.auto_clip:
                if not self.recorder.live:
                    log.info("%s: hype detected but video is not recording, skipped", self.channel_name)
                    continue
                log.info("%s: HYPE x%s (%.1f/s vs normal %.2f/s)", self.channel_name, moment["score"],
                         moment["peak_rate"], moment["baseline"])
                clipper.submit(self, moment, self.settings)
                self.clips_made += 1

    def _worth_it(self, moment):
        """'Best moments only': skip hype that is weaker than what this stream usually gets."""
        scores = self.hype_scores
        scores.append(moment["score"])
        del scores[:-30]
        s = self.settings
        if moment["score"] < s["min_hype_score"]:
            log.info("%s: hype x%s below minimum x%s, skipped", self.channel_name, moment["score"], s["min_hype_score"])
            return False
        if s["best_moments_only"] and len(scores) >= 6:
            ranked = sorted(scores)
            cut = ranked[int(len(ranked) * 0.6)]  # must beat 60% of this stream's recent hype moments
            if moment["score"] < cut:
                log.info("%s: hype x%s weaker than usual (needs x%s), skipped", self.channel_name,
                         moment["score"], cut)
                return False
        return True

    def clip_now(self):
        """Manual clip: the last ~30 seconds plus 10 seconds after pressing."""
        moment = {"peak_ts": time.time() - 10, "score": "manual"}
        self.clips_made += 1
        return clipper.submit(self, moment, self.settings, manual=True)

    def info(self):
        return {"key": self.key, "platform": self.platform, "channel": self.channel, "name": self.channel_name,
                "topic": self.topic, "auto_clip": self.auto_clip, "show_chat": self.show_chat, "facecam": self.facecam, "chat_status": self.chat_status,
                "video_status": self.video_status, "live": self.recorder.live, "clips_made": self.clips_made,
                **self.detector.snapshot()}

    def config(self):
        return {"platform": self.platform, "channel": self.channel, "topic": self.topic, "auto_clip": self.auto_clip,
                "show_chat": self.show_chat, "facecam": self.facecam}


class Manager:
    def __init__(self, settings):
        self.settings = settings
        self.workers: dict[str, StreamerWorker] = {}

    async def start(self):
        for s in self.settings["streamers"]:
            try:
                await self.add(s["channel"], s.get("platform"), s.get("topic", ""), s.get("auto_clip", True),
                               s.get("show_chat", False), s.get("facecam"),
                               save=False)
            except Exception as e:  # noqa: BLE001
                log.warning("could not start %s: %s", s, e)

    async def add(self, channel, platform=None, topic="", auto_clip=True, show_chat=False, facecam=None, save=True):
        platform, channel = parse_channel(channel, platform)
        if platform not in READERS:
            raise ValueError(f"unknown platform {platform}")
        key = make_key(platform, channel)
        if key in self.workers:
            raise ValueError("already watching this streamer")
        w = StreamerWorker(platform, channel, self.settings, topic, auto_clip, show_chat, facecam)
        self.workers[key] = w
        w.start()
        log.info("Watching %s on %s", w.channel_name, platform)
        if save:
            self._save()
        return w

    async def remove(self, key):
        w = self.workers.pop(key, None)
        if w:
            await w.stop()
            self._save()
        return w is not None

    def update(self, key, topic=None, auto_clip=None, show_chat=None, facecam=False):
        w = self.workers[key]
        if topic is not None:
            w.topic = topic
        if auto_clip is not None:
            w.auto_clip = bool(auto_clip)
        if show_chat is not None:
            w.show_chat = bool(show_chat)
        if facecam is not False:  # None clears it
            w.facecam = facecam
        self._save()

    def _save(self):
        self.settings.set_streamers([w.config() for w in self.workers.values()])

    async def shutdown(self):
        await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)
