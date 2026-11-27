from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import threading
from contextlib import ExitStack
from dataclasses import replace
from typing import Any, Callable

from .db import CatalogDatabase
from .errors import AppError
from .models import PlaylistSummary, Song
from .netease_client import NeteaseClient
from .storage import JsonStore
from .snapshots import checksum, save_snapshot


class CatalogService:
    FILE_NAME = "catalog.json"

    def __init__(
        self,
        store: JsonStore,
        client: NeteaseClient,
        max_batch_size: int = 500,
        db: CatalogDatabase | None = None,
    ):
        self.store = store
        self.client = client
        self.max_batch_size = max_batch_size
        self.db = db or CatalogDatabase(store.data_dir)
        self._update_lock = threading.RLock()
        self._playlist_locks: dict[str, threading.RLock] = {}
        self._active_account_id: str | None = None

    def set_active_account(self, account_id: str | None) -> None:
        self._active_account_id = str(account_id) if account_id is not None else None

    def _playlist_lock(self, playlist_id: int | str) -> threading.RLock:
        key = str(playlist_id)
        with self._update_lock:
            lock = self._playlist_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._playlist_locks[key] = lock
            return lock

    def _sync_legacy_json(self) -> None:
        """Fail-closed validation & non-destructive migration of legacy catalog.json."""
        target = self.store.path(self.FILE_NAME)
        try:
            raw_bytes = target.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise AppError(
                "LOCAL_DATA_UNREADABLE",
                f"无法读取本地数据文件 {self.FILE_NAME}；未对其进行覆盖",
                500,
            ) from exc

        digest = hashlib.sha256(raw_bytes).hexdigest()
        last_digest = self.db.get_meta("legacy_catalog_sha256")
        if last_digest == digest:
            return

        # Parse & validate via JsonStore (raises LOCAL_DATA_CORRUPT on invalid JSON
        # without ever touching the original file on disk).
        value = self.store.read(self.FILE_NAME, None)
        if not isinstance(value, dict) or not isinstance(value.get("playlists"), dict):
            raise AppError("LOCAL_DATA_INVALID", "本地曲库结构无效；原文件已保留，未执行覆盖", 500)

        for key, raw_playlist in value["playlists"].items():
            if not isinstance(raw_playlist, dict):
                continue
            raw_summary = raw_playlist.get("summary")
            if not isinstance(raw_summary, dict):
                continue
            kind = str(raw_summary.get("kind") or "self_created")
            pid_raw = raw_summary.get("id", key)
            if kind == "imported" or str(key) == "imported":
                pid: int | str = "imported"
                kind = "imported"
                target_account = str(raw_playlist.get("account_id") or "local")
            else:
                pid = int(pid_raw) if isinstance(pid_raw, int) or str(pid_raw).isdigit() else str(pid_raw)
                target_account = str(raw_playlist.get("account_id") or "unclaimed")

            raw_songs = raw_playlist.get("songs", [])
            songs: list[Song] = []
            if isinstance(raw_songs, list):
                for item in raw_songs:
                    if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                        continue
                    song = _song_from_stored(item)
                    if kind == "imported" and song.source_playlist_id is None:
                        song = replace(song, source_playlist_id="imported")
                    elif song.source_playlist_id is None:
                        song = replace(song, source_playlist_id=pid)
                    songs.append(song)

            summary = PlaylistSummary(
                id=pid,
                name=str(raw_summary.get("name") or "未命名歌单"),
                track_count=len(songs),
                kind=kind,
                privacy=str(raw_summary.get("privacy") or ("local" if kind == "imported" else "public")),
                cover_url=str(raw_summary.get("cover_url") or ""),
            )
            self.db.replace_playlist_tracks(target_account, summary, songs)

        self.db.set_meta("legacy_catalog_sha256", digest)

    def get(self, account_id: str | None = None) -> dict[str, Any]:
        self._sync_legacy_json()
        if account_id is not None:
            return self.db.get_catalog_dict(str(account_id))
        if self._active_account_id:
            active_data = self.db.get_catalog_dict(self._active_account_id)
            if active_data["playlists"]:
                return active_data
        # Legacy no-arg get(): inspect active, then unclaimed, then local
        merged: dict[str, Any] = {}
        for scope in (self._active_account_id, "unclaimed", "local"):
            if not scope:
                continue
            data = self.db.get_catalog_dict(scope)
            for pid, entry in data["playlists"].items():
                if pid not in merged:
                    merged[pid] = entry
        return {"version": 1, "playlists": merged}

    def view(
        self,
        *,
        account_id: str | None = None,
        playlist_id: str | int | None = None,
        q: str = "",
        filter_name: str = "",
        sort: str = "",
        direction: str = "asc",
        page: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        self._sync_legacy_json()
        effective_account = str(account_id) if account_id is not None else (self._active_account_id or "local")
        pid_str = str(playlist_id) if playlist_id is not None and str(playlist_id) != "" else None
        return self.db.query_view(
            effective_account,
            playlist_id=pid_str,
            q=q,
            filter_name=filter_name,
            sort=sort,
            direction=direction,
            page=page,
            page_size=page_size,
        )

    def sync(
        self,
        playlist: PlaylistSummary,
        *,
        account_id: str | None = None,
        client_override: NeteaseClient | None = None,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        aid = str(account_id) if account_id is not None else (self._active_account_id or "local")
        active_client = client_override or self.client
        with self._playlist_lock(playlist.id):
            if on_progress:
                on_progress({"stage": "fetching", "playlist_id": playlist.id, "playlist_name": playlist.name, "progress": 0.1})
            raw_songs = active_client.playlist_tracks(playlist.id, fresh=True)
            songs = [
                replace(
                    song,
                    source_playlist_id=playlist.id,
                    source_playlist_name=playlist.name,
                )
                for song in raw_songs
            ]
            updated_playlist = replace(playlist, track_count=len(songs))
            if on_progress:
                on_progress({"stage": "writing", "playlist_id": playlist.id, "playlist_name": playlist.name, "count": len(songs), "progress": 0.85})
            self.db.replace_playlist_tracks(aid, updated_playlist, songs)
            if on_progress:
                on_progress({"stage": "done", "playlist_id": playlist.id, "playlist_name": playlist.name, "count": len(songs), "progress": 1.0})
        return {
            "synced_count": len(songs),
            "playlist": updated_playlist.to_dict(),
            "tracks": [song.to_dict() for song in songs],
        }

    def sync_cloud(self, songs: list[Song], *, account_id: str) -> dict[str, Any]:
        aid = str(account_id)
        summary = PlaylistSummary(
            id="cloud",
            name="网易云云盘",
            track_count=len(songs),
            kind="cloud",
            privacy="private",
            cover_url=songs[0].cover_url if songs else "",
        )
        normalized = [
            replace(song, source_playlist_id="cloud", source_playlist_name="网易云云盘", is_cloud=True)
            for song in songs
        ]
        with self._playlist_lock("cloud"):
            self.db.replace_playlist_tracks(aid, summary, normalized)
        return {
            "synced_count": len(normalized),
            "playlist": summary.to_dict(),
            "tracks": [song.to_dict() for song in normalized],
        }

    def import_catalog(
        self,
        content: str,
        format_name: str,
        playlist_name: str = "导入歌曲",
        *,
        mode: str = "merge",
        account_id: str | None = None,
    ) -> dict[str, Any]:
        if mode not in ("merge", "replace"):
            raise AppError("IMPORT_MODE_INVALID", "导入方式必须是合并或替换")
        aid = str(account_id) if account_id is not None else (self._active_account_id or "local")
        if not isinstance(content, str) or not content.strip():
            raise AppError("IMPORT_EMPTY", "导入内容为空")
        normalized_format = format_name.strip().lower()
        if normalized_format == "json":
            try:
                value = json.loads(content)
            except ValueError as exc:
                raise AppError("IMPORT_JSON_INVALID", "JSON 格式错误") from exc
            if isinstance(value, dict) and value.get("schema_version") == 2:
                return self._restore_snapshot(value, aid, mode)
            rows = self._json_rows(value)
        elif normalized_format == "csv":
            rows = list(csv.DictReader(io.StringIO(content)))
        else:
            raise AppError("IMPORT_FORMAT_UNSUPPORTED", "只支持 JSON 或 CSV")
        if len(rows) > self.max_batch_size:
            raise AppError("IMPORT_TOO_LARGE", f"一次最多导入 {self.max_batch_size} 首歌曲", 413)
        songs: list[Song] = []
        seen: set[int] = set()
        failures: list[dict[str, Any]] = []
        name = str(playlist_name or "导入歌曲")[:100]
        import_id = "imported"
        for index, row in enumerate(rows, start=1):
            try:
                song = _song_from_import(row)
            except (TypeError, ValueError) as exc:
                failures.append({"row": index, "message": str(exc)})
                continue
            if song.id not in seen:
                seen.add(song.id)
                songs.append(replace(song, source_playlist_id=import_id, source_playlist_name=name))
        summary = PlaylistSummary(
            id=import_id,
            name=name,
            track_count=len(songs),
            kind="imported",
            privacy="local",
        )
        result = self._store_import(aid, [(summary, songs)], mode, failures)
        result["existing_count"] += len(rows) - len(failures) - len(songs)
        return result

    import_content = import_catalog

    def _restore_snapshot(self, value, aid, mode):
        if not value.get("checksum") or value["checksum"] != checksum(value):
            raise AppError("SNAPSHOT_CHECKSUM_INVALID", "快照校验失败", 400)
        if isinstance(value.get("tracks"), list) and len(value["tracks"]) > 100_000:
            raise AppError("IMPORT_TOO_LARGE", "快照超过 100000 首上限", 413)
        try:
            tracks = {}
            for raw in value["tracks"]:
                song = _song_from_import(raw)
                if song.id in tracks:
                    raise ValueError("duplicate track")
                tracks[song.id] = _song_from_stored(raw)
            groups = []
            seen = set()
            for row in value["playlists"]:
                pid = row["id"]
                if isinstance(pid, bool) or not isinstance(pid, (str, int)) or not str(pid) or str(pid) in seen:
                    raise ValueError("invalid playlist")
                seen.add(str(pid))
                ids = row["track_ids"]
                if not isinstance(ids, list) or any(type(sid) is not int for sid in ids) or len(set(ids)) != len(ids):
                    raise ValueError("invalid membership")
                songs = [tracks[sid] for sid in ids]
                summary = PlaylistSummary(pid, str(row["name"]), len(ids), str(row["kind"]),
                                          str(row["privacy"]), str(row.get("cover_url", "")))
                groups.append((summary, songs))
        except (ValueError, AttributeError, KeyError, TypeError) as exc:
            raise AppError("IMPORT_JSON_INVALID", "快照中的歌单或曲目无效") from exc
        return self._store_import(aid, groups, mode, [])

    def _store_import(self, aid, groups, mode, failures):
        count = len({song.id for _, songs in groups for song in songs})
        if not count:
            raise AppError("IMPORT_NO_VALID_TRACKS", "未解析到有效曲目，原有导入列表保持不变", 400)
        self._sync_legacy_json()
        with ExitStack() as locks:
            for pid in sorted(str(summary.id) for summary, _ in groups):
                locks.enter_context(self._playlist_lock(pid))
            existing = self.db.get_catalog_dict(aid)["playlists"]
            added_ids = set()
            writes = []
            backups = []
            for summary, incoming in groups:
                previous = existing.get(str(summary.id))
                previous_ids = {row["id"] for row in previous["songs"]} if previous else set()
                added_ids.update(song.id for song in incoming if song.id not in previous_ids)
                songs = incoming
                if previous and previous["songs"]:
                    if mode == "replace":
                        backups.append(previous)
                    else:
                        old = [_song_from_stored(raw) for raw in previous["songs"]]
                        known = {song.id for song in old}
                        songs = old + [song for song in incoming if song.id not in known]
                writes.append((replace(summary, track_count=len(songs)), songs))
            backup = save_snapshot(self.store, backups, aid)[0] if backups else None
            self.db.replace_playlists(aid, writes)
            self._active_account_id = aid
        summary, songs = writes[0]
        stored = [replace(song, source_playlist_id=summary.id, source_playlist_name=summary.name).to_dict()
                  for song in songs]
        return {"imported_count": count, "added_count": len(added_ids), "existing_count": count - len(added_ids),
                "failed_count": len(failures), "failed_rows": failures, "tracks": stored,
                "playlist": {"summary": summary.to_dict(), "songs": stored},
                **({"backup_id": backup["backup_id"]} if backup else {})}

    def song(self, song_id: int, account_id: str | None = None) -> Song | None:
        self._sync_legacy_json()
        aid = str(account_id) if account_id is not None else self._active_account_id
        return self.db.get_song(song_id, account_id=aid)

    def add_confirmed_likes(self, ids, account_id, playlist, *, songs_by_id: dict[int, Song] | None = None):
        if not ids:
            return
        with self._playlist_lock(playlist.id):
            self.db.claim_unclaimed_playlist(account_id, str(playlist.id))
            self.db.append_liked_members(account_id, playlist, ids, songs_by_id=songs_by_id)

    def remove_confirmed_unlikes(
        self,
        ids: list[int],
        *,
        account_id: str | None = None,
        playlist_id: int | str | None = None,
    ) -> None:
        self._sync_legacy_json()
        unique_ids = list(dict.fromkeys(int(s) for s in ids))
        if not unique_ids:
            return
        aid = str(account_id) if account_id is not None else (self._active_account_id or "local")
        pid_str = str(playlist_id) if playlist_id is not None else None
        lock_key = pid_str or "__liked__"
        with self._playlist_lock(lock_key):
            if pid_str is not None and aid != "local":
                self.db.claim_unclaimed_playlist(aid, pid_str)
            self.db.remove_tracks_from_liked(aid, unique_ids, playlist_id=pid_str)
            self._active_account_id = aid

    def remove_confirmed_tracks(
        self,
        playlist_id: int | str,
        ids: list[int],
        *,
        account_id: str | None = None,
    ) -> None:
        self._sync_legacy_json()
        unique_ids = list(dict.fromkeys(int(s) for s in ids))
        if not unique_ids:
            return
        aid = str(account_id) if account_id is not None else (self._active_account_id or "local")
        pid_str = str(playlist_id)
        with self._playlist_lock(pid_str):
            if aid != "local":
                self.db.claim_unclaimed_playlist(aid, pid_str)
            self.db.remove_tracks_from_playlist(aid, pid_str, unique_ids)
            self._active_account_id = aid

    @staticmethod
    def _json_rows(value) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = value.get("songs", value.get("tracks"))
        if not isinstance(value, list):
            raise AppError("IMPORT_JSON_INVALID", "JSON 必须是歌曲数组或包含 songs 数组")
        return value


class RulesService:
    FILE_NAME = "rules.json"

    def __init__(self, store: JsonStore):
        self.store = store

    def get(self) -> list[dict[str, Any]]:
        value = self.store.read(self.FILE_NAME, [])
        if not isinstance(value, list):
            raise AppError("LOCAL_DATA_INVALID", "本地规则结构无效；原文件已保留，未执行覆盖", 500)
        return value

    def put(self, rules: Any) -> list[dict[str, Any]]:
        with self.store._lock:
            # Preserve a corrupt existing file for recovery.
            self.get()
            validated = self.validate(rules)
            self.store.write(self.FILE_NAME, validated)
            return validated

    @staticmethod
    def validate(rules):
        if not isinstance(rules, list) or len(rules) > 100:
            raise AppError("RULES_INVALID", "规则必须是数组，且最多 100 条")
        validated: list[dict[str, Any]] = []
        ids: set[str] = set()
        for raw in rules:
            if not isinstance(raw, dict):
                raise AppError("RULE_INVALID", "每条规则必须是对象")
            rule_id = str(raw.get("id") or "").strip()
            name = str(raw.get("name") or "").strip()
            output = str(raw.get("output_playlist_name") or "").strip()
            clauses = raw.get("match_clauses")
            if not rule_id or len(rule_id) > 80 or rule_id in ids or not name or len(name) > 100:
                raise AppError("RULE_INVALID", "规则 ID 或名称无效或重复")
            if not isinstance(clauses, list) or len(clauses) > 20 or not all(isinstance(item, dict) for item in clauses):
                raise AppError("RULE_INVALID", f"规则“{name}”的匹配条件无效")
            if not all(_valid_clause(clause) for clause in clauses):
                raise AppError("RULE_INVALID", f"规则“{name}”的字段、运算符或值无效")
            if len(output) > 100:
                raise AppError("RULE_INVALID", f"规则“{name}”的输出歌单名无效")
            if raw.get("action", "prune") != "prune" or raw.get("match_mode", "all") not in ("all", "any"):
                raise AppError("RULE_INVALID", f"规则“{name}”的动作或匹配模式无效")
            ids.add(rule_id)
            validated.append(
                {
                    "id": rule_id,
                    "name": name,
                    "enabled": bool(raw.get("enabled", True)),
                    "match_clauses": clauses,
                    "output_playlist_name": output,
                    **{key: raw[key] for key in ("action", "match_mode") if key in raw},
                }
            )
        return validated


# Match the portable token grammar used by RuleEngine before native compilation.
_PORTABLE_ESCAPE = r"\\(?:[dDsSwWbBfnrtv\\.^$|?*+(){}\[\]\/-]|x[0-9A-Fa-f]{2}|u(?:[0-9A-CE-Fa-ce-f][0-9A-Fa-f]{3}|[Dd][0-7][0-9A-Fa-f]{2}))"
_PORTABLE_CLASS = r"\[\^?(?:" + _PORTABLE_ESCAPE + r"|[^\]\\])+\]"
_PORTABLE_TOKEN = re.compile(
    r"(?:\\[1-9][0-9]*|" + _PORTABLE_ESCAPE + "|" + _PORTABLE_CLASS
    + r"|\(\?[:=!]|\((?!\?)|\{[0-9]+(?:,[0-9]*)?\}|[^\\\[\](){}]|\))"
)
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")


def _portable_regex(pattern):
    groups = []
    closed = set()
    capture_count = 0
    pos = 0
    previous = ""
    while pos < len(pattern):
        match = _PORTABLE_TOKEN.match(pattern, pos)
        if not match:
            return False
        part = match.group()
        pos = match.end()
        if part.startswith("("):
            capture_count += part == "("
            groups.append((part, capture_count))
        elif part == ")":
            if not groups:
                return False
            group, index = groups.pop()
            if group == "(":
                closed.add(index)
            part = "assertion" if group in ("(?=", "(?!") else ")"
        elif part.startswith("\\") and part[1:].isdigit():
            if len(part) > 3 or int(part[1:]) not in closed:
                return False
        elif part.startswith("{") or part in ("*", "+", "?"):
            if previous == "assertion" or (part == "+" and previous == "quantifier"):
                return False
            if part.startswith("{") and any(int(n) >= 2**32-1 for n in re.findall(r"[0-9]+", part)):
                return False
            part = "quantifier"
        elif part.startswith("[") and part in ("[]", "[^]"):
            return False
        elif part == r"\-":
            return False
        previous = part
    return not groups


def _valid_clause(clause):
    field, op, value = (clause.get(k) for k in ("field", "operator", "value"))
    if not isinstance(value, (str, int, float)) or not str(value).strip():
        return False
    if field == "liked":
        return op in ("equals", "not_equals") and str(value).lower() in ("true", "false")
    if isinstance(value, bool):
        return False
    if field == "duration":
        if op not in ("equals", "not_equals", "lt", "lte", "gt", "gte"):
            return False
        if isinstance(value, (int, float)):
            return math.isfinite(float(value))
        try:
            return bool(_DECIMAL.fullmatch(value)) and math.isfinite(float(value))
        except (ValueError, OverflowError):
            return False
    if field not in ("title", "artist", "artists", "album", "availability"):
        return False
    if op == "regex":
        try:
            if not _portable_regex(str(value)):
                return False
            re.compile(str(value))
            return True
        except (re.error, ValueError, RecursionError, OverflowError):
            return False
    return op in ("equals", "not_equals", "contains", "not_contains", "starts_with", "ends_with")


def _song_from_import(raw: dict[str, Any]) -> Song:
    if not isinstance(raw, dict):
        raise ValueError("歌曲行必须是对象")
    if str(raw.get("local_path") or "").strip():
        raise ValueError("仅支持导入网易云曲目")
    raw_id = raw.get("id", raw.get("song_id"))
    providers = [str(raw.get(k) or "").strip().lower() for k in ("provider", "source")]
    key = str(raw.get("track_key") or "").strip().lower()
    if any(p not in ("", "netease", "cloud", "imported") for p in providers) or (key and not key.startswith("netease:")):
        raise ValueError("仅支持导入网易云曲目")
    if not str(raw_id).isascii() or not str(raw_id).isdigit():
        raise ValueError("仅支持导入网易云曲目")
    song_id = int(raw_id)
    if not 0 < song_id <= 2**63 - 1 or (key and key != f"netease:{song_id}"):
        raise ValueError("仅支持导入网易云曲目")
    title = str(raw.get("title") or raw.get("name") or "").strip()
    if not title:
        raise ValueError("歌曲名称不能为空")
    artists_raw = raw.get("artists", raw.get("artist", []))
    if isinstance(artists_raw, str):
        artists = [item.strip() for item in artists_raw.replace("；", ";").split(";") if item.strip()]
    elif isinstance(artists_raw, list):
        artists = [str(item).strip() for item in artists_raw if str(item).strip()]
    else:
        artists = []
    duration = int(raw.get("duration_ms") or raw.get("duration") or 0)
    return Song(
        id=song_id,
        title=title[:300],
        artists=artists[:20],
        album=str(raw.get("album") or "")[:300],
        duration=max(0, duration),
        cover_url=str(raw.get("cover_url") or ""),
        source_playlist_id=None,
        availability=str(raw.get("availability") or "unknown"),
        fee=raw.get("fee") if isinstance(raw.get("fee"), int) else None,
        privilege_st=raw.get("privilege_st") if isinstance(raw.get("privilege_st"), int) else None,
        max_br_level=str(raw.get("max_br_level") or ""),
        is_cloud=bool(raw.get("is_cloud", False)),
        artist_ids=[i for i in raw.get("artist_ids", []) if type(i) is int and i > 0] if isinstance(raw.get("artist_ids"), list) else [],
    )


def _song_from_stored(raw: dict[str, Any]) -> Song:
    return Song(
        id=int(raw["id"]),
        title=str(raw.get("title") or ""),
        artists=[str(item) for item in raw.get("artists", [])],
        album=str(raw.get("album") or ""),
        duration=int(raw.get("duration_ms") or raw.get("duration") or 0),
        cover_url=str(raw.get("cover_url") or ""),
        source_playlist_id=(
            raw.get("source_playlist_id")
            if isinstance(raw.get("source_playlist_id"), int)
            or raw.get("source_playlist_id") in {"imported", "cloud"}
            else None
        ),
        availability=str(raw.get("availability") or "unknown"),
        chorus_start=raw.get("chorus_start") if isinstance(raw.get("chorus_start"), int) else None,
        chorus_end=raw.get("chorus_end") if isinstance(raw.get("chorus_end"), int) else None,
        fee=raw.get("fee") if isinstance(raw.get("fee"), int) else None,
        privilege_st=raw.get("privilege_st") if isinstance(raw.get("privilege_st"), int) else None,
        max_br_level=str(raw.get("max_br_level") or ""),
        is_cloud=bool(raw.get("is_cloud", False)),
        artist_ids=[i for i in raw.get("artist_ids", []) if type(i) is int and i > 0] if isinstance(raw.get("artist_ids"), list) else [],
        source_playlist_name=str(raw.get("source_playlist_name") or ""),
    )
