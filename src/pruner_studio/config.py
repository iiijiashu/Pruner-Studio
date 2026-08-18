from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "Pruner Studio"
DEFAULT_API_BASE = "https://music.163.com"


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_NAME


def bundled_web_dir() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "web"
    return Path(__file__).resolve().parents[2] / "web"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    web_dir: Path
    api_base: str = DEFAULT_API_BASE
    host: str = "127.0.0.1"
    port: int = 8765
    max_body_bytes: int = 1_048_576
    max_batch_size: int = 500
    alternative_duration_tolerance_ms: int = 5_000

    @classmethod
    def default(cls) -> "Settings":
        try:
            port = int(os.environ.get("PRUNER_STUDIO_PORT", "8765"))
        except ValueError:
            port = 8765
        if not 1 <= port <= 65535:
            port = 8765
        return cls(
            data_dir=user_data_dir(),
            web_dir=bundled_web_dir(),
            api_base=os.environ.get("PRUNER_STUDIO_API_BASE", DEFAULT_API_BASE),
            port=port,
        )
