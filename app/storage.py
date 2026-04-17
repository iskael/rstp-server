from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .models import Camera, CameraIn, Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  name                  TEXT NOT NULL,
  rtsp_url              TEXT NOT NULL,
  http_port             INTEGER NOT NULL UNIQUE,
  width                 INTEGER,
  height                INTEGER,
  fps                   INTEGER NOT NULL DEFAULT 10,
  jpeg_quality          INTEGER NOT NULL DEFAULT 5,
  enabled               INTEGER NOT NULL DEFAULT 1,
  capture_enabled       INTEGER NOT NULL DEFAULT 0,
  capture_interval_secs INTEGER NOT NULL DEFAULT 60,
  created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "captures_dir": "./data/captures",
    "disk_high_watermark_pct": "75",
    "disk_low_watermark_pct": "65",
    "janitor_interval_secs": "300",
}


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                self._conn.execute(
                    "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                    (k, v),
                )

    # ----- cameras -----
    def list_cameras(self) -> list[Camera]:
        rows = self._conn.execute(
            "SELECT * FROM cameras ORDER BY id ASC"
        ).fetchall()
        return [self._row_to_camera(r) for r in rows]

    def get_camera(self, cam_id: int) -> Optional[Camera]:
        row = self._conn.execute(
            "SELECT * FROM cameras WHERE id=?", (cam_id,)
        ).fetchone()
        return self._row_to_camera(row) if row else None

    def create_camera(self, data: CameraIn) -> Camera:
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cur = self._conn.execute(
                """INSERT INTO cameras(
                    name, rtsp_url, http_port, width, height, fps,
                    jpeg_quality, enabled, capture_enabled,
                    capture_interval_secs, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    data.name, data.rtsp_url, data.http_port,
                    data.width, data.height, data.fps,
                    data.jpeg_quality, int(data.enabled),
                    int(data.capture_enabled),
                    data.capture_interval_secs, now,
                ),
            )
        return self.get_camera(cur.lastrowid)  # type: ignore[return-value]

    def update_camera(self, cam_id: int, data: CameraIn) -> Optional[Camera]:
        with self._conn:
            self._conn.execute(
                """UPDATE cameras SET
                    name=?, rtsp_url=?, http_port=?, width=?, height=?,
                    fps=?, jpeg_quality=?, enabled=?, capture_enabled=?,
                    capture_interval_secs=?
                   WHERE id=?""",
                (
                    data.name, data.rtsp_url, data.http_port,
                    data.width, data.height, data.fps,
                    data.jpeg_quality, int(data.enabled),
                    int(data.capture_enabled),
                    data.capture_interval_secs, cam_id,
                ),
            )
        return self.get_camera(cam_id)

    def delete_camera(self, cam_id: int) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM cameras WHERE id=?", (cam_id,))
        return cur.rowcount > 0

    @staticmethod
    def _row_to_camera(row: sqlite3.Row) -> Camera:
        return Camera(
            id=row["id"],
            name=row["name"],
            rtsp_url=row["rtsp_url"],
            http_port=row["http_port"],
            width=row["width"],
            height=row["height"],
            fps=row["fps"],
            jpeg_quality=row["jpeg_quality"],
            enabled=bool(row["enabled"]),
            capture_enabled=bool(row["capture_enabled"]),
            capture_interval_secs=row["capture_interval_secs"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # ----- settings -----
    def get_settings(self) -> Settings:
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        kv = {r["key"]: r["value"] for r in rows}
        return Settings(
            captures_dir=kv.get("captures_dir", "./data/captures"),
            disk_high_watermark_pct=float(kv.get("disk_high_watermark_pct", "75")),
            disk_low_watermark_pct=float(kv.get("disk_low_watermark_pct", "65")),
            janitor_interval_secs=int(kv.get("janitor_interval_secs", "300")),
        )

    def update_settings(self, s: Settings) -> Settings:
        items: Iterable[tuple[str, str]] = (
            ("captures_dir", s.captures_dir),
            ("disk_high_watermark_pct", str(s.disk_high_watermark_pct)),
            ("disk_low_watermark_pct", str(s.disk_low_watermark_pct)),
            ("janitor_interval_secs", str(s.janitor_interval_secs)),
        )
        with self._conn:
            self._conn.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                items,
            )
        return self.get_settings()

    def close(self) -> None:
        self._conn.close()
