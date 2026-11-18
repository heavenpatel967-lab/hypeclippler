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


def generate(settings, ctx):
    """ctx: streamer, platform, topic, chat, timeline, duration, fallback_captions, main_cat,
    quote, recent_captions, recent_titles, sound_kinds {kind: label}, max_sfx, seed."""
    from .story import clean_captions
    fb = _fallback(ctx)
    sounds_list = "\n".join(f"- {k}: {v}" for k, v in ctx["sound_kinds"].items() if k != "whoosh")
    avoid = sorted(set(GENERIC + [c.lower() for c in ctx.get("recent_captions", [])]))[:40]
    prompt = f"""You are a top YouTube Shorts editor. You turn live stream moments into viral clips.

Streamer: {ctx['streamer']} ({ctx['platform']})
Game/topic: {ctx['topic'] or "unknown"}
Clip length: {ctx['duration']:.0f} seconds.

TIMELINE (seconds from clip start). "on screen" = what a vision model saw in the video,
"streamer says" = speech, "chat reacts" = live chat reaction, "loud" = loud audio:
{ctx['timeline'][:3500]}

Chat messages at the peak: {"; ".join(ctx['chat'][-25:])[:900]}

Write captions that tell THE STORY OF THIS CLIP using the specific things in the timeline:
what he does, what happens to him, what he says, how chat reacts. Name concrete things
(the cops, the car, the jump, his exact words). Use his funniest/most dramatic words as a
quote caption when he says something good. Captions are uppercase-friendly, 2 to 6 words,
punchy, no emojis, no hashtags.

Good examples (for other clips): "HE THOUGHT HE WAS SAFE", "COPS PULLED UP INSTANTLY",
"\\"BRO I'M DONE\\"", "THE CAR JUST FLIPPED", "CHAT SAW IT COMING", "HE PANICKED".
NEVER use these generic lines: {", ".join(avoid)}.

Sound effects available (pick only from this list, use the name before the colon):
{sounds_list}

Return JSON with keys:
"title": Shorts title under 70 characters, specific to this clip, includes {ctx['streamer']},
"captions": 4 to 7 items {{"time": seconds, "text": "..."}}. First at 0 sets up the story and makes
  people keep watching. Others land exactly when things happen in the timeline. 3+ seconds apart,
"sounds": up to {ctx['max_sfx']} items {{"time": seconds, "sound": "name", "why": "short reason"}} placed
  exactly on the action that fits (a fail, a crash, a shock, a funny line), each sound name used once,
"description": 1-2 sentences about what happens,
"hashtags": 5-8 hashtags with # (include #shorts, the streamer and the game).
Only JSON."""
    try:
        data = llm.generate_json(settings, prompt, timeout=180)
        tags = data.get("hashtags") or []
        if isinstance(tags, str):
            tags = tags.split()
        tags = [t if t.startswith("#") else "#" + t for t in map(str, tags) if t.strip("#")][:10]
        caps = clean_captions(data.get("captions"), ctx["duration"], fb["captions"])
        # drop generic lines the model used anyway; fall back if too few remain
        if caps is not fb["captions"]:
            kept = [c for c in caps if c["text"].lower().strip(" .!?") not in GENERIC]
            caps = kept if len(kept) >= 3 else fb["captions"]
        title = str(data.get("title", "")).strip().strip('"')[:100]
        if title:
            info = llm.resolve(settings)
            return {
                "title": title,
                "hook": caps[0]["text"] if caps else fb["hook"],
                "description": str(data.get("description", "")).strip() or fb["description"],
                "hashtags": tags or fb["hashtags"],
                "captions": caps,
                "sounds": _clean_sounds(data.get("sounds"), ctx),
                "source": f"AI ({info['model']}{', saw the video' if ctx.get('saw_video') else ''})"
                          + ("" if caps is not fb["captions"] else ", template captions"),
            }
        reason = "it returned no title"
    except Exception as e:  # noqa: BLE001
        reason = str(e)[:150]
    log.warning("AI not used (%s); using template titles", reason)
    fb["source"] = f"template (AI: {reason})"
    return fb
