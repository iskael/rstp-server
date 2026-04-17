from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

REVAL_EVERY_N = 100  # re-check disk_usage every N deletions


@dataclass
class JanitorReport:
    started_at: float = 0.0
    finished_at: float = 0.0
    used_pct_before: float = 0.0
    used_pct_after: float = 0.0
    files_deleted: int = 0
    bytes_freed: int = 0
    purged: bool = False
    error: str = ""


@dataclass
class JanitorState:
    last_report: Optional[JanitorReport] = None
    history: list[JanitorReport] = field(default_factory=list)
    history_max: int = 20


class DiskJanitor:
    """Watches disk usage of the captures directory and prunes oldest JPGs.

    Settings (high/low watermarks, interval, captures_dir) are read fresh on
    every cycle via ``settings_provider`` so changes from the GUI are picked
    up without restarting the janitor.
    """

    def __init__(self, settings_provider: Callable):
        self.settings_provider = settings_provider
        self.state = JanitorState()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="disk-janitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("janitor cycle failed")
            interval = self.settings_provider().janitor_interval_secs
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                continue

    async def run_once(self) -> JanitorReport:
        report = JanitorReport(started_at=time.time())
        try:
            settings = self.settings_provider()
            captures_dir = Path(settings.captures_dir).resolve()
            captures_dir.mkdir(parents=True, exist_ok=True)
            high = settings.disk_high_watermark_pct
            low = settings.disk_low_watermark_pct
            usage = shutil.disk_usage(captures_dir)
            used_pct = usage.used / usage.total * 100
            report.used_pct_before = used_pct
            report.used_pct_after = used_pct
            if used_pct < high:
                log.debug(
                    "janitor: disk %.2f%% < high=%.2f%%, no purge", used_pct, high
                )
                return report
            log.info(
                "janitor: disk %.2f%% ≥ high=%.2f%%, purging down to %.2f%%",
                used_pct, high, low,
            )
            report.purged = True
            await asyncio.to_thread(self._purge_until, captures_dir, low, report)
            usage = shutil.disk_usage(captures_dir)
            report.used_pct_after = usage.used / usage.total * 100
            log.info(
                "janitor: purged %d files (%.1f MB), disk now %.2f%%",
                report.files_deleted,
                report.bytes_freed / 1_048_576,
                report.used_pct_after,
            )
        except Exception as e:  # noqa: BLE001
            report.error = f"{type(e).__name__}: {e}"
            log.exception("janitor error")
        finally:
            report.finished_at = time.time()
            self.state.last_report = report
            self.state.history.append(report)
            if len(self.state.history) > self.state.history_max:
                self.state.history = self.state.history[-self.state.history_max:]
        return report

    @staticmethod
    def _iter_jpgs_by_mtime(root: Path):
        """Yield all jpg files under root sorted by mtime ascending."""
        files: list[tuple[float, Path, int]] = []
        for p in root.rglob("*.jpg"):
            try:
                st = p.stat()
            except FileNotFoundError:
                continue
            files.append((st.st_mtime, p, st.st_size))
        files.sort(key=lambda x: x[0])
        return files

    def _purge_until(self, captures_dir: Path, target_pct: float, report: JanitorReport) -> None:
        files = self._iter_jpgs_by_mtime(captures_dir)
        if not files:
            log.info("janitor: nothing to purge (no jpg files)")
            return
        deleted_since_check = 0
        for _, path, size in files:
            try:
                path.unlink()
                report.files_deleted += 1
                report.bytes_freed += size
                deleted_since_check += 1
            except FileNotFoundError:
                continue
            except OSError as e:
                log.warning("janitor: cannot delete %s: %s", path, e)
                continue
            if deleted_since_check >= REVAL_EVERY_N:
                deleted_since_check = 0
                usage = shutil.disk_usage(captures_dir)
                pct = usage.used / usage.total * 100
                if pct <= target_pct:
                    break
        # final cleanup of empty day dirs
        for day_dir in sorted(captures_dir.glob("*/*"), reverse=True):
            try:
                if day_dir.is_dir() and not any(day_dir.iterdir()):
                    day_dir.rmdir()
            except OSError:
                pass
