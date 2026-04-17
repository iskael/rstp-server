from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from .capture import CaptureWriter
from .models import Camera

log = logging.getLogger(__name__)

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
MJPEG_BOUNDARY = "frame"
READ_CHUNK = 64 * 1024
MAX_BUFFER = 8 * 1024 * 1024  # 8 MB safety cap


@dataclass
class StreamerStatus:
    state: str = "stopped"          # starting | running | error | stopped
    last_error: str = ""
    restart_count: int = 0
    started_at: Optional[float] = None
    last_frame_at: Optional[float] = None
    frames_total: int = 0
    clients: int = 0
    extra: dict = field(default_factory=dict)


class FrameBus:
    """Holds the latest JPEG frame and notifies async waiters."""

    def __init__(self) -> None:
        self._frame: Optional[bytes] = None
        self._cond = asyncio.Condition()
        self._version = 0

    async def publish(self, frame: bytes) -> None:
        async with self._cond:
            self._frame = frame
            self._version += 1
            self._cond.notify_all()

    @property
    def latest(self) -> Optional[bytes]:
        return self._frame

    async def wait_next(self, last_seen: int) -> tuple[int, bytes]:
        async with self._cond:
            while self._version == last_seen or self._frame is None:
                await self._cond.wait()
            return self._version, self._frame


class CameraStreamer:
    """Owns one ffmpeg process + one uvicorn server on the camera's port."""

    def __init__(self, camera: Camera, captures_root: str):
        self.camera = camera
        self.captures_root = captures_root
        self.bus = FrameBus()
        self.status = StreamerStatus()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._server: Optional[uvicorn.Server] = None
        self._capture: Optional[CaptureWriter] = None
        self._proc: Optional[asyncio.subprocess.Process] = None

    # -------- lifecycle --------
    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        self._tasks.append(asyncio.create_task(
            self._ffmpeg_supervisor(), name=f"ffmpeg-sup[{self.camera.id}]"
        ))
        self._tasks.append(asyncio.create_task(
            self._serve_http(), name=f"http[{self.camera.id}]"
        ))
        if self.camera.capture_enabled:
            self._capture = CaptureWriter(self.camera, self.bus, self.captures_root)
            self._tasks.append(asyncio.create_task(
                self._capture.run(self._stop), name=f"capture[{self.camera.id}]"
            ))

    async def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.should_exit = True
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        self.status.state = "stopped"

    # -------- ffmpeg supervisor --------
    async def _ffmpeg_supervisor(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self.status.state = "starting"
            try:
                await self._run_ffmpeg_once()
                # Process exited cleanly
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status.state = "error"
                self.status.last_error = f"{type(e).__name__}: {e}"
                log.exception("camera %s ffmpeg crashed", self.camera.id)
            if self._stop.is_set():
                break
            self.status.restart_count += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    async def _run_ffmpeg_once(self) -> None:
        cmd = self._build_ffmpeg_cmd()
        log.info("camera %s starting ffmpeg: %s", self.camera.id, " ".join(cmd))
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.status.started_at = time.time()
        stderr_task = asyncio.create_task(self._drain_stderr(self._proc.stderr))
        try:
            await self._read_frames(self._proc.stdout)
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            if self._proc.returncode is None:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=3)
                except (ProcessLookupError, asyncio.TimeoutError):
                    try:
                        self._proc.kill()
                    except ProcessLookupError:
                        pass
            rc = self._proc.returncode
            self._proc = None
            if rc not in (0, None) and not self._stop.is_set():
                raise RuntimeError(f"ffmpeg exited rc={rc}")

    def _build_ffmpeg_cmd(self) -> list[str]:
        c = self.camera
        vf = [f"fps={c.fps}"]
        if c.width and c.height:
            vf.append(f"scale={c.width}:{c.height}")
        return [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-fflags", "nobuffer",
            "-i", c.rtsp_url,
            "-an",
            "-vf", ",".join(vf),
            "-q:v", str(c.jpeg_quality),
            "-f", "mjpeg", "pipe:1",
        ]

    async def _drain_stderr(self, stream: Optional[asyncio.StreamReader]) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                log.warning("camera %s ffmpeg: %s", self.camera.id, text)
                self.status.last_error = text

    async def _read_frames(self, stream: Optional[asyncio.StreamReader]) -> None:
        if stream is None:
            raise RuntimeError("ffmpeg stdout missing")
        buf = bytearray()
        while not self._stop.is_set():
            chunk = await stream.read(READ_CHUNK)
            if not chunk:
                return
            buf.extend(chunk)
            if len(buf) > MAX_BUFFER:
                # malformed stream: drop everything before last SOI
                idx = buf.rfind(JPEG_SOI)
                if idx > 0:
                    del buf[:idx]
                else:
                    buf.clear()
            # Extract complete JPEGs
            while True:
                soi = buf.find(JPEG_SOI)
                if soi < 0:
                    buf.clear()
                    break
                if soi > 0:
                    del buf[:soi]
                eoi = buf.find(JPEG_EOI, 2)
                if eoi < 0:
                    break
                frame = bytes(buf[: eoi + 2])
                del buf[: eoi + 2]
                self.status.state = "running"
                self.status.last_frame_at = time.time()
                self.status.frames_total += 1
                await self.bus.publish(frame)

    # -------- HTTP server (per camera) --------
    async def _serve_http(self) -> None:
        app = self._make_http_app()
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.camera.http_port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        try:
            await self._server.serve()
        except asyncio.CancelledError:
            self._server.should_exit = True
            raise

    def _make_http_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        bus = self.bus
        status = self.status

        @app.get("/")
        async def root(request: Request, action: str | None = None):
            if action == "stream":
                return await stream(request)
            return await snapshot()

        @app.get("/snapshot.jpg")
        @app.get("/snapshot")
        async def snapshot_jpg():
            return await snapshot()

        @app.get("/stream.mjpg")
        @app.get("/video_feed")
        async def stream_mjpg(request: Request):
            return await stream(request)

        async def snapshot() -> Response:
            frame = bus.latest
            if frame is None:
                return Response(status_code=503, content=b"no frame yet")
            return Response(content=frame, media_type="image/jpeg",
                            headers={"Cache-Control": "no-store"})

        async def stream(request: Request) -> StreamingResponse:
            async def gen():
                status.clients += 1
                try:
                    last_seen = -1
                    # send latest immediately if present
                    if bus.latest is not None:
                        yield _mjpeg_part(bus.latest)
                    while True:
                        if await request.is_disconnected():
                            return
                        try:
                            last_seen, frame = await asyncio.wait_for(
                                bus.wait_next(last_seen), timeout=10.0
                            )
                        except asyncio.TimeoutError:
                            continue
                        yield _mjpeg_part(frame)
                finally:
                    status.clients = max(0, status.clients - 1)

            return StreamingResponse(
                gen(),
                media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Pragma": "no-cache",
                    "Connection": "close",
                },
            )

        return app


def _mjpeg_part(frame: bytes) -> bytes:
    return (
        b"--" + MJPEG_BOUNDARY.encode() + b"\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
        + frame + b"\r\n"
    )
