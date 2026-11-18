"""Looks at the clip: grabs frames at the key moments and asks a local vision model
what is happening on screen ("police car rams the player", "he falls off the roof").
Those descriptions feed the captions, title and sound choices."""
import base64
import logging
from pathlib import Path

from . import llm, tools

log = logging.getLogger("scenes")


def key_times(moments, duration, max_frames=6):
    times = [1.0]
    for m in sorted(moments, key=lambda m: -m["strength"]):
        times.append(max(0.5, m["t"]))
    times.append(max(1.0, duration - 3))
    out = []
    for t in sorted(times):
        if all(abs(t - o) >= 3 for o in out):
            out.append(round(t, 1))
    if len(out) > max_frames:  # keep start, end and the strongest moments in between
        middle = [round(max(0.5, m["t"]), 1) for m in sorted(moments, key=lambda m: -m["strength"])]
        keep = {out[0], out[-1]}
        for t in middle:
            if len(keep) >= max_frames:
                break
            if t in out:
                keep.add(t)
        out = sorted(keep)
    return out


def extract_frames(media: Path, times, out_dir: Path):
    frames = []
    for i, t in enumerate(times):
        path = out_dir / f"frame_{i}.jpg"
        try:
            tools.run([tools.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i",
                       str(media), "-frames:v", "1", "-vf", "scale=768:-2", "-q:v", "4", str(path)], timeout=60)
            frames.append((t, path))
        except Exception as e:  # noqa: BLE001
            log.debug("frame at %.1fs failed: %s", t, e)
    return frames


def describe(settings, frames, topic, streamer):
    """[{time, what}] or [] if no vision model is available."""
    info = llm.resolve(settings)
    if not info["model"] or not info["vision"] or not frames:
        return []
    images = [base64.b64encode(p.read_bytes()).decode() for _, p in frames]
    listing = "\n".join(f"image {i + 1}: {t:.1f}s" for i, (t, _) in enumerate(frames))
    prompt = f"""These {len(frames)} images are frames from a {topic or "video game"} live stream clip by {streamer},
in time order:
{listing}
For each image, describe in one short concrete sentence what is happening: the action, characters,
vehicles, places, and any big on-screen text (like WASTED, KNOCKOUT, BUSTED, a kill feed or a score).
The small webcam box in a corner is the streamer; mention his facial expression if visible.
Return JSON: {{"frames": [{{"time": seconds, "what": "..."}}]}}"""
    try:
        data = llm.generate_json(settings, prompt, images=images, temperature=0.3, timeout=180)
        out = []
        for i, f in enumerate(data.get("frames") or []):
            what = str(f.get("what", "")).strip()
            if what:
                t = f.get("time", frames[min(i, len(frames) - 1)][0])
                try:
                    t = float(t)
                except (TypeError, ValueError):
                    t = frames[min(i, len(frames) - 1)][0]
                out.append({"time": round(t, 1), "what": what[:200]})
        if out:
            return out
    except Exception as e:  # noqa: BLE001
        log.info("batch frame description failed (%s), trying one by one", e)
    out = []  # some small models only handle one image at a time
    for t, p in frames[:4]:
        try:
            text = llm.generate(settings, f"One short concrete sentence: what is happening in this frame of a "
                                          f"{topic or 'video game'} live stream? Mention big on-screen text.",
                                images=[base64.b64encode(p.read_bytes()).decode()], temperature=0.3, timeout=90)
            if text.strip():
                out.append({"time": t, "what": text.strip().split("\n")[0][:200]})
        except Exception as e:  # noqa: BLE001
            log.info("frame description failed: %s", e)
            break
    return out
