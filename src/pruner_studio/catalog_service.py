from __future__ import annotations

import csv
import io
import json
import threading
from typing import Any

from .errors import AppError
from .models import PlaylistSummary, Song
from .netease_client import NeteaseClient
from .storage import JsonStore


class CatalogService:
    FILE_NAME = "catalog.json"

    def __init__(self, store: JsonStore, client: NeteaseClient, max_batch_size: int = 500):
        self.store = store
        self.client = client
        self.max_batch_size = max_batch_size
        self._update_lock = threading.RLock()

    def get(self) -> dict[str, Any]:
        value = self.store.read(self.FILE_NAME, {"version": 1, "playlists": {}})
        if not isinstance(value, dict) or not isinstance(value.get("playlists"), dict):
            return {"version": 1, "playlists": {}}
        return value

    def view(self) -> dict[str, Any]:
        catalog = self.get()
        playlists: list[dict[str, Any]] = []
        tracks: list[dict[str, Any]] = []
        for value in catalog["playlists"].values():
            if not isinstance(value, dict):
                continue
            summary = value.get("summary")
            if isinstance(summary, dict):
                playlists.append(summary)
            for song in value.get("songs", []):
                if not isinstance(song, dict):
                    continue
                exposed = dict(song)
                if (
                    isinstance(summary, dict)
                    and summary.get("kind") == "imported"
                    and exposed.get("source_playlist_id") is None
                ):
                    exposed["source_playlist_id"] = "imported"
                tracks.append(exposed)
        return {"version": catalog.get("version", 1), "playlists": playlists, "tracks": tracks}

    def sync(self, playlist: PlaylistSummary) -> dict[str, Any]:
        with self._update_lock:
            songs = self.client.playlist_tracks(playlist.id, fresh=True)
            catalog = self.get()
            catalog["playlists"][str(playlist.id)] = {
                "summary": playlist.to_dict(),
                "songs": [song.to_dict() for song in songs],
            }
            self.store.write(self.FILE_NAME, catalog)
        return {"synced_count": len(songs), "playlist": playlist.to_dict(), "tracks": [song.to_dict() for song in songs]}

    def import_content(self, content: str, format_name: str, playlist_name: str = "导入歌曲") -> dict[str, Any]:
        if not isinstance(content, str) or not content.strip():
            raise AppError("IMPORT_EMPTY", "导入内容为空")
        normalized_format = format_name.strip().lower()
        if normalized_format == "json":
            rows = self._json_rows(content)
        elif normalized_format == "csv":
            rows = list(csv.DictReader(io.StringIO(content)))
        else:
            raise AppError("IMPORT_FORMAT_UNSUPPORTED", "只支持 JSON 或 CSV")
        if len(rows) > self.max_batch_size:
            raise AppError("IMPORT_TOO_LARGE", f"一次最多导入 {self.max_batch_size} 首歌曲", 413)
        songs: list[Song] = []
        seen: set[int] = set()
        failures: list[dict[str, Any]] = []
        for index, row in enumerate(rows, start=1):
            try:
                song = _song_from_import(row)
            except (TypeError, ValueError) as exc:
                failures.append({"row": index, "message": str(exc)})
                continue
            if song.id not in seen:
                seen.add(song.id)
                songs.append(song)
        import_id = "imported"
        stored_songs = [{**song.to_dict(), "source_playlist_id": import_id} for song in songs]
        with self._update_lock:
            catalog = self.get()
            catalog["playlists"][import_id] = {
                "summary": {
                    "id": import_id,
                    "name": str(playlist_name or "导入歌曲")[:100],
                    "track_count": len(songs),
                    "kind": "imported",
                    "privacy": "local",
                },
                "songs": stored_songs,
            }
            self.store.write(self.FILE_NAME, catalog)
        return {
            "imported_count": len(songs),
            "failed_rows": failures,
            "playlist": catalog["playlists"][import_id],
            "tracks": stored_songs,
        }

    def song(self, song_id: int) -> Song | None:
        for playlist in self.get()["playlists"].values():
            if not isinstance(playlist, dict):
                continue
            for raw in playlist.get("songs", []):
                if isinstance(raw, dict) and raw.get("id") == song_id:
                    return _song_from_stored(raw)
        return None

    def remove_confirmed_unlikes(self, ids: list[int]) -> None:
        remove = set(ids)
        if not remove:
            return
        with self._update_lock:
            catalog = self.get()
            changed = False
            for playlist in catalog["playlists"].values():
                if not isinstance(playlist, dict):
                    continue
                summary = playlist.get("summary")
                if not isinstance(summary, dict) or summary.get("kind") != "liked":
                    continue
                songs = playlist.get("songs", [])
                retained = [song for song in songs if not (isinstance(song, dict) and song.get("id") in remove)]
                if len(retained) != len(songs):
                    playlist["songs"] = retained
                    summary["track_count"] = len(retained)
                    changed = True
            if changed:
                self.store.write(self.FILE_NAME, catalog)

    def remove_confirmed_tracks(self, playlist_id: int, ids: list[int]) -> None:
        remove = set(ids)
        if not remove:
            return
        with self._update_lock:
            catalog = self.get()
            playlist = catalog["playlists"].get(str(playlist_id))
            if not isinstance(playlist, dict):
                return
            songs = playlist.get("songs", [])
            if not isinstance(songs, list):
                return
            retained = [song for song in songs if not (isinstance(song, dict) and song.get("id") in remove)]
            if len(retained) == len(songs):
                return
            playlist["songs"] = retained
            summary = playlist.get("summary")
            if isinstance(summary, dict):
                summary["track_count"] = len(retained)
            self.store.write(self.FILE_NAME, catalog)

    @staticmethod
    def _json_rows(content: str) -> list[dict[str, Any]]:
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise AppError("IMPORT_JSON_INVALID", f"JSON 格式错误：第 {exc.lineno} 行") from exc
        if isinstance(value, dict):
            value = value.get("songs", value.get("tracks"))
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise AppError("IMPORT_JSON_INVALID", "JSON 必须是歌曲数组或包含 songs 数组")
        return value


class RulesService:
    FILE_NAME = "rules.json"

    def __init__(self, store: JsonStore):
        self.store = store

    def get(self) -> list[dict[str, Any]]:
        value = self.store.read(self.FILE_NAME, [])
        return value if isinstance(value, list) else []

    def put(self, rules: Any) -> list[dict[str, Any]]:
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
            if not output or len(output) > 100:
                raise AppError("RULE_INVALID", f"规则“{name}”的输出歌单名无效")
            ids.add(rule_id)
            validated.append(
                {
                    "id": rule_id,
                    "name": name,
                    "enabled": bool(raw.get("enabled", True)),
                    "match_clauses": clauses,
                    "output_playlist_name": output,
                }
            )
        self.store.write(self.FILE_NAME, validated)
        return validated


def _song_from_import(raw: dict[str, Any]) -> Song:
    song_id = int(raw.get("id") or raw.get("song_id") or 0)
    if song_id <= 0:
        raise ValueError("歌曲 ID 必须是正整数")
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
    duration = int(raw.get("duration") or raw.get("duration_ms") or 0)
    return Song(
        id=song_id,
        title=title[:300],
        artists=artists[:20],
        album=str(raw.get("album") or "")[:300],
        duration=max(0, duration),
        cover_url=str(raw.get("cover_url") or ""),
        source_playlist_id=None,
        availability=str(raw.get("availability") or "unknown"),
    )


def _song_from_stored(raw: dict[str, Any]) -> Song:
    return Song(
        id=int(raw["id"]),
        title=str(raw.get("title") or ""),
        artists=[str(item) for item in raw.get("artists", [])],
        album=str(raw.get("album") or ""),
        duration=int(raw.get("duration") or 0),
        cover_url=str(raw.get("cover_url") or ""),
        source_playlist_id=(
            raw.get("source_playlist_id")
            if isinstance(raw.get("source_playlist_id"), int) or raw.get("source_playlist_id") == "imported"
            else None
        ),
        availability=str(raw.get("availability") or "unknown"),
        chorus_start=raw.get("chorus_start") if isinstance(raw.get("chorus_start"), int) else None,
        chorus_end=raw.get("chorus_end") if isinstance(raw.get("chorus_end"), int) else None,
    )
