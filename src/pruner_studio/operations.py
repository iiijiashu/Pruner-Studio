from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .catalog_service import CatalogService
from .errors import AppError, UpstreamError
from .models import FailedItem, OperationResult, PlaylistSummary
from .netease_client import NeteaseClient
from .storage import JsonStore


class OperationsService:
    def __init__(self, store: JsonStore, client: NeteaseClient, catalog: CatalogService, max_batch_size: int = 500):
        self.store = store
        self.client = client
        self.catalog = catalog
        self.max_batch_size = max_batch_size
        self._write_lock = threading.Lock()

    def backup(self, requested_ids: list[int] | None = None) -> OperationResult:
        catalog = self.catalog.get()
        requested = _positive_ids(requested_ids or [], self.max_batch_size)
        if requested:
            allowed = set(requested)
            snapshot = {
                "version": 1,
                "playlists": {
                    key: {
                        **playlist,
                        "songs": [
                            song
                            for song in playlist.get("songs", [])
                            if isinstance(song, dict) and song.get("id") in allowed
                        ],
                    }
                    for key, playlist in catalog["playlists"].items()
                    if isinstance(playlist, dict)
                },
            }
        else:
            snapshot = catalog
            requested = sorted(
                {
                    int(song["id"])
                    for playlist in catalog["playlists"].values()
                    if isinstance(playlist, dict)
                    for song in playlist.get("songs", [])
                    if isinstance(song, dict) and isinstance(song.get("id"), int)
                }
            )
        operation_id = uuid.uuid4().hex
        payload = {
            "operation_id": operation_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "catalog": snapshot,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        payload["sha256"] = hashlib.sha256(raw).hexdigest()
        filename = f"backup-{operation_id}.json"
        with self._write_lock:
            self.store.write(filename, payload)
            persisted = self.store.read(filename, None)
            if persisted != payload:
                raise AppError("BACKUP_UNVERIFIED", "备份写入后校验失败", 500)
            self._prune_backups()
        return OperationResult(operation_id, requested, requested, [], "本地备份已创建并校验")

    def _prune_backups(self, keep: int = 10) -> None:
        backups = sorted(
            self.store.data_dir.glob("backup-*.json"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for stale in backups[keep:]:
            self.store.delete(stale.name)

    def create_playlist(self, name: str, ids: Any = None, privacy: Any = 0) -> OperationResult:
        normalized = str(name or "").strip()
        if not normalized or len(normalized) > 100:
            raise AppError("PLAYLIST_NAME_INVALID", "歌单名称必须为 1–100 个字符")
        requested = _positive_ids(ids or [], self.max_batch_size)
        try:
            privacy_value = int(privacy)
        except (TypeError, ValueError) as exc:
            raise AppError("PLAYLIST_PRIVACY_INVALID", "歌单隐私设置无效") from exc
        if privacy_value not in {0, 10}:
            raise AppError("PLAYLIST_PRIVACY_INVALID", "歌单隐私设置无效")
        with self._write_lock:
            playlist_id = self.client.create_playlist(normalized, privacy_value)
            try:
                succeeded, failed_ids = self.client.add_tracks(playlist_id, requested)
            except (AppError, UpstreamError) as exc:
                succeeded = []
                failed = [FailedItem(song_id, exc.code, exc.message) for song_id in requested]
            else:
                failed = [FailedItem(song_id, "ADD_TRACK_UNVERIFIED", "远端歌单中未确认这首歌") for song_id in failed_ids]
        message = "歌单已创建并确认"
        if requested:
            message += f"；已确认添加 {len(succeeded)} 首，{len(failed)} 首未添加"
        return OperationResult(uuid.uuid4().hex, requested, succeeded, failed, message, playlist_id)

    def unlike(self, ids: Any, source_playlist_id: Any) -> OperationResult:
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        user_id, _ = self._liked_playlist(source_playlist_id)
        succeeded: list[int] = []
        failures: dict[int, FailedItem] = {}
        unknowns: dict[int, FailedItem] = {}
        with self._write_lock:
            liked_before = self.client.liked_ids(user_id)
            for song_id in requested:
                if song_id not in liked_before:
                    failures[song_id] = FailedItem(song_id, "SONG_NOT_LIKED", "这首歌原本不在喜欢列表中")
                    continue
                write_error: AppError | None = None
                try:
                    self.client.request_unlike(song_id)
                except (AppError, UpstreamError) as exc:
                    write_error = exc
                try:
                    is_still_liked = song_id in self.client.liked_ids(user_id)
                except (AppError, UpstreamError):
                    unknowns[song_id] = FailedItem(
                        song_id,
                        "UNLIKE_RESULT_UNKNOWN",
                        "未能核对远端结果；没有更新本地曲库，请稍后只读核对",
                    )
                    continue
                if not is_still_liked:
                    succeeded.append(song_id)
                elif write_error is not None:
                    failures[song_id] = FailedItem(song_id, write_error.code, write_error.message)
                else:
                    failures[song_id] = FailedItem(song_id, "UNLIKE_UNVERIFIED", "远端仍在喜欢列表中")
            failed = [failures[song_id] for song_id in requested if song_id in failures]
            unknown = [unknowns[song_id] for song_id in requested if song_id in unknowns]
            self.catalog.remove_confirmed_unlikes(succeeded)
        message = f"已确认取消喜欢 {len(succeeded)} 首"
        if failed:
            message += f"，{len(failed)} 首保留未改动"
        if unknown:
            message += f"，{len(unknown)} 首结果待核对"
        return OperationResult(uuid.uuid4().hex, requested, succeeded, failed, message, unknown_items=unknown)

    def remove_tracks(self, playlist_id: int, ids: Any, source_playlist_id: Any) -> OperationResult:
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        source_id = _source_playlist_id(source_playlist_id)
        if source_id != playlist_id:
            raise AppError("SOURCE_PLAYLIST_MISMATCH", "歌曲来源歌单与目标歌单不一致", 409)
        playlist = self._owned_playlist(playlist_id, require_self_created=True)
        succeeded: list[int] = []
        unknowns: dict[int, FailedItem] = {}
        with self._write_lock:
            remote_before = {song.id for song in self.client.playlist_tracks(playlist.id, fresh=True)}
            candidates = [song_id for song_id in requested if song_id in remote_before]
            failures: dict[int, FailedItem] = {
                song_id: FailedItem(song_id, "TRACK_NOT_IN_PLAYLIST", "这首歌原本不在该歌单中")
                for song_id in requested
                if song_id not in remote_before
            }
            if candidates:
                write_error: AppError | None = None
                try:
                    self.client.request_remove_tracks(playlist.id, candidates)
                except (AppError, UpstreamError) as exc:
                    write_error = exc
                try:
                    remote_after = {song.id for song in self.client.playlist_tracks(playlist.id, fresh=True)}
                except (AppError, UpstreamError):
                    remote_after = None
                if remote_after is None:
                    for song_id in candidates:
                        unknowns[song_id] = FailedItem(
                            song_id,
                            "REMOVE_TRACK_RESULT_UNKNOWN",
                            "未能核对远端结果；没有更新本地曲库，请稍后只读核对",
                        )
                else:
                    for song_id in candidates:
                        if song_id not in remote_after:
                            succeeded.append(song_id)
                        elif write_error is not None:
                            failures[song_id] = FailedItem(song_id, write_error.code, write_error.message)
                        else:
                            failures[song_id] = FailedItem(song_id, "REMOVE_TRACK_UNVERIFIED", "远端歌单中仍有这首歌")
            failed = [failures[song_id] for song_id in requested if song_id in failures]
            unknown = [unknowns[song_id] for song_id in requested if song_id in unknowns]
            self.catalog.remove_confirmed_tracks(playlist.id, succeeded)
        message = f"已确认从“{playlist.name}”移出 {len(succeeded)} 首"
        if failed:
            message += f"，{len(failed)} 首保留未改动"
        if unknown:
            message += f"，{len(unknown)} 首结果待核对"
        return OperationResult(uuid.uuid4().hex, requested, succeeded, failed, message, playlist.id, unknown)

    def reconcile(self, action: Any, playlist_id: Any, ids: Any, source_playlist_id: Any) -> OperationResult:
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        normalized_action = str(action or "").strip()
        if normalized_action == "unlike":
            user_id, _ = self._liked_playlist(source_playlist_id)
            with self._write_lock:
                remote_ids = self.client.liked_ids(user_id)
                succeeded = [song_id for song_id in requested if song_id not in remote_ids]
                failed = [
                    FailedItem(song_id, "UNLIKE_NOT_APPLIED", "远端仍在喜欢列表中")
                    for song_id in requested
                    if song_id in remote_ids
                ]
                self.catalog.remove_confirmed_unlikes(succeeded)
            message = f"只读核对完成：{len(succeeded)} 首已取消喜欢，{len(failed)} 首仍在喜欢列表"
            return OperationResult(uuid.uuid4().hex, requested, succeeded, failed, message)
        if normalized_action == "remove_tracks":
            normalized_playlist_id = _positive_playlist_id(playlist_id)
            source_id = _source_playlist_id(source_playlist_id)
            if source_id != normalized_playlist_id:
                raise AppError("SOURCE_PLAYLIST_MISMATCH", "歌曲来源歌单与目标歌单不一致", 409)
            playlist = self._owned_playlist(normalized_playlist_id, require_self_created=True)
            with self._write_lock:
                remote_ids = {song.id for song in self.client.playlist_tracks(playlist.id, fresh=True)}
                succeeded = [song_id for song_id in requested if song_id not in remote_ids]
                failed = [
                    FailedItem(song_id, "REMOVE_TRACK_NOT_APPLIED", "远端歌单中仍有这首歌")
                    for song_id in requested
                    if song_id in remote_ids
                ]
                self.catalog.remove_confirmed_tracks(playlist.id, succeeded)
            message = f"只读核对完成：{len(succeeded)} 首已移出，{len(failed)} 首仍在歌单中"
            return OperationResult(uuid.uuid4().hex, requested, succeeded, failed, message, playlist.id)
        raise AppError("RECONCILE_ACTION_INVALID", "只支持核对 unlike 或 remove_tracks")

    def _liked_playlist(self, source_playlist_id: Any) -> tuple[int, PlaylistSummary]:
        source_id = _source_playlist_id(source_playlist_id)
        account = self.client.account()
        liked = next((item for item in self.client.playlists(account) if item.kind == "liked"), None)
        if liked is None:
            raise AppError("PLAYLIST_NOT_FOUND", "找不到当前账号的喜欢列表", 404)
        if liked.id != source_id:
            raise AppError("SOURCE_PLAYLIST_MISMATCH", "歌曲来源不是当前账号的喜欢列表", 409)
        return account.user_id, liked

    def _owned_playlist(self, playlist_id: int, *, require_self_created: bool) -> PlaylistSummary:
        account = self.client.account()
        playlist = next((item for item in self.client.playlists(account) if item.id == playlist_id), None)
        if playlist is None:
            raise AppError("PLAYLIST_NOT_FOUND", "找不到这个自建歌单", 404)
        if require_self_created and playlist.kind != "self_created":
            raise AppError("PLAYLIST_KIND_INVALID", "喜欢列表请使用取消喜欢操作", 409)
        return playlist


def _positive_ids(value: Any, maximum: int) -> list[int]:
    if not isinstance(value, list):
        raise AppError("SONG_IDS_INVALID", "歌曲 ID 必须是数组")
    if len(value) > maximum:
        raise AppError("BATCH_TOO_LARGE", f"一次最多操作 {maximum} 首歌曲", 413)
    result: list[int] = []
    seen: set[int] = set()
    for raw in value:
        if isinstance(raw, bool):
            raise AppError("SONG_ID_INVALID", "歌曲 ID 必须是正整数")
        try:
            song_id = int(raw)
        except (TypeError, ValueError) as exc:
            raise AppError("SONG_ID_INVALID", "歌曲 ID 必须是正整数") from exc
        if song_id <= 0:
            raise AppError("SONG_ID_INVALID", "歌曲 ID 必须是正整数")
        if song_id not in seen:
            seen.add(song_id)
            result.append(song_id)
    return result


def _positive_playlist_id(value: Any) -> int:
    if isinstance(value, bool):
        raise AppError("PLAYLIST_ID_INVALID", "歌单 ID 必须是正整数")
    try:
        playlist_id = int(value)
    except (TypeError, ValueError) as exc:
        raise AppError("PLAYLIST_ID_INVALID", "歌单 ID 必须是正整数") from exc
    if playlist_id <= 0:
        raise AppError("PLAYLIST_ID_INVALID", "歌单 ID 必须是正整数")
    return playlist_id


def _source_playlist_id(value: Any) -> int:
    if isinstance(value, bool):
        raise AppError("SOURCE_PLAYLIST_ID_INVALID", "来源歌单 ID 必须是正整数")
    try:
        playlist_id = int(value)
    except (TypeError, ValueError) as exc:
        raise AppError("SOURCE_PLAYLIST_ID_INVALID", "来源歌单 ID 必须是正整数") from exc
    if playlist_id <= 0:
        raise AppError("SOURCE_PLAYLIST_ID_INVALID", "来源歌单 ID 必须是正整数")
    return playlist_id
