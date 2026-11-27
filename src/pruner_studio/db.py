from __future__ import annotations

import json
import math
import sqlite3
import threading
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .track_identity import normalize_provider, parse_track_key
from .models import PlaylistSummary, Song

FEEDBACK_MAX_ROWS_PER_ACCOUNT = 2000
FEEDBACK_RETENTION_DAYS = 30
VALID_FEEDBACK_ACTIONS = frozenset({"not_interested", "replay", "like_explicit", "relisten_later"})
PLAY_COUNTERS = ("play_count", "completed_count", "total_listen_ms")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_playlist_id(raw: str) -> int | str:
    return int(raw) if raw.isdigit() else raw


def is_stub_song(song: Song | dict[str, Any] | None) -> bool:
    """Return True when a song record only holds placeholder stub metadata (e.g. '歌曲 #<id>')."""
    if song is None:
        return True
    if isinstance(song, Song):
        sid = int(song.id)
        title = str(song.title or "").strip()
        artists = [str(a).strip() for a in (song.artists or []) if str(a).strip()]
        duration = int(song.duration or 0)
    elif isinstance(song, dict):
        try:
            sid = int(song.get("id") or 0)
        except (TypeError, ValueError):
            sid = 0
        title = str(song.get("title") or "").strip()
        raw_artists = song.get("artists")
        artists = [str(a).strip() for a in raw_artists if str(a).strip()] if isinstance(raw_artists, list) else []
        try:
            duration = int(song.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
    else:
        return True
    if not title:
        return True
    if sid > 0 and title == f"歌曲 #{sid}" and not artists and duration <= 0:
        return True
    return False


class CatalogDatabase:
    """Account-partitioned SQLite WAL storage for Pruner Studio."""

    FILE_NAME = "catalog.sqlite3"

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / self.FILE_NAME
        self._lock = threading.RLock()
        self._init_schema()
        self._init_discovery_schema()
        self._init_canonical_schema()
        self._migrate_composite_keys()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def write_batch(self):
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    def close(self) -> None:
        # Connections are opened and closed per operation so Windows temp
        # directories and process shutdown never encounter lingering handles.
        return None

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if row and row[0] == "2":
                    return
                if row and row[0] != "1":
                    raise ValueError("Unsupported catalog schema version")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    avatar_url TEXT NOT NULL DEFAULT '',
                    vip_type INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS playlists (
                    account_id TEXT NOT NULL,
                    playlist_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    track_count INTEGER NOT NULL DEFAULT 0,
                    kind TEXT NOT NULL,
                    privacy TEXT NOT NULL DEFAULT 'public',
                    cover_url TEXT NOT NULL DEFAULT '',
                    synced_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, playlist_id)
                );

                CREATE TABLE IF NOT EXISTS songs (
                    song_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    artists_json TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    duration INTEGER NOT NULL DEFAULT 0,
                    cover_url TEXT NOT NULL DEFAULT '',
                    fee INTEGER,
                    privilege_st INTEGER,
                    max_br_level TEXT NOT NULL DEFAULT '',
                    is_cloud INTEGER NOT NULL DEFAULT 0,
                    chorus_start INTEGER,
                    chorus_end INTEGER,
                    availability TEXT NOT NULL DEFAULT 'unknown'
                );

                CREATE TABLE IF NOT EXISTS playlist_tracks (
                    account_id TEXT NOT NULL,
                    playlist_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    song_id INTEGER NOT NULL,
                    PRIMARY KEY (account_id, playlist_id, position)
                );

                CREATE INDEX IF NOT EXISTS idx_playlist_tracks_account_song
                    ON playlist_tracks(account_id, song_id);
                CREATE INDEX IF NOT EXISTS idx_playlist_tracks_account_playlist_song
                    ON playlist_tracks(account_id, playlist_id, song_id);

                CREATE TABLE IF NOT EXISTS explicit_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id TEXT NOT NULL,
                    song_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    context TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_explicit_feedback_account_created
                    ON explicit_feedback(account_id, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_explicit_feedback_account_song_action
                    ON explicit_feedback(account_id, song_id, action);

                CREATE TABLE IF NOT EXISTS local_files (
                    path TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL DEFAULT '',
                    album TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    format TEXT NOT NULL DEFAULT '',
                    bitrate INTEGER NOT NULL DEFAULT 0,
                    mtime REAL NOT NULL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS play_history (
                    account_id TEXT NOT NULL,
                    song_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    artists_json TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    duration INTEGER NOT NULL DEFAULT 0,
                    cover_url TEXT NOT NULL DEFAULT '',
                    play_count INTEGER NOT NULL DEFAULT 0,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    total_listen_ms INTEGER NOT NULL DEFAULT 0,
                    last_played_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, song_id)
                );

                CREATE INDEX IF NOT EXISTS idx_play_history_account_last
                    ON play_history(account_id, last_played_at DESC);
                """
            )

            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version and version[0] == "2":
                return
            if version and version[0] != "1":
                raise ValueError("Unsupported catalog schema version")
            conn.execute("""CREATE TABLE IF NOT EXISTS account_songs (
                    account_id TEXT NOT NULL,
                    song_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    artists_json TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    duration INTEGER NOT NULL DEFAULT 0,
                    cover_url TEXT NOT NULL DEFAULT '',
                    fee INTEGER,
                    privilege_st INTEGER,
                    max_br_level TEXT NOT NULL DEFAULT '',
                    is_cloud INTEGER NOT NULL DEFAULT 0,
                    chorus_start INTEGER,
                    chorus_end INTEGER,
                    availability TEXT NOT NULL DEFAULT 'unknown',
                    PRIMARY KEY (account_id, song_id)
                );""")
            conn.execute("""INSERT INTO account_songs SELECT DISTINCT pt.account_id, s.*
                            FROM playlist_tracks pt JOIN songs s ON s.song_id=pt.song_id""")
            conn.execute("""CREATE TABLE IF NOT EXISTS play_history_v2 (
                    track_key TEXT NOT NULL, provider TEXT NOT NULL, external_id TEXT NOT NULL, local_path TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    song_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    artists_json TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    duration INTEGER NOT NULL DEFAULT 0,
                    cover_url TEXT NOT NULL DEFAULT '',
                    play_count INTEGER NOT NULL DEFAULT 0,
                    completed_count INTEGER NOT NULL DEFAULT 0,
                    total_listen_ms INTEGER NOT NULL DEFAULT 0,
                    last_played_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, track_key)
                );""")
            conn.execute("""INSERT INTO play_history_v2
                            SELECT 'netease:' || song_id, 'netease', CAST(song_id AS TEXT), '', h.*
                            FROM play_history h""")
            conn.execute("DROP TABLE play_history")
            conn.execute("ALTER TABLE play_history_v2 RENAME TO play_history")
            conn.execute("CREATE INDEX idx_play_history_account_last ON play_history(account_id,last_played_at DESC)")
            conn.execute("""CREATE TABLE play_events (
                event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, track_key TEXT NOT NULL,
                recorded_listen_ms INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE playback_sessions (
                account_id TEXT PRIMARY KEY, current_track_json TEXT NOT NULL DEFAULT '',
                position_sec REAL NOT NULL DEFAULT 0, duration_sec REAL NOT NULL DEFAULT 0,
                queue_json TEXT NOT NULL DEFAULT '[]', queue_index INTEGER NOT NULL DEFAULT -1,
                play_mode TEXT NOT NULL DEFAULT 'order', volume REAL NOT NULL DEFAULT 0.8,
                muted INTEGER NOT NULL DEFAULT 0, playback_rate REAL NOT NULL DEFAULT 1,
                active_tab TEXT NOT NULL DEFAULT 'discover', updated_at TEXT NOT NULL)""")
            conn.execute("ALTER TABLE local_files ADD COLUMN account_id TEXT NOT NULL DEFAULT 'local' CHECK(account_id='local')")
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version','2')")

    def _init_discovery_schema(self):
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if "artist_ids_json" not in {r["name"] for r in conn.execute("PRAGMA table_info(account_songs)")}:
                conn.execute("ALTER TABLE account_songs ADD COLUMN artist_ids_json TEXT NOT NULL DEFAULT '[]'")
            if "archived" not in {r["name"] for r in conn.execute("PRAGMA table_info(explicit_feedback)")}:
                conn.execute("ALTER TABLE explicit_feedback ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            if "track_key" not in {r["name"] for r in conn.execute("PRAGMA table_info(explicit_feedback)")}:
                conn.execute("ALTER TABLE explicit_feedback ADD COLUMN track_key TEXT NOT NULL DEFAULT ''")
            if "client_event_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(explicit_feedback)")}:
                conn.execute("ALTER TABLE explicit_feedback ADD COLUMN client_event_id TEXT NOT NULL DEFAULT ''")
            conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_explicit_feedback_account_client_event
                ON explicit_feedback(account_id, client_event_id) WHERE client_event_id != ''""")
            # Feedback rows expire. Keep receipts so replay cannot recreate expired activity.
            conn.execute("""CREATE TABLE IF NOT EXISTS lan_feedback_receipts (
                account_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(account_id,event_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS daily_recommend_snapshots (
                account_id TEXT NOT NULL, recommend_day TEXT NOT NULL, songs_json TEXT NOT NULL,
                raw_ids_json TEXT NOT NULL, checked_at REAL NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id,recommend_day))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS radio_sessions (
                account_id TEXT NOT NULL, radio_session_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(account_id, radio_session_id))""")
            conn.execute("""CREATE TABLE IF NOT EXISTS radio_session_tracks (
                account_id TEXT NOT NULL, radio_session_id TEXT NOT NULL, track_key TEXT NOT NULL,
                PRIMARY KEY(account_id, radio_session_id, track_key),
                FOREIGN KEY(account_id, radio_session_id) REFERENCES radio_sessions ON DELETE CASCADE)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS daily_history_dates (
                account_id TEXT PRIMARY KEY, dates_json TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS radio_superseded_requests (
                account_id TEXT NOT NULL, radio_session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(account_id,radio_session_id,request_id))""")
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='radio_served_history'").fetchone()
            migrate = schema and "'superseded'" not in schema[0]
            if migrate:
                conn.execute("ALTER TABLE radio_served_history RENAME TO radio_served_history_old")
            conn.execute("""CREATE TABLE IF NOT EXISTS radio_served_history (
                id INTEGER PRIMARY KEY, account_id TEXT NOT NULL, radio_session_id TEXT NOT NULL,
                request_id TEXT NOT NULL, track_key TEXT NOT NULL, song_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('served','played','completed','superseded')), served_at TEXT NOT NULL)""")
            if migrate:
                conn.execute("INSERT INTO radio_served_history SELECT * FROM radio_served_history_old")
                conn.execute("DROP TABLE radio_served_history_old")
            conn.execute("""CREATE INDEX IF NOT EXISTS radio_history_session
                            ON radio_served_history(account_id,radio_session_id,id)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS radio_batches (
                account_id TEXT NOT NULL, radio_session_id TEXT NOT NULL, request_id TEXT NOT NULL,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(account_id, radio_session_id, request_id))""")

    def _init_canonical_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            local_cols = {r["name"] for r in conn.execute("PRAGMA table_info(local_files)")}
            for col_name, col_def in (
                ("sample_rate", "INTEGER NOT NULL DEFAULT 0"),
                ("cover_id", "TEXT NOT NULL DEFAULT ''"),
                ("isrc", "TEXT NOT NULL DEFAULT ''"),
                ("mbid", "TEXT NOT NULL DEFAULT ''"),
                ("replaygain_track_gain_db", "REAL"),
                ("replaygain_track_peak", "REAL"),
                ("replaygain_album_gain_db", "REAL"),
                ("replaygain_album_peak", "REAL"),
                ("file_size", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col_name not in local_cols:
                    conn.execute(f"ALTER TABLE local_files ADD COLUMN {col_name} {col_def}")

            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sync_deletions (
                    account_id TEXT, kind TEXT, parent TEXT, item TEXT, deleted_at TEXT NOT NULL,
                    PRIMARY KEY(account_id,kind,parent,item)
                );
                CREATE TABLE IF NOT EXISTS sync_play_components (
                    account_id TEXT NOT NULL,
                    track_key TEXT NOT NULL,
                    components_json TEXT NOT NULL,
                    PRIMARY KEY(account_id, track_key)
                );
                CREATE TABLE IF NOT EXISTS canonical_tracks (
                    account_id TEXT NOT NULL,
                    track_key TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT 'netease',
                    external_id TEXT NOT NULL DEFAULT '',
                    song_id INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL DEFAULT '',
                    artists_json TEXT NOT NULL DEFAULT '[]',
                    album TEXT NOT NULL DEFAULT '',
                    duration INTEGER NOT NULL DEFAULT 0,
                    cover_url TEXT NOT NULL DEFAULT '',
                    isrc TEXT NOT NULL DEFAULT '',
                    mbid TEXT NOT NULL DEFAULT '',
                    local_path TEXT NOT NULL DEFAULT '',
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, track_key)
                );

                CREATE INDEX IF NOT EXISTS idx_canonical_tracks_account_isrc
                    ON canonical_tracks(account_id, isrc) WHERE isrc != '';

                CREATE TABLE IF NOT EXISTS canonical_favorites (
                    account_id TEXT NOT NULL,
                    track_key TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, track_key)
                );

                CREATE TABLE IF NOT EXISTS hybrid_playlists (
                    account_id TEXT NOT NULL,
                    playlist_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'hybrid',
                    cover_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, playlist_id)
                );

                CREATE TABLE IF NOT EXISTS hybrid_playlist_tracks (
                    account_id TEXT NOT NULL,
                    playlist_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    track_key TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, playlist_id, position),
                    FOREIGN KEY (account_id, playlist_id)
                        REFERENCES hybrid_playlists(account_id, playlist_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_hybrid_playlist_tracks_key
                    ON hybrid_playlist_tracks(account_id, playlist_id, track_key);

                CREATE TABLE IF NOT EXISTS track_source_routes (
                    account_id TEXT NOT NULL,
                    track_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    external_id TEXT NOT NULL DEFAULT '',
                    quality TEXT NOT NULL DEFAULT '',
                    format TEXT NOT NULL DEFAULT '',
                    bitrate INTEGER NOT NULL DEFAULT 0,
                    sample_rate INTEGER NOT NULL DEFAULT 0,
                    stream_url_hint TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, track_key)
                );

                CREATE TABLE IF NOT EXISTS track_candidate_blocklist (
                    account_id TEXT NOT NULL,
                    track_key TEXT NOT NULL,
                    candidate_key TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (account_id, track_key, candidate_key)
                );
                """
            )

    def _migrate_composite_keys(self):
        with self.write_batch() as conn:
            if conn.execute("SELECT 1 FROM meta WHERE key='composite_keys_v1'").fetchone():
                return
            tables = ("canonical_tracks", "canonical_favorites", "hybrid_playlist_tracks", "play_history",
                      "explicit_feedback", "play_events", "track_source_routes", "track_candidate_blocklist",
                      "radio_session_tracks", "radio_served_history")
            for table in tables:
                for col in (("track_key","candidate_key") if table == "track_candidate_blocklist" else ("track_key",)):
                    rows = conn.execute(f"SELECT rowid AS rid,* FROM {table} WHERE {col} GLOB 'gdstudio[.:]*' "
                                        f"OR {col} GLOB 'resolver[.:]*' OR {col} GLOB 'resolver_v2[.:]*'").fetchall()
                    for row in rows:
                        provider, ext = parse_track_key(row[col])
                        if not provider or not ext:
                            continue
                        key = f"{provider}:{ext}"
                        if table == "hybrid_playlist_tracks" and key != row[col] and conn.execute(
                            "SELECT 1 FROM hybrid_playlist_tracks WHERE account_id=? AND playlist_id=? AND track_key=?",
                            (row["account_id"],row["playlist_id"],key)).fetchone():
                            conn.execute("DELETE FROM hybrid_playlist_tracks WHERE rowid=?", (row["rid"],))
                            continue
                        if table == "play_history" and key != row[col]:
                            conn.execute("""UPDATE play_history SET play_count=play_count+?,completed_count=completed_count+?,
                                total_listen_ms=total_listen_ms+?,last_played_at=max(last_played_at,?)
                                WHERE account_id=? AND track_key=?""",
                                (row["play_count"],row["completed_count"],row["total_listen_ms"],row["last_played_at"],row["account_id"],key))
                        cur = conn.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE rowid=?", (key,row["rid"]))
                        if cur.rowcount == 0:
                            conn.execute(f"DELETE FROM {table} WHERE rowid=?", (row["rid"],))
                        elif table in ("canonical_tracks","play_history"):
                            conn.execute(f"UPDATE {table} SET provider=?,external_id=? WHERE rowid=?", (provider,ext,row["rid"]))
            conn.execute("INSERT INTO meta VALUES ('composite_keys_v1','1')")

    def list_canonical_tracks(self, account_id):
        with self._connect() as conn:
            return [self._canonical_row_to_dict(row) for row in conn.execute(
                "SELECT * FROM canonical_tracks WHERE account_id=?", (account_id,))]

    @staticmethod
    def _mark_deleted(conn, aid, kind, parent, item, timestamp):
        conn.execute("""INSERT INTO sync_deletions VALUES (?,?,?,?,?)
            ON CONFLICT(account_id,kind,parent,item) DO UPDATE SET deleted_at=max(deleted_at,excluded.deleted_at)""",
            (aid,kind,parent,item,timestamp))

    def list_sync_deletions(self, aid):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT kind,parent,item,deleted_at FROM sync_deletions WHERE account_id=?", (aid,))]

    def list_sync_play_history(self, account_id: str) -> list[dict[str, Any]]:
        with self.write_batch() as conn:
            rows = conn.execute(
                """SELECT h.*, c.components_json FROM play_history h LEFT JOIN sync_play_components c
                   USING (account_id, track_key) WHERE h.account_id=? ORDER BY h.last_played_at DESC, h.track_key""",
                (account_id,),
            ).fetchall()
            if not rows:
                return []
            device = conn.execute("SELECT value FROM meta WHERE key='sync_play_device'").fetchone()
            if device is None:
                origin = uuid.uuid4().hex
                conn.execute("INSERT INTO meta VALUES ('sync_play_device',?)", (origin,))
            else:
                origin = device[0]
            history = self._history_items(rows)
            for row, track in zip(rows, history):
                components = json.loads(row["components_json"] or "{}")
                previous = components.get(origin, {})
                # The visible history also includes listens recorded since the last pull.
                local = {
                    field: previous.get(field, 0) + max(
                        0, row[field] - sum(counters[field] for counters in components.values())
                    )
                    for field in PLAY_COUNTERS
                }
                if any(local.values()):
                    components[origin] = local
                track["play_components"] = components
            return history

    def apply_sync_state(self, aid, state, restore_session):
        with self.write_batch() as conn:
            for row in state["play_history"]:
                self.merge_play_history_aggregate(aid,row,conn)
            for row in state["feedback"]:
                if not conn.execute("""SELECT 1 FROM explicit_feedback WHERE account_id=? AND
                      (client_event_id=? OR (created_at=? AND song_id=? AND action=?))""",
                      (aid,row["client_event_id"],row["created_at"],row["song_id"],row["action"])).fetchone():
                    self.add_feedback(aid,row["song_id"],row["action"],context=row.get("context",""),
                                      track_key=row["track_key"],client_event_id=row["client_event_id"],connection=conn)
            for d in state["deletions"]:
                self._mark_deleted(conn,aid,d["kind"],d["parent"],d["item"],d["deleted_at"])
                if d["kind"] == "favorite":
                    conn.execute("DELETE FROM canonical_favorites WHERE account_id=? AND track_key=? AND added_at<=?",
                                 (aid,d["item"],d["deleted_at"]))
                elif d["kind"] == "playlist":
                    conn.execute("DELETE FROM hybrid_playlists WHERE account_id=? AND playlist_id=? AND created_at<=?",
                                 (aid,d["item"],d["deleted_at"]))
                elif d["kind"] == "member":
                    conn.execute("DELETE FROM hybrid_playlist_tracks WHERE account_id=? AND playlist_id=? AND track_key=? AND added_at<=?",
                                 (aid,d["parent"],d["item"],d["deleted_at"]))
            for fav in state["favorites"]:
                self.upsert_canonical_track(aid,fav,connection=conn)
                conn.execute("INSERT OR REPLACE INTO canonical_favorites VALUES (?,?,?)",(aid,fav["track_key"],fav["added_at"]))
            for pl in state["hybrid_playlists"]:
                pid = pl["id"]
                conn.execute("""INSERT INTO hybrid_playlists VALUES (?,?,?,?,'hybrid',?,?,?)
                    ON CONFLICT(account_id,playlist_id) DO UPDATE SET name=excluded.name,description=excluded.description,
                    cover_url=excluded.cover_url,created_at=excluded.created_at,updated_at=excluded.updated_at""",
                    (aid,pid,pl["name"],pl.get("description",""),pl.get("cover_url",""),pl["created_at"],pl["updated_at"]))
                conn.execute("DELETE FROM hybrid_playlist_tracks WHERE account_id=? AND playlist_id=?",(aid,pid))
                for position, song in enumerate(pl["songs"]):
                    self.upsert_canonical_track(aid,song,connection=conn)
                    conn.execute("INSERT INTO hybrid_playlist_tracks VALUES (?,?,?,?,?)",
                                 (aid,pid,position,song["track_key"],song["added_at"]))
            if restore_session:
                session = state["playback_session"]
                self.save_playback_session(aid,session,updated_at=session["updated_at"],connection=conn)

    def merge_play_history_aggregate(self, aid, row, conn):
        provider, ext = parse_track_key(row["track_key"])
        conn.execute("""INSERT INTO play_history(track_key,provider,external_id,local_path,account_id,song_id,
            title,artists_json,album,duration,cover_url,play_count,completed_count,total_listen_ms,last_played_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,track_key) DO UPDATE SET
            play_count=excluded.play_count,completed_count=excluded.completed_count,
            total_listen_ms=excluded.total_listen_ms,last_played_at=excluded.last_played_at""",
            (row["track_key"],provider,ext,row.get("local_path",""),aid,row["song_id"],row["title"],
             json.dumps(row.get("artists",[])),row.get("album",""),row.get("duration",0),row.get("cover_url",""),
             row["play_count"],row["completed_count"],row["total_listen_ms"],row["last_played_at"]))
        conn.execute(
            """INSERT INTO sync_play_components VALUES (?,?,?)
               ON CONFLICT(account_id,track_key) DO UPDATE SET components_json=excluded.components_json""",
            (aid, row["track_key"], json.dumps(row["play_components"], separators=(",", ":"))),
        )

    def get_daily_history_dates(self, account_id):
        with self._connect() as conn:
            row = conn.execute("SELECT dates_json FROM daily_history_dates WHERE account_id=?", (account_id,)).fetchone()
            return json.loads(row[0]) if row else []

    def save_daily_history_dates(self, account_id, dates):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT dates_json FROM daily_history_dates WHERE account_id=?", (account_id,)).fetchone()
            merged = sorted(set(dates).union(json.loads(row[0]) if row else []), reverse=True)
            conn.execute("INSERT OR REPLACE INTO daily_history_dates VALUES (?,?,?)",
                         (account_id, json.dumps(merged), _utc_now()))
            return merged

    def save_daily_snapshot(self, account, day, songs, raw_ids, checked_at):
        with self._connect() as conn:
            if songs:
                conn.execute("INSERT OR REPLACE INTO daily_recommend_snapshots VALUES (?,?,?,?,?,?)",
                             (account, day, json.dumps(songs), json.dumps(raw_ids), checked_at, _utc_now()))
            else:
                conn.execute("UPDATE daily_recommend_snapshots SET checked_at=? WHERE account_id=? AND recommend_day=?",
                             (checked_at, account, day))

    def get_daily_snapshot(self, account, day):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM daily_recommend_snapshots WHERE account_id=? AND recommend_day=?",
                               (account, day)).fetchone()
            if row:
                return {"songs": json.loads(row["songs_json"]), "raw_song_ids": json.loads(row["raw_ids_json"]),
                        "checked_at": row["checked_at"]}

    def list_daily_snapshot_dates(self, account):
        with self._connect() as conn:
            return [r[0] for r in conn.execute("SELECT recommend_day FROM daily_recommend_snapshots WHERE account_id=? ORDER BY recommend_day DESC", (account,))]

    def replay_tracks(self, account, session):
        tracks = {}
        with self._connect() as conn:
            rows = conn.execute("""SELECT payload_json FROM radio_batches WHERE account_id=?
                                   ORDER BY (radio_session_id = ?), rowid""", (account, session))
            for row in rows:
                for track in json.loads(row[0])["songs"]:
                    tracks[track["track_key"]] = track
        return list(tracks.values())

    def radio_batch(self, account, session, request):
        with self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM radio_batches WHERE account_id=? AND radio_session_id=? AND request_id=?",
                               (account, session, request)).fetchone()
            return json.loads(row[0]) if row else None

    def mark_radio_requests_superseded(self, account_id, radio_session_id, request_ids):
        scope = (account_id, radio_session_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for request_id in request_ids:
                key = (*scope, request_id)
                conn.execute("INSERT OR IGNORE INTO radio_superseded_requests VALUES (?,?,?,?)", (*key, _utc_now()))
                conn.execute("""UPDATE radio_served_history SET status='superseded' WHERE account_id=?
                                AND radio_session_id=? AND request_id=? AND status='served'""", key)
            conn.execute("""DELETE FROM radio_session_tracks WHERE account_id=? AND radio_session_id=?
                            AND track_key IN (SELECT track_key FROM radio_served_history WHERE account_id=?
                                AND radio_session_id=? AND status='superseded')
                            AND track_key NOT IN (SELECT track_key FROM radio_served_history WHERE account_id=?
                                AND radio_session_id=? AND status!='superseded')""", scope * 3)

    def radio_served_keys(self, account, session):
        with self._connect() as conn:
            rows = conn.execute("SELECT track_key FROM radio_session_tracks WHERE account_id=? AND radio_session_id=?",
                                (account, session))
            return {r[0] for r in rows}

    def blocked_song_ids(self, account):
        with self._connect() as conn:
            return {r[0] for r in conn.execute("SELECT DISTINCT song_id FROM explicit_feedback WHERE account_id=? AND song_id>0 AND action='not_interested'", (account,))}

    def blocked_track_keys(self, account):
        with self._connect() as conn:
            return {r[0] for r in conn.execute("""SELECT DISTINCT track_key FROM explicit_feedback
                       WHERE account_id=? AND track_key!='' AND action='not_interested'""", (account,))}

    def commit_radio_batch(self, account, payload):
        now = _utc_now()
        key = (account, payload["radio_session_id"], payload["request_id"])
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = conn.execute("INSERT OR IGNORE INTO radio_batches VALUES (?,?,?,?,?)",
                                    (*key, json.dumps(payload, ensure_ascii=False), now))
            if not inserted.rowcount:
                return json.loads(conn.execute("SELECT payload_json FROM radio_batches WHERE account_id=? AND radio_session_id=? AND request_id=?", key).fetchone()[0])
            superseded = conn.execute("""SELECT 1 FROM radio_superseded_requests WHERE account_id=?
                                         AND radio_session_id=? AND request_id=?""", key).fetchone()
            status = "superseded" if superseded else "served"
            conn.executemany("""INSERT INTO radio_served_history(account_id,radio_session_id,request_id,track_key,song_id,status,served_at)
                                VALUES (?,?,?,?,?,?,?)""", [(*key, t["track_key"], t["id"], status, now) for t in payload["songs"]])
            # Session membership outlives the detailed 600-row exposure ring.
            conn.execute("""INSERT INTO radio_sessions VALUES (?,?,?)
                            ON CONFLICT(account_id, radio_session_id) DO UPDATE SET updated_at=excluded.updated_at""",
                         (*key[:2], now))
            if not superseded:
                conn.executemany("INSERT OR IGNORE INTO radio_session_tracks VALUES (?,?,?)",
                             [(*key[:2], t["track_key"]) for t in payload["songs"]])
            conn.execute("""DELETE FROM radio_sessions WHERE rowid IN
                            (SELECT rowid FROM radio_sessions WHERE account_id=? AND radio_session_id != ? ORDER BY updated_at DESC,rowid DESC
                             LIMIT -1 OFFSET 119)""", key[:2])
            for table, cap in (("radio_served_history", 600), ("radio_batches", 120)):
                conn.execute(f"""DELETE FROM {table} WHERE rowid IN
                    (SELECT rowid FROM {table} WHERE account_id=? AND radio_session_id != ? ORDER BY rowid DESC LIMIT -1 OFFSET ?)""", (*key[:2], cap))
        return payload

    def journal_mode(self) -> str:
        with self._connect() as conn:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower() if row else ""

    def get_meta(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def upsert_account(
        self,
        account_id: str,
        display_name: str,
        avatar_url: str = "",
        vip_type: int = 0,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts(account_id, display_name, avatar_url, vip_type, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    avatar_url = excluded.avatar_url,
                    vip_type = excluded.vip_type,
                    updated_at = excluded.updated_at
                """,
                (str(account_id), str(display_name), str(avatar_url or ""), int(vip_type or 0), _utc_now()),
            )

    def list_cached_accounts(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    p.account_id AS account_id,
                    COALESCE(a.display_name, CASE WHEN p.account_id = 'local' THEN '本地导入' ELSE p.account_id END) AS display_name,
                    COALESCE(a.avatar_url, '') AS avatar_url,
                    COALESCE(a.vip_type, 0) AS vip_type,
                    COALESCE(a.updated_at, MAX(p.synced_at)) AS updated_at,
                    COUNT(DISTINCT p.playlist_id) AS playlist_count,
                    (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.account_id = p.account_id) AS track_count
                FROM playlists p
                LEFT JOIN accounts a ON a.account_id = p.account_id
                WHERE p.account_id != 'unclaimed'
                GROUP BY p.account_id
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [
                {
                    "account_id": str(row["account_id"]),
                    "user_id": int(row["account_id"]) if str(row["account_id"]).isdigit() else None,
                    "display_name": str(row["display_name"]),
                    "avatar_url": str(row["avatar_url"]),
                    "vip_type": int(row["vip_type"] or 0),
                    "playlist_count": int(row["playlist_count"] or 0),
                    "track_count": int(row["track_count"] or 0),
                    "updated_at": str(row["updated_at"] or ""),
                }
                for row in rows
            ]

    def has_any_playlists(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM playlists LIMIT 1").fetchone()
            return row is not None

    def replace_playlist_tracks(
        self,
        account_id: str,
        playlist: PlaylistSummary,
        songs: list[Song],
    ) -> None:
        """Staged replacement: write playlist summary, upsert songs, and replace membership in ONE transaction."""
        self.replace_playlists(account_id, [(playlist, songs)])

    def replace_playlists(self, account_id, playlists):
        with self._lock, self._connect() as conn:
            for playlist, songs in playlists:
                self._replace_playlist(conn, str(account_id), playlist, songs)

    @staticmethod
    def _replace_playlist(conn, aid, playlist, songs):
        pid = str(playlist.id)
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO playlists(account_id, playlist_id, name, track_count, kind, privacy, cover_url, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, playlist_id) DO UPDATE SET
                name = excluded.name,
                track_count = excluded.track_count,
                kind = excluded.kind,
                privacy = excluded.privacy,
                cover_url = excluded.cover_url,
                synced_at = excluded.synced_at
            """,
            (
                aid,
                pid,
                str(playlist.name),
                len(songs),
                str(playlist.kind),
                str(playlist.privacy),
                str(playlist.cover_url or ""),
                now,
            ),
        )
        CatalogDatabase._upsert_songs(conn, aid, songs)
        conn.execute(
            "DELETE FROM playlist_tracks WHERE account_id = ? AND playlist_id = ?",
            (aid, pid),
        )
        if songs:
            conn.executemany(
                """
                INSERT INTO playlist_tracks(account_id, playlist_id, position, song_id)
                VALUES (?, ?, ?, ?)
                """,
                [(aid, pid, idx, int(song.id)) for idx, song in enumerate(songs)],
            )

    def upsert_songs(self, account_id: str, songs: list[Song]) -> None:
        with self._lock, self._connect() as conn:
            self._upsert_songs(conn, str(account_id), songs)

    @staticmethod
    def _upsert_songs(conn, aid, songs):
        if songs:
            conn.executemany(
                """
                INSERT INTO account_songs(
                    account_id,
                    song_id, title, artists_json, album, duration, cover_url,
                    fee, privilege_st, max_br_level, is_cloud, chorus_start, chorus_end, availability, artist_ids_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, song_id) DO UPDATE SET
                    title = excluded.title,
                    artists_json = excluded.artists_json,
                    artist_ids_json = CASE WHEN excluded.artist_ids_json != '[]' THEN excluded.artist_ids_json ELSE account_songs.artist_ids_json END,
                    album = excluded.album,
                    duration = excluded.duration,
                    cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE account_songs.cover_url END,
                    fee = COALESCE(excluded.fee, account_songs.fee),
                    privilege_st = COALESCE(excluded.privilege_st, account_songs.privilege_st),
                    max_br_level = CASE WHEN excluded.max_br_level != '' THEN excluded.max_br_level ELSE account_songs.max_br_level END,
                    is_cloud = CASE WHEN excluded.is_cloud != 0 THEN excluded.is_cloud ELSE account_songs.is_cloud END,
                    chorus_start = COALESCE(excluded.chorus_start, account_songs.chorus_start),
                    chorus_end = COALESCE(excluded.chorus_end, account_songs.chorus_end),
                    availability = excluded.availability
                """,
                [
                    (
                        aid, int(song.id),
                        str(song.title),
                        json.dumps([str(a) for a in song.artists], ensure_ascii=False),
                        str(song.album or ""),
                        max(0, int(song.duration or 0)),
                        str(song.cover_url or ""),
                        song.fee if isinstance(song.fee, int) else None,
                        song.privilege_st if isinstance(song.privilege_st, int) else None,
                        str(song.max_br_level or ""),
                        1 if song.is_cloud else 0,
                        song.chorus_start if isinstance(song.chorus_start, int) else None,
                        song.chorus_end if isinstance(song.chorus_end, int) else None,
                        str(song.availability or "unknown"),
                        json.dumps(song.artist_ids),
                    )
                    for song in songs
                ],
            )

    def upsert_song_metadata(self, account_id: str, song: Song) -> None:
        """Persist hydrated Song metadata into both account_songs and global songs tables."""
        aid = str(account_id)
        with self._lock, self._connect() as conn:
            self._upsert_songs(conn, aid, [song])
            conn.execute(
                """
                INSERT INTO songs(
                    song_id, title, artists_json, album, duration, cover_url,
                    fee, privilege_st, max_br_level, is_cloud, chorus_start, chorus_end, availability
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(song_id) DO UPDATE SET
                    title = excluded.title,
                    artists_json = excluded.artists_json,
                    album = excluded.album,
                    duration = excluded.duration,
                    cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE songs.cover_url END,
                    fee = COALESCE(excluded.fee, songs.fee),
                    privilege_st = COALESCE(excluded.privilege_st, songs.privilege_st),
                    max_br_level = CASE WHEN excluded.max_br_level != '' THEN excluded.max_br_level ELSE songs.max_br_level END,
                    is_cloud = CASE WHEN excluded.is_cloud != 0 THEN excluded.is_cloud ELSE songs.is_cloud END,
                    chorus_start = COALESCE(excluded.chorus_start, songs.chorus_start),
                    chorus_end = COALESCE(excluded.chorus_end, songs.chorus_end),
                    availability = excluded.availability
                """,
                (
                    int(song.id),
                    str(song.title),
                    json.dumps([str(a) for a in song.artists], ensure_ascii=False),
                    str(song.album or ""),
                    max(0, int(song.duration or 0)),
                    str(song.cover_url or ""),
                    song.fee if isinstance(song.fee, int) else None,
                    song.privilege_st if isinstance(song.privilege_st, int) else None,
                    str(song.max_br_level or ""),
                    1 if song.is_cloud else 0,
                    song.chorus_start if isinstance(song.chorus_start, int) else None,
                    song.chorus_end if isinstance(song.chorus_end, int) else None,
                    str(song.availability or "unknown"),
                ),
            )

    def append_liked_members(
        self,
        account_id: str,
        playlist: PlaylistSummary,
        ids: list[int],
        *,
        songs_by_id: dict[int, Song] | None = None,
    ) -> None:
        target = (account_id, str(playlist.id))
        hydrated_map = songs_by_id or {}
        with self._lock, self._connect() as conn:
            conn.execute("""INSERT OR IGNORE INTO playlists
                (account_id, playlist_id, name, track_count, kind, privacy, cover_url, synced_at)
                VALUES (?, ?, ?, 0, 'liked', ?, ?, ?)""",
                         (*target, playlist.name, playlist.privacy, playlist.cover_url, _utc_now()))
            hydrated_songs = [hydrated_map[sid] for sid in ids if sid in hydrated_map and not is_stub_song(hydrated_map[sid])]
            if hydrated_songs:
                self._upsert_songs(conn, str(account_id), hydrated_songs)
            for sid in ids:
                conn.execute("INSERT OR IGNORE INTO account_songs(account_id,song_id, title, artists_json) VALUES (?, ?, ?, '[]')",
                             (account_id, sid, f"歌曲 #{sid}"))
                conn.execute("""INSERT INTO playlist_tracks(account_id, playlist_id, position, song_id)
                    SELECT ?, ?, (SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_tracks
                                  WHERE account_id=? AND playlist_id=?), ?
                    WHERE NOT EXISTS (SELECT 1 FROM playlist_tracks WHERE account_id=? AND playlist_id=? AND song_id=?)""",
                             (*target, *target, sid, *target, sid))
            conn.execute("""UPDATE playlists SET track_count=(SELECT COUNT(*) FROM playlist_tracks
                            WHERE account_id=? AND playlist_id=?) WHERE account_id=? AND playlist_id=?""",
                         (*target, *target))

    def claim_unclaimed_playlist(self, account_id: str, playlist_id: str) -> bool:
        """Move a specific verified-owned legacy 'unclaimed' playlist into account_id."""
        aid = str(account_id)
        pid = str(playlist_id)
        if aid == "unclaimed":
            return False
        with self._lock, self._connect() as conn:
            exists_target = conn.execute(
                "SELECT 1 FROM playlists WHERE account_id = ? AND playlist_id = ?",
                (aid, pid),
            ).fetchone()
            if exists_target:
                return False
            row = conn.execute(
                "SELECT * FROM playlists WHERE account_id = 'unclaimed' AND playlist_id = ?",
                (pid,),
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """
                INSERT INTO playlists(account_id, playlist_id, name, track_count, kind, privacy, cover_url, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    pid,
                    row["name"],
                    row["track_count"],
                    row["kind"],
                    row["privacy"],
                    row["cover_url"],
                    row["synced_at"],
                ),
            )
            conn.execute("""INSERT OR IGNORE INTO account_songs SELECT ?, song_id,title,artists_json,album,duration,cover_url,fee,privilege_st,max_br_level,is_cloud,chorus_start,chorus_end,availability,artist_ids_json
                FROM account_songs WHERE account_id='unclaimed' AND song_id IN
                (SELECT song_id FROM playlist_tracks WHERE account_id='unclaimed' AND playlist_id=?)""", (aid, pid))
            conn.execute(
                """
                INSERT INTO playlist_tracks(account_id, playlist_id, position, song_id)
                SELECT ?, playlist_id, position, song_id
                FROM playlist_tracks
                WHERE account_id = 'unclaimed' AND playlist_id = ?
                """,
                (aid, pid),
            )
            conn.execute(
                "DELETE FROM playlist_tracks WHERE account_id = 'unclaimed' AND playlist_id = ?",
                (pid,),
            )
            conn.execute(
                "DELETE FROM playlists WHERE account_id = 'unclaimed' AND playlist_id = ?",
                (pid,),
            )
            return True

    def remove_tracks_from_playlist(self, account_id: str, playlist_id: str, song_ids: list[int]) -> int:
        unique_ids = list(dict.fromkeys(int(s) for s in song_ids))
        if not unique_ids:
            return 0
        aid = str(account_id)
        pid = str(playlist_id)
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock, self._connect() as conn:
            playlist_row = conn.execute(
                "SELECT 1 FROM playlists WHERE account_id = ? AND playlist_id = ?",
                (aid, pid),
            ).fetchone()
            if playlist_row is None:
                return 0
            cursor = conn.execute(
                f"DELETE FROM playlist_tracks WHERE account_id = ? AND playlist_id = ? AND song_id IN ({placeholders})",
                [aid, pid, *unique_ids],
            )
            removed = cursor.rowcount
            if removed > 0:
                count_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM playlist_tracks WHERE account_id = ? AND playlist_id = ?",
                    (aid, pid),
                ).fetchone()
                remaining = int(count_row["cnt"]) if count_row else 0
                conn.execute(
                    "UPDATE playlists SET track_count = ? WHERE account_id = ? AND playlist_id = ?",
                    (remaining, aid, pid),
                )
            return removed

    def remove_tracks_from_liked(self, account_id: str, song_ids: list[int], playlist_id: str | None = None) -> int:
        unique_ids = list(dict.fromkeys(int(s) for s in song_ids))
        if not unique_ids:
            return 0
        aid = str(account_id)
        placeholders = ",".join("?" for _ in unique_ids)
        total_removed = 0
        with self._lock, self._connect() as conn:
            if playlist_id is not None:
                rows = conn.execute(
                    "SELECT playlist_id FROM playlists WHERE account_id = ? AND kind = 'liked' AND playlist_id = ?",
                    (aid, str(playlist_id)),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT playlist_id FROM playlists WHERE account_id = ? AND kind = 'liked'",
                    (aid,),
                ).fetchall()
            for row in rows:
                pid = str(row["playlist_id"])
                cursor = conn.execute(
                    f"DELETE FROM playlist_tracks WHERE account_id = ? AND playlist_id = ? AND song_id IN ({placeholders})",
                    [aid, pid, *unique_ids],
                )
                if cursor.rowcount > 0:
                    total_removed += cursor.rowcount
                    count_row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM playlist_tracks WHERE account_id = ? AND playlist_id = ?",
                        (aid, pid),
                    ).fetchone()
                    remaining = int(count_row["cnt"]) if count_row else 0
                    conn.execute(
                        "UPDATE playlists SET track_count = ? WHERE account_id = ? AND playlist_id = ?",
                        (remaining, aid, pid),
                    )
        return total_removed

    def get_catalog_dict(self, account_id: str) -> dict[str, Any]:
        """Legacy-shaped {version: 1, playlists: {pid: {summary, songs}}} strictly for account_id."""
        aid = str(account_id)
        with self._connect() as conn:
            conn.execute("BEGIN")
            playlist_rows = conn.execute(
                """
                SELECT playlist_id, name, track_count, kind, privacy, cover_url, synced_at
                FROM playlists
                WHERE account_id = ?
                ORDER BY CASE kind
                    WHEN 'liked' THEN 0
                    WHEN 'self_created' THEN 1
                    WHEN 'subscribed' THEN 2
                    WHEN 'cloud' THEN 3
                    WHEN 'imported' THEN 4
                    ELSE 5
                END, playlist_id
                """,
                (aid,),
            ).fetchall()
            playlists_map: dict[str, Any] = {}
            for prow in playlist_rows:
                pid = str(prow["playlist_id"])
                parsed_pid = _parse_playlist_id(pid)
                summary = {
                    "id": parsed_pid,
                    "name": str(prow["name"]),
                    "track_count": int(prow["track_count"]),
                    "kind": str(prow["kind"]),
                    "privacy": str(prow["privacy"]),
                    "cover_url": str(prow["cover_url"] or ""),
                }
                track_rows = conn.execute(
                    """
                    SELECT s.*
                    FROM playlist_tracks pt
                    JOIN account_songs s ON s.account_id = pt.account_id AND s.song_id = pt.song_id
                    WHERE pt.account_id = ? AND pt.playlist_id = ?
                    ORDER BY pt.position ASC
                    """,
                    (aid, pid),
                ).fetchall()
                songs_list = [
                    self._row_to_song_dict(trow, parsed_pid, str(prow["name"]))
                    for trow in track_rows
                ]
                playlists_map[pid] = {"summary": summary, "songs": songs_list}
            hybrid_rows = conn.execute(
                """
                SELECT hp.*,
                       (SELECT COUNT(*) FROM hybrid_playlist_tracks hpt
                        WHERE hpt.account_id = hp.account_id AND hpt.playlist_id = hp.playlist_id) AS track_count
                FROM hybrid_playlists hp
                WHERE hp.account_id = ?
                ORDER BY hp.updated_at DESC, hp.playlist_id ASC
                """,
                (aid,),
            ).fetchall()
            for hrow in hybrid_rows:
                hpid = str(hrow["playlist_id"])
                hsongs = self._list_hybrid_playlist_tracks_conn(conn, aid, hpid, str(hrow["name"]))
                hcover = str(hrow["cover_url"] or "") or (hsongs[0].get("cover_url", "") if hsongs else "")
                playlists_map[hpid] = {
                    "summary": {
                        "id": hpid,
                        "name": str(hrow["name"]),
                        "track_count": len(hsongs),
                        "kind": "hybrid",
                        "privacy": "local",
                        "cover_url": hcover,
                    },
                    "songs": hsongs,
                }
            return {"version": 1, "playlists": playlists_map}

    def query_view(
        self,
        account_id: str,
        *,
        playlist_id: str | None = None,
        q: str = "",
        filter_name: str = "",
        sort: str = "",
        direction: str = "asc",
        page: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        aid = str(account_id)
        with self._connect() as conn:
            playlist_rows = conn.execute(
                """
                SELECT playlist_id, name, track_count, kind, privacy, cover_url, synced_at
                FROM playlists
                WHERE account_id = ?
                ORDER BY CASE kind
                    WHEN 'liked' THEN 0
                    WHEN 'self_created' THEN 1
                    WHEN 'subscribed' THEN 2
                    WHEN 'cloud' THEN 3
                    WHEN 'imported' THEN 4
                    ELSE 5
                END, playlist_id
                """,
                (aid,),
            ).fetchall()
            playlists = [
                {
                    "id": _parse_playlist_id(str(row["playlist_id"])),
                    "name": str(row["name"]),
                    "track_count": int(row["track_count"]),
                    "kind": str(row["kind"]),
                    "privacy": str(row["privacy"]),
                    "cover_url": str(row["cover_url"] or ""),
                    "synced_at": str(row["synced_at"] or ""),
                }
                for row in playlist_rows
            ]

            hybrid_rows = conn.execute(
                """
                SELECT hp.*,
                       (SELECT COUNT(*) FROM hybrid_playlist_tracks hpt
                        WHERE hpt.account_id = hp.account_id AND hpt.playlist_id = hp.playlist_id) AS track_count
                FROM hybrid_playlists hp
                WHERE hp.account_id = ?
                ORDER BY hp.updated_at DESC, hp.playlist_id ASC
                """,
                (aid,),
            ).fetchall()
            hybrid_songs_by_pid: dict[str, list[dict[str, Any]]] = {}
            for hrow in hybrid_rows:
                hpid = str(hrow["playlist_id"])
                hsongs_all = self._list_hybrid_playlist_tracks_conn(conn, aid, hpid, str(hrow["name"]))
                hybrid_songs_by_pid[hpid] = hsongs_all
                hcover = str(hrow["cover_url"] or "") or (hsongs_all[0].get("cover_url", "") if hsongs_all else "")
                playlists.append(
                    {
                        "id": hpid,
                        "name": str(hrow["name"]),
                        "track_count": int(hrow["track_count"] or 0),
                        "kind": "hybrid",
                        "privacy": "local",
                        "cover_url": hcover,
                        "synced_at": str(hrow["updated_at"] or ""),
                    }
                )

            def _filter_and_sort_tracks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
                out = list(items)
                cq = str(q or "").strip().lower()
                if cq:
                    out = [
                        s
                        for s in out
                        if cq in str(s.get("title") or "").lower()
                        or cq in str(s.get("artist") or "").lower()
                        or cq in " ".join(str(a) for a in (s.get("artists") or [])).lower()
                        or cq in str(s.get("album") or "").lower()
                    ]
                nf = str(filter_name or "").strip().lower()
                if nf == "available":
                    out = [s for s in out if (s.get("availability") or "available") == "available"]
                elif nf == "unavailable":
                    out = [s for s in out if s.get("availability") == "unavailable"]
                elif nf == "vip":
                    out = [s for s in out if s.get("availability") == "vip" or s.get("fee") == 1]
                elif nf == "hybrid":
                    out = [s for s in out if str(s.get("source_playlist_id") or "").startswith("hybrid:")]
                elif nf in {"liked", "self_created", "subscribed", "imported", "cloud"}:
                    out = [s for s in out if not str(s.get("source_playlist_id") or "").startswith("hybrid:")]
                sk = str(sort or "").strip().lower()
                rev = str(direction or "asc").strip().lower() == "desc"
                if sk == "title":
                    out.sort(key=lambda s: str(s.get("title") or "").lower(), reverse=rev)
                elif sk in {"artist", "artists"}:
                    out.sort(key=lambda s: str(s.get("artist") or " ".join(s.get("artists") or [])).lower(), reverse=rev)
                elif sk == "album":
                    out.sort(key=lambda s: str(s.get("album") or "").lower(), reverse=rev)
                elif sk == "duration":
                    out.sort(key=lambda s: int(s.get("duration") or s.get("duration_ms") or 0), reverse=rev)
                elif sk == "id":
                    out.sort(key=lambda s: (int(s.get("id") or 0), str(s.get("track_key") or "")), reverse=rev)
                elif sk == "availability":
                    out.sort(key=lambda s: str(s.get("availability") or ""), reverse=rev)
                return out

            if playlist_id is not None and str(playlist_id).strip().startswith("hybrid:"):
                hpid = str(playlist_id).strip()
                hsongs = _filter_and_sort_tracks(hybrid_songs_by_pid.get(hpid, []))
                htotal = len(hsongs)
                ep = max(1, int(page or 1)) if (page is not None or page_size is not None) else 1
                eps = max(1, min(2000, int(page_size or 100))) if (page is not None or page_size is not None) else htotal
                if page is not None or page_size is not None:
                    start = (ep - 1) * eps
                    hsongs = hsongs[start : start + eps]
                return {
                    "version": 2,
                    "account_id": aid,
                    "playlists": playlists,
                    "tracks": hsongs,
                    "total": htotal,
                    "page": ep,
                    "page_size": eps,
                }

            where_clauses = ["pt.account_id = ?"]
            params: list[Any] = [aid]

            if playlist_id is not None and str(playlist_id).strip() != "":
                where_clauses.append("pt.playlist_id = ?")
                params.append(str(playlist_id).strip())

            cleaned_q = str(q or "").strip()
            if cleaned_q:
                like_q = f"%{cleaned_q.lower()}%"
                where_clauses.append(
                    "(LOWER(s.title) LIKE ? OR LOWER(s.artists_json) LIKE ? OR LOWER(s.album) LIKE ?)"
                )
                params.extend([like_q, like_q, like_q])

            normalized_filter = str(filter_name or "").strip().lower()
            if normalized_filter == "available":
                where_clauses.append("s.availability = 'available'")
            elif normalized_filter == "vip":
                where_clauses.append("(s.availability = 'vip' OR s.fee = 1)")
            elif normalized_filter == "unavailable":
                where_clauses.append("s.availability = 'unavailable'")
            elif normalized_filter == "cloud":
                where_clauses.append("(s.is_cloud = 1 OR p.kind = 'cloud')")
            elif normalized_filter in {"liked", "self_created", "subscribed", "imported"}:
                where_clauses.append("p.kind = ?")
                params.append(normalized_filter)
            elif normalized_filter == "relisten_later":
                where_clauses.append(
                    "EXISTS (SELECT 1 FROM explicit_feedback ef WHERE ef.account_id = pt.account_id AND ef.song_id = pt.song_id AND ef.action = 'relisten_later')"
                )

            where_sql = " AND ".join(where_clauses)

            count_row = conn.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM playlist_tracks pt
                JOIN playlists p ON p.account_id = pt.account_id AND p.playlist_id = pt.playlist_id
                JOIN account_songs s ON s.account_id = pt.account_id AND s.song_id = pt.song_id
                WHERE {where_sql}
                """,
                params,
            ).fetchone()
            total = int(count_row["cnt"]) if count_row else 0

            dir_sql = "DESC" if str(direction or "asc").strip().lower() == "desc" else "ASC"
            sort_key = str(sort or "").strip().lower()
            if sort_key == "title":
                order_sql = f"LOWER(s.title) {dir_sql}, pt.playlist_id ASC, pt.position ASC"
            elif sort_key in {"artist", "artists"}:
                order_sql = f"LOWER(s.artists_json) {dir_sql}, pt.playlist_id ASC, pt.position ASC"
            elif sort_key == "album":
                order_sql = f"LOWER(s.album) {dir_sql}, pt.playlist_id ASC, pt.position ASC"
            elif sort_key == "duration":
                order_sql = f"s.duration {dir_sql}, pt.playlist_id ASC, pt.position ASC"
            elif sort_key == "id":
                order_sql = f"s.song_id {dir_sql}, pt.playlist_id ASC, pt.position ASC"
            elif sort_key == "availability":
                order_sql = f"s.availability {dir_sql}, pt.playlist_id ASC, pt.position ASC"
            else:
                order_sql = f"CASE p.kind WHEN 'liked' THEN 0 WHEN 'self_created' THEN 1 WHEN 'subscribed' THEN 2 WHEN 'cloud' THEN 3 WHEN 'imported' THEN 4 ELSE 5 END ASC, pt.playlist_id ASC, pt.position {dir_sql}"

            limit_sql = ""
            effective_page = 1
            effective_page_size = total
            query_params = list(params)
            if page is not None or page_size is not None:
                effective_page = max(1, int(page or 1))
                effective_page_size = max(1, min(2000, int(page_size or 100)))
                offset = (effective_page - 1) * effective_page_size
                limit_sql = " LIMIT ? OFFSET ?"
                query_params.extend([effective_page_size, offset])

            rows = conn.execute(
                f"""
                SELECT s.*, pt.playlist_id AS source_pid, p.name AS source_pname, p.kind AS source_pkind
                FROM playlist_tracks pt
                JOIN playlists p ON p.account_id = pt.account_id AND p.playlist_id = pt.playlist_id
                JOIN account_songs s ON s.account_id = pt.account_id AND s.song_id = pt.song_id
                WHERE {where_sql}
                ORDER BY {order_sql}{limit_sql}
                """,
                query_params,
            ).fetchall()

            tracks = []
            for row in rows:
                raw_pid = str(row["source_pid"])
                parsed_pid: int | str = "imported" if row["source_pkind"] == "imported" else _parse_playlist_id(raw_pid)
                tracks.append(self._row_to_song_dict(row, parsed_pid, str(row["source_pname"] or "")))

            # Include hybrid playlist tracks in unified All-Catalog view when no specific playlist is selected.
            if (
                (playlist_id is None or str(playlist_id).strip() == "")
                and hybrid_songs_by_pid
                and normalized_filter not in {"liked", "self_created", "subscribed", "imported", "cloud", "relisten_later"}
            ):
                all_hybrid = [song for group in hybrid_songs_by_pid.values() for song in group]
                filtered_hybrid = _filter_and_sort_tracks(all_hybrid)
                if filtered_hybrid:
                    if page is None and page_size is None:
                        tracks.extend(filtered_hybrid)
                        total = len(tracks)
                        effective_page_size = total
                    else:
                        combined_total = total + len(filtered_hybrid)
                        offset = (effective_page - 1) * effective_page_size
                        if len(tracks) < effective_page_size:
                            hy_offset = max(0, offset - total)
                            take_n = effective_page_size - len(tracks)
                            tracks.extend(filtered_hybrid[hy_offset : hy_offset + take_n])
                        total = combined_total

            return {
                "version": 2,
                "account_id": aid,
                "playlists": playlists,
                "tracks": tracks,
                "total": total,
                "page": effective_page,
                "page_size": effective_page_size,
            }

    def get_song(self, song_id: int, account_id: str | None = None) -> Song | None:
        sid = int(song_id)
        with self._connect() as conn:
            if account_id is not None:
                row = conn.execute(
                    """
                    SELECT s.*, pt.playlist_id AS source_pid, p.name AS source_pname, p.kind AS source_pkind
                    FROM playlist_tracks pt
                    JOIN playlists p ON p.account_id = pt.account_id AND p.playlist_id = pt.playlist_id
                    JOIN account_songs s ON s.account_id = pt.account_id AND s.song_id = pt.song_id
                    WHERE pt.account_id = ? AND pt.song_id = ?
                    LIMIT 1
                    """,
                    (str(account_id), sid),
                ).fetchone()
                if row is not None:
                    pid = "imported" if row["source_pkind"] == "imported" else _parse_playlist_id(str(row["source_pid"]))
                    return self._row_to_song(row, pid, str(row["source_pname"] or ""))
                row = conn.execute("SELECT * FROM account_songs WHERE account_id=? AND song_id=?",
                                   (str(account_id), sid)).fetchone()
            else:
                row = conn.execute("SELECT * FROM songs WHERE song_id = ?", (sid,)).fetchone()

            if row is None:
                return None
            return self._row_to_song(row, None, "")

    def update_song_chorus(self, song_id: int, chorus_start: int | None, chorus_end: int | None, account_id: str | None = None) -> None:
        if chorus_start is None and chorus_end is None:
            return
        with self._lock, self._connect() as conn:
            if account_id is not None:
                conn.execute("UPDATE account_songs SET chorus_start=?, chorus_end=? WHERE account_id=? AND song_id=?",
                             (chorus_start, chorus_end, account_id, song_id))
                return
            conn.execute(
                "UPDATE songs SET chorus_start = ?, chorus_end = ? WHERE song_id = ?",
                (chorus_start, chorus_end, int(song_id)),
            )

    # --- Explicit Feedback (bounded to 2000 rows / 30 days per account) ---

    def add_feedback(self, account_id: str, song_id: int, action: str, context: str = "", *,
                     track_key: str = "", client_event_id: str = "", connection=None) -> dict[str, Any]:
        aid = str(account_id)
        sid = int(song_id)
        act = str(action).strip().lower()
        ctx = str(context or "")[:200]
        now = _utc_now()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=FEEDBACK_RETENTION_DAYS)).isoformat()
        with self._lock, (self._connect() if connection is None else nullcontext(connection)) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO explicit_feedback(account_id, song_id, action, context, created_at, track_key, client_event_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (aid, sid, act, ctx, now, track_key, client_event_id),
            )
            row_id = int(cursor.lastrowid or 0) if cursor.rowcount else 0
            # Prune expired rows (> 30 days) for this account
            conn.execute(
                "DELETE FROM explicit_feedback WHERE account_id = ? AND action != 'not_interested' AND created_at < ?",
                (aid, cutoff),
            )
            # Keep the activity feed bounded. Archived blocks remain in the durable blocklist.
            overflow = """SELECT id FROM explicit_feedback WHERE account_id=? AND archived=0
                          ORDER BY created_at DESC,id DESC LIMIT -1 OFFSET ?"""
            conn.execute(f"UPDATE explicit_feedback SET archived=1 WHERE action='not_interested' AND id IN ({overflow})",
                         (aid, FEEDBACK_MAX_ROWS_PER_ACCOUNT))
            conn.execute(f"DELETE FROM explicit_feedback WHERE action!='not_interested' AND id IN ({overflow})",
                         (aid, FEEDBACK_MAX_ROWS_PER_ACCOUNT))
        return {
            "id": row_id,
            "account_id": aid,
            "song_id": sid,
            "track_key": track_key or f"netease:{sid}",
            "action": act,
            "context": ctx,
            "created_at": now,
        }

    def list_feedback(
        self,
        account_id: str,
        *,
        action: str | None = None,
        limit: int = FEEDBACK_MAX_ROWS_PER_ACCOUNT,
    ) -> list[dict[str, Any]]:
        aid = str(account_id)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=FEEDBACK_RETENTION_DAYS)).isoformat()
        with self._connect() as conn:
            if action:
                rows = conn.execute(
                    """
                    SELECT id, account_id, song_id, action, context, created_at,
                           COALESCE(NULLIF(track_key,''), 'netease:' || song_id) AS track_key
                    FROM explicit_feedback
                    WHERE account_id = ? AND action = ? AND (created_at >= ? OR action='not_interested')
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (aid, str(action).strip().lower(), cutoff, max(1, min(FEEDBACK_MAX_ROWS_PER_ACCOUNT, int(limit)))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, account_id, song_id, action, context, created_at,
                           COALESCE(NULLIF(track_key,''), 'netease:' || song_id) AS track_key
                    FROM explicit_feedback
                    WHERE account_id = ? AND archived=0 AND (created_at >= ? OR action='not_interested')
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (aid, cutoff, max(1, min(FEEDBACK_MAX_ROWS_PER_ACCOUNT, int(limit)))),
                ).fetchall()
            return [dict(row) for row in rows]

    def delete_feedback(
        self,
        account_id: str,
        *,
        song_id: int | None = None,
        action: str | None = None,
        track_key: str = "",
    ) -> int:
        aid = str(account_id)
        clauses = ["account_id = ?"]
        params: list[Any] = [aid]
        if track_key:
            clauses.append("track_key = ?")
            params.append(track_key)
        if song_id is not None:
            clauses.append("song_id = ?")
            params.append(int(song_id))
        if action:
            clauses.append("action = ?")
            params.append(str(action).strip().lower())
        with self._lock, self._connect() as conn:
            cursor = conn.execute(f"DELETE FROM explicit_feedback WHERE {' AND '.join(clauses)}", params)
            return cursor.rowcount

    # --- Local Music Library Index ---

    def replace_local_files(self, files: list[dict[str, Any]]) -> int:
        return self.update_local_files_for_dirs(None, files)

    def update_local_files_for_dirs(self, dirs, files, protected=()):
        roots = [Path(p) for p in dirs or ()]
        excluded = [Path(p) for p in protected]
        with self._lock, self._connect() as conn:
            if dirs is None:
                conn.execute("DELETE FROM local_files")
            elif roots:
                stale = [(path,) for path, in conn.execute("SELECT path FROM local_files")
                         if any(Path(path).is_relative_to(root) for root in roots)
                         and not any(Path(path).is_relative_to(root) for root in excluded)]
                conn.executemany("DELETE FROM local_files WHERE path=?", stale)
            if files:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO local_files(
                        path, title, artist, album, duration_ms, format, bitrate, mtime,
                        sample_rate, cover_id, isrc, mbid,
                        replaygain_track_gain_db, replaygain_track_peak,
                        replaygain_album_gain_db, replaygain_album_peak, file_size
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(item["path"]),
                            str(item.get("title") or ""),
                            str(item.get("artist") or ""),
                            str(item.get("album") or ""),
                            max(0, int(item.get("duration_ms") or 0)),
                            str(item.get("format") or ""),
                            max(0, int(item.get("bitrate") or 0)),
                            float(item.get("mtime") or 0.0),
                            max(0, int(item.get("sample_rate") or 0)),
                            str(item.get("cover_id") or ""),
                            str(item.get("isrc") or ""),
                            str(item.get("mbid") or ""),
                            item.get("replaygain_track_gain_db"),
                            item.get("replaygain_track_peak"),
                            item.get("replaygain_album_gain_db"),
                            item.get("replaygain_album_peak"),
                            max(0, int(item.get("file_size") or 0)),
                        )
                        for item in files
                    ],
                )
        return len(files)

    _LOCAL_FILE_COLS = (
        "path, title, artist, album, duration_ms, format, bitrate, mtime, "
        "sample_rate, cover_id, isrc, mbid, "
        "replaygain_track_gain_db, replaygain_track_peak, "
        "replaygain_album_gain_db, replaygain_album_peak, file_size"
    )

    def get_local_file(self, path: str) -> dict[str, Any] | None:
        cleaned = str(path or "").strip()
        if not cleaned:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {self._LOCAL_FILE_COLS} FROM local_files WHERE path = ? LIMIT 1",
                (cleaned,),
            ).fetchone()
            if row is not None:
                return dict(row)
            # Normalized Windows case/separator fallback strictly against indexed rows
            try:
                target_norm = str(Path(cleaned).resolve()).casefold()
            except OSError:
                return None
            rows = conn.execute(
                f"SELECT {self._LOCAL_FILE_COLS} FROM local_files LIMIT 5000"
            ).fetchall()
            for candidate in rows:
                try:
                    if str(Path(str(candidate["path"])).resolve()).casefold() == target_norm:
                        return dict(candidate)
                except OSError:
                    continue
            return None

    @staticmethod
    def _local_filter(q):
        pattern = "%" + q.strip().lower() + "%"
        return (" WHERE LOWER(title) LIKE ? OR LOWER(artist) LIKE ? OR LOWER(album) LIKE ?", [pattern]*3) if q.strip() else ("", [])

    def count_local_files(self, q: str = "") -> int:
        where, args = self._local_filter(q)
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM local_files" + where, args).fetchone()[0]

    def list_local_files(self, q: str = "", limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        where, args = self._local_filter(q)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {self._LOCAL_FILE_COLS} FROM local_files"
                + where
                + " ORDER BY LOWER(title), path LIMIT ? OFFSET ?",
                [*args, max(1, min(5000, limit)), max(0, offset)],
            )
            result = []
            for row in rows:
                d = dict(row)
                d["track_key"] = f"local:{d['path']}"
                d["provider"] = "local"
                if d.get("cover_id"):
                    d["cover_url"] = f"/api/local-cover/{d['cover_id']}"
                result.append(d)
            return result

    # --- Play History & Listening Insights (Account-Partitioned) ---

    def record_play(
        self,
        account_id: str,
        song_dict: dict[str, Any],
        *,
        listened_ms: int = 0,
        listen_ms: int | None = None,
        event_id: str | None = None,
        completed: bool = False,
        ignore_unchanged_event: bool = False,
        connection=None,
    ) -> dict[str, Any]:
        aid = str(account_id or "local")
        track_key = song_dict.get("track_key") or ""
        key_provider, key_identity = parse_track_key(track_key)
        raw_provider = song_dict.get("provider")
        provider = normalize_provider(raw_provider, default="") if raw_provider else key_provider or "netease"
        if not provider or (track_key and provider != key_provider):
            raise ValueError("Invalid track identity")
        local_path = song_dict.get("local_path") or (key_identity if provider == "local" else "")
        external_id = str(song_dict.get("external_id") or key_identity or song_dict.get("song_id") or song_dict.get("id") or "")
        identity = local_path if provider == "local" else external_id
        sid = int(identity) if provider == "netease" else 0
        if not identity or (provider == "netease" and sid <= 0) or (track_key and key_identity != identity):
            raise ValueError("Invalid track identity")
        track_key = f"{provider}:{identity}"
        listened = max(0, int(listened_ms if listen_ms is None else listen_ms))
        if listened > 2**53 or sid > 2**63-1:
            raise ValueError("Invalid playback value")
        is_completed = bool(completed)
        # Ignore < 25s skips unless playback genuinely completed (e.g. short interlude track)
        if not event_id and listened < 25_000 and not is_completed:
            return {
                "recorded": False,
                "reason": "skip_ignored",
                "account_id": aid,
                "song_id": sid,
            }

        title = str(song_dict.get("title") or song_dict.get("name") or "")[:300]
        raw_artists = song_dict.get("artists")
        if isinstance(raw_artists, list):
            artists = [str(a).strip() for a in raw_artists if str(a).strip()][:20]
        elif isinstance(song_dict.get("artist"), str) and song_dict["artist"].strip():
            artists = [song_dict["artist"].strip()]
        else:
            artists = []
        album = str(song_dict.get("album") or "")[:300]
        try:
            duration = max(0, int(song_dict.get("duration_ms") or song_dict.get("duration") or 0))
        except (TypeError, ValueError):
            duration = 0
        cover_url = str(song_dict.get("cover_url") or "")[:512]
        now = _utc_now()
        artists_json = json.dumps(artists, ensure_ascii=False)

        with (self.write_batch() if connection is None else nullcontext(connection)) as conn:
            plays = 1
            completion = int(is_completed)
            delta = listened
            if event_id:
                prev = conn.execute("SELECT * FROM play_events WHERE event_id=?", (event_id,)).fetchone()
                if prev is None and listened < 15_000 and not is_completed:
                    return {"recorded": False, "reason": "skip_ignored", "account_id": aid, "song_id": sid}
                if prev:
                    if (prev["account_id"], prev["track_key"]) != (aid, track_key):
                        raise ValueError("Playback event belongs to another track")
                    plays = 0
                    delta = max(0, listened - prev["recorded_listen_ms"])
                    completion = int(is_completed and not prev["completed"])
                    if ignore_unchanged_event and not delta and not completion:
                        return {"recorded": False, "reason": "duplicate", "account_id": aid, "song_id": sid}
                conn.execute("""INSERT INTO play_events VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET recorded_listen_ms=max(recorded_listen_ms,excluded.recorded_listen_ms),
                    completed=max(completed,excluded.completed), updated_at=excluded.updated_at""",
                    (event_id, aid, track_key, listened, int(is_completed), now))
            conn.execute(
                """
                INSERT INTO play_history(
                    track_key, provider, external_id, local_path, account_id, song_id, title, artists_json, album, duration, cover_url,
                    play_count, completed_count, total_listen_ms, last_played_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, track_key) DO UPDATE SET
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE play_history.title END,
                    artists_json = CASE WHEN excluded.artists_json != '[]' THEN excluded.artists_json ELSE play_history.artists_json END,
                    album = CASE WHEN excluded.album != '' THEN excluded.album ELSE play_history.album END,
                    duration = CASE WHEN excluded.duration > 0 THEN excluded.duration ELSE play_history.duration END,
                    cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE play_history.cover_url END,
                    play_count = play_history.play_count + excluded.play_count,
                    completed_count = play_history.completed_count + excluded.completed_count,
                    total_listen_ms = play_history.total_listen_ms + excluded.total_listen_ms,
                    last_played_at = excluded.last_played_at
                """,
                (track_key, provider, external_id, local_path, aid, sid, title, artists_json, album, duration, cover_url, plays, completion, delta, now),
            )
            conn.execute("""UPDATE radio_served_history SET status=?
                            WHERE id=(SELECT max(id) FROM radio_served_history WHERE account_id=? AND track_key=? AND status!='superseded')
                            AND status != 'completed'""", ("completed" if is_completed else "played", aid, track_key))
            row = conn.execute(
                "SELECT * FROM play_history WHERE account_id = ? AND track_key = ?",
                (aid, track_key),
            ).fetchone()
        return {
            "recorded": True,
            "track_key": track_key,
            "account_id": aid,
            "song_id": sid,
            "play_count": int(row["play_count"]) if row else 1,
            "completed_count": int(row["completed_count"]) if row else (1 if is_completed else 0),
            "total_listen_ms": int(row["total_listen_ms"]) if row else listened,
            "last_played_at": str(row["last_played_at"]) if row else now,
        }

    def list_play_history(self, account_id: str, limit: int = 100, offset: int = 0, query: str = '') -> list[dict[str, Any]]:
        aid = str(account_id or "local")
        capped = max(1, min(10_000, int(limit or 5000)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT track_key, provider, external_id, local_path, song_id, title, artists_json, album, duration, cover_url,
                       play_count, completed_count, total_listen_ms, last_played_at
                FROM play_history
                WHERE account_id = ? AND instr(lower(title || ' ' || artists_json || ' ' || album || ' ' || track_key), lower(?)) > 0
                ORDER BY last_played_at DESC, track_key
                LIMIT ? OFFSET ?
                """,
                (aid, query, capped, max(0, offset)),
            ).fetchall()
        return self._history_items(rows)

    @staticmethod
    def _history_items(rows):
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                artists = json.loads(row["artists_json"])
                if not isinstance(artists, list):
                    artists = []
            except (TypeError, json.JSONDecodeError):
                artists = []
            result.append(
                {
                    "song_id": int(row["song_id"]),
                    **{k: row[k] for k in ("track_key","provider","external_id","local_path")},
                    "id": int(row["song_id"]),
                    "title": str(row["title"] or ""),
                    "artists": [str(a) for a in artists],
                    "album": str(row["album"] or ""),
                    "duration": int(row["duration"] or 0),
                    "duration_ms": int(row["duration"] or 0),
                    "cover_url": str(row["cover_url"] or ""),
                    "play_count": int(row["play_count"] or 0),
                    "completed_count": int(row["completed_count"] or 0),
                    "total_listen_ms": int(row["total_listen_ms"] or 0),
                    "last_played_at": str(row["last_played_at"] or ""),
                }
            )
        return result

    def list_play_history_delta(self, account_id, *, since="", until="", after_ts="", after_track_key="", limit=100):
        page = self._delta_page(account_id, "play_history", "last_played_at", "track_key",
                                since, until, after_ts, after_track_key, limit)
        page["items"] = self._history_items(page["items"])
        return page

    def list_feedback_delta(self, account_id, *, since="", until="", after_ts="", after_id=0, limit=200):
        page = self._delta_page(account_id, "explicit_feedback", "created_at", "id",
                                since, until, after_ts, after_id, limit)
        for item in page["items"]:
            item["track_key"] = item["track_key"] or f"netease:{item['song_id']}"
        return page

    def _delta_page(self, account, table, timestamp, key, since, until, after_ts, after_key, limit):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("Invalid page size")
        where = ["account_id = ?"]
        params = [account]
        if table == "explicit_feedback":
            where.append("archived = 0 AND (created_at >= ? OR action = 'not_interested')")
            params.append((datetime.now(timezone.utc) - timedelta(days=FEEDBACK_RETENTION_DAYS)).isoformat(timespec="microseconds"))
        if until:
            where.append(f"{timestamp} <= ?")
            params.append(until)
        if after_ts and after_key:
            where.append(f"({timestamp}, {key}) > (?, ?)")
            params.extend([after_ts, after_key])
        elif since:
            where.append(f"{timestamp} > ?")
            params.append(since)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table} WHERE {' AND '.join(where)} "
                                f"ORDER BY {timestamp}, {key} LIMIT ?", [*params, limit + 1]).fetchall()
        more = len(rows) > limit
        cursor = {"after_ts": rows[limit-1][timestamp], "after_" + key: rows[limit-1][key]} if more else None
        return {"items": [dict(row) for row in rows[:limit]], "has_more": more, "next_cursor": cursor}

    def apply_lan_sync_push(self, account, events, feedback, incoming, force_session=False):
        accepted_events = accepted_feedback = 0
        updated = False
        with self.write_batch() as conn:
            for event in events:
                result = self.record_play(account, connection=conn, ignore_unchanged_event=True, **event)
                accepted_events += result["recorded"]
            for item in feedback:
                receipt = conn.execute("INSERT OR IGNORE INTO lan_feedback_receipts VALUES (?, ?)",
                                       (account, item["client_event_id"]))
                if receipt.rowcount:
                    saved = self.add_feedback(account, connection=conn, **item)
                    accepted_feedback += bool(saved["id"])
            if incoming is not None:
                previous = conn.execute("SELECT updated_at FROM playback_sessions WHERE account_id=?", (account,)).fetchone()
                ts = incoming.get("updated_at")
                if force_session or not previous or ts and datetime.fromisoformat(ts) > datetime.fromisoformat(previous[0]):
                    saved = self.save_playback_session(account, incoming, updated_at=ts, connection=conn)
                    updated = saved["updated_at"]
        return dict(accepted_events=accepted_events, accepted_feedback=accepted_feedback,
                    session_updated=bool(updated)), updated

    def delete_play_history(self, account_id: str, track_key: str | None = None) -> int:
        where = "account_id=?" + (" AND track_key=?" if track_key is not None else "")
        args = (account_id, track_key) if track_key is not None else (account_id,)
        with self._connect() as conn:
            conn.execute("DELETE FROM play_events WHERE " + where, args)
            return conn.execute("DELETE FROM play_history WHERE " + where, args).rowcount

    def get_playback_session(self, account_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM playback_sessions WHERE account_id=?", (account_id,)).fetchone()
        return self._session_dict(row) if row else None

    @staticmethod
    def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["queue"] = json.loads(data.pop("queue_json"))
        data["current_track"] = json.loads(data.pop("current_track_json") or "null")
        data["muted"] = bool(data["muted"])
        return data

    def save_playback_session(self, account_id: str, payload: dict[str, Any], *, updated_at: str | None = None,
                              connection=None) -> dict[str, Any]:
        values = self.playback_session_values(payload)
        with (self._connect() if connection is None else nullcontext(connection)) as conn:
            conn.execute("INSERT OR REPLACE INTO playback_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (account_id, *values, updated_at or _utc_now()))
            return self._session_dict(conn.execute("SELECT * FROM playback_sessions WHERE account_id=?", (account_id,)).fetchone())

    @staticmethod
    def playback_session_values(payload):
        queue = payload.get("queue", [])
        track = payload.get("current_track")
        if (not isinstance(queue, list) or len(queue) > 2000 or any(not isinstance(t, dict) for t in queue)
                or (track is not None and not isinstance(track, dict))):
            raise ValueError("Invalid session tracks")
        def number(key, default, lo, hi):
            v = payload.get(key, default)
            if type(v) not in (float, int) or not lo <= v <= hi or not math.isfinite(v):
                raise ValueError("Invalid " + key)
            return v
        idx = payload.get("queue_index", -1)
        muted = payload.get("muted", False)
        mode = payload.get("play_mode", "order")
        tab = payload.get("active_tab", "discover")
        if (type(idx) is not int or not -1 <= idx < len(queue) or type(muted) is not bool
                or any(not isinstance(v, str) or not 0 < len(v) <= 64 for v in (mode, tab))):
            raise ValueError("Invalid session state")
        if len(queue) > 221:
            start = max(0, min(idx - 110, len(queue) - 221))
            queue = queue[start:start + 221]
            idx -= start
        return (json.dumps(track, allow_nan=False), number("position_sec",0,0,1e9),
                number("duration_sec",0,0,1e9), json.dumps(queue, allow_nan=False), idx, mode,
                number("volume",0.8,0,1), muted, number("playback_rate",1,0.5,2), tab)

    def clear_playback_session(self, account_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM playback_sessions WHERE account_id=?", (account_id,))

    def radio_play_history(self, account_id):
        with self._connect() as conn:
            rows = conn.execute("""SELECT track_key, song_id, provider, last_played_at, play_count, completed_count
                                   FROM play_history WHERE account_id=?""", (account_id,))
            return {r["track_key"]: dict(r) for r in rows}

    def get_play_history_map(self, account_id: str) -> dict[int, dict[str, Any]]:
        return {item["song_id"]: item for item in self.list_play_history(account_id, limit=10_000) if item["provider"] == "netease"}

    def get_listening_insights(self, account_id: str) -> dict[str, Any]:
        aid = str(account_id or "local")
        cutoff_30d = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self._connect() as conn:
            history_rows = conn.execute(
                """
                SELECT song_id, artists_json, play_count, completed_count, total_listen_ms, last_played_at
                FROM play_history
                WHERE account_id = ?
                """,
                (aid,),
            ).fetchall()

            total_plays = 0
            total_listen_ms = 0
            artist_stats: dict[str, dict[str, Any]] = {}
            for row in history_rows:
                pc = int(row["play_count"] or 0)
                lms = int(row["total_listen_ms"] or 0)
                total_plays += pc
                total_listen_ms += lms
                try:
                    artists = json.loads(row["artists_json"])
                except (TypeError, json.JSONDecodeError):
                    artists = []
                if isinstance(artists, list) and artists:
                    primary = str(artists[0]).strip()
                    if primary:
                        bucket = artist_stats.setdefault(
                            primary, {"artist": primary, "play_count": 0, "listen_ms": 0, "song_count": 0}
                        )
                        bucket["play_count"] += pc
                        bucket["listen_ms"] += lms
                        bucket["song_count"] += 1

            top_artists = [
                {
                    "artist": item["artist"],
                    "play_count": item["play_count"],
                    "listen_minutes": round(item["listen_ms"] / 60_000.0, 1),
                    "song_count": item["song_count"],
                }
                for item in sorted(
                    artist_stats.values(),
                    key=lambda x: (-x["play_count"], -x["listen_ms"], x["artist"]),
                )[:5]
            ]

            catalog_rows = conn.execute(
                """
                SELECT DISTINCT s.song_id, s.max_br_level, s.is_cloud,
                       EXISTS(
                           SELECT 1 FROM playlists p2
                           JOIN playlist_tracks pt2 ON pt2.account_id = p2.account_id AND pt2.playlist_id = p2.playlist_id
                           WHERE pt2.account_id = ? AND pt2.song_id = s.song_id AND p2.kind = 'cloud'
                       ) AS in_cloud_playlist
                FROM playlist_tracks pt
                JOIN account_songs s ON s.account_id = pt.account_id AND s.song_id = pt.song_id
                WHERE pt.account_id = ?
                """,
                (aid, aid),
            ).fetchall()

            played_recent: set[int] = {
                int(row["song_id"])
                for row in history_rows
                if str(row["last_played_at"] or "") >= cutoff_30d
            }

            quality_breakdown: dict[str, int] = {}
            cloud_count = 0
            dusty_gems_count = 0
            for row in catalog_rows:
                sid = int(row["song_id"])
                if sid not in played_recent:
                    dusty_gems_count += 1
                if row["is_cloud"] or row["in_cloud_playlist"]:
                    cloud_count += 1
                lvl = str(row["max_br_level"] or "").strip().lower() or "unknown"
                quality_breakdown[lvl] = quality_breakdown.get(lvl, 0) + 1

            local_row = conn.execute("SELECT COUNT(*) AS cnt FROM local_files").fetchone()
            local_count = int(local_row["cnt"]) if local_row else 0

        return {
            "account_id": aid,
            "total_plays": total_plays,
            "total_completed": sum(r["completed_count"] for r in history_rows),
            "unique_songs_played": len(history_rows),
            "total_listen_ms": total_listen_ms,
            "total_listen_minutes": round(total_listen_ms / 60_000.0, 1),
            "top_artists": top_artists,
            "dusty_gems_count": dusty_gems_count,
            "catalog_track_count": len(catalog_rows),
            "quality_breakdown": quality_breakdown,
            "cloud_count": cloud_count,
            "local_count": local_count,
        }

    @staticmethod
    def _row_to_song_dict(row: sqlite3.Row, source_playlist_id: int | str | None, source_playlist_name: str) -> dict[str, Any]:
        try:
            artists = json.loads(row["artists_json"])
            if not isinstance(artists, list):
                artists = []
        except (TypeError, json.JSONDecodeError):
            artists = []
        return {
            "id": int(row["song_id"]),
            "track_key": f"netease:{int(row['song_id'])}",
            "provider": "netease",
            "external_id": str(row["song_id"]),
            "title": str(row["title"]),
            "artists": [str(a) for a in artists],
            "artist_ids": json.loads(row["artist_ids_json"]) if "artist_ids_json" in row.keys() else [],
            "album": str(row["album"] or ""),
            "duration": int(row["duration"] or 0),
            "duration_ms": int(row["duration"] or 0),
            "cover_url": str(row["cover_url"] or ""),
            "source_playlist_id": source_playlist_id,
            "availability": str(row["availability"] or "unknown"),
            "chorus_start": row["chorus_start"],
            "chorus_end": row["chorus_end"],
            "fee": row["fee"],
            "privilege_st": row["privilege_st"],
            "max_br_level": str(row["max_br_level"] or ""),
            "is_cloud": bool(row["is_cloud"]),
            "source_playlist_name": source_playlist_name,
        }

    @classmethod
    def _row_to_song(cls, row: sqlite3.Row, source_playlist_id: int | str | None, source_playlist_name: str) -> Song:
        data = cls._row_to_song_dict(row, source_playlist_id, source_playlist_name)
        data.pop("duration_ms")
        return Song(**data)

    # --- Canonical Tracks, Unified Favorites, Hybrid Playlists, Route Cache & Candidate Blocklist ---

    def upsert_canonical_track(self, account_id: str, raw_track: dict[str, Any], *, connection=None) -> dict[str, Any]:
        from .track_identity import normalize_canonical_track

        aid = str(account_id or "local")
        norm = normalize_canonical_track(raw_track)
        tkey = norm.get("track_key")
        if not tkey:
            raise ValueError("Invalid canonical track_key")
        extra = {
            k: v
            for k, v in norm.items()
            if k not in {
                "id", "song_id", "track_key", "provider", "external_id", "title", "artists",
                "artist", "album", "duration", "duration_ms", "cover_url", "isrc", "mbid", "local_path",
            }
            and v not in (None, "", [], {})
        }
        now_iso = _utc_now()
        params = (
            aid,
            tkey,
            norm["provider"],
            norm["external_id"],
            int(norm.get("id") or 0),
            norm["title"],
            json.dumps(norm["artists"], ensure_ascii=False),
            norm["album"],
            int(norm["duration_ms"]),
            norm["cover_url"],
            norm.get("isrc", ""),
            norm.get("mbid", ""),
            norm.get("local_path", ""),
            json.dumps(extra, ensure_ascii=False),
            now_iso,
        )
        sql = """
            INSERT INTO canonical_tracks(
                account_id, track_key, provider, external_id, song_id,
                title, artists_json, album, duration, cover_url,
                isrc, mbid, local_path, extra_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, track_key) DO UPDATE SET
                provider = excluded.provider,
                external_id = excluded.external_id,
                song_id = CASE WHEN excluded.song_id > 0 THEN excluded.song_id ELSE canonical_tracks.song_id END,
                title = CASE WHEN excluded.title != '' THEN excluded.title ELSE canonical_tracks.title END,
                artists_json = CASE WHEN excluded.artists_json != '[]' THEN excluded.artists_json ELSE canonical_tracks.artists_json END,
                album = CASE WHEN excluded.album != '' THEN excluded.album ELSE canonical_tracks.album END,
                duration = CASE WHEN excluded.duration > 0 THEN excluded.duration ELSE canonical_tracks.duration END,
                cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE canonical_tracks.cover_url END,
                isrc = CASE WHEN excluded.isrc != '' THEN excluded.isrc ELSE canonical_tracks.isrc END,
                mbid = CASE WHEN excluded.mbid != '' THEN excluded.mbid ELSE canonical_tracks.mbid END,
                local_path = CASE WHEN excluded.local_path != '' THEN excluded.local_path ELSE canonical_tracks.local_path END,
                extra_json = CASE WHEN excluded.extra_json != '{}' THEN excluded.extra_json ELSE canonical_tracks.extra_json END,
                updated_at = excluded.updated_at
        """
        if connection is not None:
            connection.execute(sql, params)
        else:
            with self._lock, self._connect() as conn:
                conn.execute(sql, params)
        return norm

    @staticmethod
    def _canonical_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            artists = json.loads(row["artists_json"])
            if not isinstance(artists, list):
                artists = []
        except Exception:
            artists = []
        try:
            extra = json.loads(row["extra_json"])
            if not isinstance(extra, dict):
                extra = {}
        except Exception:
            extra = {}
        dur = int(row["duration"] or 0)
        return {
            **extra,
            "id": int(row["song_id"] or 0),
            "song_id": int(row["song_id"] or 0),
            "track_key": str(row["track_key"]),
            "provider": str(row["provider"]),
            "external_id": str(row["external_id"]),
            "title": str(row["title"]),
            "artists": [str(a) for a in artists],
            "artist": ", ".join(str(a) for a in artists),
            "album": str(row["album"] or ""),
            "duration": dur,
            "duration_ms": dur,
            "cover_url": str(row["cover_url"] or ""),
            "isrc": str(row["isrc"] or ""),
            "mbid": str(row["mbid"] or ""),
            "local_path": str(row["local_path"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    def get_canonical_track(self, account_id: str, track_key: str) -> dict[str, Any] | None:
        aid = str(account_id or "local")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM canonical_tracks WHERE account_id = ? AND track_key = ?",
                (aid, str(track_key)),
            ).fetchone()
            return self._canonical_row_to_dict(row) if row is not None else None

    def set_canonical_favorite(self, account_id: str, track_key: str, liked: bool = True, track: dict[str, Any] | None = None) -> dict[str, Any]:
        aid = str(account_id or "local")
        tkey = str(track_key or "").strip()
        if not tkey:
            raise ValueError("track_key required")
        with self._lock, self._connect() as conn:
            if liked:
                if isinstance(track, dict):
                    self.upsert_canonical_track(aid, {**track, "track_key": tkey}, connection=conn)
                conn.execute(
                    "INSERT OR REPLACE INTO canonical_favorites(account_id, track_key, added_at) VALUES (?, ?, ?)",
                    (aid, tkey, _utc_now()),
                )
            else:
                self._mark_deleted(conn, aid, "favorite", "", tkey, _utc_now())
                conn.execute(
                    "DELETE FROM canonical_favorites WHERE account_id = ? AND track_key = ?",
                    (aid, tkey),
                )
        return {"track_key": tkey, "liked": bool(liked)}

    def is_canonical_favorite(self, account_id: str, track_key: str) -> bool:
        aid = str(account_id or "local")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM canonical_favorites WHERE account_id = ? AND track_key = ?",
                (aid, str(track_key)),
            ).fetchone()
            return row is not None

    def list_canonical_favorites(self, account_id: str, limit: int | None = 2000) -> list[dict[str, Any]]:
        aid = str(account_id or "local")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT cf.track_key, cf.added_at, ct.provider, ct.external_id, ct.song_id,
                       ct.title, ct.artists_json, ct.album, ct.duration, ct.cover_url,
                       ct.isrc, ct.mbid, ct.local_path, ct.extra_json, ct.updated_at
                FROM canonical_favorites cf
                LEFT JOIN canonical_tracks ct
                    ON ct.account_id = cf.account_id AND ct.track_key = cf.track_key
                WHERE cf.account_id = ?
                ORDER BY cf.added_at DESC
                LIMIT ?
                """,
                (aid, -1 if limit is None else max(1, min(5000, int(limit or 2000)))),
            ).fetchall()
            out = []
            for row in rows:
                if row["title"] is not None:
                    item = self._canonical_row_to_dict(row)
                    item["added_at"] = str(row["added_at"])
                    out.append(item)
                else:
                    prov, ext = parse_track_key(row["track_key"])
                    out.append(
                        {
                            "id": int(ext) if prov == "netease" and ext.isdigit() else 0,
                            "track_key": str(row["track_key"]),
                            "provider": prov or "external",
                            "external_id": ext,
                            "title": ext,
                            "artists": [],
                            "album": "",
                            "duration": 0,
                            "duration_ms": 0,
                            "cover_url": "",
                            "added_at": str(row["added_at"]),
                        }
                    )
            return out

    def create_hybrid_playlist(
        self,
        account_id: str,
        name: str,
        *,
        playlist_id: str | None = None,
        description: str = "",
        cover_url: str = "",
    ) -> dict[str, Any]:
        import uuid as _uuid

        aid = str(account_id or "local")
        clean_name = str(name or "").strip()[:120]
        if not clean_name:
            raise ValueError("Playlist name required")
        pid = str(playlist_id or f"hybrid:{_uuid.uuid4().hex[:12]}").strip()
        if not pid.startswith("hybrid:"):
            pid = f"hybrid:{pid}"
        now_iso = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO hybrid_playlists(account_id, playlist_id, name, description, kind, cover_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'hybrid', ?, ?, ?)
                ON CONFLICT(account_id, playlist_id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    cover_url = CASE WHEN excluded.cover_url != '' THEN excluded.cover_url ELSE hybrid_playlists.cover_url END,
                    updated_at = excluded.updated_at
                """,
                (aid, pid, clean_name, str(description or "").strip()[:500], str(cover_url or "").strip(), now_iso, now_iso),
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM hybrid_playlist_tracks WHERE account_id = ? AND playlist_id = ?",
                (aid, pid),
            ).fetchone()[0]
        return {
            "id": pid,
            "name": clean_name,
            "description": str(description or "").strip()[:500],
            "kind": "hybrid",
            "privacy": "local",
            "cover_url": str(cover_url or "").strip(),
            "track_count": int(count),
            "created_at": now_iso,
            "updated_at": now_iso,
        }

    def list_hybrid_playlists(self, account_id: str, *, include_tracks: bool = False) -> list[dict[str, Any]]:
        aid = str(account_id or "local")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT hp.*,
                       (SELECT COUNT(*) FROM hybrid_playlist_tracks hpt
                        WHERE hpt.account_id = hp.account_id AND hpt.playlist_id = hp.playlist_id) AS track_count
                FROM hybrid_playlists hp
                WHERE hp.account_id = ?
                ORDER BY hp.updated_at DESC, hp.playlist_id ASC
                """,
                (aid,),
            ).fetchall()
            out = []
            for row in rows:
                item = {
                    "id": str(row["playlist_id"]),
                    "name": str(row["name"]),
                    "description": str(row["description"] or ""),
                    "kind": "hybrid",
                    "privacy": "local",
                    "cover_url": str(row["cover_url"] or ""),
                    "track_count": int(row["track_count"] or 0),
                    "created_at": str(row["created_at"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                }
                if include_tracks:
                    item["songs"] = self._list_hybrid_playlist_tracks_conn(conn, aid, str(row["playlist_id"]), str(row["name"]))
                out.append(item)
            return out

    def _list_hybrid_playlist_tracks_conn(
        self,
        conn: sqlite3.Connection,
        account_id: str,
        playlist_id: str,
        playlist_name: str,
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT hpt.position, hpt.track_key, hpt.added_at,
                   ct.provider, ct.external_id, ct.song_id, ct.title, ct.artists_json,
                   ct.album, ct.duration, ct.cover_url, ct.isrc, ct.mbid,
                   ct.local_path, ct.extra_json, ct.updated_at
            FROM hybrid_playlist_tracks hpt
            LEFT JOIN canonical_tracks ct
                ON ct.account_id = hpt.account_id AND ct.track_key = hpt.track_key
            WHERE hpt.account_id = ? AND hpt.playlist_id = ?
            ORDER BY hpt.position ASC
            """,
            (account_id, playlist_id),
        ).fetchall()
        songs: list[dict[str, Any]] = []
        for row in rows:
            if row["title"] is not None:
                d = self._canonical_row_to_dict(row)
            else:
                prov, ext = parse_track_key(row["track_key"])
                d = {
                    "id": int(ext) if prov == "netease" and ext.isdigit() else 0,
                    "track_key": str(row["track_key"]),
                    "provider": prov or "external",
                    "external_id": ext,
                    "title": ext,
                    "artists": [],
                    "album": "",
                    "duration": 0,
                    "duration_ms": 0,
                    "cover_url": "",
                }
            d["added_at"] = row["added_at"]
            d["source_playlist_id"] = playlist_id
            d["source_playlist_name"] = playlist_name
            d["availability"] = d.get("availability") or "available"
            songs.append(d)
        return songs

    def get_hybrid_playlist(self, account_id: str, playlist_id: str) -> dict[str, Any] | None:
        aid = str(account_id or "local")
        pid = str(playlist_id or "").strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM hybrid_playlists WHERE account_id = ? AND playlist_id = ?",
                (aid, pid),
            ).fetchone()
            if row is None:
                return None
            songs = self._list_hybrid_playlist_tracks_conn(conn, aid, pid, str(row["name"]))
            return {
                "id": pid,
                "name": str(row["name"]),
                "description": str(row["description"] or ""),
                "kind": "hybrid",
                "privacy": "local",
                "cover_url": str(row["cover_url"] or "") or (songs[0].get("cover_url", "") if songs else ""),
                "track_count": len(songs),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "songs": songs,
            }

    def delete_hybrid_playlist(self, account_id: str, playlist_id: str) -> bool:
        aid = str(account_id or "local")
        with self._lock, self._connect() as conn:
            self._mark_deleted(conn, aid, "playlist", "", str(playlist_id), _utc_now())
            cur = conn.execute(
                "DELETE FROM hybrid_playlists WHERE account_id = ? AND playlist_id = ?",
                (aid, str(playlist_id)),
            )
            return cur.rowcount > 0

    def add_tracks_to_hybrid_playlist(
        self,
        account_id: str,
        playlist_id: str,
        tracks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        aid = str(account_id or "local")
        pid = str(playlist_id or "").strip()
        now_iso = _utc_now()
        added = 0
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM hybrid_playlists WHERE account_id = ? AND playlist_id = ?",
                (aid, pid),
            ).fetchone()
            if exists is None:
                raise KeyError(f"Hybrid playlist not found: {pid}")
            existing_keys = {
                r[0]
                for r in conn.execute(
                    "SELECT track_key FROM hybrid_playlist_tracks WHERE account_id = ? AND playlist_id = ?",
                    (aid, pid),
                )
            }
            max_pos_row = conn.execute(
                "SELECT COALESCE(MAX(position), -1) FROM hybrid_playlist_tracks WHERE account_id = ? AND playlist_id = ?",
                (aid, pid),
            ).fetchone()
            next_pos = int(max_pos_row[0]) + 1
            for raw in tracks:
                if not isinstance(raw, dict):
                    continue
                norm = self.upsert_canonical_track(aid, raw, connection=conn)
                tkey = norm["track_key"]
                if not tkey or tkey in existing_keys:
                    continue
                conn.execute(
                    """
                    INSERT INTO hybrid_playlist_tracks(account_id, playlist_id, position, track_key, added_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (aid, pid, next_pos, tkey, now_iso),
                )
                existing_keys.add(tkey)
                next_pos += 1
                added += 1
            conn.execute(
                "UPDATE hybrid_playlists SET updated_at = ? WHERE account_id = ? AND playlist_id = ?",
                (now_iso, aid, pid),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM hybrid_playlist_tracks WHERE account_id = ? AND playlist_id = ?",
                (aid, pid),
            ).fetchone()[0]
        return {"playlist_id": pid, "added": added, "track_count": int(total)}

    def remove_tracks_from_hybrid_playlist(
        self,
        account_id: str,
        playlist_id: str,
        track_keys: list[str],
    ) -> dict[str, Any]:
        aid = str(account_id or "local")
        pid = str(playlist_id or "").strip()
        targets = [str(k).strip() for k in track_keys if str(k).strip()]
        if not targets:
            return {"playlist_id": pid, "removed": 0}
        with self._lock, self._connect() as conn:
            timestamp = _utc_now()
            for key in targets:
                self._mark_deleted(conn, aid, "member", pid, key, timestamp)
            placeholders = ",".join("?" for _ in targets)
            cur = conn.execute(
                f"DELETE FROM hybrid_playlist_tracks WHERE account_id = ? AND playlist_id = ? AND track_key IN ({placeholders})",
                [aid, pid, *targets],
            )
            removed = cur.rowcount
            conn.execute(
                "UPDATE hybrid_playlists SET updated_at = ? WHERE account_id = ? AND playlist_id = ?",
                (_utc_now(), aid, pid),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM hybrid_playlist_tracks WHERE account_id = ? AND playlist_id = ?",
                (aid, pid),
            ).fetchone()[0]
        return {"playlist_id": pid, "removed": int(removed), "track_count": int(total)}

    # --- Per-Track Source Route Cache & Candidate Blocklist ---

    def save_track_source_route(
        self,
        account_id: str,
        track_key: str,
        *,
        provider: str,
        external_id: str = "",
        quality: str = "",
        format_name: str = "",
        bitrate: int = 0,
        sample_rate: int = 0,
        stream_url_hint: str = "",
    ) -> dict[str, Any]:
        aid = str(account_id or "local")
        tkey = str(track_key or "").strip()
        if not tkey or not provider:
            raise ValueError("track_key and provider required")
        now_iso = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO track_source_routes(
                    account_id, track_key, provider, external_id, quality,
                    format, bitrate, sample_rate, stream_url_hint, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    tkey,
                    str(provider).strip(),
                    str(external_id or "").strip(),
                    str(quality or "").strip(),
                    str(format_name or "").strip().lower(),
                    max(0, int(bitrate or 0)),
                    max(0, int(sample_rate or 0)),
                    str(stream_url_hint or "").strip(),
                    now_iso,
                ),
            )
        return self.get_track_source_route(aid, tkey) or {}

    def get_track_source_route(self, account_id: str, track_key: str) -> dict[str, Any] | None:
        aid = str(account_id or "local")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM track_source_routes WHERE account_id = ? AND track_key = ?",
                (aid, str(track_key)),
            ).fetchone()
            return dict(row) if row is not None else None

    def delete_track_source_route(self, account_id: str, track_key: str) -> bool:
        aid = str(account_id or "local")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM track_source_routes WHERE account_id = ? AND track_key = ?",
                (aid, str(track_key)),
            )
            return cur.rowcount > 0

    def block_track_candidate(
        self,
        account_id: str,
        track_key: str,
        candidate_key: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        aid = str(account_id or "local")
        tkey = str(track_key or "").strip()
        ckey = str(candidate_key or "").strip()
        if not tkey or not ckey:
            raise ValueError("track_key and candidate_key required")
        now_iso = _utc_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO track_candidate_blocklist(account_id, track_key, candidate_key, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (aid, tkey, ckey, str(reason or "").strip()[:200], now_iso),
            )
            # Evict cached source route if it points to the newly blocked candidate.
            route = conn.execute(
                "SELECT provider, external_id FROM track_source_routes WHERE account_id = ? AND track_key = ?",
                (aid, tkey),
            ).fetchone()
            if route is not None:
                route_cand = f"{route['provider']}:{route['external_id']}" if route["external_id"] else str(route["provider"])
                if route_cand == ckey or str(route["provider"]) == ckey:
                    conn.execute(
                        "DELETE FROM track_source_routes WHERE account_id = ? AND track_key = ?",
                        (aid, tkey),
                    )
        return {"track_key": tkey, "candidate_key": ckey, "reason": str(reason or "").strip()[:200], "created_at": now_iso}

    def unblock_track_candidate(self, account_id: str, track_key: str, candidate_key: str) -> bool:
        aid = str(account_id or "local")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM track_candidate_blocklist WHERE account_id = ? AND track_key = ? AND candidate_key = ?",
                (aid, str(track_key), str(candidate_key)),
            )
            return cur.rowcount > 0

    def list_blocked_candidates(self, account_id: str, track_key: str) -> list[dict[str, Any]]:
        aid = str(account_id or "local")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT track_key, candidate_key, reason, created_at
                FROM track_candidate_blocklist
                WHERE account_id = ? AND track_key = ?
                ORDER BY created_at DESC
                """,
                (aid, str(track_key)),
            ).fetchall()
            return [dict(r) for r in rows]
