"""Keeps the last few minutes of a live stream on disk as short .ts chunks.

streamlink pulls the live video, ffmpeg splits it (no re-encoding, almost no CPU)
into ~4 second files named after the wall-clock time they started. This rolling
buffer lets us cut "20 seconds before the chat peak" after the peak happened.
"""
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import tools
from .config import BUFFERS

log = logging.getLogger("recorder")
SEG_SECONDS = 4
NAME_FMT = "seg_%Y%m%d-%H%M%S.ts"


def stream_url(platform: str, channel: str) -> str:
    if channel.startswith("http"):
        return channel
    if platform == "twitch":
        return f"https://www.twitch.tv/{channel}"
    if platform == "kick":
        return f"https://kick.com/{channel}"
    handle = channel if channel.startswith("@") else "@" + channel
    return f"https://www.youtube.com/{handle}/live"


def _seg_time(path: Path) -> float:
    return datetime.strptime(path.name, NAME_FMT).timestamp()


class Recorder:
    def __init__(self, key: str, platform: str, channel: str, settings, status):
        self.key, self.platform, self.channel = key, platform, channel
        self.settings, self.status = settings, status
        self.dir = BUFFERS / key
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stop_event = threading.Event()
        self.procs = []
        self.live = False
        self.thread = threading.Thread(target=self._loop, name=f"rec-{key}", daemon=True)
        self.count_streamers = lambda: 1  # set by the manager

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self._kill()

    def _kill(self):
        for p in self.procs:
            try:
                p.kill()
            except OSError:
                pass
        self.procs = []

    def quality(self):
        """'auto': 1080p for up to 4 streamers, 720p for more (bandwidth, disk and RAM)."""
        q = self.settings["quality"]
        if q != "auto":
            return q
        return "1080p60,1080p,720p60,best" if self.count_streamers() <= 4 else "720p60,720p,720p30,best"

    def gaps(self, window=120):
        """Seconds of video missing in the last `window` seconds (internet or PC too slow)."""
        segs = [t for t, _ in self.segments() if t > time.time() - window]
        missing = 0.0
        for a, b in zip(segs, segs[1:]):
            if b - a > SEG_SECONDS * 2:
                missing += b - a - SEG_SECONDS
        return round(missing, 1)

    def _source(self, attempt: int) -> str:
        url = stream_url(self.platform, self.channel)
        if self.platform == "kick" and attempt % 2 == 1:
            # fallback when the streamlink Kick plugin is blocked: ask Kick's API for the HLS url
            try:
                from .chat import kick_channel_info
                info = kick_channel_info(self.channel)
                if info.get("playback_url"):
                    return "hls://" + info["playback_url"]
            except Exception as e:  # noqa: BLE001
                log.debug("kick api fallback failed: %s", e)
        return url

    def _loop(self):
        attempt = 0
        while not self.stop_event.is_set():
            self.live = False
            src = self._source(attempt)
            sl = tools.streamlink() + ["--stdout", "--loglevel", "error", "--hls-live-edge", "3",
                                       "--stream-segment-threads", "1", src, self.quality()]
            ff = [tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                  "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
                  # timestamps keep running across chunks, so joined chunks play as one stream
                  "-segment_time", str(SEG_SECONDS), "-strftime", "1",
                  str(self.dir / NAME_FMT)]
            started = time.time()
            errlog = self.dir / "streamlink.log"
            try:
                with open(errlog, "wb") as errf:
                    p1 = subprocess.Popen(sl, stdout=subprocess.PIPE, stderr=errf,
                                          creationflags=tools.NO_WINDOW)
                p2 = subprocess.Popen(ff, stdin=p1.stdout, stderr=subprocess.DEVNULL,
                                      creationflags=tools.NO_WINDOW)
                p1.stdout.close()
                self.procs = [p1, p2]
                self.status("connecting to video")
                while not self.stop_event.is_set() and p2.poll() is None:
                    if not self.live and self.latest_segment_time():
                        if time.time() - self.latest_segment_time() < 15:
                            self.live = True
                            attempt = 0
                            self.status("live, recording")
                    self._cleanup()
                    time.sleep(1)
                p1.wait(timeout=10)
                err = errlog.read_text(errors="replace").strip()
            except Exception as e:  # noqa: BLE001
                err = str(e)
            self._kill()
            if self.stop_event.is_set():
                break
            self.live = False
            ran = time.time() - started
            if "No playable streams" in err or "offline" in err.lower() or ran < 20:
                self.status("offline, checking every 30s" + (f" ({err.splitlines()[-1][:80]})" if err else ""))
                attempt += 1
                self.stop_event.wait(30)
            else:
                self.status("video dropped, reconnecting")
                self.stop_event.wait(3)

    # -------------------------------------------------------------- buffer
    def segments(self):
        out = []
        for p in sorted(self.dir.glob("seg_*.ts")):
            try:
                out.append((_seg_time(p), p))
            except ValueError:
                continue
        return out

    def latest_segment_time(self):
        segs = self.segments()
        return segs[-1][0] if segs else None

    def _cleanup(self):
        keep = self.settings["buffer_seconds"]
        cutoff = time.time() - max(keep, self.settings["pre_seconds"] + self.settings["post_seconds"] + 60)
        for t, p in self.segments()[:-2]:
            if t < cutoff:
                try:
                    p.unlink()
                except OSError:
                    pass

    def export(self, t0: float, t1: float, out_dir: Path):
        """Copies the chunks covering [t0, t1] into out_dir and writes an ffmpeg concat list.
        The concat reader rebuilds clean, continuous timestamps (plain joining caused frozen
        video after reconnects/ads). Copies are used because the buffer is cleaned up while
        clips wait in the queue. Returns (list_path, offset of t0, seconds missing)."""
        segs = self.segments()
        chosen = []
        for i, (start, path) in enumerate(segs):
            end = segs[i + 1][0] if i + 1 < len(segs) else path.stat().st_mtime
            if end > t0 and start < t1:
                chosen.append((start, path))
        if not chosen:
            raise RuntimeError("no recorded video for that moment (stream offline or buffer too short)")
        missing = sum(max(0.0, b[0] - a[0] - SEG_SECONDS * 1.5) for a, b in zip(chosen, chosen[1:]))
        names = []
        for i, (_, path) in enumerate(chosen):
            name = f"part_{i:03d}.ts"
            shutil.copyfile(path, out_dir / name)
            names.append(name)
        lst = out_dir / "parts.txt"
        lst.write_text("".join(f"file '{n}'\n" for n in names), encoding="utf-8")
        return lst, max(0.0, t0 - chosen[0][0]), round(missing, 1)

    def clear(self):
        shutil.rmtree(self.dir, ignore_errors=True)
