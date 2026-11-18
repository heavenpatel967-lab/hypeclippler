"""Turns a hype moment into a finished 9:16 clip with captions, hook, title and hashtags."""
import json
import logging
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import captions, scenes, sfx, story, titles, tools
from .config import CLIPS
from .recorder import SEG_SECONDS

log = logging.getLogger("clipper")

# One heavy job (whisper + LLM + encode) at a time so the GPU isn't overloaded.
# Cutting from the buffer happens right away, outside this lock, so a long
# queue never loses video.
_heavy = threading.Semaphore(1)
jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set(job_id, **kw):
    with _jobs_lock:
        jobs[job_id].update(kw)


def list_jobs():
    now = time.time()
    with _jobs_lock:
        for jid in [k for k, j in jobs.items()
                    if j["state"] == "done" or (j["state"] == "failed" and now - j.get("ended", now) > 600)]:
            del jobs[jid]
        return [dict(j) for j in jobs.values()]


def list_clips():
    out = []
    for meta in CLIPS.glob("*/meta.json"):
        try:
            out.append(json.loads(meta.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda m: m.get("created", 0), reverse=True)
    return out


def submit(streamer, moment: dict, settings, manual=False):
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    with _jobs_lock:
        jobs[job_id] = {"id": job_id, "streamer": streamer.channel_name, "platform": streamer.platform,
                        "state": "waiting for video", "score": moment.get("score"), "error": None}
    t = threading.Thread(target=_process, args=(job_id, streamer, moment, settings, manual),
                         name=f"clip-{job_id}", daemon=True)
    t.start()
    return job_id


def _has_audio(media: Path) -> bool:
    p = subprocess.run([tools.ffmpeg(), "-hide_banner", "-i", str(media)], capture_output=True, text=True,
                       errors="replace", creationflags=tools.NO_WINDOW)
    return "Audio:" in p.stderr


def _recent_history(n=12):
    """What the last clips used, so new clips don't repeat the same captions and sounds."""
    out = {"captions": [], "titles": [], "sound_kinds": [], "sound_files": []}
    for meta in reversed(list_clips()[:n]):  # oldest first
        out["captions"] += [c.get("text", "") for c in meta.get("captions") or []]
        out["titles"].append(meta.get("title", ""))
        for snd in meta.get("sfx") or []:
            if snd.get("sound") != "whoosh":
                out["sound_kinds"].append(snd.get("sound"))
            out["sound_files"].append(snd.get("file"))
    return out


def _tag_moods(caps, moments, main_cat):
    """Gives each top caption the mood of the nearest moment (used for its colour)."""
    for i, c in enumerate(caps):
        near = [m for m in moments if -1.5 <= m["t"] - c["time"] <= 3.5]
        c["cat"] = near[0]["cat"] if near else ("setup" if i == 0 else main_cat)


def _render_filter(layout, video_y):
    if layout == "crop":
        return "[0:v]scale=-2:1920,crop=1080:1920,ass=captions.ass,format=yuv420p[v]"
    y = "(H-h)/2" if video_y is None else str(video_y)
    # background: blurred at low resolution (fast), then scaled up
    return ("[0:v]split=2[a][b];[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
            "boxblur=10:2,scale=1080:1920,eq=brightness=-0.12[bg];[b]scale=1080:-2[fg];"
            f"[bg][fg]overlay=(W-w)/2:{y},ass=captions.ass,format=yuv420p[v]")


def _process(job_id, streamer, moment, settings, manual):
    out_dir = CLIPS / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        pre, post = settings["pre_seconds"], settings["post_seconds"]
        t0, t1 = moment["peak_ts"] - pre, moment["peak_ts"] + post
        ready_at = t1 + SEG_SECONDS + 2
        while time.time() < ready_at:
            time.sleep(0.5)
        # chat is collected after the clip window ended, so it covers all of it
        # (plus 15 s before, used to pre-fill the chat box)
        chat_log = streamer.detector.chat_log(t0 - 15, t1)
        chat_log = [{**m, "t": round(m["t"] - 15, 2)} for m in chat_log]
        chat = streamer.detector.messages_between(moment["peak_ts"] - 15, moment["peak_ts"] + 10)
        show_chat = bool(getattr(streamer, "show_chat", False))

        _set(job_id, state="cutting")
        src = out_dir / "source.ts"
        offset = streamer.recorder.export(t0, t1, src)
        duration = t1 - t0
        ff = tools.ffmpeg()
        enc = tools.video_encoder()

        with _heavy:
            wide = out_dir / "clip_16x9.mp4"
            tools.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{offset:.2f}", "-i", str(src),
                       "-t", f"{duration:.2f}", "-map", "0:v:0", "-map", "0:a:0?", *enc,
                       "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(wide)])
            src.unlink(missing_ok=True)

            _set(job_id, state="captioning")
            try:
                words, lang = captions.transcribe(wide, settings)
            except Exception as e:  # noqa: BLE001 - a clip without captions is better than none
                log.warning("captions failed: %s", e)
                words, lang = [], "?"
            transcript = " ".join(w["word"] for w in words)
            if not words:
                log.info("%s: no speech found in clip, no word captions", streamer.channel_name)

            _set(job_id, state="finding key moments")
            peaks = story.audio_peaks(wide, duration)
            in_clip_chat = [m for m in chat_log if m["t"] >= 0]
            moments = story.build_moments(in_clip_chat, words, peaks, duration)
            main_cat = story.main_category(moments)
            topic = streamer.topic or settings["default_topic"]
            recent = _recent_history()

            scenes_found = []
            if settings["vision"]:
                _set(job_id, state="watching the clip (AI vision)")
                frames = scenes.extract_frames(wide, scenes.key_times(moments, duration), out_dir)
                scenes_found = scenes.describe(settings, frames, topic, streamer.channel_name)
                for _, f in frames:
                    f.unlink(missing_ok=True)

            fallback_caps = story.fallback_captions(moments, duration, main_cat, seed=job_id, words=words,
                                                    avoid=recent["captions"], topic=topic)
            available_sounds = sfx.available() if settings["sfx_enabled"] else {}
            _set(job_id, state="writing title + captions")
            meta_text = titles.generate(settings, {
                "streamer": streamer.channel_name, "platform": streamer.platform, "topic": topic,
                "chat": chat, "duration": duration, "main_cat": main_cat, "seed": job_id,
                "timeline": story.timeline_text(moments, words, duration, scenes_found),
                "fallback_captions": fallback_caps, "quote": story.best_quote(words, moments),
                "recent_captions": recent["captions"], "recent_titles": recent["titles"],
                "sound_kinds": {k: sfx.KINDS[k] for k in available_sounds},
                "max_sfx": settings["max_sfx"], "saw_video": bool(scenes_found),
            })
            ai_sounds = meta_text.pop("sounds", None)

            _tag_moods(meta_text["captions"], moments, main_cat)
            sounds = []
            if settings["sfx_enabled"]:
                sounds = story.pick_sfx(moments, duration, available_sounds, settings["max_sfx"],
                                        settings["sfx_intro_whoosh"], seed=job_id,
                                        recent_kinds=recent["sound_kinds"], recent_files=recent["sound_files"],
                                        ai_picks=ai_sounds)

            _set(job_id, state="rendering 9:16")
            layout = settings["layout"]
            geo = captions.geometry(layout, show_chat, settings["caption_position"])
            (out_dir / "captions.ass").write_text(
                captions.build_ass(words, duration, meta_text["captions"], layout, settings["caption_font"],
                                   settings["burn_hook"], chat_log, show_chat, settings["caption_position"],
                                   settings["top_caption_style"], settings["word_color"]),
                encoding="utf-8")
            vf = _render_filter(layout, geo["video_y"])
            has_audio = bool(peaks) or _has_audio(wide)
            plain = ["-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-c:a", "copy"]

            def render(audio_args):
                # cwd=out_dir so the subtitle path has no "C:\" (ffmpeg filter escaping on Windows)
                tools.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", wide.name, *audio_args, *enc,
                           "-movflags", "+faststart", "clip_9x16.mp4"], cwd=out_dir)

            if sounds and has_audio:
                extra_in, af, alabel = sfx.mix_args(sounds, settings["sfx_volume"])
                try:
                    render([*extra_in, "-filter_complex", vf + ";" + af, "-map", "[v]", "-map", alabel,
                            "-c:a", "aac", "-b:a", "192k"])
                except RuntimeError as e:  # a broken sound file shouldn't cost the whole clip
                    log.warning("sound effects failed (%s), rendering without them", e)
                    sounds = []
                    render(plain)
            else:
                sounds = []
                render(plain)
            tools.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{min(pre, duration / 2):.2f}",
                       "-i", "clip_9x16.mp4", "-frames:v", "1", "-vf", "scale=360:-2", "thumb.jpg"], cwd=out_dir)

        meta = {
            "id": job_id,
            "created": time.time(),
            "streamer": streamer.channel_name,
            "platform": streamer.platform,
            "peak_time": datetime.fromtimestamp(moment["peak_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "score": moment.get("score"),
            "manual": manual,
            "duration": round(duration, 1),
            "language": lang,
            "transcript": transcript,
            "chat": chat[-25:],
            "show_chat": show_chat,
            "moments": [{k: m[k] for k in ("t", "cat", "src")} for m in moments],
            "sfx": [{"time": s["time"], "sound": s["sound"], "for": s.get("why") or s["cat"],
                     "file": Path(s["file"]).name} for s in sounds],
            "scenes": scenes_found,
            **meta_text,
            "files": {"vertical": f"/clips/{job_id}/clip_9x16.mp4", "wide": f"/clips/{job_id}/clip_16x9.mp4",
                      "thumb": f"/clips/{job_id}/thumb.jpg"},
        }
        (out_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        _set(job_id, state="done")
        log.info("Clip ready: %s - %s", streamer.channel_name, meta["title"])
    except Exception as e:  # noqa: BLE001
        log.exception("clip %s failed", job_id)
        _set(job_id, state="failed", error=str(e)[:300], ended=time.time())
        for f in out_dir.glob("*"):
            f.unlink(missing_ok=True)
        try:
            out_dir.rmdir()
        except OSError:
            pass


def delete_clip(clip_id: str) -> bool:
    d = (CLIPS / clip_id).resolve()
    if d.parent != CLIPS.resolve() or not d.is_dir():
        return False
    for f in d.glob("*"):
        f.unlink(missing_ok=True)
    d.rmdir()
    return True
