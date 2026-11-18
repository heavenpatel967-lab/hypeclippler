"""Web dashboard + API. Start with:  python run.py"""
import asyncio
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import VERSION, clipper, llm, render, resources, sfx, tools
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
    return clipper.list_clips()


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
