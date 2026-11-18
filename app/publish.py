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
YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
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


def caption_text(meta, limit=2200):
    tags = " ".join(meta.get("hashtags") or [])
    credit = f"🎮 Streamer: {meta['streamer']} (watch live on {meta['platform'].title()})"
    return f"{meta.get('headline') or meta['title']}\n\n{meta.get('description', '')}\n\n{credit}\n\n{tags}"[:limit]


def _safe(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", name).strip(" .")[:80] or "clip"


# --------------------------------------------------------------- downloads
def download_folder(settings) -> Path:
    raw = settings["download_folder"].strip()
    folder = Path(raw).expanduser() if raw else Path.home() / "Videos" / "HypeClipper"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_to_folder(clip_id, settings):
    """Copies the 9:16 clip + a text file with title/description/hashtags."""
    meta = load_meta(clip_id)
    if not meta:
        raise ValueError("clip not found")
    folder = download_folder(settings)
    base = _safe(f"{meta['peak_time'][:16].replace(':', '-')} {meta['streamer']} - {meta.get('title', '')}")
    src = CLIPS / clip_id / "clip_9x16.mp4"
    dst = folder / f"{base}.mp4"
    shutil.copyfile(src, dst)
    if settings["download_16x9"] and (CLIPS / clip_id / "clip_16x9.mp4").exists():
        shutil.copyfile(CLIPS / clip_id / "clip_16x9.mp4", folder / f"{base} (16x9).mp4")
    uploads = meta.get("uploads") or {}
    links = "\n".join(f"{k}: {v.get('url')}" for k, v in uploads.items() if v.get("url"))
    (folder / f"{base}.txt").write_text(
        f"TITLE:\n{meta.get('title', '')}\n\nHEADLINE:\n{meta.get('headline', '')}\n\nCAPTION:\n{caption_text(meta)}\n"
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
        "scope": YT_SCOPES, "access_type": "offline", "prompt": "consent", "state": state})


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
    if not tok.get("refresh_token"):
        raise ValueError("Google gave no refresh token, remove the app's access in your Google account and retry")
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    _write(YT_TOKEN, tok)
    tok["channel"] = _yt_channel(tok["access_token"])
    _write(YT_TOKEN, tok)
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


def _yt_channel(access):
    try:
        r = httpx.get("https://www.googleapis.com/youtube/v3/channels", timeout=20,
                      params={"part": "snippet", "mine": "true"}, headers={"Authorization": f"Bearer {access}"})
        items = r.json().get("items") or []
        return items[0]["snippet"]["title"] if items else "(no channel on this account)"
    except Exception:  # noqa: BLE001
        return "?"


def yt_upload(clip_id, settings):
    meta = load_meta(clip_id)
    path = CLIPS / clip_id / "clip_9x16.mp4"
    title = (meta.get("title") or meta.get("headline") or "Stream moment").replace("<", "").replace(">", "")
    if "#shorts" not in title.lower() and len(title) <= 90:
        title += " #shorts"
    tags = [t.lstrip("#") for t in meta.get("hashtags") or []][:15]
    body = {"snippet": {"title": title[:100], "description": caption_text(meta, 4900).replace("<", "").replace(">", ""),
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
    return {"state": "done", "url": f"https://youtube.com/shorts/{vid}", "privacy": settings["yt_privacy"],
            "at": time.time()}


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
    path = CLIPS / clip_id / "clip_9x16.mp4"
    acc = _ig_token()
    token = acc["token"]
    r = httpx.post(f"{IG_API}/me/media", timeout=60, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption_text(meta),
        "share_to_feed": "true", "access_token": token})
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
    return {"youtube": {"client": bool(yt_client()), "connected": bool(yt), "channel": (yt or {}).get("channel")},
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
