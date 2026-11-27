"""Versioned local catalog snapshots with ordered playlist membership."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import threading

from datetime import datetime, timezone

from .errors import AppError
from .storage import JsonStore


_rotation_lock = threading.Lock()


def checksum(payload):
    fields = {key: value for key, value in payload.items() if key != "checksum"}
    raw = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def save_snapshot(store, entries, account_id, allowed=None):
    backup_id = uuid.uuid4().hex
    tracks = {}
    playlists = []
    for entry in entries:
        ids = []
        for song in entry["songs"]:
            if allowed is None or song["id"] in allowed:
                ids.append(song["id"])
                tracks.setdefault(song["id"], {k: v for k, v in song.items()
                                               if k not in {"source_playlist_id", "source_playlist_name"}})
        if allowed is not None and not ids:
            continue
        playlists.append({**entry["summary"], "track_count": len(ids), "track_ids": ids})
    payload = {"schema_version": 2, "backup_id": backup_id, "account_id": account_id,
               "created_at": datetime.now(timezone.utc).isoformat(),
               "playlists": playlists, "tracks": list(tracks.values())}
    payload["checksum"] = checksum(payload)
    root = store.data_dir / "backups"
    backups = JsonStore(root / _account_folder(account_id))
    with _rotation_lock:
        backups.write(backup_id + ".json", payload)
        if json.loads(download_snapshot(store, backup_id, account_id)) != payload:
            raise AppError("BACKUP_UNVERIFIED", "备份写入后校验失败", 500)
        files = list(backups.data_dir.glob("*.json"))
        for legacy in root.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}", legacy.stem):
                continue
            try:
                if json.loads(legacy.read_bytes()).get("account_id") == account_id:
                    files.append(legacy)
            except (OSError, ValueError, AttributeError):
                continue
        for stale in sorted(files, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)[10:]:
            stale.unlink(missing_ok=True)
    return payload, backups.path(backup_id + ".json")


def _account_folder(account_id):
    return hashlib.sha256(json.dumps(account_id).encode()).hexdigest()


def download_snapshot(store, backup_id, account_id):
    if not re.fullmatch(r"[0-9a-f]{32}", backup_id):
        raise AppError("BACKUP_NOT_FOUND", "找不到该备份", 404)
    try:
        root = store.data_dir / "backups"
        path = root / _account_folder(account_id) / (backup_id + ".json")
        raw = (path if path.exists() else root / (backup_id + ".json")).read_bytes()
        data = json.loads(raw)
    except FileNotFoundError:
        raise AppError("BACKUP_NOT_FOUND", "找不到该备份", 404)
    except (UnicodeError, ValueError):
        raise AppError("BACKUP_UNVERIFIED", "备份校验失败", 500)
    if not isinstance(data, dict) or data.get("backup_id") != backup_id or data.get("checksum") != checksum(data):
        raise AppError("BACKUP_UNVERIFIED", "备份校验失败", 500)
    if data.get("account_id") != account_id:
        raise AppError("BACKUP_NOT_FOUND", "找不到该备份", 404)
    return raw
