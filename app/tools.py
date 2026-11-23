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


def run(cmd, timeout=600, cwd=None, max_wait=1800):
    """Runs a command. If Windows is out of memory (WinError 8) it frees memory and waits
    until there is room, for up to max_wait seconds, instead of failing the clip."""
    import time
    from . import resources
    name = Path(cmd[0]).name
    start = time.time()
    while True:
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                               timeout=timeout, cwd=cwd, creationflags=NO_WINDOW)
        except OSError as e:
            if not resources.is_oom(e) or time.time() - start > max_wait:
                raise RuntimeError(f"{name} could not start: {e}") from e
            log.warning("out of memory starting %s, freeing memory and waiting", name)
            resources.wait_until_runnable(name, max_wait=max(60, max_wait - (time.time() - start)))
            continue
        if p.returncode != 0:
            err = p.stderr.strip()[-800:]
            if resources.is_oom(err) and time.time() - start < max_wait:
                log.warning("%s ran out of memory, freeing memory and waiting", name)
                resources.wait_until_runnable(name, max_wait=max(60, max_wait - (time.time() - start)))
                continue
            raise RuntimeError(f"{name} failed: {err}")
        return p


def grab_frame(media, t, out, width=768, cwd=None, duration=None):
    """Saves one JPG frame. Uses full-range colours (the JPG encoder rejects the video's
    limited range on some ffmpeg builds: 'Error while opening encoder') and, if the time
    is past the end of the video (no frame written), tries earlier times.
    Returns True when the file exists."""
    out = Path(out)
    target = out if (out.is_absolute() or cwd is None) else Path(cwd) / out
    tries = [t, t - 1.0, 0.5] if t > 1.5 else [max(0.0, t), 0.0]
    if duration:
        tries = [min(x, max(0.0, duration - 0.6)) for x in tries]
    for x in tries:
        target.unlink(missing_ok=True)
        try:
            run([ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, x):.2f}", "-i", str(media),
                 "-frames:v", "1", "-vf", f"scale={width}:-2:flags=lanczos,format=yuvj420p", "-q:v", "3",
                 "-strict", "unofficial", str(out)], timeout=60, cwd=cwd, max_wait=120)
        except RuntimeError as e:
            log.debug("frame at %.1fs failed: %s", x, e)
            continue
        if target.exists() and target.stat().st_size > 0:
            return True
    return False
