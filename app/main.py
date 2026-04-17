from __future__ import annotations

import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .janitor import DiskJanitor
from .manager import CameraManager
from .models import CameraIn, Settings
from .storage import Storage

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("rstp-server")

DB_PATH = os.environ.get("RSTP_DB", "./data/cameras.db")
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

storage = Storage(DB_PATH)
manager = CameraManager(storage)
janitor = DiskJanitor(settings_provider=storage.get_settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting rstp-server")
    Path(storage.get_settings().captures_dir).mkdir(parents=True, exist_ok=True)
    await manager.start_all()
    janitor.start()
    try:
        yield
    finally:
        log.info("stopping rstp-server")
        await janitor.stop()
        await manager.stop_all()
        storage.close()


app = FastAPI(title="rstp-server", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------- GUI ----------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cams = storage.list_cameras()
    settings = storage.get_settings()
    statuses = {cid: s.status for cid, s in manager.list_streamers().items()}
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "cameras": cams,
            "statuses": statuses,
            "settings": settings,
            "disk": _disk_payload(settings),
            "janitor": janitor.state.last_report,
        },
    )


@app.post("/cameras")
async def create_camera(
    name: str = Form(...),
    rtsp_url: str = Form(...),
    http_port: int = Form(...),
    fps: int = Form(10),
    jpeg_quality: int = Form(5),
    width: str = Form(""),
    height: str = Form(""),
    enabled: str = Form("on"),
    capture_enabled: str = Form(""),
    capture_interval_secs: int = Form(60),
):
    data = CameraIn(
        name=name,
        rtsp_url=rtsp_url,
        http_port=http_port,
        width=int(width) if width.strip() else None,
        height=int(height) if height.strip() else None,
        fps=fps,
        jpeg_quality=jpeg_quality,
        enabled=enabled == "on",
        capture_enabled=capture_enabled == "on",
        capture_interval_secs=capture_interval_secs,
    )
    cam = storage.create_camera(data)
    await manager.add_or_replace(cam)
    return RedirectResponse(url="/", status_code=303)


@app.post("/cameras/{cam_id}/update")
async def update_camera(
    cam_id: int,
    name: str = Form(...),
    rtsp_url: str = Form(...),
    http_port: int = Form(...),
    fps: int = Form(10),
    jpeg_quality: int = Form(5),
    width: str = Form(""),
    height: str = Form(""),
    enabled: str = Form(""),
    capture_enabled: str = Form(""),
    capture_interval_secs: int = Form(60),
):
    if storage.get_camera(cam_id) is None:
        raise HTTPException(404, "camera not found")
    data = CameraIn(
        name=name,
        rtsp_url=rtsp_url,
        http_port=http_port,
        width=int(width) if width.strip() else None,
        height=int(height) if height.strip() else None,
        fps=fps,
        jpeg_quality=jpeg_quality,
        enabled=enabled == "on",
        capture_enabled=capture_enabled == "on",
        capture_interval_secs=capture_interval_secs,
    )
    cam = storage.update_camera(cam_id, data)
    if cam is not None:
        await manager.add_or_replace(cam)
    return RedirectResponse(url="/", status_code=303)


@app.post("/cameras/{cam_id}/delete")
async def delete_camera(cam_id: int):
    await manager.remove(cam_id)
    storage.delete_camera(cam_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/cameras/{cam_id}/toggle")
async def toggle_camera(cam_id: int):
    cam = storage.get_camera(cam_id)
    if cam is None:
        raise HTTPException(404, "camera not found")
    cam_in = CameraIn(**cam.model_dump(exclude={"id", "created_at"}))
    cam_in.enabled = not cam_in.enabled
    updated = storage.update_camera(cam_id, cam_in)
    if updated is not None:
        await manager.add_or_replace(updated)
    return RedirectResponse(url="/", status_code=303)


@app.post("/settings")
async def update_settings(
    captures_dir: str = Form(...),
    disk_high_watermark_pct: float = Form(...),
    disk_low_watermark_pct: float = Form(...),
    janitor_interval_secs: int = Form(...),
):
    s = Settings(
        captures_dir=captures_dir,
        disk_high_watermark_pct=disk_high_watermark_pct,
        disk_low_watermark_pct=disk_low_watermark_pct,
        janitor_interval_secs=janitor_interval_secs,
    )
    storage.update_settings(s)
    return RedirectResponse(url="/", status_code=303)


@app.post("/janitor/run")
async def run_janitor_now():
    report = await janitor.run_once()
    return JSONResponse(report.__dict__)


# ---------------- API ----------------

@app.get("/api/cameras")
async def api_list_cameras():
    cams = storage.list_cameras()
    statuses = manager.list_streamers()
    return [
        {
            **cam.model_dump(),
            "status": (statuses[cam.id].status.__dict__
                       if cam.id in statuses else None),
        }
        for cam in cams
    ]


@app.get("/api/disk")
async def api_disk():
    settings = storage.get_settings()
    return {
        **_disk_payload(settings),
        "last_purge": (janitor.state.last_report.__dict__
                       if janitor.state.last_report else None),
    }


# ---------------- helpers ----------------

def _disk_payload(settings: Settings) -> dict:
    captures_dir = Path(settings.captures_dir).resolve()
    captures_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(captures_dir)
    return {
        "captures_dir": str(captures_dir),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "used_pct": round(usage.used / usage.total * 100, 2),
        "high_watermark_pct": settings.disk_high_watermark_pct,
        "low_watermark_pct": settings.disk_low_watermark_pct,
    }
