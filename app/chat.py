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
import uuid

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
# Since 13 Oct 2026 Kick no longer uses its old Pusher chat (error 4001 "App key ...
# not in this cluster"). The kick.com web player now:
#   1. asks web.kick.com which realtime server to use (Centrifugo, or Pusher with a key)
#   2. gets an anonymous guest token (no account, ~45 min)
#   3. connects to wss://realtime.<region>.platform.kick.com/connection/websocket and
#      subscribes to chatrooms.<chatroom_id>.v2 with the Centrifugo JSON protocol.
# Messages inside are the same as before (App\Events\ChatMessageEvent ...).
KICK_API = "https://web.kick.com/api/v1/realtime"
KICK_HOSTS = ["wss://realtime.us-east-1.platform.kick.com/connection/websocket",
              "wss://realtime.us-west-2.platform.kick.com/connection/websocket"]
KICK_HEADERS = {"content-type": "application/json", "accept": "application/json",
                "origin": "https://kick.com", "referer": "https://kick.com/", "user-agent": UA}
# legacy Pusher settings, only used if Kick's server ever offers Pusher again
KICK_PUSHER = {"key": "32cbd69e4b950bf97679", "cluster": "us2"}


def kick_channel_info(slug: str) -> dict:
    """kick.com is behind Cloudflare; curl_cffi imitates a real Chrome browser."""
    from curl_cffi import requests as creq
    r = creq.get(f"https://kick.com/api/v2/channels/{slug}", impersonate="chrome", timeout=20)
    r.raise_for_status()
    return r.json()


def _kick_post(path: str, body: dict) -> dict:
    from curl_cffi import requests as creq
    r = creq.post(KICK_API + path, json=body, headers=KICK_HEADERS, impersonate="chrome", timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"{path} answered HTTP {r.status_code}")
    return r.json()


def kick_negotiate(channel_id) -> dict:
    """Which realtime server Kick wants us on. Returns
    {"provider": "centrifugo", "urls": [...], "token": ...} or {"provider": "pusher", ...}."""
    client_id = str(uuid.uuid4())
    body = {"client": {"id": client_id, "type": "web"},
            "capabilities": {"accepted_providers": [{"provider": "pusher"}, {"provider": "centrifugo"}]}}
    urls = []
    for path in (f"/channels/{channel_id}/chat/connection", "/connection"):
        try:
            data = (_kick_post(path, body) or {}).get("data") or {}
        except Exception as e:  # noqa: BLE001
            log.debug("kick negotiation %s failed: %s", path, e)
            continue
        for conn in data.get("connections") or []:
            creds = conn.get("credentials") or {}
            if conn.get("provider") == "centrifugo" and str(creds.get("url", "")).startswith("wss://"):
                urls.append(creds["url"])
            elif conn.get("provider") == "pusher" and creds.get("app_key") and creds.get("cluster"):
                return {"provider": "pusher", "key": creds["app_key"], "cluster": creds["cluster"]}
        if urls:
            break
    token = ((_kick_post("/auth/connection", {"client_id": client_id}) or {}).get("data") or {}).get("token")
    if not token:
        raise RuntimeError("Kick gave no guest chat token")
    return {"provider": "centrifugo", "urls": urls + [h for h in KICK_HOSTS if h not in urls], "token": token}


def _kick_message(event: str, data, seen: set, on_message):
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        return
    if event.endswith("ChatMessageEvent") or event.endswith("ChatMessageSentEvent"):
        mid = data.get("id")
        if mid in seen:          # the same message can arrive on two channels
            return
        seen.add(mid)
        if len(seen) > 5000:
            seen.clear()
        content = re.sub(r"\[emote:\d+:([^\]]+)\]", r"\1", data.get("content", ""))
        sender = data.get("sender") or {}
        color = (sender.get("identity") or {}).get("color")
        on_message(sender.get("username", "?"), content, "chat", color)
    elif "Subscription" in event or "GiftedSubscriptions" in event or "Kicks" in event:
        on_message("kick", event.split("\\")[-1], "event")


async def _kick_centrifugo(conn, chatroom_id, on_message, stop, status):
    last_error = None
    for url in conn["urls"]:
        try:
            async with websockets.connect(url, origin="https://kick.com", user_agent_header=UA,
                                          ping_interval=None, open_timeout=15) as ws:
                await ws.send(json.dumps({"id": 1, "connect": {"token": conn["token"], "name": "js"}}))
                await ws.send(json.dumps({"id": 2, "subscribe": {"channel": f"chatrooms.{chatroom_id}.v2",
                                                                 "flag": 1}}))
                seen, expires_at = set(), None
                loop = asyncio.get_running_loop()
                while not stop.is_set():
                    if expires_at and loop.time() > expires_at:
                        return  # guest token about to expire: reconnect with a fresh one
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=40)
                    except asyncio.TimeoutError:
                        raise ConnectionError("no data from Kick for 40s")
                    for line in str(raw).split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            fr = json.loads(line)
                        except ValueError:
                            continue
                        if fr == {}:                       # server ping
                            await ws.send("{}")
                            continue
                        if "error" in fr:
                            err = fr["error"] or {}
                            raise ConnectionError(f"Kick chat error {err.get('code')}: {err.get('message')}")
                        if fr.get("id") == 1 and "connect" in fr:
                            ttl = (fr["connect"] or {}).get("ttl")
                            if ttl:
                                expires_at = loop.time() + max(60, int(ttl) - 120)
                        elif fr.get("id") == 2 and "subscribe" in fr:
                            status("chat connected")
                        push = fr.get("push") or {}
                        if "disconnect" in push:
                            raise ConnectionError(f"Kick disconnected us: {push['disconnect'].get('reason')}")
                        env = (push.get("pub") or {}).get("data")
                        if isinstance(env, str):
                            try:
                                env = json.loads(env)
                            except ValueError:
                                env = None
                        if isinstance(env, dict) and env.get("event"):
                            _kick_message(env["event"], env.get("data", "{}"), seen, on_message)
                return
        except Exception as e:  # noqa: BLE001 - try the next region
            if stop.is_set():
                return
            last_error = e
    raise ConnectionError(f"no Kick chat server accepted us ({last_error})")


async def _kick_pusher(key, cluster, chatroom_id, on_message, stop, status):
    url = f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version=8.4.0&flash=false"
    async with websockets.connect(url, ping_interval=None) as ws:
        for chan in (f"chatrooms.{chatroom_id}.v2", f"chatrooms.{chatroom_id}"):
            await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": chan}}))
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
            elif event == "pusher_internal:subscription_succeeded":
                status("chat connected")
            elif event == "pusher:error":
                raise ConnectionError(f"Kick Pusher error: {str(msg.get('data'))[:80]}")
            else:
                _kick_message(event, msg.get("data"), seen, on_message)


async def kick_chat(slug: str, on_message, stop: asyncio.Event, status):
    chatroom_id = channel_id = None
    while not stop.is_set():
        try:
            if chatroom_id is None:
                info = await asyncio.to_thread(kick_channel_info, slug)
                chatroom_id, channel_id = info["chatroom"]["id"], info.get("id")
            status("chat connecting")
            conn = await asyncio.to_thread(kick_negotiate, channel_id)
            if conn["provider"] == "pusher":
                await _kick_pusher(conn["key"], conn["cluster"], chatroom_id, on_message, stop, status)
            else:
                await _kick_centrifugo(conn, chatroom_id, on_message, stop, status)
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
