"""Live chat readers for Twitch, Kick and YouTube. No API keys, no quotas.

Each reader is an async function that keeps reconnecting until `stop` is set and
calls on_message(user, text, kind, color=None) for every message. kind is "chat" or "event"
(subs, bits, donations, super chats). Events count extra in the hype score.
"""
import asyncio
import json
import logging
import random
import re

import httpx
import websockets

log = logging.getLogger("chat")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


async def _sleep_or_stop(stop: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


# --------------------------------------------------------------------- Twitch
def _parse_irc(line: str):
    tags, prefix = {}, ""
    if line.startswith("@"):
        raw, line = line[1:].split(" ", 1)
        for item in raw.split(";"):
            k, _, v = item.partition("=")
            tags[k] = v
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
    if " :" in line:
        line, trailing = line.split(" :", 1)
        params = line.split() + [trailing]
    else:
        params = line.split()
    command = params.pop(0) if params else ""
    return tags, prefix, command, params


async def twitch_chat(channel: str, on_message, stop: asyncio.Event, status):
    channel = channel.lower().lstrip("#")
    while not stop.is_set():
        try:
            async with websockets.connect("wss://irc-ws.chat.twitch.tv:443", ping_interval=60) as ws:
                await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
                await ws.send("PASS SCHMOOPIIE")
                await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
                await ws.send(f"JOIN #{channel}")
                status("chat connected")
                while not stop.is_set():
                    try:
                        data = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    for line in data.split("\r\n"):
                        if not line:
                            continue
                        tags, prefix, cmd, params = _parse_irc(line)
                        if cmd == "PING":
                            await ws.send("PONG :" + (params[-1] if params else "tmi.twitch.tv"))
                        elif cmd == "PRIVMSG" and len(params) >= 2:
                            user = tags.get("display-name") or prefix.split("!")[0]
                            kind = "event" if tags.get("bits") else "chat"
                            on_message(user, params[1], kind, tags.get("color") or None)
                        elif cmd == "USERNOTICE":
                            on_message(tags.get("login", "?"), tags.get("msg-id", "sub"), "event")
                        elif cmd == "RECONNECT":
                            raise ConnectionError("twitch asked to reconnect")
        except Exception as e:  # noqa: BLE001 - keep the worker alive no matter what
            if stop.is_set():
                break
            status(f"chat reconnecting ({type(e).__name__})")
            await _sleep_or_stop(stop, 5)


# ----------------------------------------------------------------------- Kick
# Kick's public Pusher key/cluster used by kick.com itself. Editable in the dashboard
# settings. To find the current one: open kick.com, DevTools > Network > WS, look for
# "ws-<cluster>.pusher.com/app/<key>".
KICK_PUSHER = {"key": "32cbd69e4b950bf97679", "cluster": "us2"}


def _kick_ws_url():
    return (f"wss://ws-{KICK_PUSHER['cluster']}.pusher.com/app/{KICK_PUSHER['key']}"
            "?protocol=7&client=js&version=8.4.0&flash=false")


def kick_channel_info(slug: str) -> dict:
    """kick.com is behind Cloudflare; curl_cffi imitates a real Chrome browser."""
    from curl_cffi import requests as creq
    r = creq.get(f"https://kick.com/api/v2/channels/{slug}", impersonate="chrome", timeout=20)
    r.raise_for_status()
    return r.json()


async def kick_chat(slug: str, on_message, stop: asyncio.Event, status):
    chatroom_id = None
    while not stop.is_set():
        try:
            if chatroom_id is None:
                info = await asyncio.to_thread(kick_channel_info, slug)
                chatroom_id = info["chatroom"]["id"]
            async with websockets.connect(_kick_ws_url(), ping_interval=None) as ws:
                for chan in (f"chatrooms.{chatroom_id}.v2", f"chatrooms.{chatroom_id}"):
                    await ws.send(json.dumps({"event": "pusher:subscribe",
                                              "data": {"auth": "", "channel": chan}}))
                status("chat connecting")
                seen = set()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                        continue
                    msg = json.loads(raw)
                    event = msg.get("event", "")
                    if event == "pusher:ping":
                        await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                        continue
                    if event == "pusher_internal:subscription_succeeded":
                        status("chat connected")
                        continue
                    if event == "pusher:error":
                        # e.g. 4001 "App key ... not in this cluster" = Kick changed its chat server
                        raise ConnectionError("Kick chat server rejected us, update the Kick Pusher key "
                                              f"in settings: {str(msg.get('data'))[:80]}")
                    data = msg.get("data")
                    if isinstance(data, str):
                        try:
                            data = json.loads(data)
                        except ValueError:
                            data = {}
                    if event.endswith("ChatMessageEvent") or event.endswith("ChatMessageSentEvent"):
                        mid = data.get("id")
                        if mid in seen:          # same message arrives on both channels
                            continue
                        seen.add(mid)
                        if len(seen) > 5000:
                            seen.clear()
                        content = data.get("content", "")
                        content = re.sub(r"\[emote:\d+:([^\]]+)\]", r"\1", content)
                        sender = data.get("sender") or {}
                        color = (sender.get("identity") or {}).get("color")
                        on_message(sender.get("username", "?"), content, "chat", color)
                    elif "Subscription" in event or "GiftedSubscriptions" in event or "Kicks" in event:
                        on_message("kick", event.split("\\")[-1], "event")
        except Exception as e:  # noqa: BLE001
            if stop.is_set():
                break
            status(f"chat reconnecting ({type(e).__name__}: {str(e)[:160]})")
            await _sleep_or_stop(stop, 10)


# -------------------------------------------------------------------- YouTube
async def youtube_live_video_id(client: httpx.AsyncClient, channel: str) -> str | None:
    """Accepts a video id, a watch/live URL, @handle, or channel URL."""
    if re.fullmatch(r"[\w-]{11}", channel) and not channel.startswith("@"):
        return channel
    m = re.search(r"(?:v=|youtu\.be/|/live/)([\w-]{11})", channel)
    if m:
        return m.group(1)
    if channel.startswith("http"):
        url = channel.rstrip("/")
        if not url.endswith("/live"):
            url += "/live"
    else:
        url = f"https://www.youtube.com/{channel if channel.startswith('@') else '@' + channel}/live"
    r = await client.get(url)
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([\w-]{11})"', r.text)
    # /@handle/live only points to a watch page while the channel is live (or has a scheduled stream)
    return m.group(1) if m else None


def _yt_text(runs):
    out = []
    for run in runs or []:
        if "text" in run:
            out.append(run["text"])
        elif "emoji" in run:
            shortcuts = run["emoji"].get("shortcuts") or [""]
            out.append(shortcuts[0])
    return "".join(out)


def _yt_continuation(conts):
    for c in conts or []:
        for key in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
            if key in c:
                return c[key].get("continuation"), c[key].get("timeoutMs", 2000)
    return None, 2000


async def youtube_chat(channel: str, on_message, stop: asyncio.Event, status):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    cookies = {"CONSENT": "YES+1", "SOCS": "CAI"}
    async with httpx.AsyncClient(headers=headers, cookies=cookies, timeout=20, follow_redirects=True) as client:
        while not stop.is_set():
            try:
                vid = await youtube_live_video_id(client, channel)
                if not vid:
                    status("offline (no live video found)")
                    await _sleep_or_stop(stop, 60)
                    continue
                page = (await client.get(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}")).text
                key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
                ver = re.search(r'"clientVersion":"([^"]+)"', page)
                cont = re.search(r'"continuation":"([^"]+)"', page)
                if not (key and ver and cont):
                    status("offline (chat not available)")
                    await _sleep_or_stop(stop, 60)
                    continue
                key, ver, cont = key.group(1), ver.group(1), cont.group(1)
                status("chat connected")
                while cont and not stop.is_set():
                    r = await client.post(
                        f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key}&prettyPrint=false",
                        json={"context": {"client": {"clientName": "WEB", "clientVersion": ver}},
                              "continuation": cont})
                    r.raise_for_status()
                    lcc = r.json().get("continuationContents", {}).get("liveChatContinuation", {})
                    for action in lcc.get("actions", []):
                        item = action.get("addChatItemAction", {}).get("item", {})
                        if "liveChatTextMessageRenderer" in item:
                            m = item["liveChatTextMessageRenderer"]
                            on_message(m.get("authorName", {}).get("simpleText", "?"),
                                       _yt_text(m.get("message", {}).get("runs")), "chat")
                        elif "liveChatPaidMessageRenderer" in item or "liveChatMembershipItemRenderer" in item:
                            m = item.get("liveChatPaidMessageRenderer") or item["liveChatMembershipItemRenderer"]
                            on_message(m.get("authorName", {}).get("simpleText", "?"),
                                       _yt_text(m.get("message", {}).get("runs")) or "superchat", "event")
                    cont, timeout_ms = _yt_continuation(lcc.get("continuations"))
                    await _sleep_or_stop(stop, min(max(timeout_ms, 1000), 5000) / 1000)
                status("chat ended, checking again")
                await _sleep_or_stop(stop, 15)
            except Exception as e:  # noqa: BLE001
                if stop.is_set():
                    break
                status(f"chat reconnecting ({type(e).__name__})")
                await _sleep_or_stop(stop, 10)


READERS = {"twitch": twitch_chat, "kick": kick_chat, "youtube": youtube_chat}
