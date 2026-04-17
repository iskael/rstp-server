from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Camera
    from .streamer import FrameBus

log = logging.getLogger(__name__)


class CaptureWriter:
    """Periodically writes the latest frame of a camera to disk as JPG."""

    def __init__(self, camera: "Camera", bus: "FrameBus", captures_root: str):
        self.camera = camera
        self.bus = bus
        self.captures_root = Path(captures_root).resolve()
        self.last_written_at: float | None = None
        self.files_written: int = 0

    async def run(self, stop: asyncio.Event) -> None:
        interval = max(1, self.camera.capture_interval_secs)
        log.info(
            "camera %s capture started: every %ds → %s",
            self.camera.id, interval, self._camera_dir(),
        )
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                self._write_one()
            except Exception:  # noqa: BLE001
                log.exception("camera %s capture write failed", self.camera.id)

    def _camera_dir(self) -> Path:
        return self.captures_root / str(self.camera.id)

    def _write_one(self) -> None:
        frame = self.bus.latest
        if frame is None:
            log.warning("camera %s capture skipped (no frame yet)", self.camera.id)
            return
        now = datetime.now()
        day_dir = self._camera_dir() / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        fname = now.strftime("%H%M%S-") + f"{now.microsecond // 1000:03d}.jpg"
        final_path = day_dir / fname
        tmp_path = day_dir / (fname + ".tmp")
        with open(tmp_path, "wb") as f:
            f.write(frame)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, final_path)
        self.last_written_at = time.time()
        self.files_written += 1
