"""Settings stored in data/config.json. Edited from the dashboard."""
import copy
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CLIPS = DATA / "clips"
BUFFERS = DATA / "buffers"
STATIC = Path(__file__).resolve().parent / "static"
CONFIG_FILE = DATA / "config.json"

DEFAULTS = {
    # clip window around the chat peak
    "pre_seconds": 20,
    "post_seconds": 20,
    # how many seconds of each stream are kept on disk (must be > pre + post)
    "buffer_seconds": 300,       # also how long "Clip anyway" works for skipped moments
    # hype detection
    "sensitivity": 2.5,          # chat must be this many times faster than normal
    "min_msgs_per_sec": 1.0,     # ignore spikes in tiny chats below this speed
    "cooldown_seconds": 90,      # min gap between two auto clips of the same streamer
    "warmup_seconds": 60,        # learn the normal chat speed before clipping
    "min_unique_chatters": 6,    # different people reacting at the peak (stops spam from triggering)
    "min_hype_score": 2.5,       # final hype score needed for an auto clip
    "best_moments_only": True,   # skip moments much weaker than this stream's recent hype
    "best_moments_percentile": 35,  # must beat this % of the stream's recent hype moments
    "min_clip_score": 4,         # 0-10 virality score from the AI (or the app) needed to keep a clip, 0 = keep all
    "keep_low_score": True,      # keep low-score clips for review instead of deleting them
    "always_keep_hype": 8.0,     # chat hype this strong is always kept, whatever the AI says
    "fast_when_queue_over": 3,   # skip AI vision when more clips than this are waiting
    "smart_trim": True,          # record a wider window and let the AI cut the best part
    "min_clip_seconds": 30,      # shortest clip the cut may produce
    "max_clip_seconds": 58,      # longest (Shorts limit is 60 s)
    # recording
    "quality": "auto",           # auto = 1080p60 for up to 4 streamers, 720p60 above that
    "low_memory": True,          # free Whisper/AI memory between steps, smaller AI context (stops WinError 8)
    "min_free_ram_mb": 3000,     # clip work waits until this much memory is free
    "max_queue": 15,             # clips waiting at once; weaker new ones are skipped when full
    # output
    "layout": "facecam",         # facecam = webcam on top + gameplay below (when a webcam box is set),
                                 # blur = 16:9 video on blurred background, crop = fill screen
    "auto_facecam": True,        # find the webcam automatically when no box is drawn
    "no_facecam_layout": "crop", # layout when no webcam is found: crop (fill screen) or blur
    "caption_mode": "title",     # title = one headline for the whole clip, story = changing captions
    "chat_box_enabled": False,   # master switch for the chat box under the video
    "output_fps": 30,            # 30 = smoother on a busy PC and smaller files, 60 = smoother motion
    "video_zoom": 1.2,           # blur layout: crop the sides so the video is bigger (1.0-1.4)
    "zoom_punch": True,          # quick zoom-in on the key moments
    "caption_position": "above",  # word captions: above | on_video | below the video
    "burn_hook": True,
    "word_captions": False,       # word-by-word captions of what the streamer says
    "top_caption_style": "tiktok",  # title mode: tiktok | impact; story mode: pop | box | classic
    "word_color": "yellow",       # yellow | green | red | cyan | pink           # burn the timed story captions at the top of the clip
    # sound effects (sfx/<kind>/ folders)
    "sfx_enabled": True,
    "sfx_volume": 0.8,
    "max_sfx": 3,                # per clip, not counting the intro whoosh
    "sfx_intro_whoosh": True,
    "caption_font": "Arial Black",
    "default_topic": "GTA 6",
    # captions (local, free)
    "whisper_model": "large-v3-turbo",
    "whisper_device": "auto",    # auto | cuda | cpu
    # titles (local, free)
    "ollama_url": "http://localhost:11434",
    "ollama_model": "auto",       # auto = best installed model (vision models first)
    "vision": True,               # let a vision model look at frames of the clip
    # saving and uploading
    "auto_download": False,          # copy every finished clip to the download folder
    "download_folder": "",           # empty = Videos/HypeClipper in your user folder
    "download_subfolders": "none",   # none | streamer | date | streamer_date
    "download_16x9": False,          # also save the 16:9 version
    "delete_after_download": False,  # remove the clip from the dashboard once it's saved
    "auto_upload_youtube": False,
    "auto_upload_instagram": False,
    "auto_upload_min_score": 7,      # only clips the AI scored this high are uploaded automatically
    "upload_gap_minutes": 20,        # minimum time between two automatic uploads per platform
    "yt_privacy": "public",          # public | unlisted | private
    # web dashboard
    "host": "127.0.0.1",
    "port": 8000,
    "streamers": [],
}


# when a default changes, saved configs that still have the OLD default get the new one
MIGRATIONS = {"min_clip_score": [5], "min_hype_score": [3.0], "max_queue": [6], "buffer_seconds": [180],
              "quality": ["1080p60,1080p,720p60,best", "720p60,720p,720p30,best"]}


class Settings:
    def __init__(self):
        for d in (DATA, CLIPS, BUFFERS):
            d.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        if CONFIG_FILE.exists():
            try:
                saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                for key, olds in MIGRATIONS.items():
                    if saved.get(key) in olds:
                        saved[key] = DEFAULTS[key]
                self.data.update(saved)
            except (OSError, ValueError):
                pass
        self.save()

    def __getitem__(self, key):
        with self._lock:
            return self.data.get(key, DEFAULTS.get(key))

    def public(self):
        with self._lock:
            return {k: v for k, v in self.data.items() if k != "streamers"}

    def update(self, values: dict):
        with self._lock:
            for key, value in values.items():
                if key not in DEFAULTS or key == "streamers":
                    continue
                default = DEFAULTS[key]
                try:
                    if isinstance(default, bool):
                        value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                    elif isinstance(default, int):
                        value = int(float(value))
                    elif isinstance(default, float):
                        value = float(value)
                    else:
                        value = str(value)
                except (TypeError, ValueError):
                    continue
                self.data[key] = value
            self.save()

    def set_streamers(self, streamers):
        with self._lock:
            self.data["streamers"] = streamers
            self.save()

    def save(self):
        with self._lock:
            tmp = CONFIG_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            tmp.replace(CONFIG_FILE)
