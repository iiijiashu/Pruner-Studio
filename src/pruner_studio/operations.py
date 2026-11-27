from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from typing import Any

from .catalog_service import CatalogService
from .db import is_stub_song
from .errors import AppError, UpstreamError
from .models import AccountSummary, FailedItem, OperationResult, PlaylistSummary
from .netease_client import NeteaseClient
from .operation_journal import OperationJournal
from .storage import JsonStore
from .snapshots import save_snapshot

UNLIKE_MINI_BATCH_SIZE = 8
PLAYLIST_WRITE_CHUNK_SIZE = 50


def compute_manifest_hash(
    action: str,
    playlist_id: int | None,
    source_playlist_id: int | None,
    song_ids: list[int],
) -> str:
    """Canonical SHA-256 digest of a frozen destructive operation manifest."""
    canonical = json.dumps(
        {
            "action": str(action),
            "playlist_id": int(playlist_id) if playlist_id is not None else None,
            "source_playlist_id": int(source_playlist_id) if source_playlist_id is not None else None,
            "song_ids": [int(sid) for sid in song_ids],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_fnv1a_manifest_hash(
    action: str,
    playlist_id: int | str | None,
    song_ids: list[int],
) -> str:
    """Matches web/js/utils.js computeManifestHash for per-group frozen manifests."""
    pid_str = "" if playlist_id is None else str(playlist_id)
    canonical = "|".join(sorted(f"{action}:{pid_str}:{int(sid)}" for sid in song_ids))
    h = 2166136261
    for ch in canonical:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return f"m_{h:08x}"


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

    @staticmethod
    def _verify_session_generation(
        expected: int | None,
        current: int | None,
    ) -> None:
        if expected is None or current is None:
            return
        if type(expected) is not int and not (isinstance(expected, str) and expected.isdecimal()):
            raise AppError("SESSION_GENERATION_INVALID", "会话代数参数无效", 400)
        try:
            expected_int = int(expected)
        except (TypeError, ValueError) as exc:
            raise AppError("SESSION_GENERATION_INVALID", "会话代数参数无效", 400) from exc
        if expected_int != current:
            raise AppError(
                "SESSION_GENERATION_MISMATCH",
                "登录会话已变更，操作已取消；请刷新状态后重试",
                409,
            )

    @staticmethod
    def _verify_manifest_hash(
        manifest_hash: str | None,
        action: str,
        playlist_id: int | None,
        source_playlist_id: int | None,
        song_ids: list[int],
    ) -> str | None:
        if manifest_hash is None or manifest_hash == "":
            return None
        if not isinstance(manifest_hash, str):
            raise AppError("MANIFEST_HASH_INVALID", "操作清单校验值无效", 400)
        provided = manifest_hash.strip().lower()
        expected = compute_manifest_hash(action, playlist_id, source_playlist_id, song_ids)
        simple_alt = hashlib.sha256(
            f"{action}:{playlist_id or ''}:{source_playlist_id or ''}:{','.join(str(s) for s in song_ids)}".encode("utf-8")
        ).hexdigest()
        fnv_source = compute_fnv1a_manifest_hash(action, source_playlist_id, song_ids)
        fnv_target = compute_fnv1a_manifest_hash(action, playlist_id, song_ids)
        # Also support frontend 'remove' alias for remove_tracks just in case
        fnv_aliases = {fnv_source, fnv_target}
        if action == "remove_tracks":
            fnv_aliases.add(compute_fnv1a_manifest_hash("remove", source_playlist_id, song_ids))
            fnv_aliases.add(compute_fnv1a_manifest_hash("remove", playlist_id, song_ids))
        if provided not in {expected, simple_alt} | fnv_aliases:
            raise AppError(
                "MANIFEST_HASH_MISMATCH",
                "待执行歌曲清单与确认弹窗不一致，操作已取消",
                409,
            )
        return provided

    def backup(
        self,
        requested_ids: list[int] | None = None,
        *,
        account_id: str | None = None,
    ) -> OperationResult:
        account_id = str(account_id or self.catalog._active_account_id or "local")
        catalog = self.catalog.get(account_id=account_id)
        requested = _positive_ids(requested_ids or [], self.max_batch_size)
        with self._write_lock:
            snapshot, path = save_snapshot(self.store, catalog["playlists"].values(), account_id,
                                           set(requested) if requested else None)
        saved_ids = {row["id"] for row in snapshot["tracks"]}
        ids = requested or sorted(saved_ids)
        confirmed = [sid for sid in ids if sid in saved_ids]
        missing = [FailedItem(sid, "TRACK_NOT_CACHED", "本地快照中没有这首歌") for sid in ids if sid not in saved_ids]
        return OperationResult(snapshot["backup_id"], ids, confirmed, missing, "本地备份已创建并校验",
                               backup_id=snapshot["backup_id"], checksum=snapshot["checksum"], snapshot_path=str(path))

    def like_track(
        self,
        song_id: Any,
        like: bool = True,
        *,
        expected_session_generation: int | None = None,
        current_session_generation: int | None = None,
    ) -> OperationResult:
        sid = _positive_ids([song_id], 1)[0]
        if not isinstance(like, bool):
            raise AppError("LIKE_FLAG_INVALID", "like 参数必须是布尔值")
        self._verify_session_generation(expected_session_generation, current_session_generation)
        with self._write_lock:
            self.client.check_write_allowed()
            account = self.client.account()
            liked = next((p for p in self.client.playlists(account) if p.kind == "liked"), None)
            if liked is None:
                raise AppError("PLAYLIST_NOT_FOUND", "找不到当前账号的喜欢列表", 404)
            action = "like" if like else "unlike"
            key = _song_operation_key(action, liked.id, sid)
            self.journal.assert_allowed(account.user_id, [key])
            entry = self.journal.begin(account.user_id, action, [key],
                                       {"source_playlist_id": liked.id, "playlist_id": liked.id, "song_ids": [sid]})
            operation_id = entry["operation_id"]
            self.journal.mark_sent(operation_id)
            error = None
            try:
                self.client.request_like(sid, like=like)
            except AppError as exc:
                error = exc
                if exc.code in {"UPSTREAM_REJECTED", "RISK_CONTROL_COOLDOWN"}:
                    self.journal.resolve(operation_id)
                    return OperationResult(operation_id, [sid], [], [FailedItem(sid, exc.code, exc.message)],
                                           exc.message, cooldown_sec=self.client.cooldown_seconds())
            try:
                if like:
                    applied = error is None and sid in self.client.liked_ids(account.user_id)
                else:
                    members = self._verify_liked_members_complete(account, liked.id)
                    applied = error is None and members is not None and sid not in members
            except AppError as exc:
                applied = False
                # A rejected verification read says nothing about the earlier write.
                error = exc if exc.code != "UPSTREAM_REJECTED" else None
            if applied:
                if like:
                    hydrated_map: dict[int, Any] = {}
                    existing = self.catalog.song(sid, account_id=str(account.user_id))
                    if is_stub_song(existing):
                        try:
                            detail = self.client.song_detail(sid)
                        except Exception:
                            detail = None
                        if detail is not None and not is_stub_song(detail):
                            hydrated_map[sid] = detail
                    self.catalog.add_confirmed_likes(
                        [sid],
                        str(account.user_id),
                        liked,
                        songs_by_id=hydrated_map or None,
                    )
                else:
                    self.catalog.remove_confirmed_unlikes([sid], account_id=str(account.user_id), playlist_id=liked.id)
                self.journal.resolve(operation_id)
                return OperationResult(operation_id, [sid], [sid], [], "已加入我喜欢的音乐" if like else "已取消喜欢",
                                       cooldown_sec=self.client.cooldown_seconds())
            self.journal.retain_only(operation_id, keys=[key], song_ids=[sid], error=error)
            return OperationResult(operation_id, [sid], message="远端尚未确认红心状态，已保留待核对",
                                   unknown_items=[FailedItem(sid, action.upper() + "_RESULT_UNKNOWN", "请稍后只读核对")],
                                   pending_operation_ids=[operation_id], cooldown_sec=self.client.cooldown_seconds())

    def create_playlist(
        self,
        name: str,
        ids: Any = None,
        privacy: Any = 0,
        *,
        expected_session_generation: int | None = None,
        current_session_generation: int | None = None,
    ) -> OperationResult:
        normalized = str(name or "").strip()
        if not normalized or len(normalized) > 100:
            raise AppError("PLAYLIST_NAME_INVALID", "歌单名称必须为 1–100 个字符")
        requested = [] if ids is None else _positive_ids(ids, self.max_batch_size)
        try:
            privacy_value = int(privacy)
        except (TypeError, ValueError) as exc:
            raise AppError("PLAYLIST_PRIVACY_INVALID", "歌单隐私设置无效") from exc
        if privacy_value not in {0, 10}:
            raise AppError("PLAYLIST_PRIVACY_INVALID", "歌单隐私设置无效")
        self._verify_session_generation(expected_session_generation, current_session_generation)
        with self._write_lock:
            self.client.check_write_allowed()
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
                if exc.code == "RISK_CONTROL_COOLDOWN":
                    self.journal.resolve(operation_id)
                    raise
                self.journal.mark_sent(operation_id, error=exc)
                return OperationResult(
                    operation_id=operation_id,
                    requested_ids=requested,
                    unknown_items=[FailedItem(None, "CREATE_PLAYLIST_RESULT_UNKNOWN", "创建请求已发出但结果未确认；请只读核对，禁止重复创建")],
                    message="歌单创建结果待核对；未继续发送加歌请求",
                    pending_operation_ids=[operation_id], cooldown_sec=self.client.cooldown_seconds(),
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
            return self._add_tracks(playlist_id, requested, add_entry, prefix="歌单已创建；")

    def add_tracks(
        self,
        playlist_id: Any,
        ids: Any,
        *,
        expected_session_generation: int | None = None,
        current_session_generation: int | None = None,
        manifest_hash: str | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> OperationResult:
        pid = _positive_playlist_id(playlist_id)
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        self._verify_session_generation(expected_session_generation, current_session_generation)
        verified_hash = self._verify_manifest_hash(manifest_hash, "add_tracks", pid, pid, requested)
        with self._write_lock:
            self.client.check_write_allowed()
            account = self.client.account()
            progress_callback = _scoped_progress(progress_callback, account.user_id, current_session_generation)
            keys = [_song_operation_key("add_tracks", pid, song_id) for song_id in requested]
            self.journal.assert_allowed(account.user_id, keys)
            # Only owned self_created playlists may receive added tracks (never subscribed/imported/cloud).
            playlist = self._owned_playlist(pid, require_self_created=True, account=account)
            target_payload: dict[str, Any] = {
                "playlist_id": playlist.id,
                "source_playlist_id": playlist.id,
                "song_ids": requested,
            }
            if verified_hash:
                target_payload["manifest_hash"] = verified_hash
            entry = self.journal.begin(
                account.user_id,
                "add_tracks",
                keys,
                target_payload,
            )
            return self._add_tracks(playlist.id, requested, entry, prefix=f"“{playlist.name}”", progress_callback=progress_callback)

    def _add_tracks(
        self,
        playlist_id: int,
        requested: list[int],
        entry: dict[str, Any],
        *,
        prefix: str = "歌单已创建；",
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> OperationResult:
        return self._write_playlist_chunks("add_tracks", playlist_id, requested, requested, entry, [], prefix, progress_callback)

    def _write_playlist_chunks(self, action, pid, requested, candidates, entry, failed, prefix, progress_callback=None):
        operation_id = entry["operation_id"]
        adding = action == "add_tracks"
        write = self.client.request_add_tracks if adding else self.client.request_remove_tracks
        self.journal.mark_sent(operation_id, resource_id=pid)
        sent = []
        rejected = {}
        last_error = None
        for start in range(0, len(candidates), PLAYLIST_WRITE_CHUNK_SIZE):
            chunk = candidates[start:start + PLAYLIST_WRITE_CHUNK_SIZE]
            try:
                write(pid, chunk)
            except AppError as exc:
                last_error = exc
                if exc.code == "UPSTREAM_REJECTED":
                    rejected.update(dict.fromkeys(chunk, exc))
                if _should_circuit_break(exc):
                    next_index = start
                    if exc.code != "RISK_CONTROL_COOLDOWN":
                        sent.extend(chunk)
                        next_index += len(chunk)
                    failed.extend(FailedItem(sid, "ABORTED_RISK_CONTROL", "远端异常，尚未发送") for sid in candidates[next_index:])
                    break
            sent.extend(chunk)
            if progress_callback:
                progress_callback({"operation_id": operation_id, "action": action, "processed": len(sent),
                                   "total": len(requested), "succeeded": 0, "status": "verifying",
                                   "cooldown_sec": self.client.cooldown_seconds()})
        try:
            remote, complete = self._fetch_playlist_track_ids(pid) if sent else (set(), True)
        except AppError as exc:
            remote, complete = set(), False
            last_error = exc
        confirmed = []
        unknown = []
        for sid in sent:
            if complete and ((sid in remote) == adding):
                confirmed.append(sid)
            elif sid in rejected:
                exc = rejected[sid]
                failed.append(FailedItem(sid, exc.code, exc.message))
            else:
                code = "ADD_TRACK_RESULT_UNKNOWN" if adding else "REMOVE_TRACK_RESULT_UNKNOWN"
                unknown.append(FailedItem(sid, code, "远端尚未确认，请稍后只读核对"))
        if unknown:
            ids = [item.id for item in unknown]
            # An explicit rejection belongs only to its own chunk, never other unresolved writes.
            error = last_error if last_error and last_error.code != "UPSTREAM_REJECTED" else None
            self.journal.retain_only(operation_id, keys=[_song_operation_key(action, pid, sid) for sid in ids],
                                     song_ids=ids, error=error)
        else:
            self.journal.resolve(operation_id)
        if progress_callback:
            progress_callback({"operation_id": operation_id, "action": action, "processed": len(sent),
                               "total": len(requested), "succeeded": len(confirmed),
                               "status": "aborted" if len(sent) < len(candidates) else "verified",
                               "cooldown_sec": self.client.cooldown_seconds()})
        verb = "添加" if adding else "移出"
        message = f"{prefix}已确认{verb} {len(confirmed)} 首，{len(failed)} 首失败，{len(unknown)} 首待核对"
        return OperationResult(operation_id, requested, confirmed, failed, message, pid, unknown,
                               [operation_id] if unknown else [], cooldown_sec=self.client.cooldown_seconds())

    def unlike(
        self,
        ids: Any,
        source_playlist_id: Any,
        *,
        expected_session_generation: int | None = None,
        current_session_generation: int | None = None,
        manifest_hash: str | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> OperationResult:
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        self._verify_session_generation(expected_session_generation, current_session_generation)
        source_id = _source_playlist_id(source_playlist_id)
        verified_hash = self._verify_manifest_hash(manifest_hash, "unlike", None, source_id, requested)
        with self._write_lock:
            self.client.check_write_allowed()
            account = self.client.account()
            progress_callback = _scoped_progress(progress_callback, account.user_id, current_session_generation)
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
            last_unknown_error: AppError | None = None
            if candidates:
                keys = [_song_operation_key("unlike", liked_playlist.id, song_id) for song_id in candidates]
                target_payload: dict[str, Any] = {
                    "playlist_id": liked_playlist.id,
                    "source_playlist_id": liked_playlist.id,
                    "song_ids": candidates,
                }
                if verified_hash:
                    target_payload["manifest_hash"] = verified_hash
                entry = self.journal.begin(
                    account.user_id,
                    "unlike",
                    keys,
                    target_payload,
                )
                operation_id = entry["operation_id"]
                self.journal.mark_sent(operation_id)

            circuit_broken = False
            sent_count = 0
            for batch_start in range(0, len(candidates), UNLIKE_MINI_BATCH_SIZE):
                if circuit_broken:
                    break
                batch = candidates[batch_start : batch_start + UNLIKE_MINI_BATCH_SIZE]
                sent_in_batch: list[int] = []
                write_errors: dict[int, AppError] = {}
                broken_song_id: int | None = None

                for song_id in batch:
                    sent_in_batch.append(song_id)
                    sent_count += 1
                    try:
                        self.client.request_unlike(song_id)
                    except (AppError, UpstreamError) as exc:
                        write_errors[song_id] = exc
                        if _should_circuit_break(exc):
                            circuit_broken = True
                            if exc.code == "RISK_CONTROL_COOLDOWN":
                                sent_in_batch.pop()
                                sent_count -= 1
                            else:
                                broken_song_id = song_id
                            last_unknown_error = exc
                            break

                if sent_in_batch:
                    self.client.pacer.settle()
                # ONE verification read per mini-batch instead of N reads.
                remote_after: set[int] | None = None
                try:
                    preserved = liked_before.difference(candidates[:sent_count])
                    remote_after = (self._verify_liked_members_complete(account, liked_playlist.id, expected_prev_ids=preserved)
                                    if sent_in_batch else set())
                except (AppError, UpstreamError) as exc:
                    if exc.code != "UPSTREAM_REJECTED":
                        last_unknown_error = exc
                    circuit_broken |= _should_circuit_break(exc)
                    remote_after = None

                if remote_after is None:
                    # If circuit-break happened on the last track in this mini-batch and
                    # earlier tracks were already sent cleanly, attempt at most ONE
                    # confirmation read for the earlier prefix so confirmed successes aren't lost.
                    earlier_prefix = [sid for sid in sent_in_batch if sid != broken_song_id]
                    if broken_song_id is not None:
                        unknowns[broken_song_id] = FailedItem(
                            broken_song_id,
                            "UNLIKE_RESULT_UNKNOWN",
                            "未能核对远端结果；没有更新本地曲库，请稍后只读核对",
                        )
                    if broken_song_id is not None and earlier_prefix and not self.client.cooldown_seconds():
                        try:
                            prefix_after = self._verify_liked_members_complete(account, liked_playlist.id, expected_prev_ids=preserved)
                        except (AppError, UpstreamError):
                            prefix_after = None
                        if prefix_after is not None:
                            for song_id in earlier_prefix:
                                err = write_errors.get(song_id)
                                if song_id not in prefix_after:
                                    succeeded.append(song_id)
                                elif err is not None and err.code == "UPSTREAM_REJECTED":
                                    failures[song_id] = FailedItem(song_id, err.code, err.message)
                                else:
                                    unknowns[song_id] = FailedItem(
                                        song_id,
                                        "UNLIKE_RESULT_UNKNOWN",
                                        "远端尚未确认取消喜欢；请稍后只读核对",
                                    )
                        else:
                            for song_id in earlier_prefix:
                                unknowns[song_id] = FailedItem(
                                    song_id,
                                    "UNLIKE_RESULT_UNKNOWN",
                                    "未能核对远端结果；没有更新本地曲库，请稍后只读核对",
                                )
                    else:
                        for song_id in sent_in_batch:
                            unknowns[song_id] = FailedItem(
                                song_id,
                                "UNLIKE_RESULT_UNKNOWN",
                                "未能核对远端结果；没有更新本地曲库，请稍后只读核对",
                            )
                else:
                    for song_id in sent_in_batch:
                        err = write_errors.get(song_id)
                        if song_id not in remote_after:
                            succeeded.append(song_id)
                        elif err is not None and err.code == "UPSTREAM_REJECTED":
                            failures[song_id] = FailedItem(song_id, err.code, err.message)
                        else:
                            unknowns[song_id] = FailedItem(
                                song_id,
                                "UNLIKE_RESULT_UNKNOWN",
                                "远端尚未确认取消喜欢；请稍后只读核对",
                            )

                if progress_callback:
                    progress_callback({"operation_id": operation_id, "action": "unlike", "processed": sent_count,
                                       "total": len(requested), "succeeded": len(succeeded),
                                       "status": "aborted" if circuit_broken else "verified",
                                       "cooldown_sec": self.client.cooldown_seconds()})

            # Any unsent candidates after circuit-breaker are guaranteed untouched -> failed, NOT unknown.
            if circuit_broken and sent_count < len(candidates):
                for unsent_id in candidates[sent_count:]:
                    failures[unsent_id] = FailedItem(
                        unsent_id,
                        "UNLIKE_ABORTED_CIRCUIT_OPEN",
                        "检测到远端异常或限流，已熔断停止发送剩余请求",
                    )

            failed = [failures[song_id] for song_id in requested if song_id in failures]
            unknown = [unknowns[song_id] for song_id in requested if song_id in unknowns]
            self.catalog.remove_confirmed_unlikes(
                succeeded,
                account_id=str(account.user_id),
                playlist_id=liked_playlist.id,
            )
            if candidates:
                if unknown:
                    unknown_ids = [item.id for item in unknown if isinstance(item.id, int)]
                    self.journal.retain_only(
                        operation_id,
                        keys=[_song_operation_key("unlike", liked_playlist.id, song_id) for song_id in unknown_ids],
                        song_ids=unknown_ids,
                        error=last_unknown_error,
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
            cooldown_sec=self.client.cooldown_seconds(),
            unknown_items=unknown,
            pending_operation_ids=[operation_id] if unknown else [],
        )

    def remove_tracks(
        self,
        playlist_id: int,
        ids: Any,
        source_playlist_id: Any,
        *,
        expected_session_generation: int | None = None,
        current_session_generation: int | None = None,
        manifest_hash: str | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> OperationResult:
        playlist_id = _positive_playlist_id(playlist_id)
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        self._verify_session_generation(expected_session_generation, current_session_generation)
        source_id = _source_playlist_id(source_playlist_id)
        if source_id != playlist_id:
            raise AppError("SOURCE_PLAYLIST_MISMATCH", "歌曲来源歌单与目标歌单不一致", 409)
        verified_hash = self._verify_manifest_hash(manifest_hash, "remove_tracks", playlist_id, source_id, requested)
        with self._write_lock:
            self.client.check_write_allowed()
            account = self.client.account()
            progress_callback = _scoped_progress(progress_callback, account.user_id, current_session_generation)
            requested_keys = [_song_operation_key("remove_tracks", playlist_id, song_id) for song_id in requested]
            self.journal.assert_allowed(account.user_id, requested_keys)
            playlist = self._owned_playlist(playlist_id, require_self_created=True, account=account)
            remote_before, before_complete = self._fetch_playlist_track_ids(playlist.id)
            if not before_complete:
                raise UpstreamError("TRACKS_INCOMPLETE", "远端歌单快照不完整，已拒绝写入以保护数据")
            candidates = [song_id for song_id in requested if song_id in remote_before]
            failures: dict[int, FailedItem] = {
                song_id: FailedItem(song_id, "TRACK_NOT_IN_PLAYLIST", "这首歌原本不在该歌单中")
                for song_id in requested
                if song_id not in remote_before
            }
            operation_id = uuid.uuid4().hex
            if candidates:
                keys = [_song_operation_key("remove_tracks", playlist.id, song_id) for song_id in candidates]
                target_payload: dict[str, Any] = {
                    "playlist_id": playlist.id,
                    "source_playlist_id": playlist.id,
                    "song_ids": candidates,
                }
                if verified_hash:
                    target_payload["manifest_hash"] = verified_hash
                entry = self.journal.begin(
                    account.user_id,
                    "remove_tracks",
                    keys,
                    target_payload,
                )
                operation_id = entry["operation_id"]
                result = self._write_playlist_chunks("remove_tracks", playlist.id, requested, candidates,
                                                     entry, list(failures.values()), f"“{playlist.name}”", progress_callback)
                self.catalog.remove_confirmed_tracks(playlist.id, result.successful_ids, account_id=str(account.user_id))
                return result
            return OperationResult(operation_id, requested, [], list(failures.values()), "没有需要移出的曲目", playlist.id)

    def reconcile(
        self,
        action: Any,
        playlist_id: Any,
        ids: Any,
        source_playlist_id: Any,
        *,
        confirm_not_applied: bool = False,
    ) -> OperationResult:
        requested = _positive_ids(ids, self.max_batch_size)
        if not requested:
            raise AppError("SONG_IDS_EMPTY", "请选择至少一首歌曲")
        normalized_action = str(action or "").strip()
        if normalized_action == "unlike":
            source_playlist_id = _source_playlist_id(source_playlist_id)
            if playlist_id is not None and playlist_id != "":
                _positive_playlist_id(playlist_id)
            with self._write_lock:
                account = self.client.account()
                user_id, liked_playlist = self._liked_playlist(source_playlist_id, account)
                remote_ids = self._verify_liked_members_complete(account, liked_playlist.id)
                if remote_ids is None:
                    keys = [_song_operation_key("unlike", liked_playlist.id, sid) for sid in requested]
                    _, pending = self.journal.active_unrejected_keys(account.user_id, keys)
                    return OperationResult(uuid.uuid4().hex, requested, unknown_items=[
                        FailedItem(sid, "UNLIKE_RESULT_UNKNOWN", "喜欢列表快照不完整") for sid in requested
                    ], pending_operation_ids=pending)
                succeeded = [song_id for song_id in requested if song_id not in remote_ids]
                still_present = [song_id for song_id in requested if song_id in remote_ids]
                still_keys = [_song_operation_key("unlike", liked_playlist.id, song_id) for song_id in still_present]
                guarded_keys, pending_ids = self.journal.active_unrejected_keys(account.user_id, still_keys)

                failed: list[FailedItem] = []
                unknown: list[FailedItem] = []
                keys_to_resolve = [_song_operation_key("unlike", liked_playlist.id, song_id) for song_id in succeeded]
                for song_id in still_present:
                    key = _song_operation_key("unlike", liked_playlist.id, song_id)
                    if key in guarded_keys and not confirm_not_applied:
                        unknown.append(
                            FailedItem(
                                song_id,
                                "UNLIKE_RESULT_UNKNOWN",
                                "远端仍在喜欢列表中且原写入无明确拒绝，保留待核对状态以防延迟生效",
                            )
                        )
                    else:
                        failed.append(FailedItem(song_id, "UNLIKE_NOT_APPLIED", "远端仍在喜欢列表中"))
                        keys_to_resolve.append(key)

                self.catalog.remove_confirmed_unlikes(
                    succeeded,
                    account_id=str(account.user_id),
                    playlist_id=liked_playlist.id,
                )
                self.journal.resolve_keys(account.user_id, keys_to_resolve)
            message = f"只读核对完成：{len(succeeded)} 首已取消喜欢"
            if failed:
                message += f"，{len(failed)} 首仍在喜欢列表"
            if unknown:
                message += f"，{len(unknown)} 首仍待核对"
            return OperationResult(
                uuid.uuid4().hex,
                requested,
                succeeded,
                failed,
                message,
                unknown_items=unknown,
                pending_operation_ids=list(dict.fromkeys(pending_ids)) if unknown else [],
            )
        if normalized_action == "remove_tracks":
            normalized_playlist_id = _positive_playlist_id(playlist_id)
            source_id = _source_playlist_id(source_playlist_id)
            if source_id != normalized_playlist_id:
                raise AppError("SOURCE_PLAYLIST_MISMATCH", "歌曲来源歌单与目标歌单不一致", 409)
            with self._write_lock:
                account = self.client.account()
                playlist = self._owned_playlist(normalized_playlist_id, require_self_created=True, account=account)
                remote_ids, complete = self._fetch_playlist_track_ids(playlist.id)
                if not complete:
                    raise UpstreamError("TRACKS_INCOMPLETE", "远端歌单快照不完整，无法核对结果")
                succeeded = [song_id for song_id in requested if song_id not in remote_ids]
                still_present = [song_id for song_id in requested if song_id in remote_ids]
                still_keys = [_song_operation_key("remove_tracks", playlist.id, song_id) for song_id in still_present]
                guarded_keys, pending_ids = self.journal.active_unrejected_keys(account.user_id, still_keys)

                failed = []
                unknown = []
                keys_to_resolve = [_song_operation_key("remove_tracks", playlist.id, song_id) for song_id in succeeded]
                for song_id in still_present:
                    key = _song_operation_key("remove_tracks", playlist.id, song_id)
                    if key in guarded_keys and not confirm_not_applied:
                        unknown.append(
                            FailedItem(
                                song_id,
                                "REMOVE_TRACK_RESULT_UNKNOWN",
                                "远端歌单仍有这首歌且原写入无明确拒绝，保留待核对状态以防延迟生效",
                            )
                        )
                    else:
                        failed.append(FailedItem(song_id, "REMOVE_TRACK_NOT_APPLIED", "远端歌单中仍有这首歌"))
                        keys_to_resolve.append(key)

                self.catalog.remove_confirmed_tracks(
                    playlist.id,
                    succeeded,
                    account_id=str(account.user_id),
                )
                self.journal.resolve_keys(account.user_id, keys_to_resolve)
            message = f"只读核对完成：{len(succeeded)} 首已移出"
            if failed:
                message += f"，{len(failed)} 首仍在歌单中"
            if unknown:
                message += f"，{len(unknown)} 首仍待核对"
            return OperationResult(
                uuid.uuid4().hex,
                requested,
                succeeded,
                failed,
                message,
                playlist.id,
                unknown_items=unknown,
                pending_operation_ids=list(dict.fromkeys(pending_ids)) if unknown else [],
            )
        raise AppError("RECONCILE_ACTION_INVALID", "只支持核对 unlike 或 remove_tracks")

    def pending_for(self, user_id: int) -> list[dict[str, Any]]:
        return self.journal.pending_for(user_id)

    def ignore_pending(self, user_id: int, operation_id: str) -> dict[str, Any]:
        with self._write_lock:
            return self.journal.ignore(user_id, operation_id)

    def reconcile_pending(
        self,
        user_id: int,
        operation_id: str,
        *,
        confirm_not_applied: bool = False,
    ) -> OperationResult:
        with self._write_lock:
            entry = self.journal.get(user_id, operation_id)
            action = entry["action"]
            target = entry["target"]
            last_error = entry.get("last_error")
            explicit_rejection = isinstance(last_error, dict) and last_error.get("code") == "UPSTREAM_REJECTED"
            can_resolve_unapplied = explicit_rejection or confirm_not_applied
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
            if action == "like":
                account = self.client.account()
                _, liked = self._liked_playlist(target["source_playlist_id"], account)
                remote = self.client.liked_ids(account.user_id)
                confirmed = [sid for sid in song_ids if sid in remote]
                missing = [sid for sid in song_ids if sid not in remote]
                self.catalog.add_confirmed_likes(confirmed, str(account.user_id), liked)
                if missing and not can_resolve_unapplied:
                    self.journal.retain_only(operation_id, keys=[_song_operation_key(action, liked.id, sid) for sid in missing],
                                             song_ids=missing)
                    return OperationResult(operation_id, song_ids, confirmed, message="喜欢状态仍待核对",
                                           unknown_items=[FailedItem(sid, "LIKE_RESULT_UNKNOWN", "远端尚未确认") for sid in missing],
                                           pending_operation_ids=[operation_id])
                self.journal.resolve(operation_id)
                return OperationResult(operation_id, song_ids, confirmed,
                                       [FailedItem(sid, "LIKE_NOT_APPLIED", "远端未收藏") for sid in missing], "只读核对完成")
            if action == "unlike":
                source_id = _source_playlist_id(target.get("source_playlist_id"))
                account = self.client.account()
                owner_id, liked = self._liked_playlist(source_id, account)
                remote_ids = self._verify_liked_members_complete(account, liked.id)
                if remote_ids is None:
                    return OperationResult(operation_id, song_ids, unknown_items=[
                        FailedItem(sid, "UNLIKE_RESULT_UNKNOWN", "喜欢列表快照不完整") for sid in song_ids
                    ], pending_operation_ids=[operation_id])
                succeeded = [song_id for song_id in song_ids if song_id not in remote_ids]
                still_present = [song_id for song_id in song_ids if song_id in remote_ids]
                self.catalog.remove_confirmed_unlikes(
                    succeeded,
                    account_id=str(account.user_id),
                    playlist_id=liked.id,
                )
                if not still_present or can_resolve_unapplied:
                    failed = [
                        FailedItem(song_id, "UNLIKE_NOT_APPLIED", "远端仍在喜欢列表中")
                        for song_id in still_present
                    ]
                    self.journal.resolve(operation_id)
                    return OperationResult(operation_id, song_ids, succeeded, failed, "只读核对完成")
                unknown = [
                    FailedItem(song_id, "UNLIKE_RESULT_UNKNOWN", "远端仍在喜欢列表中；为防写入延迟生效，继续保留待核对状态")
                    for song_id in still_present
                ]
                self.journal.retain_only(
                    operation_id,
                    keys=[_song_operation_key("unlike", liked.id, song_id) for song_id in still_present],
                    song_ids=still_present,
                )
                return OperationResult(
                    operation_id,
                    song_ids,
                    succeeded,
                    [],
                    f"只读核对：已确认 {len(succeeded)} 首，{len(still_present)} 首仍待核对",
                    unknown_items=unknown,
                    pending_operation_ids=[operation_id],
                )
            if action in {"remove_tracks", "add_tracks"}:
                playlist_id = _positive_playlist_id(target.get("playlist_id"))
                account = self.client.account()
                playlist = self._owned_playlist(playlist_id, require_self_created=True, account=account)
                try:
                    remote_ids, complete = self._fetch_playlist_track_ids(playlist.id)
                except (AppError, UpstreamError):
                    complete = False
                    remote_ids = set()
                if not complete:
                    code = "REMOVE_TRACK_RESULT_UNKNOWN" if action == "remove_tracks" else "ADD_TRACK_RESULT_UNKNOWN"
                    unknown = [
                        FailedItem(song_id, code, "远端歌单快照不完整或暂时不可读，继续保留待核对状态")
                        for song_id in song_ids
                    ]
                    return OperationResult(
                        operation_id,
                        song_ids,
                        [],
                        [],
                        "远端快照未完成，仍待核对",
                        playlist.id,
                        unknown_items=unknown,
                        pending_operation_ids=[operation_id],
                    )
                if action == "remove_tracks":
                    succeeded = [song_id for song_id in song_ids if song_id not in remote_ids]
                    unresolved = [song_id for song_id in song_ids if song_id in remote_ids]
                    self.catalog.remove_confirmed_tracks(
                        playlist.id,
                        succeeded,
                        account_id=str(account.user_id),
                    )
                    not_applied_code = "REMOVE_TRACK_NOT_APPLIED"
                    not_applied_msg = "远端歌单中仍有这首歌"
                    unknown_code = "REMOVE_TRACK_RESULT_UNKNOWN"
                    unknown_msg = "远端歌单中仍有这首歌；为防写入延迟生效，继续保留待核对状态"
                else:
                    succeeded = [song_id for song_id in song_ids if song_id in remote_ids]
                    unresolved = [song_id for song_id in song_ids if song_id not in remote_ids]
                    not_applied_code = "ADD_TRACK_NOT_APPLIED"
                    not_applied_msg = "远端歌单中仍没有这首歌"
                    unknown_code = "ADD_TRACK_RESULT_UNKNOWN"
                    unknown_msg = "远端歌单中仍没有这首歌；为防写入延迟生效，继续保留待核对状态"

                if not unresolved or can_resolve_unapplied:
                    failed = [FailedItem(song_id, not_applied_code, not_applied_msg) for song_id in unresolved]
                    self.journal.resolve(operation_id)
                    return OperationResult(operation_id, song_ids, succeeded, failed, "只读核对完成", playlist.id)
                unknown = [FailedItem(song_id, unknown_code, unknown_msg) for song_id in unresolved]
                self.journal.retain_only(
                    operation_id,
                    keys=[_song_operation_key(action, playlist.id, song_id) for song_id in unresolved],
                    song_ids=unresolved,
                )
                return OperationResult(
                    operation_id,
                    song_ids,
                    succeeded,
                    [],
                    f"只读核对：已确认 {len(succeeded)} 首，{len(unresolved)} 首仍待核对",
                    playlist.id,
                    unknown_items=unknown,
                    pending_operation_ids=[operation_id],
                )
            raise AppError("RECONCILE_ACTION_INVALID", "这条待核对操作类型无效", 409)

    def _verify_liked_members_complete(self, account, liked_playlist_id, *, expected_prev_ids=None):
        members = self.client.liked_ids(account.user_id)
        try:
            remote, complete = self._fetch_playlist_track_ids(liked_playlist_id)
            _, summary = self._liked_playlist(liked_playlist_id, account)
        except AppError as exc:
            if exc.code in {"UPSTREAM_RISK_CONTROL", "RISK_CONTROL_COOLDOWN"}:
                raise
            return None
        if expected_prev_ids and not expected_prev_ids.issubset(members):
            return None
        if complete and len(remote) == summary.track_count and remote == members:
            return remote
        return None

    def _fetch_playlist_track_ids(self, playlist_id: int) -> tuple[set[int], bool]:
        """Read a fresh remote track snapshot and verify completeness."""
        tracks = self.client.playlist_tracks(playlist_id, fresh=True)
        if not isinstance(tracks, list):
            return set(), False
        # Support optional client snapshot completeness attribute if provided.
        complete_flag = getattr(self.client, "last_playlist_tracks_complete", True)
        if complete_flag is False:
            return set(), False
        return {song.id for song in tracks if isinstance(getattr(song, "id", None), int)}, True

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


def _scoped_progress(callback, user_id, generation):
    if callback is None:
        return None
    return lambda event: callback({**event, "account_id": str(user_id), "session_generation": generation})


def _should_circuit_break(exc: AppError) -> bool:
    """Stop remaining writes on unknown transport errors or rate limits."""
    return exc.status in {405, 429, 500, 502, 503, 504} or exc.code in {
        "UPSTREAM_RISK_CONTROL", "RISK_CONTROL_COOLDOWN", "UPSTREAM_UNAVAILABLE",
        "UPSTREAM_HTTP_ERROR", "UPSTREAM_RATE_LIMITED", "UPSTREAM_TIMEOUT",
    }


def _positive_ids(value: Any, maximum: int) -> list[int]:
    if not isinstance(value, list):
        raise AppError("SONG_IDS_INVALID", "歌曲 ID 必须是数组")
    if len(value) > maximum:
        raise AppError("BATCH_TOO_LARGE", f"一次最多操作 {maximum} 首歌曲", 413)
    result: list[int] = []
    seen: set[int] = set()
    for raw in value:
        song_id = positive_id(raw)
        if song_id not in seen:
            seen.add(song_id)
            result.append(song_id)
    return result


def positive_id(value: Any, code: str = "SONG_ID_INVALID") -> int:
    if isinstance(value, str):
        digits = value.strip()
        if digits.isascii() and digits.isdigit():
            digits = digits.lstrip("0")
            if 0 < len(digits) <= 19:
                value = int(digits)
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise AppError(code, "ID 必须是正整数", 400)
    return value


def _positive_playlist_id(value: Any) -> int:
    return positive_id(value, "PLAYLIST_ID_INVALID")


def _source_playlist_id(value: Any) -> int:
    return positive_id(value, "SOURCE_PLAYLIST_ID_INVALID")


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
