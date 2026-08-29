from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import AppError


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
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AppError(
                    "LOCAL_DATA_CORRUPT",
                    f"本地数据文件 {name} 已损坏；原文件已保留，请先恢复或移走后再继续",
                    500,
                ) from exc
            except OSError as exc:
                raise AppError(
                    "LOCAL_DATA_UNREADABLE",
                    f"无法读取本地数据文件 {name}；未对其进行覆盖",
                    500,
                ) from exc

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

    def quarantine(self, name: str) -> Path | None:
        target = self.path(name)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantined = target.with_name(f"{target.stem}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}.json")
        with self._lock:
            try:
                os.replace(target, quarantined)
            except FileNotFoundError:
                return None
        return quarantined


class SessionStore:
    FILE_NAME = "session.json"

    def __init__(self, store: JsonStore):
        self.store = store

    def load_cookie(self) -> str | None:
        missing = object()
        try:
            value = self.store.read(self.FILE_NAME, missing)
        except AppError as exc:
            if exc.code != "LOCAL_DATA_CORRUPT":
                raise
            self.store.quarantine(self.FILE_NAME)
            return None
        if value is missing:
            return None
        cookie = value.get("cookie") if isinstance(value, dict) else None
        if not isinstance(cookie, str) or not cookie or "\r" in cookie or "\n" in cookie:
            self.store.quarantine(self.FILE_NAME)
            return None
        return cookie

    def save_cookie(self, cookie: str) -> None:
        if not cookie or "\r" in cookie or "\n" in cookie:
            raise ValueError("invalid session cookie")
        self.store.write(self.FILE_NAME, {"cookie": cookie})

    def clear(self) -> None:
        self.store.delete(self.FILE_NAME)
