from __future__ import annotations

from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone
import math
import re
import json
import random
import time
import threading
import unicodedata
from typing import Any, Callable

from .errors import propagate_risk_control, AppError
from .models import Song
from .netease_client import NeteaseClient, recommendation_reads, ReadBudgetReached
from .shuffle_engine import balanced_shuffle, parse_timestamp_seconds, recency_bucket

def beijing_recommend_day(now_ts=None) -> str:
    now = datetime.fromtimestamp(time.time() if now_ts is None else now_ts, timezone(timedelta(hours=8)))
    return (now - timedelta(hours=6)).date().isoformat()


def _version_key(track):
    title = unicodedata.normalize("NFKC", track["title"]).casefold()
    title = re.sub(r"[(\[（]?\s*(?:\d{4}\s*)?remaster(?:ed)?(?:\s*\d{4})?\s*[)\]）]?", "", title)
    title = "".join(ch for ch in title if ch.isalnum())
    artists = track.get("artists") or [""]
    return title, _norm_artist(artists[0]), round((track.get("duration") or 0) / 3000)


NEGATIVE_FEEDBACK_ACTIONS = frozenset({"not_interested"})
POSITIVE_FEEDBACK_ACTIONS = frozenset({"replay", "like_explicit"})
_ACTION_WEIGHTS = {
    "like_explicit": 0.18,
    "replay": 0.14,
}
_MAX_ARTIST_BOOST = 0.45

_DUSTY_VALID_KINDS = frozenset({"all", "forgotten", "unplayed"})
_DUSTY_VALID_SORTS = frozenset({"balanced", "oldest", "most_played"})
_DUSTY_SECONDS_PER_DAY = 86_400.0
_DUSTY_MAX_PLAYLISTS = 24


def _resolve_dusty_playlist_name(track: dict[str, Any]) -> str:
    existing = str(track.get("source_playlist_name") or "").strip()
    if existing:
        return existing
    if str(track.get("provider") or "").strip().lower() == "local":
        return "本地曲库"
    if bool(track.get("is_cloud", False)):
        return "网易云云盘"
    return ""


def _dusty_memory_reason(
    dusty_kind: str,
    days_since_played: int | None,
    play_count: int,
    source_playlist_name: str,
) -> str:
    if dusty_kind == "forgotten":
        if days_since_played is not None and play_count > 0:
            return f"{days_since_played} 天未听 · 曾播 {play_count} 次"
        if days_since_played is not None:
            return f"{days_since_played} 天未听"
        return "超过 30 天未听"
    if source_playlist_name:
        return f"尚未播放 · 来自「{source_playlist_name}」"
    return "收藏后尚未播放"


def _enrich_dusty_track(
    track: dict[str, Any],
    *,
    bucket: str,
    history_row: dict[str, Any] | None,
    now_ts: float,
) -> tuple[dict[str, Any], float | None]:
    dusty_kind = "forgotten" if bucket == "dusty" else "unplayed"
    row = history_row if isinstance(history_row, dict) else None
    play_count = max(0, int(row.get("play_count") or 0)) if row else 0
    completed_count = max(0, int(row.get("completed_count") or 0)) if row else 0
    raw_last = row.get("last_played_at") if row else None
    last_played_at = str(raw_last).strip() if raw_last is not None and str(raw_last).strip() else None
    last_ts = parse_timestamp_seconds(raw_last) if raw_last is not None and raw_last != "" else None
    days_since_played = (
        max(0, int((now_ts - last_ts) // _DUSTY_SECONDS_PER_DAY))
        if last_ts is not None
        else None
    )
    source_playlist_name = _resolve_dusty_playlist_name(track)
    entry = dict(track)
    entry["dusty_kind"] = dusty_kind
    entry["days_since_played"] = days_since_played
    entry["play_count"] = play_count
    entry["completed_count"] = completed_count
    entry["last_played_at"] = last_played_at
    entry["source_playlist_name"] = source_playlist_name
    entry["recommend_source"] = "dusty_gems"
    entry["recommend_reason"] = _dusty_memory_reason(
        dusty_kind, days_since_played, play_count, source_playlist_name
    )
    return entry, last_ts


def _matches_dusty_query(track: dict[str, Any], q_lower: str) -> bool:
    if q_lower in str(track.get("title") or "").casefold():
        return True
    raw_artists = track.get("artists")
    if isinstance(raw_artists, list):
        parts = [str(a).strip() for a in raw_artists if str(a).strip()]
        if q_lower in " ".join(parts).casefold() or (
            len(parts) > 1 and q_lower in " / ".join(parts).casefold()
        ):
            return True
    elif isinstance(raw_artists, str) and q_lower in raw_artists.casefold():
        return True
    if q_lower in str(track.get("album") or "").casefold():
        return True
    return q_lower in str(track.get("source_playlist_name") or "").casefold()


def _dusty_artist_key(item: dict[str, Any]) -> str:
    raw = item.get("artists")
    if isinstance(raw, list):
        for artist in raw:
            cleaned = _norm_artist(str(artist))
            if cleaned:
                return cleaned
    elif isinstance(raw, str):
        return _norm_artist(raw)
    single = item.get("artist")
    return _norm_artist(str(single)) if isinstance(single, str) else ""


def _interleave_dusty_balanced(
    forgotten: list[dict[str, Any]],
    unplayed: list[dict[str, Any]],
    play_history: dict[Any, Any],
    *,
    seed: int | None,
    now_ts: float,
) -> list[dict[str, Any]]:
    if seed is not None:
        seed_rng = random.Random(seed)
        f_seed: int | None = seed_rng.randrange(1 << 63)
        u_seed: int | None = seed_rng.randrange(1 << 63)
    else:
        f_seed = None
        u_seed = None

    f_list = balanced_shuffle(forgotten, play_history, seed=f_seed, now_ts=now_ts)
    u_list = balanced_shuffle(unplayed, play_history, seed=u_seed, now_ts=now_ts)
    f_keys = [_dusty_artist_key(item) for item in f_list]
    u_keys = [_dusty_artist_key(item) for item in u_list]

    n_f = len(f_list)
    n_u = len(u_list)
    fi = 0
    ui = 0
    merged: list[tuple[str, dict[str, Any]]] = []
    last_artist = ""

    def _pick_non_colliding(
        items: list[dict[str, Any]], keys: list[str], idx: int, total: int, avoid: str
    ) -> tuple[str, dict[str, Any]] | None:
        if idx >= total:
            return None
        if not avoid or not keys[idx] or keys[idx] != avoid:
            return keys[idx], items[idx]
        limit_scan = min(total, idx + 16)
        for j in range(idx + 1, limit_scan):
            if not keys[j] or keys[j] != avoid:
                items[idx], items[j] = items[j], items[idx]
                keys[idx], keys[j] = keys[j], keys[idx]
                return keys[idx], items[idx]
        return None

    while fi < n_f or ui < n_u:
        coord_f = (fi + 0.5) / n_f if fi < n_f else float("inf")
        coord_u = (ui + 0.5) / n_u if ui < n_u else float("inf")
        prefer_f = coord_f <= coord_u

        if prefer_f:
            picked = _pick_non_colliding(f_list, f_keys, fi, n_f, last_artist)
            if picked is not None:
                fi += 1
            else:
                picked = _pick_non_colliding(u_list, u_keys, ui, n_u, last_artist)
                if picked is not None:
                    ui += 1
                else:
                    picked = (f_keys[fi], f_list[fi])
                    fi += 1
        else:
            picked = _pick_non_colliding(u_list, u_keys, ui, n_u, last_artist)
            if picked is not None:
                ui += 1
            else:
                picked = _pick_non_colliding(f_list, f_keys, fi, n_f, last_artist)
                if picked is not None:
                    fi += 1
                else:
                    picked = (u_keys[ui], u_list[ui])
                    ui += 1

        merged.append(picked)
        last_artist = picked[0]

    has_collision = any(
        merged[i][0] and merged[i][0] == merged[i - 1][0] for i in range(1, len(merged))
    )
    if has_collision:
        for _ in range(len(merged)):
            collision_idx = -1
            for i in range(1, len(merged)):
                if merged[i][0] and merged[i][0] == merged[i - 1][0]:
                    collision_idx = i
                    break
            if collision_idx < 0:
                break
            artist_to_move = merged[collision_idx][0]
            moved = False
            for insert_at in range(len(merged)):
                if insert_at == collision_idx or insert_at == collision_idx - 1:
                    continue
                prev_artist = merged[insert_at - 1][0] if insert_at > 0 else ""
                next_artist = merged[insert_at][0] if insert_at < len(merged) else ""
                if artist_to_move != prev_artist and artist_to_move != next_artist:
                    item = merged.pop(collision_idx)
                    adjusted = insert_at if insert_at < collision_idx else insert_at - 1
                    merged.insert(adjusted, item)
                    moved = True
                    break
            if not moved:
                break

    return [item for _akey, item in merged]


_SOURCE_LABELS = {
    "daily": "网易云每日推荐",
    "personal_fm": "网易云私人漫游",
    "intelligence": "网易云心动模式",
    "similar": "与当前歌曲相似",
    "dusty_gems": "沉睡宝藏 · 长期未听",
    "smart_radio": "真·心动电台",
}

_RADIO_NOISE_TOKENS = (
    "dj版",
    "dj舞曲",
    "dj remix",
    "慢摇",
    "串烧",
    "伴奏",
    "karaoke",
    "ktv",
    "消音",
    "片段",
    "铃声",
    "试听",
    "nightcore",
    "sped up",
    "spedup",
)


def _norm_artist(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name or "").casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def _primary_artist_key(song: Song) -> str:
    if not song.artists:
        return ""
    return _norm_artist(song.artists[0])


def _is_radio_noise(title: str) -> bool:
    norm = unicodedata.normalize("NFKC", title or "").casefold()
    compact = "".join(ch for ch in norm if ch.isalnum())
    for token in _RADIO_NOISE_TOKENS:
        if token in norm or token.replace(" ", "") in compact:
            return True
    # Standalone "dj" token surrounded by spaces or parentheses
    for marker in ("(dj)", "[dj]", "（dj）", "【dj】", " dj ", "dj ", " dj"):
        if marker in norm:
            return True
    return False


def _extract_artists_from_feedback_row(
    row: dict[str, Any],
    song_artist_lookup: dict[int, list[str]],
) -> list[str]:
    direct = row.get("artists")
    if isinstance(direct, list):
        parsed = [str(item).strip() for item in direct if str(item).strip()]
        if parsed:
            return parsed
    elif isinstance(direct, str) and direct.strip():
        return [direct.strip()]

    context = row.get("context")
    if isinstance(context, str) and context.strip().startswith("{"):
        try:
            context = json.loads(context)
        except json.JSONDecodeError:
            context = None
    if isinstance(context, dict):
        ctx_artists = context.get("artists")
        if isinstance(ctx_artists, list):
            parsed = [str(item).strip() for item in ctx_artists if str(item).strip()]
            if parsed:
                return parsed
        ctx_artist = context.get("artist")
        if isinstance(ctx_artist, str) and ctx_artist.strip():
            return [ctx_artist.strip()]

    song_id = row.get("song_id")
    if isinstance(song_id, int) and song_id in song_artist_lookup:
        return song_artist_lookup[song_id]
    return []


def _spread_consecutive_artists(items: list[tuple[Song, set[str]]]) -> list[tuple[Song, set[str]]]:
    if len(items) <= 2:
        return list(items)
    remaining = list(items)
    ordered: list[tuple[Song, set[str]]] = []
    last_artist = ""
    while remaining:
        chosen_index = 0
        if last_artist:
            for idx, (song, _signals) in enumerate(remaining):
                artist_key = _primary_artist_key(song)
                if not artist_key or artist_key != last_artist:
                    chosen_index = idx
                    break
        picked = remaining.pop(chosen_index)
        ordered.append(picked)
        last_artist = _primary_artist_key(picked[0])
    return ordered


def _honest_reason(source: str, used_signals: set[str]) -> str:
    base = _SOURCE_LABELS.get(source, "网易云推荐")
    has_like = "like_explicit" in used_signals
    has_replay = "replay" in used_signals
    if has_like and has_replay:
        return f"{base} · 基于你近期主动点赞与重播的艺人"
    if has_like:
        return f"{base} · 基于你近期主动点赞的艺人"
    if has_replay:
        return f"{base} · 基于你近期重播的艺人"
    return base


def _song_from_dict(raw: dict[str, Any]) -> Song | None:
    try:
        sid = int(raw.get("id") or raw.get("song_id") or 0)
    except (TypeError, ValueError):
        return None
    if sid <= 0:
        return None
    raw_artists = raw.get("artists")
    if isinstance(raw_artists, list):
        artists = [str(a).strip() for a in raw_artists if str(a).strip()]
    elif isinstance(raw_artists, str) and raw_artists.strip():
        artists = [raw_artists.strip()]
    else:
        artists = []
    return Song(
        id=sid,
        title=str(raw.get("title") or raw.get("name") or "未知歌曲"),
        artists=artists,
        artist_ids=list(raw.get("artist_ids") or []),
        album=str(raw.get("album") or ""),
        duration=max(0, int(raw.get("duration") or 0)),
        cover_url=str(raw.get("cover_url") or ""),
        source_playlist_id=raw.get("source_playlist_id"),
        availability=str(raw.get("availability") or "unknown"),
        chorus_start=raw.get("chorus_start") if isinstance(raw.get("chorus_start"), int) else None,
        chorus_end=raw.get("chorus_end") if isinstance(raw.get("chorus_end"), int) else None,
        fee=raw.get("fee") if isinstance(raw.get("fee"), int) else None,
        privilege_st=raw.get("privilege_st") if isinstance(raw.get("privilege_st"), int) else None,
        max_br_level=str(raw.get("max_br_level") or ""),
        is_cloud=bool(raw.get("is_cloud", False)),
        source_playlist_name=str(raw.get("source_playlist_name") or ""),
    )


def rerank_recommendations(
    songs: list[Song],
    feedback_rows: list[dict[str, Any]],
    source: str,
    song_artist_lookup: dict[int, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Apply lightweight explicit-feedback filtering, mild artist boost, exploration slots, and artist spreading.

    Strictly ignores pruning actions (unlike/remove/prune) and skip noise.
    """
    artist_lookup: dict[int, list[str]] = dict(song_artist_lookup or {})
    for song in songs:
        if song.id not in artist_lookup and song.artists:
            artist_lookup[song.id] = list(song.artists)

    blocked_song_ids: set[int] = set()
    artist_boost: dict[str, float] = {}
    artist_signals: dict[str, set[str]] = {}

    for row in feedback_rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "").strip().lower()
        song_id = row.get("song_id")
        if action in NEGATIVE_FEEDBACK_ACTIONS:
            if isinstance(song_id, int) and song_id > 0:
                blocked_song_ids.add(song_id)
            continue
        if action not in POSITIVE_FEEDBACK_ACTIONS:
            # Strictly exclude pruning actions, unlikes, skips, relisten_later, etc.
            continue
        weight = _ACTION_WEIGHTS[action]
        for artist_name in _extract_artists_from_feedback_row(row, artist_lookup):
            key = _norm_artist(artist_name)
            if not key:
                continue
            artist_boost[key] = min(_MAX_ARTIST_BOOST, artist_boost.get(key, 0.0) + weight)
            artist_signals.setdefault(key, set()).add(action)

    # Deduplicate and filter out explicitly marked not_interested tracks.
    unique_songs: list[Song] = []
    seen_ids: set[int] = set()
    for song in songs:
        if song.id in seen_ids or song.id in blocked_song_ids:
            continue
        seen_ids.add(song.id)
        unique_songs.append(song)

    if not unique_songs:
        return []

    total = len(unique_songs)
    boosted: list[tuple[float, int, Song, set[str]]] = []
    explore: list[tuple[Song, set[str]]] = []

    for index, song in enumerate(unique_songs):
        base_score = 1.0 - (index / max(total, 1)) * 0.5
        best_boost = 0.0
        matched_signals: set[str] = set()
        for artist_name in song.artists:
            key = _norm_artist(artist_name)
            if key in artist_boost:
                best_boost = max(best_boost, artist_boost[key])
                matched_signals.update(artist_signals.get(key, set()))
        if best_boost > 0.0:
            boosted.append((base_score + best_boost, index, song, matched_signals))
        else:
            explore.append((song, set()))

    boosted.sort(key=lambda item: (-item[0], item[1]))
    boosted_queue = [(item[2], item[3]) for item in boosted]
    explore_queue = list(explore)

    # Interleave to guarantee >= 1/3 exploration slots whenever novel tracks exist.
    interleaved: list[tuple[Song, set[str]]] = []
    boosted_streak = 0
    while boosted_queue or explore_queue:
        if boosted_queue and (boosted_streak < 2 or not explore_queue):
            interleaved.append(boosted_queue.pop(0))
            boosted_streak += 1
        elif explore_queue:
            interleaved.append(explore_queue.pop(0))
            boosted_streak = 0

    spread = _spread_consecutive_artists(interleaved)
    output: list[dict[str, Any]] = []
    for song, signals in spread:
        entry = song.to_dict()
        entry["recommend_source"] = source
        entry["recommend_reason"] = _honest_reason(source, signals)
        output.append(entry)
    return output


class RecommendService:
    """Fault-isolated recommendation aggregator for native feeds + local-first 4-way Smart Radio."""

    MAX_FM_EMPTY_RETRIES = 2

    def __init__(
        self,
        client: NeteaseClient,
        *,
        db: Any = None,
        catalog: Any = None,
        feedback_provider: Callable[[str], list[dict[str, Any]]] | None = None,
    ):
        self.client = client
        self.db = db
        self.catalog = catalog
        self._feedback_provider = feedback_provider
        self._fm_lock = threading.Lock()
        self._account_locks = [threading.RLock() for _ in range(64)]
        self._cache_lock = threading.Lock()
        self._cache = OrderedDict()

    def account_lock(self, aid):
        return self._account_locks[hash(str(aid)) % len(self._account_locks)]

    def _cached(self, aid, key):
        with self._cache_lock:
            entry = self._cache.get((aid, key))
            if entry and entry[0] > time.monotonic():
                self._cache.move_to_end((aid, key))
                return entry[1]
            self._cache.pop((aid, key), None)
            return None

    def _remember(self, aid, key, value, ttl=1800):
        with self._cache_lock:
            self._cache[aid, key] = (time.monotonic() + ttl, value)
            self._cache.move_to_end((aid, key))
            account_keys = [k for k in self._cache if k[0] == aid]
            for old_key in account_keys[:-192]:
                del self._cache[old_key]
            while len(self._cache) > 1024:
                self._cache.popitem(last=False)
        return value

    def _load_feedback(self, account_id: str | int, *, with_artists: bool = True) -> tuple[list[dict[str, Any]], dict[int, list[str]]]:
        acct_key = str(account_id if account_id is not None else "local")
        rows: list[dict[str, Any]] = []
        if self._feedback_provider is not None:
            try:
                rows = list(self._feedback_provider(acct_key) or [])
            except Exception:
                rows = []
        elif self.db is not None:
            lister = getattr(self.db, "list_feedback", None)
            if callable(lister):
                try:
                    rows = list(lister(acct_key, limit=2000) or [])
                except TypeError:
                    try:
                        rows = list(lister(acct_key) or [])
                    except Exception:
                        rows = []
                except Exception:
                    rows = []

        artist_lookup: dict[int, list[str]] = {}
        positive_ids = {
            row.get("song_id")
            for row in rows
            if isinstance(row, dict)
            and str(row.get("action") or "").strip().lower() in POSITIVE_FEEDBACK_ACTIONS
            and isinstance(row.get("song_id"), int)
        }
        for song_id in positive_ids if with_artists else ():
            song = self._lookup_song(song_id, acct_key)
            if song is not None and song.artists:
                artist_lookup[song_id] = list(song.artists)
        if self.db is not None and hasattr(self.db, "blocked_song_ids"):
            recent = {r["song_id"] for r in rows if r.get("action") == "not_interested"}
            rows.extend({"song_id": sid, "action": "not_interested"}
                        for sid in self.db.blocked_song_ids(acct_key) - recent)
        return rows, artist_lookup

    def _load_play_history_map(self, account_id: str | int) -> dict[int, dict[str, Any]]:
        acct_key = str(account_id if account_id is not None else "local")
        if self.db is None:
            return {}
        getter = getattr(self.db, "get_play_history_map", None)
        if callable(getter):
            try:
                raw = getter(acct_key)
                if isinstance(raw, dict):
                    return {int(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
            except Exception:
                return {}
        lister = getattr(self.db, "list_play_history", None)
        if callable(lister):
            try:
                rows = lister(acct_key, limit=5000)
                result: dict[int, dict[str, Any]] = {}
                for row in rows or []:
                    if isinstance(row, dict) and isinstance(row.get("song_id"), int):
                        result[int(row["song_id"])] = dict(row)
                return result
            except Exception:
                return {}
        return {}

    def _load_catalog_songs(self, account_id: str | int) -> tuple[list[Song], set[int]]:
        """Load deduplicated user library tracks prioritizing liked & self_created playlists."""
        acct_key = str(account_id if account_id is not None else "local")
        raw_tracks: list[dict[str, Any]] = []
        liked_playlist_ids: set[str] = set()
        owned_playlist_ids: set[str] = set()

        if self.db is not None:
            query_view = getattr(self.db, "query_view", None)
            if callable(query_view):
                try:
                    view = query_view(acct_key)
                    if isinstance(view, dict):
                        for pl in view.get("playlists", []):
                            if isinstance(pl, dict):
                                pid = str(pl.get("id"))
                                kind = str(pl.get("kind") or "")
                                if kind == "liked":
                                    liked_playlist_ids.add(pid)
                                    owned_playlist_ids.add(pid)
                                elif kind in {"self_created", "cloud", "imported"}:
                                    owned_playlist_ids.add(pid)
                        raw_tracks = [t for t in view.get("tracks", []) if isinstance(t, dict)]
                except Exception:
                    raw_tracks = []

        if not raw_tracks and self.catalog is not None:
            viewer = getattr(self.catalog, "view", None)
            if callable(viewer):
                try:
                    try:
                        view = viewer(account_id=acct_key)
                    except TypeError:
                        view = viewer()
                    if isinstance(view, dict):
                        for pl in view.get("playlists", []):
                            if isinstance(pl, dict):
                                pid = str(pl.get("id"))
                                kind = str(pl.get("kind") or "")
                                if kind == "liked":
                                    liked_playlist_ids.add(pid)
                                    owned_playlist_ids.add(pid)
                                elif kind in {"self_created", "cloud", "imported"}:
                                    owned_playlist_ids.add(pid)
                        raw_tracks = [t for t in view.get("tracks", []) if isinstance(t, dict)]
                except Exception:
                    raw_tracks = []

        # Prioritize liked > self_created/cloud/imported > subscribed
        def _priority(track_dict: dict[str, Any]) -> int:
            pid = str(track_dict.get("source_playlist_id"))
            if pid in liked_playlist_ids:
                return 0
            if pid in owned_playlist_ids:
                return 1
            return 2

        sorted_raw = sorted(raw_tracks, key=_priority)
        songs: list[Song] = []
        liked_ids: set[int] = set()
        seen: set[int] = set()
        for item in sorted_raw:
            song = _song_from_dict(item)
            if song is None:
                continue
            pid = str(item.get("source_playlist_id"))
            if pid in liked_playlist_ids:
                liked_ids.add(song.id)
            if song.id in seen:
                continue
            seen.add(song.id)
            songs.append(song)
        return songs, liked_ids

    def _lookup_song(self, song_id: int, account_id: str) -> Song | None:
        if self.db is not None:
            getter = getattr(self.db, "get_song", None)
            if callable(getter):
                try:
                    found = getter(song_id, account_id=account_id)
                    if isinstance(found, Song):
                        return found
                except TypeError:
                    try:
                        found = getter(song_id)
                        if isinstance(found, Song):
                            return found
                    except Exception:
                        pass
                except Exception:
                    pass
        if self.catalog is not None:
            getter = getattr(self.catalog, "song", None)
            if callable(getter):
                try:
                    found = getter(song_id, account_id=account_id)
                    if isinstance(found, Song):
                        return found
                except Exception:
                    pass
        return None

    def _run_feed(
        self,
        source: str,
        account_id: str | int,
        fetcher: Callable[[], list[Song]],
    ) -> dict[str, Any]:
        try:
            raw_songs = fetcher()
        except AppError as exc:
            propagate_risk_control(exc)
            return {
                "songs": [],
                "source": source,
                "degraded": True,
                "reason": exc.message,
                "error_code": exc.code,
            }
        except Exception:
            return {
                "songs": [],
                "source": source,
                "degraded": True,
                "reason": "推荐服务暂时不可用，请稍后重试",
                "error_code": "RECOMMEND_DEGRADED",
            }
        feedback_rows, artist_lookup = self._load_feedback(account_id)
        ranked = rerank_recommendations(raw_songs, feedback_rows, source, artist_lookup)
        return {
            "songs": ranked,
            "source": source,
            "degraded": False,
            "reason": "",
        }

    def daily(self, account_id="local", *, client_override=None, force_check=False, known_raw_ids=None):
        client = client_override or self.client
        day = beijing_recommend_day()
        key = ("daily", day)
        with self.account_lock(account_id):
            cached = self._cached(account_id, key)
            if cached is None and self.db:
                snapshot = self.db.get_daily_snapshot(account_id, day)
                if snapshot:
                    songs = [Song(**{k: v for k, v in row.items() if k in Song.__dataclass_fields__})
                             for row in snapshot["songs"]]
                    cached = self._remember(account_id, key, (songs, snapshot["checked_at"]), 86400)
            previous = set(known_raw_ids) if known_raw_ids is not None else {s.id for s in cached[0]} if cached else set()
            reason = ""
            error_code = ""
            if (cached is None or force_check) and client.cookie:
                try:
                    with recommendation_reads(1):
                        raw = client.daily_recommend_songs()
                    if raw or not cached:
                        checked = time.time()
                        if raw and self.db:
                            self.db.save_daily_snapshot(account_id, day, [s.to_dict() for s in raw], [s.id for s in raw], checked)
                        cached = self._remember(account_id, key, (raw, checked), 86400)
                    else:
                        reason = "本次未取得曲目，显示上次有效推荐"
                except AppError as exc:
                    if not cached or not cached[0]:
                        propagate_risk_control(exc)
                    reason = exc.message
                    error_code = exc.code
                except Exception:
                    reason = "每日推荐获取失败"
                    error_code = "RECOMMEND_DEGRADED"
            if reason and cached:
                checked = time.time()
                if self.db:
                    self.db.save_daily_snapshot(account_id, day, [], [], checked)
                cached = self._remember(account_id, key, (cached[0], checked), 86400)
            raw, checked = cached or ([], None)
            ids = [s.id for s in raw]
            result = self._run_feed("daily", account_id, lambda: raw)
            return {**result, **({"error_code": error_code} if error_code else {}),
                    "reason": reason, "degraded": bool(reason), "recommend_day": day,
                    "raw_song_ids": ids, "raw_count": len(ids), "checked_at": checked,
                    "changed": not reason and set(ids) != previous, "added_count": 0 if reason else len(set(ids)-previous)}

    def daily_history(self, aid, client, day=None, *, explicit_offline=False):
        key = ("history_dates",)
        snapshot_dates = self.db.list_daily_snapshot_dates(aid) if self.db else []
        indexed = self.db.get_daily_history_dates(aid) if self.db else []
        dates = sorted(set(snapshot_dates) | set(indexed), reverse=True)
        snapshot = self.db.get_daily_snapshot(aid, day) if self.db and day else None
        local = ({"date": day, "songs": snapshot["songs"]} if snapshot else
                 {"dates": dates, "cached_dates": snapshot_dates} if dates and not day else None)
        if local:
            local.update(status="ok", reason="")
        base = {"date": day, "songs": []} if day else {"dates": []}
        if day:
            try:
                if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
                    raise ValueError()
            except ValueError:
                raise AppError("INVALID_DATE", "日期格式应为 YYYY-MM-DD", 400)
            known = self._cached(aid, key)
            if client.cookie and known is not None and day not in known and day not in dates and not snapshot:
                raise AppError("INVALID_DATE", "日期不在可用历史中", 400)
        if not client.cookie:
            if local:
                return local
            if not day and explicit_offline:
                return {"dates": [], "cached_dates": [], "status": "empty", "reason": ""}
            return {**base, "status": "auth_required", "reason": "离线模式下该日期详情尚未缓存，请登录后查看" if day else "请先登录"}
        try:
            with recommendation_reads(1):
                if day:
                    songs = [s.to_dict() for s in client.daily_history_songs(day)]
                    if songs and self.db:
                        self.db.save_daily_snapshot(aid, day, songs, [s["id"] for s in songs], time.time())
                    if not songs and local:
                        return local
                    return {**base, "songs": songs, "status": "ok" if songs else "empty", "reason": "" if songs else "暂无历史日推"}
                result = client.daily_history_dates()
                dates = sorted(set(dates) | set(result["dates"]), reverse=True)
                if self.db:
                    dates = self.db.save_daily_history_dates(aid, dates)
                if dates:
                    result = {**result, "dates": dates, "cached_dates": snapshot_dates,
                              "status": "ok", "reason": ""}
                self._remember(aid, key, result["dates"])
                return result
        except AppError as exc:
            status = {"AUTH_REQUIRED": "auth_required", "VIP_REQUIRED": "vip_required",
                      "UPSTREAM_RISK_CONTROL": "cooldown", "RISK_CONTROL_COOLDOWN": "cooldown"}.get(exc.code, "error")
            if local and not day:
                return {**local, "status": status, "reason": exc.message}
            return local or {**base, "status": status, "reason": exc.message}
        except ReadBudgetReached:
            return {**base, "status": "error", "reason": "请求超过历史查询预算"}

    def personal_fm(
        self,
        account_id: str | int = "local",
        *,
        client_override: NeteaseClient | None = None,
    ) -> dict[str, Any]:
        active_client = client_override or self.client
        with self._fm_lock:
            feedback_rows, artist_lookup = self._load_feedback(account_id)
            for _attempt in range(self.MAX_FM_EMPTY_RETRIES + 1):
                try:
                    raw_songs = active_client.personal_fm()
                except AppError as exc:
                    propagate_risk_control(exc)
                    return {
                        "songs": [],
                        "source": "personal_fm",
                        "degraded": True,
                        "reason": exc.message,
                        "error_code": exc.code,
                    }
                except Exception:
                    return {
                        "songs": [],
                        "source": "personal_fm",
                        "degraded": True,
                        "reason": "私人漫游暂时不可用，请稍后重试",
                        "error_code": "RECOMMEND_DEGRADED",
                    }
                ranked = rerank_recommendations(raw_songs, feedback_rows, "personal_fm", artist_lookup)
                if ranked:
                    return {
                        "songs": ranked,
                        "source": "personal_fm",
                        "degraded": False,
                        "reason": "",
                    }
            return {
                "songs": [],
                "source": "personal_fm",
                "degraded": True,
                "reason": "私人漫游连续返回空批次，请稍后再试",
            }

    def intelligence(
        self,
        song_id: int,
        playlist_id: int,
        account_id: str | int = "local",
        *,
        start_music_id: int | None = None,
        count: int = 30,
        client_override: NeteaseClient | None = None,
    ) -> dict[str, Any]:
        active_client = client_override or self.client
        return self._run_feed(
            "intelligence",
            account_id,
            lambda: active_client.intelligence_list(
                song_id,
                playlist_id,
                start_music_id=start_music_id,
                count=count,
            ),
        )

    def similar(
        self,
        song_id: int,
        account_id: str | int = "local",
        *,
        client_override: NeteaseClient | None = None,
    ) -> dict[str, Any]:
        active_client = client_override or self.client
        return self._run_feed(
            "similar",
            account_id,
            lambda: active_client.similar_songs(song_id),
        )

    def _local_tracks(self):
        if self.db is None:
            return []
        tracks = []
        page_size = 1000
        while True:
            page = self.db.list_local_files(limit=page_size, offset=len(tracks))
            for t in page:
                cover_id = str(t.get("cover_id") or "").strip()
                tracks.append(
                    {
                        "id": 0,
                        "title": t["title"],
                        "artists": [t["artist"]] if t.get("artist") else [],
                        "album": t.get("album") or "",
                        "duration": t.get("duration_ms") or 0,
                        "duration_ms": t.get("duration_ms") or 0,
                        "provider": "local",
                        "local_path": t["path"],
                        "track_key": "local:" + t["path"],
                        "format": str(t.get("format") or ""),
                        "bitrate": int(t.get("bitrate") or 0),
                        "sample_rate": int(t.get("sample_rate") or 0),
                        "cover_id": cover_id,
                        "cover_url": f"/api/local-cover/{cover_id}" if cover_id else "",
                        "isrc": str(t.get("isrc") or ""),
                        "mbid": str(t.get("mbid") or ""),
                        "replaygain_track_gain_db": t.get("replaygain_track_gain_db"),
                        "replaygain_track_peak": t.get("replaygain_track_peak"),
                    }
                )
            if len(page) < page_size:
                return tracks

    def _canonical_non_netease_tracks(self, account_id: str | int, *, exclude_keys: set[str] | None = None) -> list[dict[str, Any]]:
        if self.db is None or not hasattr(self.db, "list_canonical_favorites"):
            return []
        seen = set(exclude_keys or ())
        extra: list[dict[str, Any]] = []
        for fav in self.db.list_canonical_favorites(str(account_id), limit=500):
            tkey = fav.get("track_key")
            if tkey and tkey not in seen and fav.get("provider") != "netease":
                seen.add(tkey)
                extra.append(fav)
        if hasattr(self.db, "list_hybrid_playlists"):
            for hpl in self.db.list_hybrid_playlists(str(account_id), include_tracks=True):
                for hsong in hpl.get("songs") or []:
                    tkey = hsong.get("track_key")
                    if tkey and tkey not in seen and hsong.get("provider") != "netease":
                        seen.add(tkey)
                        extra.append(hsong)
        return extra

    def dusty_gems(
        self,
        account_id: str | int = "local",
        *,
        limit: int = 30,
        offset: int = 0,
        kind: str = "all",
        playlist: str = "",
        q: str = "",
        sort: str = "balanced",
        now_ts: float | None = None,
        seed: int | None = None,
        exclude_ids=(),
        exclude_keys=(),
    ) -> dict[str, Any]:
        """Surface long-unplayed (>30d) or never-played tracks from the user's own catalog."""
        cap = max(1, min(100, int(limit or 30)))
        start = max(0, int(offset or 0))
        kind_norm = str(kind or "all").strip().lower()
        if kind_norm not in _DUSTY_VALID_KINDS:
            kind_norm = "all"
        sort_norm = str(sort or "balanced").strip().lower()
        if sort_norm not in _DUSTY_VALID_SORTS:
            sort_norm = "balanced"
        playlist_filter = str(playlist or "").strip()
        query_lower = str(q or "").strip().casefold()
        effective_now = time.time() if now_ts is None else float(now_ts)
        acct_key = str(account_id if account_id is not None else "local")
        excluded_id_set = set(exclude_ids or ())
        excluded_key_set = set(exclude_keys or ())

        feedback_rows, _artist_lookup = self._load_feedback(account_id, with_artists=False)
        blocked_ids = {
            int(row["song_id"])
            for row in feedback_rows
            if isinstance(row, dict)
            and str(row.get("action") or "").strip().lower() in NEGATIVE_FEEDBACK_ACTIONS
            and isinstance(row.get("song_id"), int)
        }
        play_history = self._load_play_history_map(account_id)
        catalog_songs, _liked_ids = self._load_catalog_songs(account_id)

        candidates: list[dict[str, Any]] = []
        last_ts_by_key: dict[str, float | None] = {}
        for song in catalog_songs:
            if song.id in blocked_ids:
                continue
            if song.availability == "unavailable" and not song.is_cloud:
                continue
            history_row = play_history.get(song.id)
            bucket = recency_bucket(history_row, now_ts=effective_now)
            if bucket not in {"unplayed", "dusty"}:
                continue
            entry, last_ts = _enrich_dusty_track(
                song.to_dict(),
                bucket=bucket,
                history_row=history_row,
                now_ts=effective_now,
            )
            candidates.append(entry)
            last_ts_by_key[entry.get("track_key") or f"netease:{song.id}"] = last_ts

        local_tracks = self._local_tracks()
        seen_keys = {t["track_key"] for t in local_tracks if t.get("track_key")} | {
            f"netease:{s.id}" for s in catalog_songs
        }
        extra_canonical = self._canonical_non_netease_tracks(account_id, exclude_keys=seen_keys)
        combined_non_netease = [*local_tracks, *extra_canonical]
        history_by_key: dict[str, Any] = {}
        if combined_non_netease and self.db is not None:
            radio_hist_fn = getattr(self.db, "radio_play_history", None)
            blocked_keys_fn = getattr(self.db, "blocked_track_keys", None)
            history_by_key = radio_hist_fn(acct_key) if callable(radio_hist_fn) else {}
            blocked_keys = blocked_keys_fn(acct_key) if callable(blocked_keys_fn) else set()
            for track in combined_non_netease:
                tkey = track.get("track_key", "")
                if not tkey or tkey in blocked_keys:
                    continue
                history_row = history_by_key.get(tkey)
                bucket = recency_bucket(history_row, now_ts=effective_now)
                if bucket not in {"dusty", "unplayed"}:
                    continue
                entry, last_ts = _enrich_dusty_track(
                    track,
                    bucket=bucket,
                    history_row=history_row,
                    now_ts=effective_now,
                )
                candidates.append(entry)
                last_ts_by_key[tkey] = last_ts

        total = len(candidates)
        forgotten_count = 0
        unplayed_count = 0
        playlist_counts: dict[str, int] = defaultdict(int)
        for item in candidates:
            if item["dusty_kind"] == "forgotten":
                forgotten_count += 1
            else:
                unplayed_count += 1
            pl_name = item["source_playlist_name"]
            if pl_name:
                playlist_counts[pl_name] += 1

        counts = {
            "all": total,
            "forgotten": forgotten_count,
            "unplayed": unplayed_count,
        }
        playlists = [
            {"name": name, "count": cnt}
            for name, cnt in sorted(playlist_counts.items(), key=lambda pair: (-pair[1], pair[0]))[
                :_DUSTY_MAX_PLAYLISTS
            ]
        ]

        filtered: list[dict[str, Any]] = []
        for item in candidates:
            tid = item.get("id")
            tkey = item.get("track_key") or (f"netease:{tid}" if tid else "")
            if (tid and tid in excluded_id_set) or (tkey and tkey in excluded_key_set):
                continue
            if kind_norm != "all" and item["dusty_kind"] != kind_norm:
                continue
            if playlist_filter and item["source_playlist_name"] != playlist_filter:
                continue
            if query_lower and not _matches_dusty_query(item, query_lower):
                continue
            filtered.append(item)

        if sort_norm == "oldest":

            def _oldest_key(item: dict[str, Any]) -> tuple[int, int, float, int, str, str]:
                is_unplayed = 0 if item["dusty_kind"] == "forgotten" else 1
                days = item["days_since_played"]
                days_rank = -(days if days is not None else -1)
                tkey = item.get("track_key") or f"netease:{item.get('id') or 0}"
                ts = last_ts_by_key.get(tkey)
                ts_rank = ts if ts is not None else float("inf")
                return (
                    is_unplayed,
                    days_rank,
                    ts_rank,
                    -item["play_count"],
                    str(item.get("title") or "").casefold(),
                    tkey,
                )

            ordered = sorted(filtered, key=_oldest_key)
        elif sort_norm == "most_played":

            def _most_played_key(item: dict[str, Any]) -> tuple[int, int, int, str, str]:
                days = item["days_since_played"]
                days_rank = -(days if days is not None else -1)
                tkey = item.get("track_key") or f"netease:{item.get('id') or 0}"
                return (
                    -item["play_count"],
                    -item["completed_count"],
                    days_rank,
                    str(item.get("title") or "").casefold(),
                    tkey,
                )

            ordered = sorted(filtered, key=_most_played_key)
        else:
            combined_history: dict[Any, Any] = {**play_history, **history_by_key}
            forgotten_items = [item for item in filtered if item["dusty_kind"] == "forgotten"]
            unplayed_items = [item for item in filtered if item["dusty_kind"] == "unplayed"]
            if forgotten_items and unplayed_items:
                ordered = _interleave_dusty_balanced(
                    forgotten_items,
                    unplayed_items,
                    combined_history,
                    seed=seed,
                    now_ts=effective_now,
                )
            else:
                ordered = balanced_shuffle(filtered, combined_history, seed=seed, now_ts=effective_now)

        filtered_total = len(ordered)
        page = ordered[start : start + cap]
        remaining = max(0, filtered_total - (start + len(page)))
        return {
            "songs": page,
            "source": "dusty_gems",
            "degraded": False,
            "reason": "",
            "total_candidates": total,
            "filtered_total": filtered_total,
            "offset": start,
            "limit": cap,
            "remaining_count": remaining,
            "has_more": remaining > 0,
            "exhausted": remaining == 0,
            "counts": counts,
            "playlists": playlists,
        }

    def smart_radio(self, account_id="local", *, radio_session_id="", request_id="", commit=None, superseded_request_ids=(), **options):
        with self.account_lock(account_id):
            if self.db and superseded_request_ids:
                self.db.mark_radio_requests_superseded(account_id, radio_session_id, superseded_request_ids)
            if request_id and self.db:
                cached = self.db.radio_batch(account_id, radio_session_id, request_id)
                if cached is not None:
                    return cached
            result = self._radio_mix(account_id, radio_session_id, request_id, **options)
            if request_id and self.db:
                return commit(result) if commit else self.db.commit_radio_batch(account_id, result)
            return result

    def explore_batch(self, aid, **options):
        with self.account_lock(aid):
            history = self._cached(aid, ("explore_seen",)) or []
            options["exclude_keys"] = set(options["exclude_keys"]) | set(history)
            options["allow_replay"] = False
            result = self._radio_mix(aid, "", "", explore_only=True, **options)
            self._remember(aid, ("explore_seen",), (history + [t["track_key"] for t in result["songs"]])[-2000:])
            return {**result, "source": "explore", "source_label": "探索推荐"}

    def _radio_mix(self, aid, session, request, *, explore_ratio=0.4, exclude_ids=(), exclude_keys=(),
                   limit=30, client_override=None, now_ts=None, seed=None, allow_replay=False,
                   mode="start", explore_only=False, budget=None, modern_sources=False):
        rng = random.Random(seed)
        cap = max(1, min(100, int(limit)))
        ratio = max(0.0, min(1.0, float(explore_ratio)))
        pure_familiar = (not explore_only) and ratio <= 0.0
        pure_explore = explore_only or ratio >= 1.0
        client = client_override or self.client
        catalog, liked = self._load_catalog_songs(aid)
        rows, _ = self._load_feedback(aid, with_artists=False)
        blocked_keys = self.db.blocked_track_keys(aid) if self.db else set()
        blocked = {r["song_id"] for r in rows if r["action"] == "not_interested" and r["song_id"] > 0}
        blocked.update(s.id for s in catalog if s.availability == "unavailable" and not s.is_cloud)
        positive = {r["song_id"] for r in rows if r["action"] in POSITIVE_FEEDBACK_ACTIONS}
        history_keys = self.db.radio_play_history(aid) if self.db else {}
        history = {t["song_id"]: t for t in history_keys.values() if t["provider"] == "netease"}
        hidden = set(exclude_keys or ()) | {f"netease:{i}" for i in exclude_ids or ()}
        if session and self.db:
            hidden.update(self.db.radio_served_keys(aid, session))
        hidden.update(k for k, row in history_keys.items() if recency_bucket(row, now_ts=now_ts) == "cooldown")
        seeds = [s for s in catalog if s.id not in blocked and not _is_radio_noise(s.title)]
        previous = self._cached(aid, ("seeds",)) or []
        def seed_priority(song):
            weight = 1 + 5*(song.id in positive) + 3*(song.id in liked) + min(8, history.get(song.id, {}).get("completed_count", 0))
            if song.id in previous:
                weight *= 0.25
            return rng.random() ** (1/weight)
        weighted = sorted(seeds, key=seed_priority, reverse=True)
        artists = {}
        for song in weighted:
            artists.setdefault(_primary_artist_key(song), song)
        seed_count = min(len(weighted), rng.randint(2, 4))
        chosen = list(artists.values())[:seed_count]
        if len(chosen) < seed_count:
            chosen.extend([s for s in weighted if s not in chosen][:seed_count-len(chosen)])
        self._remember(aid, ("seeds",), [s.id for s in chosen])

        pools = {key: [] for key in ("dusty_gems", "fresh_rotation", "similar_discovery", "online_exploration")}
        library = {f"netease:{s.id}" for s in catalog}
        def add(track, bucket, reason):
            key = track.setdefault("track_key", f"netease:{track['id']}")
            unavailable = track.get("availability") == "unavailable" and not track.get("is_cloud")
            if (unavailable or track["id"] in blocked or key in blocked_keys or _is_radio_noise(track["title"])
                    or key in hidden and not allow_replay):
                return
            pools[bucket].append({**track, "recommend_source": "smart_radio", "recommend_reason": reason})

        def familiar(track):
            key = track.setdefault("track_key", f"netease:{track['id']}")
            library.add(key)
            bucket = recency_bucket(history_keys.get(key), now_ts=now_ts)
            if not pure_explore:
                if bucket in {"dusty", "unplayed"}:
                    add(track, "dusty_gems", "沉睡宝藏 · 长期未听" if bucket == "dusty" else "沉睡宝藏 · 尚未播放")
                else:
                    add(track, "fresh_rotation", "近期心头好")
        for song in seeds:
            familiar(song.to_dict())
        for track in self._local_tracks():
            familiar(track)
        for track in self._canonical_non_netease_tracks(aid, exclude_keys=library):
            familiar(track)

        if pure_familiar:
            target = 0
        elif pure_explore:
            target = cap
        else:
            target = max(round(cap * ratio), cap - sum(map(len, pools.values())))

        if allow_replay and not explore_only:
            errors, cooldown, similar_hits = [], 0, {}
            if self.db:
                for track in self.db.replay_tracks(aid, session):
                    key = track.get("track_key") or f"netease:{track['id']}"
                    if key.startswith("local:") and key not in library:
                        continue
                    if pure_familiar:
                        if key not in library:
                            continue
                        add(track, "fresh_rotation", "电台重播")
                    elif pure_explore:
                        if key in library:
                            continue
                        add(track, "online_exploration", "电台重播")
                    else:
                        add(track, "online_exploration", "电台重播")
        elif pure_familiar:
            errors, cooldown, similar_hits = [], 0, {}
        else:
            errors, cooldown, similar_hits = self._discover(aid, client, chosen, weighted, target, library, pools, add, rng,
                                                            modern=bool(session or explore_only or modern_sources), explore_only=explore_only,
                                                            mode=mode, budget=budget)

        for name, pool in pools.items():
            rng.shuffle(pool)
            if name in {"similar_discovery", "online_exploration"}:
                pool.sort(key=lambda track: track["track_key"] in library)
        selected = []
        seen, versions = set(), set()
        breakdown = dict.fromkeys(pools, 0)
        strict_novel_only = (not explore_only) and ratio >= 1.0
        def take(bucket, count):
            for t in pools[bucket]:
                if count <= 0 or len(selected) >= cap:
                    break
                key, version = t["track_key"], _version_key(t)
                if strict_novel_only and key in library:
                    continue
                if key in seen or version in versions:
                    continue
                seen.add(key)
                versions.add(version)
                if bucket == "similar_discovery" and len(similar_hits[t["id"]]) > 1:
                    t["recommend_reason"] = "多首红心共同相似"
                selected.append(t)
                breakdown[bucket] += 1
                count -= 1
        if pure_familiar:
            familiar_quota = cap
            take("dusty_gems", (familiar_quota + 1) // 2)
            take("fresh_rotation", familiar_quota - breakdown["dusty_gems"])
            take("dusty_gems", familiar_quota - len(selected))
            take("fresh_rotation", cap)
        elif pure_explore:
            familiar_quota = 0
            take("similar_discovery", max(1, round(cap * 0.625)))
            for bucket in ("online_exploration", "similar_discovery"):
                take(bucket, cap)
        else:
            familiar_quota = cap - round(cap * ratio)
            discovery_quota = cap - familiar_quota
            take("dusty_gems", (familiar_quota + 1) // 2)
            take("fresh_rotation", familiar_quota - breakdown["dusty_gems"])
            take("dusty_gems", familiar_quota - len(selected))
            if discovery_quota > 0:
                take("similar_discovery", max(1, round(discovery_quota * 0.625)))
            for bucket in ("online_exploration", "similar_discovery", "dusty_gems", "fresh_rotation"):
                take(bucket, cap)
        breakdown.update(multi_seed_similar=breakdown["similar_discovery"], exploration=breakdown["online_exploration"],
                         local_files=sum(t.get("provider")=="local" for t in selected),
                         novel_count=len(seen-library), library_count=len(seen & library))
        exhausted = not selected and not cooldown and not errors
        status = ("cooldown" if cooldown else "exhausted" if exhausted else "degraded" if not selected else
                  "partial" if errors or len(selected)<cap else "ok")
        if errors:
            reason = "；".join(dict.fromkeys(errors))
        elif exhausted:
            if pure_explore and not client.cookie:
                reason = "当前未登录网易云，无法拉取在线探索新曲"
            elif pure_explore:
                reason = "在线探索暂无更多未听新曲"
            else:
                reason = "候选曲目已用尽"
        elif status == "partial":
            reason = "在线探索可用新曲不足一批" if pure_explore else "可用曲目不足一批"
        else:
            reason = ""
        return {"songs": balanced_shuffle(selected, history, seed=seed, now_ts=now_ts), "source":"smart_radio",
                "status":status, "degraded": bool(errors and not selected), "reason": reason,
                "radio_session_id":session, "request_id":request, "new_count":len(seen-hidden), "exhausted":exhausted,
                "allow_replay_available":exhausted, "cooldown_sec":cooldown, "explore_ratio":ratio, "breakdown":breakdown}


    def _discover(self, aid, client, chosen, weighted, target, library, pools, add, rng, *, modern, explore_only, mode, budget):
        errors = []
        cooldown = 0
        similar_hits = defaultdict(set)
        def collect(songs, seed_id=None):
            for song in songs:
                if seed_id is not None:
                    similar_hits[song.id].add(seed_id)
                add(song.to_dict(), "similar_discovery" if seed_id is not None else "online_exploration",
                    "与红心曲目相似" if seed_id is not None else "探索发现")

        def enough():
            novel = {_version_key(t) for bucket in ("similar_discovery", "online_exploration")
                     for t in pools[bucket] if explore_only or t["track_key"] not in library}
            return len(novel) >= target

        if client.cookie:
            def discover(reads):
                def fetch(key, fn, *args):
                    cached = self._cached(aid, key) if key else None
                    if cached is not None:
                        return cached
                    source = ("unavailable_source", fn.__name__)
                    failure = self._cached(aid, source)
                    if failure is not None:
                        if failure:
                            errors.append(failure)
                        return []
                    if not reads.remaining:
                        return []
                    try:
                        if not isinstance(client, NeteaseClient):
                            reads.consume()
                        value = fn(*args)
                        if not value and modern:
                            self._remember(aid, source, "", ttl=10)
                        if key:
                            ttl = 86400 if key[0] == "resources" else 1800
                            self._remember(aid, key, value, ttl)
                        return value
                    except ReadBudgetReached:
                        return []
                    except AppError as exc:
                        propagate_risk_control(exc)
                        errors.append(exc.message)
                    except Exception:
                        errors.append("在线推荐源暂时不可用")
                    if modern:
                        self._remember(aid, source, errors[-1], ttl=10)
                    return []

                if modern:
                    # Exhaust usable cached discovery tracks before initiating new HTTP reads.
                    types = {"artist", "sample", "fm"} if explore_only else {"heartbeat", "artist", "sample", "similar", "fm"}
                    with self._cache_lock:
                        available = [(key, value) for (account, key), (expiry, value) in self._cache.items()
                                     if account == aid and expiry > time.monotonic()
                                     and key[0] in types]
                    rng.shuffle(available)
                    for key, value in available:
                        collect(value, key[1] if key[0] == "similar" else None)
                    if enough():
                        return
                    if not explore_only and chosen:
                        owned = self.db.query_view(aid, page=1, page_size=1)["playlists"] if self.db else []
                        owned_ids = {p["id"] for p in owned if p["kind"] in {"liked", "self_created"}}
                        member = next((s for s in chosen + weighted if s.source_playlist_id in owned_ids), None)
                        if member:
                            collect(fetch(("heartbeat", member.id, member.source_playlist_id), client.intelligence_list,
                                          member.id, member.source_playlist_id))
                    favorite_ids = [i for s in chosen for i in s.artist_ids]
                    artist_id = rng.choice(favorite_ids) if favorite_ids else None
                    if artist_id and not enough():
                        collect(fetch(("artist", artist_id), client.artist_top_songs, artist_id))
                    if not enough():
                        sources = []
                        if chosen:
                            sources.append((("similar_lists", chosen[0].id), client.similar_playlists, [chosen[0].id]))
                        sources.append((("resources", beijing_recommend_day()), client.recommend_playlists, []))
                        for key, fn, args in sources:
                            if reads.remaining < 2 and self._cached(aid, key) is None:
                                continue
                            playlists = list(fetch(key, fn, *args))
                            rng.shuffle(playlists)
                            # Rotation skips playlists whose cached sample is already exhausted.
                            pid = next((p["id"] for p in playlists if self._cached(aid, ("sample", p["id"])) is None), None)
                            if pid:
                                collect(fetch(("sample", pid), client.playlist_tracks_sample, pid))
                                break
                if not explore_only:
                    for member in chosen:
                        if enough():
                            break
                        collect(fetch(("similar", member.id), client.similar_songs, member.id), member.id)
                        if modern:
                            break
                if not enough() and reads.remaining:
                    songs = fetch(None, client.personal_fm)
                    self._remember(aid, ("fm",), songs)
                    collect(songs)
            try:
                if budget is None:
                    with recommendation_reads(2 if mode == "refill" or explore_only else 3) as reads:
                        discover(reads)
                else:
                    discover(budget)
            except AppError as exc:
                if exc.code not in {"UPSTREAM_RISK_CONTROL", "RISK_CONTROL_COOLDOWN"}:
                    raise
                cooldown = max(1, math.ceil(client.cooldown_seconds() or 60))
                errors.append(exc.message)

        return errors, cooldown, similar_hits
