"""Finds the streamer's webcam automatically.

Takes a few frames of the clip and looks for a face that stays in the same place
in most of them (the webcam overlay doesn't move; faces in the game do). Returns a
crop box around that face with the right shape for the top half of a 9:16 clip.
Needs opencv-python-headless; without it, auto detection is skipped.
"""
import logging
import subprocess

from . import tools

log = logging.getLogger("facecam")


def _frames(media, duration, n=6, width=960):
    import numpy as np
    out = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        p = subprocess.run([tools.ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(media),
                            "-frames:v", "1", "-vf", f"scale={width}:-2,format=gray", "-f", "rawvideo", "pipe:1"],
                           capture_output=True, timeout=60, creationflags=tools.NO_WINDOW)
        if p.returncode == 0 and p.stdout:
            h = len(p.stdout) // width
            if h > 0:
                out.append(np.frombuffer(p.stdout[: width * h], dtype=np.uint8).reshape(h, width))
    return out


def detect(media, duration, src_w, src_h, target_aspect=1080 / 820):
    """[x, y, w, h] as fractions of the frame, or None."""
    try:
        import cv2
    except ImportError:
        log.info("opencv not installed, no automatic webcam detection")
        return None
    try:
        frames = _frames(media, duration)
        if len(frames) < 3:
            return None
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        fh, fw = frames[0].shape
        hits = []  # (frame index, x, y, w, h)
        for i, img in enumerate(frames):
            for (x, y, w, h) in cascade.detectMultiScale(img, scaleFactor=1.1, minNeighbors=6,
                                                         minSize=(int(fh * 0.06), int(fh * 0.06))):
                hits.append((i, x + w / 2, y + h / 2, w, h))
        # group detections that stay in about the same place
        groups = []
        for hit in hits:
            for g in groups:
                gx, gy = g["cx"], g["cy"]
                if abs(hit[1] - gx) < fw * 0.06 and abs(hit[2] - gy) < fh * 0.08:
                    g["items"].append(hit)
                    g["cx"] = sum(h[1] for h in g["items"]) / len(g["items"])
                    g["cy"] = sum(h[2] for h in g["items"]) / len(g["items"])
                    break
            else:
                groups.append({"cx": hit[1], "cy": hit[2], "items": [hit]})
        good = [g for g in groups if len({h[0] for h in g["items"]}) >= max(3, len(frames) // 2)]
        if not good:
            return None
        best = max(good, key=lambda g: (len({h[0] for h in g["items"]}), sum(h[3] for h in g["items"])))
        face = sorted(h[4] for h in best["items"])[len(best["items"]) // 2]  # median face height
        cx, cy = best["cx"], best["cy"]
        # webcam overlays are about 2.5 face-heights tall: stay inside them
        crop_h = face * 2.5
        aspect_px = target_aspect  # width / height in pixels
        crop_w = crop_h * aspect_px
        if crop_w > fw:
            crop_w, crop_h = fw, fw / aspect_px
        if crop_h > fh:
            crop_h, crop_w = fh, fh * aspect_px
        x = min(max(0, cx - crop_w / 2), fw - crop_w)
        y = min(max(0, cy - crop_h * 0.45), fh - crop_h)
        box = [round(x / fw, 4), round(y / fh, 4), round(crop_w / fw, 4), round(crop_h / fh, 4)]
        log.info("webcam found at %s (face %d px)", box, face)
        return box
    except Exception as e:  # noqa: BLE001
        log.warning("webcam detection failed: %s", e)
        return None
