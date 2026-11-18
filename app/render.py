"""9:16 layouts and the ffmpeg video filter.

Layouts (1080x1920):
  facecam  streamer webcam on top, gameplay below (needs the webcam box set on the
           streamer's card; falls back to blur without it)
  blur     16:9 video in the middle (slightly zoomed so it's bigger), blurred copy behind it
  crop     video scaled up to fill the whole screen (cuts the sides)

Key moments get a short zoom-in ("zoom punch").
"""
import re
import subprocess

from . import tools

W, H = 1080, 1920


def _even(v):
    return int(round(v / 2)) * 2


def probe(media):
    """(width, height, fps) of the first video stream."""
    p = subprocess.run([tools.ffmpeg(), "-hide_banner", "-i", str(media)], capture_output=True, text=True,
                       errors="replace", creationflags=tools.NO_WINDOW)
    w, h, fps = 1920, 1080, 30.0
    for line in p.stderr.splitlines():
        if "Video:" in line:
            m = re.search(r"[ ,](\d{3,5})x(\d{3,5})[ ,\[]", line)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            m = re.search(r"([\d.]+) fps", line)
            if m:
                fps = float(m.group(1))
            break
    return w, h, min(max(fps, 24.0), 60.0)


def geometry(layout, show_chat, caption_position="above", zoom=1.2, cam_box=None, src=(1920, 1080)):
    """Where the video, captions and chat box go."""
    src_w, src_h = src
    if layout == "facecam" and cam_box:
        x, y, w, h = cam_box
        cam_h = _even(min(860, max(480, W * (h * src_h) / max(1, w * src_w))))
        game_h = H - cam_h
        # title_y: one headline caption sitting on the line between webcam and gameplay
        g = {"kind": "facecam", "cam_h": cam_h, "game_h": game_h, "top_y": cam_h + 115, "title_y": cam_h}
        if show_chat:
            chat_h = 480
            g["chat"] = (40, H - chat_h - 30, 1000, chat_h)
            g["cap_y"] = max(g["top_y"] + 170, H - chat_h - 30 - 95)
        else:
            g["chat"] = None
            g["cap_y"] = cam_h + int(game_h * 0.62)
        return g

    if layout == "crop":
        g = {"kind": "crop", "top_y": 260, "title_y": 560}
        if show_chat:
            g.update(chat=(40, 1260, 1000, 620), cap_y=470 if caption_position == "above" else 1100)
        else:
            g.update(chat=None, cap_y=470 if caption_position == "above" else 1380)
        return g

    zoom = min(1.4, max(1.0, zoom))
    video_h = _even(min(1100, W * src_h * zoom / src_w))
    video_y = _even((H - video_h) / 2)
    bottom = video_y + video_h
    g = {"kind": "blur", "zoom": zoom, "video_h": video_h, "video_y": video_y,
         "top_y": max(150, video_y - 330), "title_y": max(200, video_y - 150)}
    above = video_y - 78
    if show_chat:
        y0 = bottom + 28
        g["chat"] = (40, y0, 1000, max(300, H - 40 - y0))
        g["cap_y"] = bottom - 110 if caption_position == "on_video" else above
    else:
        g["chat"] = None
        g["cap_y"] = {"above": above, "on_video": bottom - 110, "below": bottom + 95}.get(caption_position, above)
    return g


def _punch_expr(times, strength=0.12):
    """zoompan zoom expression: quick zoom in at each time, ease back out."""
    if not times:
        return "1"
    # inside '...' in the filter graph commas don't need escaping
    parts = [f"clip((it-{t - 0.05:.2f})/0.12,0,1)*clip(({t + 0.85:.2f}-it)/0.3,0,1)" for t in times[:4]]
    expr = parts[0]
    for p in parts[1:]:
        expr = f"max({expr},{p})"
    return f"1+{strength}*{expr}"


def _zoompan(times, size, fps):
    if not times:
        return ""
    z = _punch_expr(times)
    w, h = size
    return (f",zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps:g}")


SHARP = "unsharp=5:5:0.55:5:5:0.0"


def video_filter(g, src, cam_box=None, punch_times=None, fps=30.0):
    """ffmpeg filter_complex for input 0 -> [v] (subtitles from captions.ass in the cwd)."""
    src_w, src_h = src
    end = f"{SHARP},ass=captions.ass,format=yuv420p[v]"
    if g["kind"] == "facecam":
        x, y, w, h = cam_box
        cam_h, game_h = g["cam_h"], g["game_h"]
        game = f"[g]scale=-2:{game_h}:flags=lanczos,crop={W}:{game_h}" + _zoompan(punch_times, (W, game_h), fps)
        return (f"[0:v]split=2[c][g];"
                f"[c]crop=w=iw*{w:.4f}:h=ih*{h:.4f}:x=iw*{x:.4f}:y=ih*{y:.4f},"
                f"scale={W}:{cam_h}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{cam_h}[cam];"
                f"{game}[game];[cam][game]vstack=inputs=2,{end}")
    if g["kind"] == "crop":
        return (f"[0:v]scale=-2:{H}:flags=lanczos,crop={W}:{H}" + _zoompan(punch_times, (W, H), fps) + f",{end}")
    zoom, video_h = g["zoom"], g["video_h"]
    return (f"[0:v]split=2[a][b];[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            f"boxblur=10:2,scale={W}:{H},eq=brightness=-0.15:saturation=1.2[bg];"
            f"[b]crop=iw/{zoom:.3f}:ih:(iw-iw/{zoom:.3f})/2:0,scale={W}:{video_h}:flags=lanczos"
            + _zoompan(punch_times, (W, video_h), fps) +
            f"[fg];[bg][fg]overlay=0:{g['video_y']},{end}")


def snapshot(segment_path, out_path):
    """A still frame of the live stream (for picking the webcam box)."""
    tools.run([tools.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(segment_path),
               "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "3", str(out_path)], timeout=30)
