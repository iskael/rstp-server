from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .models import Camera
from .storage import Storage
from .streamer import CameraStreamer

log = logging.getLogger(__name__)


class CameraManager:
    """Owns the running streamers and keeps them in sync with storage."""

    def __init__(self, storage: Storage):
        self.storage = storage
        self._streamers: dict[int, CameraStreamer] = {}
        self._lock = asyncio.Lock()

    def list_streamers(self) -> dict[int, CameraStreamer]:
        return dict(self._streamers)

    def get_streamer(self, cam_id: int) -> Optional[CameraStreamer]:
        return self._streamers.get(cam_id)

    async def start_all(self) -> None:
        for cam in self.storage.list_cameras():
            if cam.enabled:
                await self._start_one(cam)

    async def stop_all(self) -> None:
        async with self._lock:
            tasks = [s.stop() for s in self._streamers.values()]
            self._streamers.clear()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def add_or_replace(self, cam: Camera) -> None:
        async with self._lock:
            existing = self._streamers.pop(cam.id, None)
        if existing is not None:
            await existing.stop()
        if cam.enabled:
            await self._start_one(cam)

    async def remove(self, cam_id: int) -> None:
        async with self._lock:
            existing = self._streamers.pop(cam_id, None)
        if existing is not None:
            await existing.stop()

    async def _start_one(self, cam: Camera) -> None:
        captures_dir = self.storage.get_settings().captures_dir
        s = CameraStreamer(cam, captures_root=captures_dir)
        async with self._lock:
            self._streamers[cam.id] = s
        try:
            await s.start()
        except Exception:  # noqa: BLE001
            log.exception("failed to start camera %s", cam.id)
