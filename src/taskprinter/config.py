"""Configuration loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    value = _env(name)
    if not value:
        return default
    return int(value, 0)  # accepts 0x4470 as well as 17520


def _float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value else default


def _bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8080
    token: str = ""
    data_dir: Path = Path("data")
    cookie_secure: bool = False

    # Printer connection
    backend: str = "ble"  # "ble" or "mock"
    printer_address: str = ""
    printer_name: str = "SC03h"
    idle_disconnect: float = 120.0
    chunk_size: int = 180  # docs: max 180 bytes per write
    chunk_delay: float = 0.01  # 25 ms is used automatically for jobs > 20 KB
    hold_after: float = 3.0  # keep the link up this long after the last write
    connect_timeout: float = 20.0

    # Default print tuning (overridable at runtime from the web UI)
    width: int = 384
    energy: int = 17520  # 0x4470
    quality: int = 0x33
    speed: int = 0x23
    draw_mode: int = 0x00
    feed: int = 90  # dots (~11 mm) to clear the tear bar
    feed_mode: str = "a1"  # verified on hardware; 0xBD packets do not move paper
    encoding: str = "raw-lsb"  # verified on SC03h-14BF hardware
    mirror: bool = False
    rewind: bool = True  # pull the post-print feed back before the next print (verified on SC03h)
    rotate180: bool = True  # print upside down so text reads upright as it comes out

    # Safety limits
    max_rows: int = 6000  # ~75 cm of paper at 203 dpi
    max_upload_mb: int = 15

    font: str = ""
    font_bold: str = ""

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Config":
        _load_dotenv(Path(env_file))
        return cls(
            host=_env("TP_HOST", "0.0.0.0"),
            port=_int("TP_PORT", 8080),
            token=_env("TP_TOKEN"),
            data_dir=Path(_env("TP_DATA_DIR", "data")),
            cookie_secure=_bool("TP_COOKIE_SECURE", False),
            backend=_env("TP_BACKEND", "ble").lower(),
            printer_address=_env("TP_PRINTER_ADDRESS"),
            printer_name=_env("TP_PRINTER_NAME", "SC03h"),
            idle_disconnect=_float("TP_IDLE_DISCONNECT", 120.0),
            chunk_size=_int("TP_CHUNK_SIZE", 180),
            chunk_delay=_float("TP_CHUNK_DELAY", 0.01),
            hold_after=_float("TP_HOLD_AFTER", 3.0),
            connect_timeout=_float("TP_CONNECT_TIMEOUT", 20.0),
            width=_int("TP_WIDTH", 384),
            energy=_int("TP_ENERGY", 17520),
            quality=_int("TP_QUALITY", 0x33),
            speed=_int("TP_SPEED", 0x23),
            draw_mode=_int("TP_DRAW_MODE", 0x00),
            feed=_int("TP_FEED", 90),
            feed_mode=_env("TP_FEED_MODE", "a1").lower(),
            encoding=_env("TP_ENCODING", "raw-lsb").lower(),
            mirror=_bool("TP_MIRROR", False),
            rewind=_bool("TP_REWIND", True),
            rotate180=_bool("TP_ROTATE180", True),
            max_rows=_int("TP_MAX_ROWS", 6000),
            max_upload_mb=_int("TP_MAX_UPLOAD_MB", 15),
            font=_env("TP_FONT"),
            font_bold=_env("TP_FONT_BOLD"),
        )
