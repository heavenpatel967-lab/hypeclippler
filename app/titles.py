"""Titles, story captions, descriptions, hashtags and sound choices.

With Ollama: the model gets the full clip timeline (what's on screen from the vision
model, what the streamer says, how chat reacts, loud moments) and writes everything
specific to that clip. Without Ollama: templates built from the same timeline, quoting
the streamer's own words, and avoiding lines used in recent clips.
"""
import logging
import random
import re

from . import llm

log = logging.getLogger("titles")

GENERIC = ["what just happened", "chat went crazy", "chat went absolutely crazy", "nobody expected this",
           "this went so wrong", "chat could not stop laughing", "wait for it", "you won't believe",
           "insane moment", "that was loud", "he's screaming", "watch till the end"]

TITLE_TEMPLATES = {
    "laugh": ["{s} can't stop laughing at this", "{s} lost it after this", "I can't believe {s} did this",
              "{s} had chat in tears"],
    "hype": ["{s} actually pulled this off", "{s} with the cleanest play", "{s} went off here",
             "This {s} moment is insane"],
    "shock": ["{s} did NOT see this coming", "{s}'s reaction says it all", "Nobody was ready for this {s} moment",
              "{s} froze after this"],
    "fail": ["Everything went wrong for {s}", "{s} instantly regretted this", "This did not go to plan for {s}",
             "{s} will never live this down"],
    "suspense": ["{s} almost got caught", "{s} was so close", "The most stressful {s} moment"],
    "bruh": ["{s} what are you doing", "Why would {s} do this", "{s} confused everyone"],
    "loud": ["{s} screamed so loud", "{s} lost his mind", "{s}'s loudest moment"],
}


def _tags(streamer, platform, topic):
    tags = ["#" + re.sub(r"\W", "", streamer.lower()), f"#{platform}", "#shorts", "#clips", "#viral"]
    if topic:
        tags.insert(1, "#" + re.sub(r"\W", "", topic.lower()))
    return tags


def _fallback(ctx):
    s, topic, main = ctx["streamer"], ctx["topic"], ctx["main_cat"]
    rnd = random.Random(ctx.get("seed"))
    quote = ctx.get("quote")
    recent = {t.lower() for t in ctx.get("recent_titles", [])}
    options = [t.format(s=s) for t in TITLE_TEMPLATES.get(main, TITLE_TEMPLATES["shock"])]
    if quote:
        options += [f'{s}: "{quote}"', f'"{quote}" - {s}']
    fresh = [o for o in options if o.lower() not in recent] or options
    title = rnd.choice(fresh) + (f" | {topic}" if topic and len(fresh[0]) < 60 else "")
    caps = ctx["fallback_captions"]
    return {
        "title": title[:95],
        "hook": caps[0]["text"] if caps else "",
        "description": (f'{s}{" in " + topic if topic else ""}: ' +
                        (f'"{quote}". ' if quote else "") + "Chat went wild at this moment.\n"
                        f"Watch {s} live on {ctx['platform'].title()}."),
        "hashtags": _tags(s, ctx["platform"], topic),
        "captions": caps,
        "sounds": None,
        "story": "",
        "ai_score": None,
        "score_reason": "",
        "window": None,
        "source": "template",
    }


def _clean_sounds(raw, ctx):
    """AI sound picks -> [{time, sound, why}] or None if unusable."""
    kinds, duration = ctx["sound_kinds"], ctx["duration"]
    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            t, kind = float(item.get("time")), str(item.get("sound", "")).strip().lower()
        except (AttributeError, TypeError, ValueError):
            continue
        if kind in kinds and kind != "whoosh" and 0.5 <= t <= duration - 0.5 \
                and all(abs(t - o["time"]) >= 4 for o in out) and kind not in {o["sound"] for o in out}:
            out.append({"time": round(t, 2), "sound": kind, "why": str(item.get("why", ""))[:80]})
    return out[:ctx["max_sfx"]] or None


SCHEMA = {
    "type": "object",
    "properties": {
        "story": {"type": "string"},
        "score": {"type": "integer"},
        "reason": {"type": "string"},
        "start": {"type": "number"},
        "end": {"type": "number"},
        "title": {"type": "string"},
        "captions": {"type": "array", "items": {"type": "object", "properties": {
            "time": {"type": "number"}, "text": {"type": "string"}}, "required": ["time", "text"]}},
        "sounds": {"type": "array", "items": {"type": "object", "properties": {
            "time": {"type": "number"}, "sound": {"type": "string"}, "why": {"type": "string"}},
            "required": ["time", "sound"]}},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["story", "score", "start", "end", "title", "captions", "sounds", "description", "hashtags"],
}


def generate(settings, ctx):
    """ctx: streamer, platform, topic, chat, chat_digest, timeline, duration, fallback_captions,
    main_cat, quote, recent_captions, recent_titles, sound_kinds {kind: label}, max_sfx, seed,
    hype_score, saw_video, trim (bool)."""
    from .story import clean_captions
    fb = _fallback(ctx)
    sounds_list = "\n".join(f"- {k}: {v}" for k, v in ctx["sound_kinds"].items() if k != "whoosh") or "(none)"
    avoid = sorted(set(GENERIC + [c.lower() for c in ctx.get("recent_captions", [])]))[:40]
    d = ctx["duration"]
    trim = (f'"start"/"end": the best part to post, in seconds of this recording (0-{d:.0f}). 15-50 s long. '
            "Start a few seconds before the action so viewers understand it, end right after the "
            "reaction. Cut boring parts." if ctx.get("trim") else f'"start": 0, "end": {d:.0f}.')
    prompt = f"""You are the best YouTube Shorts / TikTok clipper in the world. You decide if a live stream
moment is worth posting and you edit it so people watch till the end.

Streamer: {ctx['streamer']} ({ctx['platform']}), game/topic: {ctx['topic'] or "unknown"}
Recording length: {d:.0f} s. Chat hype score: {ctx.get('hype_score')}x faster than normal.

TIMELINE (seconds). "on screen" = what a vision model saw, "streamer says" = his speech,
"chat reacts" = reaction bursts, "very loud moment" = screams/crashes:
{ctx['timeline'][:3500]}

LIVE CHAT ANALYSIS (chat is ~3-5 s behind the action; read WHY chat reacts):
{ctx.get('chat_digest', '')[:3500]}

Answer in JSON:
"story": 2-3 sentences: what exactly happens, in order, and why chat reacted the way it did.
"score": 1-10, how viral this clip would be for people who don't know the streamer
  (1-3 nothing happens / only chat spam, 4-5 mildly interesting, 6-7 good, 8-10 must post).
"reason": one short sentence for the score.
{trim}
"title": under 70 characters, specific to what happens, includes {ctx['streamer']}, makes people click.
"captions": 4-7 items {{"time": seconds, "text": "2-6 words"}} telling the story as it happens:
  first one at "start" sets it up so people keep watching, the others land exactly on the
  action/reaction. Name concrete things (the cops, the car, the boss, his exact words). Quote
  his best line in quotes when he says something good. 3+ seconds apart. No emojis.
  Good examples (other clips): "HE THOUGHT HE WAS SAFE", "COPS PULLED UP INSTANTLY",
  "\\"BRO I'M DONE\\"", "THE BOSS ONE-SHOT HIM", "CHAT SAW IT COMING".
  Never use: {", ".join(avoid)}.
"sounds": up to {ctx['max_sfx']} {{"time", "sound", "why"}} exactly on actions that fit, names only from:
{sounds_list}
"description": 1-2 sentences. "hashtags": 5-8 with # (#shorts, the streamer, the game).
Times are seconds of the recording (0-{d:.0f})."""
    try:
        data = llm.generate_json(settings, prompt, schema=SCHEMA, label="clip editor", timeout=240,
                                 temperature=0.7, num_predict=2000)
        tags = data.get("hashtags") or []
        if isinstance(tags, str):
            tags = tags.split()
        tags = [t if t.startswith("#") else "#" + t for t in map(str, tags) if t.strip("#")][:10]
        caps = clean_captions(data.get("captions"), d, fb["captions"])
        if caps is not fb["captions"]:
            kept = [c for c in caps if c["text"].lower().strip(" .!?") not in GENERIC]
            caps = kept if len(kept) >= 3 else fb["captions"]
        title = str(data.get("title", "")).strip().strip('"')[:100]
        if title:
            info = llm.resolve(settings)
            try:
                score = max(1, min(10, int(round(float(data.get("score"))))))
            except (TypeError, ValueError):
                score = None
            return {
                "title": title,
                "hook": caps[0]["text"] if caps else fb["hook"],
                "description": str(data.get("description", "")).strip() or fb["description"],
                "hashtags": tags or fb["hashtags"],
                "captions": caps,
                "sounds": _clean_sounds(data.get("sounds"), ctx),
                "story": str(data.get("story", "")).strip()[:600],
                "ai_score": score,
                "score_reason": str(data.get("reason", "")).strip()[:200],
                "window": (data.get("start"), data.get("end")),
                "source": f"AI ({info['model']}{', saw the video' if ctx.get('saw_video') else ''})"
                          + ("" if caps is not fb["captions"] else ", template captions"),
            }
        reason = "it returned no title"
    except Exception as e:  # noqa: BLE001
        reason = str(e)[:150]
    log.warning("AI not used (%s); using template titles", reason)
    fb["source"] = f"template (AI: {reason})"
    return fb
