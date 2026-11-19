"""Auto-download, auto-delete and uploads to YouTube Shorts and Instagram Reels.

Everything runs one job at a time in a background thread:
    finished clip -> (auto) upload to YouTube / Instagram -> (auto) save to the download
    folder -> (auto) delete from the dashboard

YouTube: official YouTube Data API v3 with your own free Google Cloud OAuth client.
Instagram: official Instagram API (Instagram Login), resumable Reels upload from this PC.
Tokens are stored only on this PC in data/ (never in the code or the logs).
"""
import json
import logging
import queue
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import CLIPS, DATA

log = logging.getLogger("publish")

YT_CLIENT = DATA / "youtube_client.json"
YT_TOKEN = DATA / "youtube_token.json"
IG_TOKEN = DATA / "instagram_token.json"
# no "openid email": Brand Account channels have no email, and asking for it makes Google
# skip the "choose a channel" screen, so you end up on the personal account without a channel
YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
NO_CHANNEL = "(no channel on this account)"
IG_API = "https://graph.instagram.com"

_q: "queue.Queue[tuple]" = queue.Queue()
_status: dict[str, dict] = {}       # clip id -> {"youtube": {...}, "instagram": {...}, "download": {...}}
_last_upload = {"youtube": 0.0, "instagram": 0.0}
_oauth_states: dict[str, float] = {}
_lock = threading.Lock()
_settings = None


# ------------------------------------------------------------------ helpers
def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _meta_path(clip_id):
    p = (CLIPS / clip_id / "meta.json").resolve()
    if p.parent.parent != CLIPS.resolve():
        raise ValueError("bad clip id")
    return p


def load_meta(clip_id):
    return _read(_meta_path(clip_id))


def _save_upload_result(clip_id, platform, result):
    with _lock:
        _status.setdefault(clip_id, {})[platform] = result
        p = _meta_path(clip_id)
        meta = _read(p)
        if meta:
            meta.setdefault("uploads", {})[platform] = result
            _write(p, meta)


def clip_status(clip_id):
    with _lock:
        return dict(_status.get(clip_id, {}))


class _Safe(dict):
    def __missing__(self, key):  # unknown {placeholder} stays as typed
        return "{" + key + "}"


def _handle(h):
    h = (h or "").strip().lstrip("@")
    return f"@{h}" if h else ""


def stream_link(meta):
    p, c = meta.get("platform"), meta.get("channel") or meta.get("streamer", "")
    if p == "twitch":
        return f"https://twitch.tv/{c}"
    if p == "kick":
        return f"https://kick.com/{c}"
    return c if str(c).startswith("http") else ""


def _fields(meta, settings, platform):
    handles = meta.get("handles") or {}
    mention = _handle(handles.get(platform)) or meta["streamer"]
    tags = []
    for t in (meta.get("hashtags") or []) + settings["extra_hashtags"].split():
        t = t if t.startswith("#") else "#" + t
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    return _Safe(title=meta.get("title", ""), headline=meta.get("headline") or meta.get("title", ""),
                 description=meta.get("description", ""), streamer=meta["streamer"], streamer_mention=mention,
                 platform=meta["platform"].title(), stream_link=stream_link(meta),
                 game=meta.get("topic") or settings["default_topic"],
                 extra_mentions=" ".join(_handle(h) for h in settings["extra_mentions"].replace(",", " ").split()),
                 hashtags=" ".join(tags[:15]))


def _render(template, fields, fallback):
    try:
        text = template.replace("\\n", "\n").format_map(fields)
    except (ValueError, IndexError):  # a broken template never stops an upload
        text = fallback.format_map(fields)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def caption_text(meta, settings=None, platform="instagram", limit=2200):
    settings = settings or _settings
    fields = _fields(meta, settings, platform)
    return _render(settings["description_template"], fields, DEFAULT_DESCRIPTION)[:limit]


def title_text(meta, settings, platform="youtube"):
    return _render(settings["title_template"], _fields(meta, settings, platform), "{title}")


DEFAULT_DESCRIPTION = ("{headline}\n\n{description}\n\n🎮 {streamer_mention} live on {platform}: {stream_link}\n"
                       "{extra_mentions}\n\n{hashtags}")


# ------------------------------------------------------------------- cover
COVER_SRC = DATA / "cover_source"   # the image you uploaded (any format ffmpeg reads)


def cover_source():
    files = sorted(DATA.glob("cover_source.*"))
    return files[0] if files else None


def save_cover(data: bytes, filename: str):
    if not data or len(data) > 15 * 2**20:
        raise ValueError("image is empty or bigger than 15 MB")
    ext = Path(filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise ValueError("use a PNG, JPG or WEBP image")
    for old in DATA.glob("cover_source.*"):
        old.unlink()
    path = DATA / f"cover_source{ext}"
    path.write_bytes(data)
    try:
        make_cover(DATA / "cover_preview.jpg", "", settings=_settings, headline_on=False)
    except Exception as e:
        path.unlink(missing_ok=True)
        raise ValueError(f"that image can't be read: {e}")
    return path


def make_cover(out: Path, headline: str, settings=None, headline_on=None):
    """1080x1920 cover: your image fitted in the middle on a blurred copy of itself,
    optionally with the clip's headline on it."""
    from . import captions, tools
    settings = settings or _settings
    src = cover_source()
    if not src:
        return None
    if headline_on is None:
        headline_on = settings["cover_headline"]
    vf = ("[0:v]split=2[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
          "boxblur=22:2,eq=brightness=-0.18[bg];[b]scale=1080:-2:flags=lanczos[fg];"
          "[bg][fg]overlay=(W-w)/2:(H-h)/2")
    cwd = out.parent
    if headline_on and headline:
        head, events = captions.headline_events(headline, 5, 1450, settings["top_caption_style"]
                                                if settings["top_caption_style"] in ("tiktok", "impact") else "impact")
        (cwd / "cover.ass").write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
            "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n" + head + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, "
            "MarginL, MarginR, MarginV, Effect, Text\n" + "\n".join(e.replace("\\fad(150,0)", "") for e in events) + "\n",
            encoding="utf-8")
        vf += ",ass=cover.ass"
    tools.run([tools.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(src.resolve()),
               "-filter_complex", vf, "-frames:v", "1", "-q:v", "3", out.name], cwd=cwd, timeout=60)
    (cwd / "cover.ass").unlink(missing_ok=True)
    return out


def _clip_cover(clip_id, meta, settings):
    if not settings["cover_enabled"] or not cover_source():
        return None
    out = CLIPS / clip_id / "cover.jpg"
    if not out.exists():
        make_cover(out, meta.get("headline") or meta.get("title", ""), settings)
    return out


def _ig_video(clip_id, meta, settings):
    """Instagram only takes a cover from a public URL, so the cover is put on the first
    0.2 s of the video and picked as the cover frame (barely visible when playing)."""
    from . import tools
    path = CLIPS / clip_id / "clip_9x16.mp4"
    cover = _clip_cover(clip_id, meta, settings)
    if not cover:
        return path, None
    out = CLIPS / clip_id / "clip_ig.mp4"
    if not out.exists():
        tools.run([tools.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-i", path.name, "-loop", "1",
                   "-t", "0.25", "-i", cover.name, "-filter_complex",
                   "[1:v]scale=1080:1920,setsar=1,format=yuv420p[c];[0:v][c]overlay=0:0:eof_action=pass:"
                   "enable='lt(t,0.2)'[v]", "-map", "[v]", "-map", "0:a?", *tools.video_encoder(),
                   "-c:a", "copy", "-movflags", "+faststart", out.name], cwd=CLIPS / clip_id, timeout=600)
    return out, 100


def _safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", name).strip(" .")[:80] or "clip"


# --------------------------------------------------------------- downloads
DEFAULT_FOLDER = Path.home() / "Videos" / "HypeClipper"


def download_folder(settings) -> Path:
    """The root download folder (falls back to the default if the saved one is unusable)."""
    raw = settings["download_folder"].strip()
    folder = Path(raw).expanduser() if raw else DEFAULT_FOLDER
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # e.g. a USB drive that was unplugged
        log.warning("download folder %s not usable (%s), using %s", folder, e, DEFAULT_FOLDER)
        folder = DEFAULT_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def check_folder(path: str) -> str:
    """Validates a new root folder: creates it and makes sure files can be written there."""
    raw = path.strip().strip('"')
    if not raw:
        return ""
    folder = Path(raw).expanduser()
    if not folder.is_absolute():
        raise ValueError("use a full path, like D:\\Clips")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        test = folder / ".hypeclipper_write_test"
        test.write_text("ok")
        test.unlink()
    except OSError as e:
        raise ValueError(f"can't save files there: {e}")
    return str(folder)


def choose_folder(start: str = "") -> str | None:
    """Opens a Windows/macOS/Linux folder picker on this PC. Returns the chosen path or None."""
    import tkinter
    from tkinter import filedialog
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)  # show it in front of the browser
    try:
        path = filedialog.askdirectory(title="Choose the HypeClipper download folder",
                                       initialdir=start or str(Path.home()), mustexist=False, parent=root)
    finally:
        root.destroy()
    return path or None


def _target_folder(meta, settings) -> Path:
    root = download_folder(settings)
    mode = settings["download_subfolders"]
    parts = []
    if mode in ("streamer", "streamer_date"):
        parts.append(_safe(meta["streamer"]))
    if mode in ("date", "streamer_date"):
        parts.append(meta["peak_time"][:10])
    folder = root.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_to_folder(clip_id, settings):
    """Copies the 9:16 clip + a text file with title/description/hashtags."""
    meta = load_meta(clip_id)
    if not meta:
        raise ValueError("clip not found")
    folder = _target_folder(meta, settings)
    base = _safe(f"{meta['peak_time'][:16].replace(':', '-')} {meta['streamer']} - {meta.get('title', '')}")
    src = CLIPS / clip_id / "clip_9x16.mp4"
    dst = folder / f"{base}.mp4"
    shutil.copyfile(src, dst)
    if settings["download_16x9"] and (CLIPS / clip_id / "clip_16x9.mp4").exists():
        shutil.copyfile(CLIPS / clip_id / "clip_16x9.mp4", folder / f"{base} (16x9).mp4")
    uploads = meta.get("uploads") or {}
    links = "\n".join(f"{k}: {v.get('url')}" for k, v in uploads.items() if v.get("url"))
    (folder / f"{base}.txt").write_text(
        f"YOUTUBE TITLE:\n{title_text(meta, settings)}\n\nYOUTUBE DESCRIPTION:\n{caption_text(meta, settings, 'youtube', 4900)}"
        f"\n\nINSTAGRAM CAPTION:\n{caption_text(meta, settings, 'instagram')}\n"
        + (f"\nUPLOADED:\n{links}\n" if links else ""), encoding="utf-8")
    with _lock:
        _status.setdefault(clip_id, {})["download"] = {"state": "done", "path": str(dst)}
    log.info("saved %s", dst)
    return dst


# ----------------------------------------------------------------- YouTube
def yt_client():
    data = _read(YT_CLIENT) or {}
    inner = data.get("installed") or data.get("web") or data
    if inner.get("client_id") and inner.get("client_secret"):
        return inner["client_id"], inner["client_secret"]
    return None


def set_yt_client(data: dict):
    inner = data.get("installed") or data.get("web") or data
    cid, secret = str(inner.get("client_id", "")).strip(), str(inner.get("client_secret", "")).strip()
    if not cid.endswith(".apps.googleusercontent.com") or not secret:
        raise ValueError("that isn't a Google OAuth client (need client_id and client_secret)")
    _write(YT_CLIENT, {"client_id": cid, "client_secret": secret})


def yt_redirect(settings):
    return f"http://localhost:{settings['port']}/api/youtube/callback"


def yt_auth_url(settings):
    client = yt_client()
    if not client:
        raise ValueError("add your Google OAuth client first")
    state = secrets.token_urlsafe(24)
    _oauth_states[state] = time.time()
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client[0], "redirect_uri": yt_redirect(settings), "response_type": "code",
        "scope": YT_SCOPES, "access_type": "offline", "prompt": "select_account consent", "state": state})  # always pick the account


def yt_finish(settings, code, state):
    started = _oauth_states.pop(state or "", None)
    if not started or time.time() - started > 900:
        raise ValueError("login expired or invalid, click Connect YouTube again")
    cid, secret = yt_client()
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "code": code, "client_id": cid, "client_secret": secret, "redirect_uri": yt_redirect(settings),
        "grant_type": "authorization_code"})
    if r.status_code != 200:
        raise ValueError(f"Google refused the login: {r.text[:200]}")
    tok = r.json()
    granted = set((tok.get("scope") or "").split())
    missing = [label for scope, label in (("https://www.googleapis.com/auth/youtube.upload", "Manage your YouTube videos"),
                                          ("https://www.googleapis.com/auth/youtube.readonly", "View your YouTube account"))
               if scope not in granted]
    if granted and missing:
        raise ValueError("you didn't tick: " + " and ".join(f"'{m}'" for m in missing) +
                         ". Click Connect YouTube again and tick 'Select all' before Continue.")
    if not tok.get("refresh_token"):
        raise ValueError("Google gave no refresh token, remove the app's access in your Google account and retry")
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    _write(YT_TOKEN, tok)
    tok["channel"] = _yt_channel(tok["access_token"])
    tok["email"] = _id_email(tok.get("id_token"))
    _write(YT_TOKEN, tok)
    if tok["channel"] == NO_CHANNEL:
        return (f"{NO_CHANNEL}. If your channel is a Brand Account, connect again and pick the channel "
                "(not your name) on Google's 'Choose an account or brand account' screen")
    return tok["channel"]


def _yt_access():
    tok = _read(YT_TOKEN)
    if not tok:
        raise RuntimeError("YouTube is not connected")
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    cid, secret = yt_client() or (None, None)
    r = httpx.post("https://oauth2.googleapis.com/token", timeout=30, data={
        "client_id": cid, "client_secret": secret, "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise RuntimeError("YouTube login expired, click Connect YouTube again "
                           "(tip: set the Google app to 'In production' so it doesn't expire every 7 days)")
    new = r.json()
    tok["access_token"] = new["access_token"]
    tok["expires_at"] = time.time() + new.get("expires_in", 3600) - 60
    _write(YT_TOKEN, tok)
    return tok["access_token"]


def _id_email(id_token):
    """Email of the Google account that signed in (only for showing it)."""
    import base64
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email")
    except Exception:  # noqa: BLE001
        return None


def _yt_channel(access):
    try:
        r = httpx.get("https://www.googleapis.com/youtube/v3/channels", timeout=20,
                      params={"part": "snippet", "mine": "true"}, headers={"Authorization": f"Bearer {access}"})
        if r.status_code != 200:  # e.g. permission not granted: that's not "no channel"
            return f"? (YouTube said: {_err(r)[:120]})"
        items = r.json().get("items") or []
        return items[0]["snippet"]["title"] if items else NO_CHANNEL
    except Exception as e:  # noqa: BLE001
        return f"? ({type(e).__name__})"


def yt_upload(clip_id, settings):
    tok = _read(YT_TOKEN) or {}
    if tok.get("channel") == NO_CHANNEL:
        raise RuntimeError(f"the connected Google account ({tok.get('email') or '?'}) has no YouTube channel: "
                           "Disconnect and connect again, choosing your channel")
    meta = load_meta(clip_id)
    path = CLIPS / clip_id / "clip_9x16.mp4"
    title = (title_text(meta, settings) or meta.get("title") or "Stream moment").replace("<", "").replace(">", "")
    if "#shorts" not in title.lower() and len(title) <= 90:
        title += " #shorts"
    tags = [t.lstrip("#") for t in meta.get("hashtags") or []][:15]
    body = {"snippet": {"title": title[:100], "description": caption_text(meta, settings, "youtube", 4900).replace("<", "").replace(">", ""),
                        "tags": tags, "categoryId": "20"},
            "status": {"privacyStatus": settings["yt_privacy"], "selfDeclaredMadeForKids": False}}
    access = _yt_access()
    size = path.stat().st_size
    r = httpx.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                   json=body, timeout=60, headers={"Authorization": f"Bearer {access}",
                                                    "X-Upload-Content-Type": "video/mp4",
                                                    "X-Upload-Content-Length": str(size)})
    if r.status_code != 200:
        raise RuntimeError(f"YouTube refused the upload: {_err(r)}")
    with open(path, "rb") as f:
        up = httpx.put(r.headers["Location"], content=f, timeout=900,
                       headers={"Content-Type": "video/mp4", "Content-Length": str(size)})
    if up.status_code not in (200, 201):
        raise RuntimeError(f"YouTube upload failed: {_err(up)}")
    vid = up.json()["id"]
    result = {"state": "done", "url": f"https://youtube.com/shorts/{vid}", "privacy": settings["yt_privacy"],
              "at": time.time()}
    cover = _clip_cover(clip_id, meta, settings)
    if cover:
        try:
            t = httpx.post(f"https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={vid}",
                           content=cover.read_bytes(), timeout=120,
                           headers={"Authorization": f"Bearer {_yt_access()}", "Content-Type": "image/jpeg"})
            result["thumbnail"] = "set" if t.status_code == 200 else f"not set: {_err(t)}"
        except httpx.HTTPError as e:
            result["thumbnail"] = f"not set: {e}"
        if result["thumbnail"] != "set":
            log.warning("YouTube thumbnail %s (verify your phone at youtube.com/verify)", result["thumbnail"])
    return result


def _err(r):
    try:
        e = r.json().get("error")
        if isinstance(e, dict):
            return e.get("message") or json.dumps(e)[:200]
        return str(e or r.text)[:200]
    except ValueError:
        return f"HTTP {r.status_code} {r.text[:150]}"


# --------------------------------------------------------------- Instagram
def ig_set_token(token):
    token = token.strip()
    r = httpx.get(f"{IG_API}/me", params={"fields": "user_id,username", "access_token": token}, timeout=20)
    if r.status_code != 200:
        raise ValueError(f"Instagram refused that token: {_err(r)}")
    me = r.json()
    _write(IG_TOKEN, {"token": token, "user_id": me.get("user_id") or me.get("id"),
                      "username": me.get("username"), "refreshed_at": time.time()})
    return me.get("username")


def _ig_token():
    data = _read(IG_TOKEN)
    if not data:
        raise RuntimeError("Instagram is not connected")
    if time.time() - data.get("refreshed_at", 0) > 7 * 86400:  # long-lived tokens last 60 days
        try:
            r = httpx.get(f"{IG_API}/refresh_access_token", timeout=20,
                          params={"grant_type": "ig_refresh_token", "access_token": data["token"]})
            if r.status_code == 200 and r.json().get("access_token"):
                data.update(token=r.json()["access_token"], refreshed_at=time.time())
                _write(IG_TOKEN, data)
        except httpx.HTTPError:
            pass
    return data


def ig_upload(clip_id, settings):
    meta = load_meta(clip_id)
    path, thumb_ms = _ig_video(clip_id, meta, settings)
    acc = _ig_token()
    token = acc["token"]
    form = {"media_type": "REELS", "upload_type": "resumable", "caption": caption_text(meta, settings, "instagram"),
            "share_to_feed": "true", "access_token": token}
    if thumb_ms is not None:
        form["thumb_offset"] = str(thumb_ms)
    r = httpx.post(f"{IG_API}/me/media", timeout=60, data=form)
    if r.status_code != 200 or "id" not in r.json():
        raise RuntimeError(f"Instagram refused the Reel: {_err(r)}")
    container, uri = r.json()["id"], r.json().get("uri") or f"https://rupload.facebook.com/ig-api-upload/{r.json()['id']}"
    size = path.stat().st_size
    with open(path, "rb") as f:
        up = httpx.post(uri, content=f, timeout=900, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
    if up.status_code != 200:
        raise RuntimeError(f"Instagram video upload failed: {_err(up)}")
    for _ in range(90):  # Instagram processes the video, usually under 2 minutes
        time.sleep(5)
        st = httpx.get(f"{IG_API}/{container}", params={"fields": "status_code,status", "access_token": token},
                       timeout=20).json()
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {st.get('status', code)}")
    else:
        raise RuntimeError("Instagram took too long to process the video")
    pub = httpx.post(f"{IG_API}/me/media_publish", data={"creation_id": container, "access_token": token}, timeout=60)
    if pub.status_code != 200:
        raise RuntimeError(f"Instagram publish failed: {_err(pub)}")
    media_id = pub.json().get("id")
    url = None
    try:
        url = httpx.get(f"{IG_API}/{media_id}", params={"fields": "permalink", "access_token": token},
                        timeout=20).json().get("permalink")
    except Exception:  # noqa: BLE001
        pass
    return {"state": "done", "url": url or f"https://instagram.com/{acc.get('username', '')}", "at": time.time()}


UPLOADERS = {"youtube": yt_upload, "instagram": ig_upload}


def connected():
    yt, ig = _read(YT_TOKEN), _read(IG_TOKEN)
    has_channel = bool(yt) and yt.get("channel") != NO_CHANNEL
    return {"youtube": {"client": bool(yt_client()), "connected": has_channel, "signed_in": bool(yt),
                        "channel": (yt or {}).get("channel"), "email": (yt or {}).get("email")},
            "instagram": {"connected": bool(ig), "username": (ig or {}).get("username")}}


# ------------------------------------------------------------- the worker
def enqueue(clip_id, platforms, manual=False):
    for p in platforms:
        with _lock:
            _status.setdefault(clip_id, {})[p] = {"state": "waiting"}
    _q.put((clip_id, list(platforms), manual))


def on_clip_ready(meta, settings):
    """Called when a clip is finished: queues auto uploads, then auto download/delete."""
    score = meta.get("clip_score") or 0
    if meta.get("low_score"):
        return  # waits for your review on the dashboard
    good = score >= settings["auto_upload_min_score"] and not meta.get("manual")
    platforms = [p for p, key in (("youtube", "auto_upload_youtube"), ("instagram", "auto_upload_instagram"))
                 if settings[key] and good and connected()[p]["connected"]]
    if platforms or settings["auto_download"]:
        _q.put((meta["id"], platforms, False))
        for p in platforms:
            with _lock:
                _status.setdefault(meta["id"], {})[p] = {"state": "waiting"}


def _worker():
    while True:
        clip_id, platforms, manual = _q.get()
        s = _settings
        try:
            if not (CLIPS / clip_id / "meta.json").exists():
                continue
            for p in platforms:
                gap = s["upload_gap_minutes"] * 60
                wait = _last_upload[p] + gap - time.time()
                if wait > 0 and not manual:  # spacing so the channel doesn't look like spam
                    _save_upload_result(clip_id, p, {"state": f"waiting {int(wait // 60) + 1} min (upload spacing)"})
                    time.sleep(wait)
                _save_upload_result(clip_id, p, {"state": "uploading"})
                try:
                    res = UPLOADERS[p](clip_id, s)
                    _last_upload[p] = time.time()
                    log.info("uploaded %s to %s: %s", clip_id, p, res.get("url"))
                except Exception as e:  # noqa: BLE001
                    log.warning("%s upload of %s failed: %s", p, clip_id, e)
                    res = {"state": "failed", "error": str(e)[:300]}
                _save_upload_result(clip_id, p, res)
            if not manual and s["auto_download"]:
                save_to_folder(clip_id, s)
                if s["delete_after_download"]:
                    from .clipper import delete_clip
                    delete_clip(clip_id)
                    with _lock:
                        _status.pop(clip_id, None)
        except Exception as e:  # noqa: BLE001
            log.warning("publish job for %s failed: %s", clip_id, e)
        finally:
            _q.task_done()


def start(settings):
    global _settings
    _settings = settings
    threading.Thread(target=_worker, name="publish", daemon=True).start()


def pending():
    return _q.qsize()
