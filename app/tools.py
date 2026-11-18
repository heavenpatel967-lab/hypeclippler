"""Finds ffmpeg / streamlink and checks if the NVIDIA encoder works."""
import functools
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("tools")

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


@functools.lru_cache
def ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        raise RuntimeError("ffmpeg not found. Install it (Windows: winget install Gyan.FFmpeg) and restart.")


@functools.lru_cache
def streamlink() -> list[str]:
    exe = Path(sys.executable).parent / ("streamlink.exe" if os.name == "nt" else "streamlink")
    if exe.exists():
        return [str(exe)]
    found = shutil.which("streamlink")
    if found:
        return [found]
    return [sys.executable, "-m", "streamlink"]


@functools.lru_cache
def video_encoder() -> list[str]:
    """h264_nvenc on NVIDIA cards (your 3070), libx264 otherwise."""
    test = [ffmpeg(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=black:s=320x240:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        if subprocess.run(test, capture_output=True, timeout=30, creationflags=NO_WINDOW).returncode == 0:
            log.info("Using NVIDIA NVENC for video encoding")
            return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", "19", "-b:v", "0",
                    "-maxrate", "20M", "-bufsize", "40M", "-spatial-aq", "1", "-profile:v", "high"]
    except Exception:  # noqa: BLE001
        pass
    log.info("NVENC not available, using libx264 (CPU)")
    return ["-c:v", "libx264", "-preset", "faster", "-crf", "19", "-profile:v", "high"]


def run(cmd, timeout=600, cwd=None):
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                       timeout=timeout, cwd=cwd, creationflags=NO_WINDOW)
    if p.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {p.stderr.strip()[-800:]}")
    return p
