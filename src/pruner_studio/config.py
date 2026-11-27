from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


APP_NAME = "Pruner Studio"
DEFAULT_API_BASE = "https://music.163.com"
TRUSTED_API_HOSTS = frozenset({
    "music.163.com",
    "interface.music.163.com",
    "interface3.music.163.com",
})
TEST_API_HOSTS = frozenset({
    "fake.invalid",
    "127.0.0.1",
    "localhost",
    "::1",
})


def validate_api_base(api_base: str, *, allow_test_hosts: bool = False) -> str:
    """Ensure NetEase API base points only to trusted HTTPS NetEase hosts."""
    if not isinstance(api_base, str) or not api_base.strip():
        raise ValueError("api_base must be a non-empty URL")
    cleaned = api_base.strip().rstrip("/")
    parsed = urlsplit(cleaned)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("api_base must not contain credentials, query, or fragment")
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("api_base has an invalid port") from exc
    if hostname in TRUSTED_API_HOSTS:
        if parsed.scheme != "https" or port not in {None, 443}:
            raise ValueError("trusted NetEase api_base must use HTTPS on standard port 443")
        return cleaned
    if allow_test_hosts and hostname in TEST_API_HOSTS:
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("test api_base must use HTTP or HTTPS")
        return cleaned
    raise ValueError(
        f"untrusted api_base host {hostname!r}; only trusted NetEase HTTPS hosts are permitted"
    )


def is_trusted_netease_api_base(api_base: str) -> bool:
    try:
        validate_api_base(api_base, allow_test_hosts=False)
        return True
    except ValueError:
        return False


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
    import_max_body_bytes: int = 16 * 1024 * 1024

    def __post_init__(self) -> None:
        validate_api_base(self.api_base, allow_test_hosts=True)

    @classmethod
    def default(cls) -> "Settings":
        try:
            port = int(os.environ.get("PRUNER_STUDIO_PORT", "8765"))
        except ValueError:
            port = 8765
        if not 0 <= port <= 65535:
            port = 8765
        raw_api_base = os.environ.get("PRUNER_STUDIO_API_BASE", DEFAULT_API_BASE)
        validated_api_base = validate_api_base(raw_api_base, allow_test_hosts=False)
        return cls(
            data_dir=Path(os.environ.get("PRUNER_STUDIO_DATA_DIR") or user_data_dir()),
            web_dir=bundled_web_dir(),
            api_base=validated_api_base,
            port=port,
        )
