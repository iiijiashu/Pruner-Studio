from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class JsonStore:
    """Small JSON store with serialized, atomic writes."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, name: str) -> Path:
        if not name or Path(name).name != name or not name.endswith(".json"):
            raise ValueError("invalid store name")
        return self.data_dir / name

    def read(self, name: str, default: Any) -> Any:
        target = self.path(name)
        with self._lock:
            try:
                with target.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except FileNotFoundError:
                return default
            except (OSError, json.JSONDecodeError):
                return default

    def write(self, name: str, value: Any) -> None:
        target = self.path(name)
        payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        with self._lock:
            fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def delete(self, name: str) -> None:
        target = self.path(name)
        with self._lock:
            try:
                target.unlink()
            except FileNotFoundError:
                pass


class SessionStore:
    FILE_NAME = "session.json"

    def __init__(self, store: JsonStore):
        self.store = store

    def load_cookie(self) -> str | None:
        value = self.store.read(self.FILE_NAME, {})
        cookie = value.get("cookie") if isinstance(value, dict) else None
        return cookie if isinstance(cookie, str) and cookie else None

    def save_cookie(self, cookie: str) -> None:
        if not cookie or "\r" in cookie or "\n" in cookie:
            raise ValueError("invalid session cookie")
        self.store.write(self.FILE_NAME, {"cookie": cookie})

    def clear(self) -> None:
        self.store.delete(self.FILE_NAME)
