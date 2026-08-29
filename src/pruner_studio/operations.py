from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from .catalog_service import CatalogService
from .errors import AppError, UpstreamError
from .models import AccountSummary, FailedItem, OperationResult, PlaylistSummary
from .netease_client import NeteaseClient
from .operation_journal import OperationJournal
from .storage import JsonStore


class OperationsService:
    def __init__(
        self,
        store: JsonStore,
        client: NeteaseClient,
        catalog: CatalogService,
        journal: OperationJournal,
        max_batch_size: int = 500,
    ):
        self.store = store
        self.client = client
        self.catalog = catalog
        self.journal = journal
        self.max_batch_size = max_batch_size
        self._write_lock = threading.RLock()

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
            account = self.client.account()
            create_key = _create_playlist_key(normalized, privacy_value)
            self.journal.assert_allowed(account.user_id, [create_key])
            before = self.client.playlists(account)
            entry = self.journal.begin(
                account.user_id,
                "create_playlist",
                [create_key],
                {
                    "name": normalized,
                    "privacy": privacy_value,
                    "song_ids": requested,
                    "before_playlist_ids": [item.id for item in before],
                },
            )
            operation_id = entry["operation_id"]
            self.journal.mark_sent(operation_id)
            try:
                playlist_id = self.client.request_create_playlist(normalized, privacy_value)
            except (AppError, UpstreamError) as exc:
                self.journal.mark_sent(operation_id, error=exc)
                return OperationResult(
                    operation_id=operation_id,
                    requested_ids=requested,
                    unknown_items=[FailedItem(None, "CREATE_PLAYLIST_RESULT_UNKNOWN", "创建请求已发出但结果未确认；请只读核对，禁止重复创建")],
                    message="歌单创建结果待核对；未继续发送加歌请求",
                    pending_operation_ids=[operation_id],
                )
            self.journal.mark_sent(operation_id, resource_id=playlist_id)
            try:
                confirmed = any(item.id == playlist_id for item in self.client.playlists(account))
            except (AppError, UpstreamError) as exc:
                self.journal.mark_sent(operation_id, resource_id=playlist_id, error=exc)
                confirmed = False
            if not confirmed:
                return OperationResult(
                    operation_id=operation_id,
                    requested_ids=requested,
                    unknown_items=[FailedItem(None, "CREATE_PLAYLIST_RESULT_UNKNOWN", "新歌单尚未能从远端列表确认；请只读核对")],
                    message="歌单创建结果待核对；未继续发送加歌请求",
                    resource_id=playlist_id,
                    pending_operation_ids=[operation_id],
                )
            if not requested:
                self.journal.resolve(operation_id)
                return OperationResult(operation_id, [], [], [], "歌单已创建并确认", playlist_id)
            keys = [_song_operation_key("add_tracks", playlist_id, song_id) for song_id in requested]
            add_entry = self.journal.transition_create_to_add(
                operation_id,
                keys=keys,
                playlist_id=playlist_id,
                song_ids=requested,
            )
            return self._add_tracks(playlist_id, requested, add_entry)

    def _add_tracks(self, playlist_id: int, requested: list[int], entry: dict[str, Any]) -> OperationResult:
        keys = list(entry["keys"])
        operation_id = entry["operation_id"]
        self.journal.mark_sent(operation_id, resource_id=playlist_id)
        write_error: AppError | None = None
        try:
            self.client.request_add_tracks(playlist_id, requested)
        except (AppError, UpstreamError) as exc:
            write_error = exc
            self.journal.mark_sent(operation_id, resource_id=playlist_id, error=exc)
        try:
            remote_ids = {song.id for song in self.client.playlist_tracks(playlist_id, fresh=True)}
        except (AppError, UpstreamError) as exc:
            self.journal.retain_only(operation_id, keys=keys, song_ids=requested, error=exc)
            unknown = [FailedItem(song_id, "ADD_TRACK_RESULT_UNKNOWN", "未能核对歌曲是否已加入；请稍后只读核对") for song_id in requested]
            return OperationResult(operation_id, requested, [], [], "歌单已创建；加歌结果待核对", playlist_id, unknown, [operation_id])
        succeeded = [song_id for song_id in requested if song_id in remote_ids]
        missing = [song_id for song_id in requested if song_id not in remote_ids]
        if write_error is not None and write_error.code == "UPSTREAM_REJECTED":
            failed = [FailedItem(song_id, write_error.code, write_error.message) for song_id in missing]
            unknown: list[FailedItem] = []
        else:
            failed = []
            unknown = [FailedItem(song_id, "ADD_TRACK_RESULT_UNKNOWN", "远端尚未确认歌曲已加入；请稍后只读核对") for song_id in missing]
        if unknown:
            unknown_ids = [item.id for item in unknown if isinstance(item.id, int)]
            self.journal.retain_only(
                operation_id,
                keys=[_song_operation_key("add_tracks", playlist_id, song_id) for song_id in unknown_ids],
                song_ids=unknown_ids,
                error=write_error,
            )
            pending = [operation_id]
        else:
            self.journal.resolve(operation_id)
            pending = []
        message = f"歌单已创建；已确认添加 {len(succeeded)} 首"
        if failed:
            message += f"，{len(failed)} 首未添加"
        if unknown:
            message += f"，{len(unknown)} 首待核对"
        return OperationResult(operation_id, requested, succeeded, failed, message, playlist_id, unknown, pending)

    def unlike(self, ids: Any, source_playlist_id: Any) -> OperationResult:
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        with self._write_lock:
            account = self.client.account()
            source_id = _source_playlist_id(source_playlist_id)
            requested_keys = [_song_operation_key("unlike", source_id, song_id) for song_id in requested]
            self.journal.assert_allowed(account.user_id, requested_keys)
            user_id, liked_playlist = self._liked_playlist(source_playlist_id, account)
            liked_before = self.client.liked_ids(user_id)
            candidates = [song_id for song_id in requested if song_id in liked_before]
            failures: dict[int, FailedItem] = {
                song_id: FailedItem(song_id, "SONG_NOT_LIKED", "这首歌原本不在喜欢列表中")
                for song_id in requested
                if song_id not in liked_before
            }
            succeeded: list[int] = []
            unknowns: dict[int, FailedItem] = {}
            operation_id = uuid.uuid4().hex
            if candidates:
                keys = [_song_operation_key("unlike", liked_playlist.id, song_id) for song_id in candidates]
                entry = self.journal.begin(
                    account.user_id,
                    "unlike",
                    keys,
                    {"playlist_id": liked_playlist.id, "source_playlist_id": liked_playlist.id, "song_ids": candidates},
                )
                operation_id = entry["operation_id"]
                self.journal.mark_sent(operation_id)
            for song_id in candidates:
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
                elif write_error is not None and write_error.code == "UPSTREAM_REJECTED":
                    failures[song_id] = FailedItem(song_id, write_error.code, write_error.message)
                else:
                    unknowns[song_id] = FailedItem(song_id, "UNLIKE_RESULT_UNKNOWN", "远端尚未确认取消喜欢；请稍后只读核对")
            failed = [failures[song_id] for song_id in requested if song_id in failures]
            unknown = [unknowns[song_id] for song_id in requested if song_id in unknowns]
            self.catalog.remove_confirmed_unlikes(succeeded)
            if candidates:
                if unknown:
                    unknown_ids = [item.id for item in unknown if isinstance(item.id, int)]
                    self.journal.retain_only(
                        operation_id,
                        keys=[_song_operation_key("unlike", liked_playlist.id, song_id) for song_id in unknown_ids],
                        song_ids=unknown_ids,
                    )
                else:
                    self.journal.resolve(operation_id)
        message = f"已确认取消喜欢 {len(succeeded)} 首"
        if failed:
            message += f"，{len(failed)} 首保留未改动"
        if unknown:
            message += f"，{len(unknown)} 首结果待核对"
        return OperationResult(
            operation_id,
            requested,
            succeeded,
            failed,
            message,
            unknown_items=unknown,
            pending_operation_ids=[operation_id] if unknown else [],
        )

    def remove_tracks(self, playlist_id: int, ids: Any, source_playlist_id: Any) -> OperationResult:
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        source_id = _source_playlist_id(source_playlist_id)
        if source_id != playlist_id:
            raise AppError("SOURCE_PLAYLIST_MISMATCH", "歌曲来源歌单与目标歌单不一致", 409)
        with self._write_lock:
            account = self.client.account()
            requested_keys = [_song_operation_key("remove_tracks", playlist_id, song_id) for song_id in requested]
            self.journal.assert_allowed(account.user_id, requested_keys)
            playlist = self._owned_playlist(playlist_id, require_self_created=True, account=account)
            succeeded: list[int] = []
            unknowns: dict[int, FailedItem] = {}
            remote_before = {song.id for song in self.client.playlist_tracks(playlist.id, fresh=True)}
            candidates = [song_id for song_id in requested if song_id in remote_before]
            failures: dict[int, FailedItem] = {
                song_id: FailedItem(song_id, "TRACK_NOT_IN_PLAYLIST", "这首歌原本不在该歌单中")
                for song_id in requested
                if song_id not in remote_before
            }
            operation_id = uuid.uuid4().hex
            if candidates:
                keys = [_song_operation_key("remove_tracks", playlist.id, song_id) for song_id in candidates]
                entry = self.journal.begin(
                    account.user_id,
                    "remove_tracks",
                    keys,
                    {"playlist_id": playlist.id, "source_playlist_id": playlist.id, "song_ids": candidates},
                )
                operation_id = entry["operation_id"]
                self.journal.mark_sent(operation_id, resource_id=playlist.id)
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
                        elif write_error is not None and write_error.code == "UPSTREAM_REJECTED":
                            failures[song_id] = FailedItem(song_id, write_error.code, write_error.message)
                        else:
                            unknowns[song_id] = FailedItem(song_id, "REMOVE_TRACK_RESULT_UNKNOWN", "远端尚未确认歌曲已移出；请稍后只读核对")
            failed = [failures[song_id] for song_id in requested if song_id in failures]
            unknown = [unknowns[song_id] for song_id in requested if song_id in unknowns]
            self.catalog.remove_confirmed_tracks(playlist.id, succeeded)
            if candidates:
                if unknown:
                    unknown_ids = [item.id for item in unknown if isinstance(item.id, int)]
                    self.journal.retain_only(
                        operation_id,
                        keys=[_song_operation_key("remove_tracks", playlist.id, song_id) for song_id in unknown_ids],
                        song_ids=unknown_ids,
                        error=write_error,
                    )
                else:
                    self.journal.resolve(operation_id)
        message = f"已确认从“{playlist.name}”移出 {len(succeeded)} 首"
        if failed:
            message += f"，{len(failed)} 首保留未改动"
        if unknown:
            message += f"，{len(unknown)} 首结果待核对"
        return OperationResult(
            operation_id,
            requested,
            succeeded,
            failed,
            message,
            playlist.id,
            unknown,
            [operation_id] if unknown else [],
        )

    def reconcile(self, action: Any, playlist_id: Any, ids: Any, source_playlist_id: Any) -> OperationResult:
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        normalized_action = str(action or "").strip()
        if normalized_action == "unlike":
            with self._write_lock:
                account = self.client.account()
                user_id, liked_playlist = self._liked_playlist(source_playlist_id, account)
                remote_ids = self.client.liked_ids(user_id)
                succeeded = [song_id for song_id in requested if song_id not in remote_ids]
                failed = [
                    FailedItem(song_id, "UNLIKE_NOT_APPLIED", "远端仍在喜欢列表中")
                    for song_id in requested
                    if song_id in remote_ids
                ]
                self.catalog.remove_confirmed_unlikes(succeeded)
                self.journal.resolve_keys(
                    account.user_id,
                    [_song_operation_key("unlike", liked_playlist.id, song_id) for song_id in requested],
                )
            message = f"只读核对完成：{len(succeeded)} 首已取消喜欢，{len(failed)} 首仍在喜欢列表"
            return OperationResult(uuid.uuid4().hex, requested, succeeded, failed, message)
        if normalized_action == "remove_tracks":
            normalized_playlist_id = _positive_playlist_id(playlist_id)
            source_id = _source_playlist_id(source_playlist_id)
            if source_id != normalized_playlist_id:
                raise AppError("SOURCE_PLAYLIST_MISMATCH", "歌曲来源歌单与目标歌单不一致", 409)
            with self._write_lock:
                account = self.client.account()
                playlist = self._owned_playlist(normalized_playlist_id, require_self_created=True, account=account)
                remote_ids = {song.id for song in self.client.playlist_tracks(playlist.id, fresh=True)}
                succeeded = [song_id for song_id in requested if song_id not in remote_ids]
                failed = [
                    FailedItem(song_id, "REMOVE_TRACK_NOT_APPLIED", "远端歌单中仍有这首歌")
                    for song_id in requested
                    if song_id in remote_ids
                ]
                self.catalog.remove_confirmed_tracks(playlist.id, succeeded)
                self.journal.resolve_keys(
                    account.user_id,
                    [_song_operation_key("remove_tracks", playlist.id, song_id) for song_id in requested],
                )
            message = f"只读核对完成：{len(succeeded)} 首已移出，{len(failed)} 首仍在歌单中"
            return OperationResult(uuid.uuid4().hex, requested, succeeded, failed, message, playlist.id)
        raise AppError("RECONCILE_ACTION_INVALID", "只支持核对 unlike 或 remove_tracks")

    def pending_for(self, user_id: int) -> list[dict[str, Any]]:
        return self.journal.pending_for(user_id)

    def ignore_pending(self, user_id: int, operation_id: str) -> dict[str, Any]:
        with self._write_lock:
            return self.journal.ignore(user_id, operation_id)

    def reconcile_pending(self, user_id: int, operation_id: str) -> OperationResult:
        with self._write_lock:
            entry = self.journal.get(user_id, operation_id)
            action = entry["action"]
            target = entry["target"]
            song_ids = _positive_ids(target.get("song_ids", []), self.max_batch_size)
            if action == "create_playlist":
                account = self.client.account()
                current = self.client.playlists(account)
                resource_id = entry.get("resource_id")
                candidate = next((item for item in current if item.id == resource_id), None) if resource_id else None
                if candidate is None and resource_id is None:
                    before_ids = {int(value) for value in target.get("before_playlist_ids", []) if isinstance(value, int)}
                    privacy = "private" if target.get("privacy") == 10 else "public"
                    candidates = [
                        item for item in current
                        if item.id not in before_ids and item.name == target.get("name") and item.privacy == privacy
                    ]
                    candidate = candidates[0] if len(candidates) == 1 else None
                if candidate is None:
                    return OperationResult(
                        operation_id,
                        song_ids,
                        unknown_items=[FailedItem(None, "CREATE_PLAYLIST_RESULT_UNKNOWN", "仍无法唯一确认新歌单；请稍后再核对或明确忽略")],
                        message="创建结果仍待核对",
                        resource_id=resource_id,
                        pending_operation_ids=[operation_id],
                    )
                self.journal.resolve(operation_id)
                return OperationResult(operation_id, song_ids, [], [], "已只读确认歌单创建成功；未自动发送加歌请求", candidate.id)
            if action == "unlike":
                source_id = _source_playlist_id(target.get("source_playlist_id"))
                account = self.client.account()
                user_id, liked = self._liked_playlist(source_id, account)
                remote_ids = self.client.liked_ids(user_id)
                succeeded = [song_id for song_id in song_ids if song_id not in remote_ids]
                failed = [FailedItem(song_id, "UNLIKE_NOT_APPLIED", "远端仍在喜欢列表中") for song_id in song_ids if song_id in remote_ids]
                self.catalog.remove_confirmed_unlikes(succeeded)
                self.journal.resolve(operation_id)
                return OperationResult(operation_id, song_ids, succeeded, failed, "只读核对完成")
            if action in {"remove_tracks", "add_tracks"}:
                playlist_id = _positive_playlist_id(target.get("playlist_id"))
                account = self.client.account()
                playlist = self._owned_playlist(playlist_id, require_self_created=True, account=account)
                remote_ids = {song.id for song in self.client.playlist_tracks(playlist.id, fresh=True)}
                if action == "remove_tracks":
                    succeeded = [song_id for song_id in song_ids if song_id not in remote_ids]
                    failed = [FailedItem(song_id, "REMOVE_TRACK_NOT_APPLIED", "远端歌单中仍有这首歌") for song_id in song_ids if song_id in remote_ids]
                    self.catalog.remove_confirmed_tracks(playlist.id, succeeded)
                else:
                    succeeded = [song_id for song_id in song_ids if song_id in remote_ids]
                    failed = [FailedItem(song_id, "ADD_TRACK_NOT_APPLIED", "远端歌单中仍没有这首歌") for song_id in song_ids if song_id not in remote_ids]
                self.journal.resolve(operation_id)
                return OperationResult(operation_id, song_ids, succeeded, failed, "只读核对完成", playlist.id)
            raise AppError("RECONCILE_ACTION_INVALID", "这条待核对操作类型无效", 409)

    def _liked_playlist(
        self,
        source_playlist_id: Any,
        account: AccountSummary | None = None,
    ) -> tuple[int, PlaylistSummary]:
        source_id = _source_playlist_id(source_playlist_id)
        owner = account or self.client.account()
        liked = next((item for item in self.client.playlists(owner) if item.kind == "liked"), None)
        if liked is None:
            raise AppError("PLAYLIST_NOT_FOUND", "找不到当前账号的喜欢列表", 404)
        if liked.id != source_id:
            raise AppError("SOURCE_PLAYLIST_MISMATCH", "歌曲来源不是当前账号的喜欢列表", 409)
        return owner.user_id, liked

    def _owned_playlist(
        self,
        playlist_id: int,
        *,
        require_self_created: bool,
        account: AccountSummary | None = None,
    ) -> PlaylistSummary:
        owner = account or self.client.account()
        playlist = next((item for item in self.client.playlists(owner) if item.id == playlist_id), None)
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


def _song_operation_key(action: str, playlist_id: int, song_id: int) -> str:
    return f"{action}:{int(playlist_id)}:{int(song_id)}"


def _create_playlist_key(name: str, privacy: int) -> str:
    payload = json.dumps(
        {"name": name, "privacy": privacy},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"create_playlist:{hashlib.sha256(payload).hexdigest()}"
