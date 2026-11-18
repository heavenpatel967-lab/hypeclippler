"""Turns a hype moment into a finished 9:16 clip with captions, hook, title and hashtags."""
import json
import logging
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import captions, facecam, llm, render, resources, scenes, sfx, story, titles, tools
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
                    if j["state"] == "done" or (j["state"] in ("failed", "skipped")
                                                and now - j.get("ended", now) > 600)]:
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


def _score(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 99.0  # manual clips


def submit(streamer, moment: dict, settings, manual=False, force=False):
    job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    with _jobs_lock:
        waiting = [j for j in jobs.values() if j["state"] not in ("done", "failed", "skipped")]
        if not manual and not force and len(waiting) >= settings["max_queue"]:
            weakest = min((_score(j.get("score")) for j in waiting), default=0)
            if _score(moment.get("score")) <= weakest:
                streamer.record_skip(moment, f"queue full ({len(waiting)} clips waiting)")
                jobs[job_id] = {"id": job_id, "streamer": streamer.channel_name, "platform": streamer.platform,
                                "state": "skipped", "score": moment.get("score"), "ended": time.time(),
                                "error": f"queue full ({len(waiting)} clips waiting), stronger moments first"}
                return job_id
        jobs[job_id] = {"id": job_id, "streamer": streamer.channel_name, "platform": streamer.platform,
                        "state": "waiting for video", "score": moment.get("score"), "error": None}
    t = threading.Thread(target=_process, args=(job_id, streamer, moment, settings, manual, force),
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


def _shift_words(words, offset, end):
    return [{**w, "start": round(w["start"] - offset, 3), "end": round(w["end"] - offset, 3)}
            for w in words if 0 <= w["start"] - offset <= end]


def _shift_captions(caps, offset, end):
    """Captions after trimming. The last caption before the cut becomes the opening one."""
    before = [c for c in caps if c["time"] - offset < 0.5]
    inside = [{**c, "time": round(c["time"] - offset, 2)} for c in caps if 0.5 <= c["time"] - offset < end - 1]
    if before:
        inside.insert(0, {**before[-1], "time": 0.0})
    elif inside:
        inside[0]["time"] = 0.0
    return inside


def _skip(job_id, out_dir, reason):
    log.info("clip %s skipped: %s", job_id, reason)
    _set(job_id, state="skipped", error=reason, ended=time.time())
    for f in out_dir.glob("*"):
        f.unlink(missing_ok=True)
    try:
        out_dir.rmdir()
    except OSError:
        pass


def _backlog():
    with _jobs_lock:
        return sum(1 for j in jobs.values() if j["state"] not in ("done", "failed", "skipped"))


def _process(job_id, streamer, moment, settings, manual, force=False):
    out_dir = CLIPS / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        pre, post = settings["pre_seconds"], settings["post_seconds"]
        trim = settings["smart_trim"] and not manual
        extra_pre, extra_post = (15, 10) if trim else (0, 0)  # wider recording, AI cuts the best part
        min_len = float(settings["min_clip_seconds"])
        max_len = float(max(settings["max_clip_seconds"], min_len + 5))
        t0 = moment["peak_ts"] - pre - extra_pre
        t1 = moment["peak_ts"] + post + extra_post
        ready_at = t1 + SEG_SECONDS + 2
        while time.time() < ready_at:
            time.sleep(0.5)
        # chat is collected after the window ended, so it covers all of it
        # (plus 15 s before, used to pre-fill the chat box)
        chat_log = streamer.detector.chat_log(t0 - 15, t1)
        chat_log = [{**m, "t": round(m["t"] - 15, 2)} for m in chat_log]
        chat = streamer.detector.messages_between(moment["peak_ts"] - 15, moment["peak_ts"] + 10)
        show_chat = bool(getattr(streamer, "show_chat", False)) and settings["chat_box_enabled"]

        _set(job_id, state="cutting")
        parts_list, offset, missing = streamer.recorder.export(t0, t1, out_dir)
        if missing > 3:
            log.warning("%s: %.0fs of video missing in this clip (internet/PC too slow)", streamer.channel_name, missing)
        duration = t1 - t0
        out_fps = str(settings["output_fps"])
        # constant frame rate + audio synced to it: no frozen video when the stream had gaps
        smooth = ["-fps_mode", "cfr", "-r", out_fps, "-af", "aresample=async=1:first_pts=0"]
        ff = tools.ffmpeg()
        enc = tools.video_encoder()

        _set(job_id, state="queued (one clip is processed at a time)")
        with _heavy:
            free = resources.wait_for_memory(
                settings["min_free_ram_mb"],
                lambda mb: _set(job_id, state=f"waiting for free memory ({mb / 1024:.1f} GB free)"))
            if free is not None and free < settings["min_free_ram_mb"]:
                log.warning("still only %d MB free after waiting, trying anyway", free)
            low = settings["low_memory"]
            full = out_dir / "full.mp4"
            tools.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt",
                       "-f", "concat", "-safe", "0", "-ss", f"{offset:.2f}", "-i", parts_list.name,
                       "-t", f"{duration:.2f}", "-map", "0:v:0", "-map", "0:a:0?", *smooth, *enc,
                       "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", full.name], cwd=out_dir)
            for f in out_dir.glob("part_*.ts"):
                f.unlink(missing_ok=True)
            parts_list.unlink(missing_ok=True)

            _set(job_id, state="captioning")
            try:
                if low:
                    llm.unload(settings)  # Whisper and the AI model take turns on the 8 GB GPU
                words, lang = captions.transcribe(full, settings)
            except Exception as e:  # noqa: BLE001 - a clip without captions is better than none
                log.warning("captions failed: %s", e)
                words, lang = [], "?"
            if not words:
                log.info("%s: no speech found in clip, no word captions", streamer.channel_name)

            _set(job_id, state="analyzing chat + audio")
            peaks = story.audio_peaks(full, duration)
            in_clip_chat = [m for m in chat_log if m["t"] >= 0]
            moments = story.build_moments(in_clip_chat, words, peaks, duration)
            main_cat = story.main_category(moments)
            digest, chat_metrics = story.chat_digest(in_clip_chat, moments, duration)
            topic = streamer.topic or settings["default_topic"]
            recent = _recent_history()

            scenes_found = []
            busy = _backlog() > settings["fast_when_queue_over"]
            if busy:
                log.info("%d clips waiting: skipping AI vision for this one to catch up", _backlog())
            if settings["vision"] and not busy:
                _set(job_id, state="watching the clip (AI vision)")
                if low:
                    captions.unload()
                frames = scenes.extract_frames(full, scenes.key_times(moments, duration, 4 if low else 6),
                                               out_dir, 640 if low else 768)
                scenes_found = scenes.describe(settings, frames, topic, streamer.channel_name)
                for _, f in frames:
                    f.unlink(missing_ok=True)

            fallback_caps = story.fallback_captions(moments, duration, main_cat, seed=job_id, words=words,
                                                    avoid=recent["captions"], topic=topic)
            available_sounds = sfx.available() if settings["sfx_enabled"] else {}
            _set(job_id, state="AI judging + writing captions")
            meta_text = titles.generate(settings, {
                "streamer": streamer.channel_name, "platform": streamer.platform, "topic": topic,
                "chat": chat, "chat_digest": digest, "duration": duration, "main_cat": main_cat,
                "seed": job_id, "hype_score": moment.get("score"), "trim": trim,
                "min_len": min_len, "max_len": max_len,
                "timeline": story.timeline_text(moments, words, duration, scenes_found),
                "fallback_captions": fallback_caps, "quote": story.best_quote(words, moments),
                "recent_captions": recent["captions"], "recent_titles": recent["titles"],
                "sound_kinds": {k: sfx.KINDS[k] for k in available_sounds},
                "max_sfx": settings["max_sfx"], "saw_video": bool(scenes_found),
            })
            (out_dir / "ai_debug.txt").write_text(llm.take_debug() or "(AI not used)", encoding="utf-8")
            if low:
                captions.unload()
                llm.unload(settings)  # free the GPU and RAM for the render
            ai_sounds = meta_text.pop("sounds", None)
            ai_window = meta_text.pop("window", None)

            # ---- is it worth posting?
            app_score = story.heuristic_score(moment.get("score"), chat_metrics, moments, words)
            score = meta_text.get("ai_score") or app_score
            low_score = None
            # small AI models are often too strict: a huge chat reaction or a high app score
            # keeps the clip even when the AI scored it low
            strong_chat = _score(moment.get("score")) >= settings["always_keep_hype"]
            if (not manual and not force and settings["min_clip_score"] and score < settings["min_clip_score"]
                    and app_score < settings["min_clip_score"] + 2 and not strong_chat):
                why = meta_text.get("score_reason") or "not enough happening"
                low_score = f"score {score}/10 < {settings['min_clip_score']}: {why}"
                if not settings["keep_low_score"]:
                    _skip(job_id, out_dir, low_score)
                    return
                log.info("low score clip kept for review: %s", low_score)

            # ---- cut the best part
            start, end = (story.smart_window(moments, words, duration, ai_window, min_len, max_len)
                          if trim else (0.0, duration))
            clip_len = end - start
            wide = out_dir / "clip_16x9.mp4"
            tools.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.2f}", "-i", str(full),
                       "-t", f"{clip_len:.2f}", "-map", "0:v:0", "-map", "0:a:0?", "-fps_mode", "cfr",
                       "-r", out_fps, *enc, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(wide)])
            full.unlink(missing_ok=True)
            words = _shift_words(words, start, clip_len)
            moments = story.shift(moments, start, clip_len)
            scenes_found = story.shift(scenes_found, start, clip_len, key="time")
            chat_log = [{**m, "t": round(m["t"] - start, 2)} for m in chat_log
                        if -15 <= m["t"] - start <= clip_len]
            meta_text["captions"] = _shift_captions(meta_text["captions"], start, clip_len) or \
                story.fallback_captions(moments, clip_len, main_cat, seed=job_id, words=words, topic=topic)
            ai_sounds = story.shift(ai_sounds or [], start, clip_len - 0.5, key="time") or None
            transcript = " ".join(w["word"] for w in words)

            _tag_moods(meta_text["captions"], moments, main_cat)
            sounds = []
            if settings["sfx_enabled"]:
                sounds = story.pick_sfx(moments, clip_len, available_sounds, settings["max_sfx"],
                                        settings["sfx_intro_whoosh"], seed=job_id,
                                        recent_kinds=recent["sound_kinds"], recent_files=recent["sound_files"],
                                        ai_picks=ai_sounds)

            # ---- render 9:16
            _set(job_id, state="rendering 9:16")
            src_w, src_h, fps = render.probe(wide)
            cam_box, cam_source = getattr(streamer, "facecam", None), "manual"
            layout = settings["layout"]
            if layout == "facecam" and not cam_box and settings["auto_facecam"]:
                _set(job_id, state="finding the webcam")
                cam_box, cam_source = facecam.detect(wide, clip_len, src_w, src_h), "auto"
                if cam_box:
                    streamer.auto_facecam = cam_box
                else:  # webcam hidden in this clip (e.g. full-screen gameplay): use the last one found
                    cam_box = getattr(streamer, "auto_facecam", None)
                    cam_source = "auto (earlier clip)" if cam_box else "none"
            if layout == "facecam" and not cam_box:
                layout = settings["no_facecam_layout"]
            geo = render.geometry(layout, show_chat, settings["caption_position"], settings["video_zoom"],
                                  cam_box, (src_w, src_h))
            (out_dir / "captions.ass").write_text(
                captions.build_ass(words if settings["word_captions"] else [], clip_len, meta_text["captions"], layout, settings["caption_font"],
                                   settings["burn_hook"], chat_log, show_chat, settings["caption_position"],
                                   settings["top_caption_style"], settings["word_color"], geo=geo,
                                   caption_mode=settings["caption_mode"], headline=meta_text.get("headline", "")),
                encoding="utf-8")
            strongest = sorted(moments, key=lambda m: -m["strength"])[:3]
            punch = sorted(m["t"] for m in strongest if m["t"] > 0.8) if settings["zoom_punch"] else []
            has_audio = bool(peaks) or _has_audio(wide)

            def render_with(punch_times, with_sfx):
                vf = render.video_filter(geo, (src_w, src_h), cam_box, punch_times, fps)
                if with_sfx:
                    extra_in, af, alabel = sfx.mix_args(sounds, settings["sfx_volume"])
                    audio = [*extra_in, "-filter_complex", vf + ";" + af, "-map", "[v]", "-map", alabel,
                             "-c:a", "aac", "-b:a", "192k"]
                else:
                    audio = ["-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-c:a", "copy"]
                # cwd=out_dir so the subtitle path has no "C:\" (ffmpeg filter escaping on Windows)
                tools.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-i", wide.name, *audio, *enc,
                           "-r", f"{fps:g}", "-movflags", "+faststart", "clip_9x16.mp4"], cwd=out_dir, timeout=900)

            use_sfx = bool(sounds) and has_audio
            attempts = []
            for a in ((tuple(punch), use_sfx), ((), use_sfx), ((), False)):
                if a not in attempts:
                    attempts.append(a)
            for n, (pt, ws) in enumerate(attempts):
                try:
                    render_with(list(pt), ws)
                    break
                except RuntimeError as e:  # zoom or a sound file broke: retry simpler
                    if n == len(attempts) - 1:
                        raise
                    log.warning("render attempt %d failed (%s), retrying simpler", n + 1, str(e)[:200])
            punch = list(pt)
            if not ws:
                sounds = []
            thumb_t = strongest[0]["t"] if strongest else clip_len / 2
            tools.run([ff, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{min(thumb_t, clip_len - 0.5):.2f}",
                       "-i", "clip_9x16.mp4", "-frames:v", "1", "-vf", "scale=360:-2", "thumb.jpg"], cwd=out_dir)

        meta = {
            "id": job_id,
            "created": time.time(),
            "streamer": streamer.channel_name,
            "platform": streamer.platform,
            "peak_time": datetime.fromtimestamp(moment["peak_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
            "score": moment.get("score"),
            "clip_score": score,
            "app_score": app_score,
            "low_score": low_score,
            "manual": manual,
            "duration": round(clip_len, 1),
            "trimmed": [start, end] if trim else None,
            "language": lang,
            "transcript": transcript,
            "chat": chat[-25:],
            "chat_stats": chat_metrics,
            "show_chat": show_chat,
            "layout": geo["kind"],
            "facecam": cam_source if geo["kind"] == "facecam" else None,
            "video_missing_s": missing,
            "zoom_punches": punch,
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
        log.info("Clip ready: %s - %s (score %s/10)", streamer.channel_name, meta["title"], score)
        from . import publish
        publish.on_clip_ready(meta, settings)
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
