from __future__ import annotations

import hashlib
import re
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .errors import AppError
from .storage import JsonStore


class OperationJournal:
    """Durable write-ahead records for remote mutations with unknown outcomes."""

    FILE_NAME = "pending-operations.json"
    VERSION = 1
    ACTIONS = {"like", "unlike", "remove_tracks", "create_playlist", "add_tracks"}
    ACTIVE_STATES = {"prepared", "sent_unknown"}
    STATES = ACTIVE_STATES | {"ignored"}

    def __init__(self, store: JsonStore):
        self.store = store
        self._lock = threading.RLock()

    @staticmethod
    def account_scope(user_id: int) -> str:
        return hashlib.sha256(f"pruner-studio-account:{int(user_id)}".encode("utf-8")).hexdigest()

    def pending_for(self, user_id: int) -> list[dict[str, Any]]:
        scope = self.account_scope(user_id)
        with self._lock:
            operations = self._load()["operations"]
            return [self._summary(item) for item in operations if item["account_scope"] == scope and item["state"] in self.ACTIVE_STATES]

    def assert_allowed(self, user_id: int, keys: list[str]) -> None:
        scope = self.account_scope(user_id)
        key_set = set(keys)
        with self._lock:
            operations = self._load()["operations"]
            if any(item["account_scope"] == scope and item["state"] in self.ACTIVE_STATES for item in operations):
                raise AppError("OPERATION_PENDING", "存在待核对的远端操作；仅允许先只读核对或明确忽略", 409)
            if any(
                item["account_scope"] == scope
                and item["state"] == "ignored"
                and key_set.intersection(self._protected_keys(item))
                for item in operations
            ):
                raise AppError("OPERATION_IGNORED", "同一远端操作此前结果未知并已忽略，禁止盲目重试", 409)

    def begin(
        self,
        user_id: int,
        action: str,
        keys: list[str],
        target: dict[str, Any],
        *,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        if action not in self.ACTIONS:
            raise ValueError("invalid journal action")
        normalized_keys = list(dict.fromkeys(str(key) for key in keys if str(key)))
        if not normalized_keys:
            raise ValueError("journal keys are required")
        scope = self.account_scope(user_id)
        with self._lock:
            document = self._load()
            operations = document["operations"]
            if any(item["account_scope"] == scope and item["state"] in self.ACTIVE_STATES for item in operations):
                raise AppError("OPERATION_PENDING", "存在待核对的远端操作；仅允许先只读核对或明确忽略", 409)
            key_set = set(normalized_keys)
            if any(
                item["account_scope"] == scope
                and item["state"] == "ignored"
                and key_set.intersection(self._protected_keys(item))
                for item in operations
            ):
                raise AppError("OPERATION_IGNORED", "同一远端操作此前结果未知并已忽略，禁止盲目重试", 409)
            now = _now()
            entry = {
                "operation_id": uuid.uuid4().hex,
                "account_scope": scope,
                "action": action,
                "keys": normalized_keys,
                "guard_keys": [],
                "state": "prepared",
                "created_at": now,
                "updated_at": now,
                "target": deepcopy(target),
                "parent_id": parent_id,
                "resource_id": None,
                "last_error": None,
            }
            operations.append(entry)
            self._save(document)
            return deepcopy(entry)

    def mark_sent(
        self,
        operation_id: str,
        *,
        resource_id: int | None = None,
        target_updates: dict[str, Any] | None = None,
        error: AppError | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            document = self._load()
            entry = self._find(document, operation_id)
            if entry["state"] not in self.ACTIVE_STATES:
                raise AppError("OPERATION_STATE_INVALID", "操作记录已结束，不能再次修改", 409)
            entry["state"] = "sent_unknown"
            if resource_id is not None:
                entry["resource_id"] = int(resource_id)
            if target_updates:
                entry["target"].update(deepcopy(target_updates))
            if error is not None:
                entry["last_error"] = {"code": error.code, "message": error.message}
            entry["updated_at"] = _now()
            self._save(document)
            return deepcopy(entry)

    def retain_only(
        self,
        operation_id: str,
        *,
        keys: list[str],
        song_ids: list[int],
        error: AppError | None = None,
    ) -> None:
        with self._lock:
            document = self._load()
            entry = self._find(document, operation_id)
            entry["state"] = "sent_unknown"
            entry["keys"] = list(dict.fromkeys(keys))
            entry["target"]["song_ids"] = list(dict.fromkeys(int(value) for value in song_ids))
            if error is not None:
                entry["last_error"] = {"code": error.code, "message": error.message}
            entry["updated_at"] = _now()
            self._save(document)

    def transition_create_to_add(
        self,
        operation_id: str,
        *,
        keys: list[str],
        playlist_id: int,
        song_ids: list[int],
    ) -> dict[str, Any]:
        normalized_keys = list(dict.fromkeys(str(key) for key in keys if str(key)))
        if not normalized_keys:
            raise ValueError("journal keys are required")
        normalized_song_ids = list(dict.fromkeys(int(value) for value in song_ids))
        with self._lock:
            document = self._load()
            entry = self._find(document, operation_id)
            if entry["action"] != "create_playlist" or entry["state"] not in self.ACTIVE_STATES:
                raise AppError("OPERATION_STATE_INVALID", "创建操作不能转换为加歌操作", 409)
            entry.update(
                {
                    "action": "add_tracks",
                    "keys": normalized_keys,
                    "guard_keys": list(entry["keys"]),
                    "state": "prepared",
                    "updated_at": _now(),
                    "target": {
                        "playlist_id": int(playlist_id),
                        "source_playlist_id": int(playlist_id),
                        "song_ids": normalized_song_ids,
                    },
                    "parent_id": None,
                    "resource_id": int(playlist_id),
                    "last_error": None,
                }
            )
            self._save(document)
            return deepcopy(entry)

    def get(self, user_id: int, operation_id: str) -> dict[str, Any]:
        scope = self.account_scope(user_id)
        with self._lock:
            entry = self._find(self._load(), operation_id)
            if entry["account_scope"] != scope:
                raise AppError("OPERATION_NOT_FOUND", "找不到这条待核对操作", 404)
            return deepcopy(entry)

    def resolve(self, operation_id: str) -> None:
        with self._lock:
            document = self._load()
            before = len(document["operations"])
            document["operations"] = [item for item in document["operations"] if item["operation_id"] != operation_id]
            if len(document["operations"]) == before:
                raise AppError("OPERATION_NOT_FOUND", "找不到这条待核对操作", 404)
            self._save(document)

    def resolve_keys(self, user_id: int, keys: list[str]) -> None:
        scope = self.account_scope(user_id)
        resolved = set(keys)
        if not resolved:
            return
        with self._lock:
            document = self._load()
            changed = False
            retained: list[dict[str, Any]] = []
            for entry in document["operations"]:
                if entry["account_scope"] != scope or entry["state"] not in self.ACTIVE_STATES:
                    retained.append(entry)
                    continue
                remaining = [key for key in entry["keys"] if key not in resolved]
                if len(remaining) == len(entry["keys"]):
                    retained.append(entry)
                    continue
                changed = True
                if remaining:
                    entry["keys"] = remaining
                    song_ids = entry["target"].get("song_ids")
                    if isinstance(song_ids, list):
                        entry["target"]["song_ids"] = [
                            value for value in song_ids if any(key.endswith(f":{value}") for key in remaining)
                        ]
                    entry["updated_at"] = _now()
                    retained.append(entry)
            if changed:
                document["operations"] = retained
                self._save(document)

    def active_unrejected_keys(self, user_id: int, keys: list[str]) -> tuple[set[str], list[str]]:
        """Return keys currently in active journal entries without explicit upstream rejection."""
        scope = self.account_scope(user_id)
        key_set = set(keys)
        matched_keys: set[str] = set()
        op_ids: list[str] = []
        with self._lock:
            operations = self._load()["operations"]
            for entry in operations:
                if entry["account_scope"] != scope or entry["state"] not in self.ACTIVE_STATES:
                    continue
                last_error = entry.get("last_error")
                if isinstance(last_error, dict) and last_error.get("code") == "UPSTREAM_REJECTED":
                    continue
                overlap = key_set.intersection(entry["keys"])
                if overlap:
                    matched_keys.update(overlap)
                    op_ids.append(entry["operation_id"])
        return matched_keys, op_ids

    def ignore(self, user_id: int, operation_id: str) -> dict[str, Any]:
        scope = self.account_scope(user_id)
        with self._lock:
            document = self._load()
            entry = self._find(document, operation_id)
            if entry["account_scope"] != scope:
                raise AppError("OPERATION_NOT_FOUND", "找不到这条待核对操作", 404)
            if entry["state"] not in self.ACTIVE_STATES:
                raise AppError("OPERATION_STATE_INVALID", "这条操作已经处理", 409)
            entry["state"] = "ignored"
            entry["updated_at"] = _now()
            self._save(document)
            return self._summary(entry)

    def _load(self) -> dict[str, Any]:
        value = self.store.read(self.FILE_NAME, {"version": self.VERSION, "operations": []})
        if not isinstance(value, dict) or value.get("version") != self.VERSION or not isinstance(value.get("operations"), list):
            raise AppError("LOCAL_DATA_INVALID", "本地操作恢复日志结构无效；原文件已保留", 500)
        for entry in value["operations"]:
            if not self._valid_entry(entry):
                raise AppError("LOCAL_DATA_INVALID", "本地操作恢复日志结构无效；原文件已保留", 500)
        return value

    def _save(self, document: dict[str, Any]) -> None:
        self.store.write(self.FILE_NAME, document)

    @classmethod
    def _valid_entry(cls, entry: Any) -> bool:
        return bool(
            isinstance(entry, dict)
            and isinstance(entry.get("operation_id"), str)
            and re.fullmatch(r"[0-9a-f]{32}", entry["operation_id"])
            and isinstance(entry.get("account_scope"), str)
            and re.fullmatch(r"[0-9a-f]{64}", entry["account_scope"])
            and entry.get("action") in cls.ACTIONS
            and entry.get("state") in cls.STATES
            and isinstance(entry.get("keys"), list)
            and bool(entry["keys"])
            and all(isinstance(key, str) and key for key in entry["keys"])
            and (
                entry.get("guard_keys") is None
                or (
                    isinstance(entry.get("guard_keys"), list)
                    and all(isinstance(key, str) and key for key in entry["guard_keys"])
                )
            )
            and isinstance(entry.get("target"), dict)
            and isinstance(entry.get("created_at"), str)
            and isinstance(entry.get("updated_at"), str)
            and (
                entry.get("resource_id") is None
                or (isinstance(entry.get("resource_id"), int) and not isinstance(entry.get("resource_id"), bool))
            )
            and (entry.get("parent_id") is None or isinstance(entry.get("parent_id"), str))
            and (entry.get("last_error") is None or isinstance(entry.get("last_error"), dict))
        )

    @staticmethod
    def _find(document: dict[str, Any], operation_id: str) -> dict[str, Any]:
        entry = next((item for item in document["operations"] if item["operation_id"] == operation_id), None)
        if entry is None:
            raise AppError("OPERATION_NOT_FOUND", "找不到这条待核对操作", 404)
        return entry

    @staticmethod
    def _protected_keys(entry: dict[str, Any]) -> set[str]:
        guard_keys = entry.get("guard_keys")
        return set(entry["keys"]) | (set(guard_keys) if isinstance(guard_keys, list) else set())

    @staticmethod
    def _summary(entry: dict[str, Any]) -> dict[str, Any]:
        target = entry["target"]
        exposed_target = {
            key: deepcopy(target[key])
            for key in ("song_ids", "playlist_id", "source_playlist_id", "name", "privacy")
            if key in target
        }
        return {
            "operation_id": entry["operation_id"],
            "action": entry["action"],
            "state": entry["state"],
            "target": exposed_target,
            "resource_id": entry["resource_id"],
            "created_at": entry["created_at"],
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
