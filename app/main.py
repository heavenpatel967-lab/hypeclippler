"""Web dashboard + API. Start with:  python run.py"""
import asyncio
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import chat, clipper, sfx, tools
from .config import CLIPS, STATIC, Settings
from .manager import Manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("app")

settings = Settings()
manager = Manager(settings)


def _apply_settings():
    chat.KICK_PUSHER.update(key=settings["kick_pusher_key"], cluster=settings["kick_pusher_cluster"])


_apply_settings()


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


class AddStreamer(BaseModel):
    channel: str
    platform: str | None = None
    topic: str = ""
    show_chat: bool = False


class UpdateStreamer(BaseModel):
    topic: str | None = None
    auto_clip: bool | None = None
    show_chat: bool | None = None


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
    _apply_settings()
    return settings.public()


@app.get("/api/health")
async def health():
    out = {"ffmpeg": None, "encoder": None, "ollama": False, "ollama_models": [],
           "sfx": {k: len(v) for k, v in sfx.available().items()}}
    try:
        out["ffmpeg"] = tools.ffmpeg()
        out["encoder"] = (await asyncio.to_thread(tools.video_encoder))[1]
    except RuntimeError as e:
        out["ffmpeg_error"] = str(e)
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(settings["ollama_url"].rstrip("/") + "/api/tags")
            out["ollama"] = r.status_code == 200
            out["ollama_models"] = [m["name"] for m in r.json().get("models", [])]
    except Exception:  # noqa: BLE001
        pass
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
