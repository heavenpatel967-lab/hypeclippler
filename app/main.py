"""Web dashboard + API. Start with:  python run.py"""
import asyncio
import html
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import VERSION, clipper, llm, publish, render, resources, sfx, tools
from .config import CLIPS, STATIC, Settings
from .manager import Manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("app")

settings = Settings()
manager = Manager(settings)



@asynccontextmanager
async def lifespan(_app):
    try:
        tools.ffmpeg()
    except RuntimeError as e:
        log.error(str(e))
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, tools.video_encoder)  # warm up the check
    loop.run_in_executor(None, sfx.ensure_library)   # built-in sound effects + sfx folders
    publish.start(settings)
    await manager.start()
    yield
    await manager.shutdown()


app = FastAPI(title="HypeClipper", lifespan=lifespan)
app.mount("/clips", StaticFiles(directory=CLIPS), name="clips")
sfx.SFX_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/sfx", StaticFiles(directory=sfx.SFX_DIR), name="sfx")


class AddStreamer(BaseModel):
    channel: str
    platform: str | None = None
    topic: str = ""
    show_chat: bool = False


class UpdateStreamer(BaseModel):
    topic: str | None = None
    auto_clip: bool | None = None
    show_chat: bool | None = None


class Facecam(BaseModel):
    box: list[float] | None = None  # [x, y, w, h] as fractions of the frame, None = remove


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {"streamers": [w.info() for w in manager.workers.values()], "jobs": clipper.list_jobs()}


@app.post("/api/streamers")
async def add_streamer(body: AddStreamer):
    try:
        w = await manager.add(body.channel, body.platform or None, body.topic, show_chat=body.show_chat)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return w.info()


@app.patch("/api/streamers/{key}")
def update_streamer(key: str, body: UpdateStreamer):
    if key not in manager.workers:
        raise HTTPException(404, "not found")
    manager.update(key, body.topic, body.auto_clip, body.show_chat)
    return manager.workers[key].info()


@app.delete("/api/streamers/{key}")
async def remove_streamer(key: str):
    if not await manager.remove(key):
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.post("/api/streamers/{key}/clip")
def clip_now(key: str):
    w = manager.workers.get(key)
    if not w:
        raise HTTPException(404, "not found")
    if not w.recorder.live:
        raise HTTPException(400, "video is not recording yet (stream offline or still connecting)")
    return {"job": w.clip_now()}


@app.get("/api/clips")
def clips():
    out = clipper.list_clips()
    for c in out:  # live upload progress
        c["uploads"] = {**(c.get("uploads") or {}), **{k: v for k, v in publish.clip_status(c["id"]).items()
                                                        if k != "download"}}
        c["saved_to"] = (publish.clip_status(c["id"]).get("download") or {}).get("path")
    return out


@app.delete("/api/clips/{clip_id}")
def delete_clip(clip_id: str):
    if not clipper.delete_clip(clip_id):
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.post("/api/settings")
def save_settings(values: dict):
    settings.update(values)
    return settings.public()


@app.get("/api/health")
async def health():
    out = {"version": VERSION, "ffmpeg": None, "encoder": None, "ollama": False, "ollama_models": [],
           "sfx": {k: len(v["files"]) for k, v in sfx.available().items()}}
    try:
        out["ffmpeg"] = tools.ffmpeg()
        out["encoder"] = (await asyncio.to_thread(tools.video_encoder))[1]
    except RuntimeError as e:
        out["ffmpeg_error"] = str(e)
    free, total = resources.memory_mb()
    workers = list(manager.workers.values())
    hi = sum(1 for w in workers if w.recorder.quality().startswith("1080"))
    out.update(ram_free_mb=free, ram_total_mb=total, streamers=len(workers),
               bandwidth_mbps=hi * 7 + (len(workers) - hi) * 4)
    info = await asyncio.to_thread(llm.resolve, settings)
    out.update(ollama=bool(info["model"]), ollama_models=info["installed"], ai_model=info["model"],
               ai_vision=info["vision"], ai_error=info["error"])
    return out


@app.post("/api/sfx/open")
def open_sfx_folder():
    """Opens the sound effects folder in Explorer so you can drop your own sounds in."""
    sfx.ensure_library()
    path = str(sfx.SFX_DIR.resolve())
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 - local app, fixed path
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass
    return {"path": path}


class SoundUrl(BaseModel):
    url: str


@app.get("/api/sfx")
def sound_library():
    return sfx.library()


@app.post("/api/sfx/{kind}/upload")
async def upload_sound(kind: str, request: Request, name: str = "sound.mp3"):
    data = await request.body()
    try:
        saved = await asyncio.to_thread(sfx.save_upload, kind, name, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"saved": saved}


@app.post("/api/sfx/{kind}/url")
async def sound_from_url(kind: str, body: SoundUrl):
    try:
        saved = await asyncio.to_thread(sfx.import_url, kind, body.url.strip())
    except (ValueError, httpx.HTTPError, OSError) as e:
        raise HTTPException(400, f"could not get that sound: {e}")
    return {"saved": saved}


@app.delete("/api/sfx/{kind}/{name}")
def delete_sound(kind: str, name: str):
    try:
        ok = sfx.delete(kind, name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "not found (built-in sounds can't be deleted)")
    return {"ok": True}


@app.get("/api/streamers/{key}/snapshot")
def streamer_snapshot(key: str):
    """Latest frame of the stream, for drawing the webcam box."""
    w = manager.workers.get(key)
    if not w:
        raise HTTPException(404, "not found")
    segs = w.recorder.segments()
    if len(segs) < 2:
        raise HTTPException(400, "no video yet (stream offline or still connecting)")
    out = w.recorder.dir / "snapshot.jpg"
    try:
        render.snapshot(segs[-2][1], out)
    except RuntimeError as e:
        raise HTTPException(500, str(e)[:200])
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/streamers/{key}/facecam")
def set_facecam(key: str, body: Facecam):
    if key not in manager.workers:
        raise HTTPException(404, "not found")
    box = None
    if body.box:
        if len(body.box) != 4:
            raise HTTPException(400, "box needs x, y, w, h")
        x, y, bw, bh = (min(1.0, max(0.0, float(v))) for v in body.box)
        if bw < 0.05 or bh < 0.05:
            raise HTTPException(400, "box too small")
        box = [round(x, 4), round(y, 4), round(min(bw, 1 - x), 4), round(min(bh, 1 - y), 4)]
    manager.update(key, facecam=box)
    return manager.workers[key].info()


@app.post("/api/shutdown")
async def shutdown():
    """Stops recordings and closes the app (the black window closes too)."""
    async def bye():
        await asyncio.sleep(0.5)
        try:
            await asyncio.wait_for(manager.shutdown(), timeout=10)
        finally:
            os._exit(0)
    asyncio.get_running_loop().create_task(bye())
    return {"ok": True}


# ------------------------------------------------------------ saving / uploading
class YtClient(BaseModel):
    data: dict


class IgToken(BaseModel):
    token: str


def _clip_or_404(clip_id):
    try:
        meta = publish.load_meta(clip_id)
    except ValueError:
        meta = None
    if not meta:
        raise HTTPException(404, "clip not found")
    return meta


@app.get("/api/publish")
def publish_status():
    return {**publish.connected(), "queue": publish.pending(),
            "download_folder": str(publish.download_folder(settings)),
            "download_default": settings["download_folder"].strip() == "",
            "download_subfolders": settings["download_subfolders"]}


@app.post("/api/clips/{clip_id}/save")
def save_clip(clip_id: str):
    _clip_or_404(clip_id)
    return {"path": str(publish.save_to_folder(clip_id, settings))}


@app.post("/api/clips/{clip_id}/upload/{platform}")
def upload_clip(clip_id: str, platform: str):
    _clip_or_404(clip_id)
    if platform not in publish.UPLOADERS:
        raise HTTPException(400, "unknown platform")
    if not publish.connected()[platform]["connected"]:
        raise HTTPException(400, f"connect {platform} first (Uploads section)")
    publish.enqueue(clip_id, [platform], manual=True)
    return {"ok": True}


class Folder(BaseModel):
    path: str = ""


_picker_lock = asyncio.Lock()


@app.post("/api/downloads/choose")
async def choose_download_folder():
    """Opens a folder picker window on this PC and saves the choice."""
    if _picker_lock.locked():
        raise HTTPException(409, "a folder window is already open (check your taskbar)")
    async with _picker_lock:
        try:
            path = await asyncio.to_thread(publish.choose_folder, str(publish.download_folder(settings)))
        except Exception as e:  # noqa: BLE001 - e.g. Python without tkinter
            raise HTTPException(500, f"couldn't open the folder window ({e}); type the path instead")
    if not path:
        return {"path": str(publish.download_folder(settings)), "changed": False}
    try:
        settings.update({"download_folder": publish.check_folder(path)})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"path": str(publish.download_folder(settings)), "changed": True}


@app.post("/api/downloads/folder")
def set_download_folder(body: Folder):
    """Sets the root download folder from a typed path ('' = back to the default)."""
    try:
        settings.update({"download_folder": publish.check_folder(body.path)})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"path": str(publish.download_folder(settings))}


@app.post("/api/downloads/open")
def open_downloads():
    path = str(publish.download_folder(settings))
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass
    return {"path": path}


@app.post("/api/youtube/client")
def youtube_client(body: YtClient):
    try:
        publish.set_yt_client(body.data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return publish.connected()


@app.get("/api/youtube/connect")
def youtube_connect():
    try:
        return RedirectResponse(publish.yt_auth_url(settings))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/youtube/callback")
def youtube_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return HTMLResponse(f"<h2>YouTube not connected: {html.escape(error)}</h2><p>Close this tab and try again.</p>")
    try:
        channel = publish.yt_finish(settings, code, state)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<h2>YouTube not connected</h2><p>{html.escape(str(e)[:300])}</p>", status_code=400)
    return HTMLResponse(f"<h2>YouTube connected: {html.escape(str(channel))}</h2><p>You can close this tab.</p>"
                        "<script>setTimeout(() => location.href = '/', 2000)</script>")


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    publish.YT_TOKEN.unlink(missing_ok=True)
    return publish.connected()


@app.post("/api/instagram/token")
def instagram_token(body: IgToken):
    try:
        user = publish.ig_set_token(body.token)
    except (ValueError, httpx.HTTPError) as e:
        raise HTTPException(400, str(e)[:300])
    return {"username": user, **publish.connected()}


@app.post("/api/instagram/disconnect")
def instagram_disconnect():
    publish.IG_TOKEN.unlink(missing_ok=True)
    return publish.connected()


@app.post("/api/streamers/{key}/rescue/{skip_id}")
def rescue_moment(key: str, skip_id: str):
    w = manager.workers.get(key)
    if not w:
        raise HTTPException(404, "not found")
    try:
        return {"job": w.rescue(skip_id)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/clips/{clip_id}/approve")
def approve_clip(clip_id: str):
    """Low-score clip you want to keep: removes the review mark and runs auto download/upload."""
    path = publish._meta_path(clip_id)
    meta = publish.load_meta(clip_id)
    if not meta:
        raise HTTPException(404, "clip not found")
    meta["low_score"] = None
    meta["approved"] = True
    publish._write(path, meta)
    publish.on_clip_ready({**meta, "clip_score": max(meta.get("clip_score") or 0, settings["auto_upload_min_score"])},
                          settings)
    return {"ok": True}
