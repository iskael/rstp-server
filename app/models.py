from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class CameraIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    rtsp_url: str = Field(min_length=8)
    http_port: int = Field(ge=1024, le=65535)
    width: Optional[int] = Field(default=None, ge=16, le=7680)
    height: Optional[int] = Field(default=None, ge=16, le=4320)
    fps: int = Field(default=10, ge=1, le=60)
    jpeg_quality: int = Field(default=5, ge=1, le=31)
    enabled: bool = True
    capture_enabled: bool = False
    capture_interval_secs: int = Field(default=60, ge=1, le=86400)

    @field_validator("rtsp_url")
    @classmethod
    def _rtsp_scheme(cls, v: str) -> str:
        if not v.lower().startswith(("rtsp://", "rtsps://")):
            raise ValueError("rtsp_url must start with rtsp:// or rtsps://")
        return v


class Camera(CameraIn):
    id: int
    created_at: datetime


class Settings(BaseModel):
    captures_dir: str = "./data/captures"
    disk_high_watermark_pct: float = Field(default=75.0, ge=1.0, le=99.0)
    disk_low_watermark_pct: float = Field(default=65.0, ge=0.0, le=99.0)
    janitor_interval_secs: int = Field(default=300, ge=10, le=86400)

    @field_validator("disk_low_watermark_pct")
    @classmethod
    def _low_lt_high(cls, v: float, info):
        high = info.data.get("disk_high_watermark_pct")
        if high is not None and v >= high:
            raise ValueError("disk_low_watermark_pct must be < disk_high_watermark_pct")
        return v
