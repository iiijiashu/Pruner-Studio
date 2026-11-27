from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import random
import time
import unicodedata
from typing import Any, TypeVar

T = TypeVar("T")

COOLDOWN_DAYS = 7.0
DUSTY_GEM_DAYS = 30.0
_SECONDS_PER_DAY = 86_400.0


def _norm_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def _track_id(item: Any) -> int | None:
    raw = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
    if isinstance(raw, bool):
        return None
    try:
        val = int(raw)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _track_key(item: Any) -> str:
    raw = item.get("track_key") if isinstance(item, dict) else getattr(item, "track_key", "")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    prov = (item.get("provider") if isinstance(item, dict) else getattr(item, "provider", "")) or ""
    ext = (item.get("external_id") if isinstance(item, dict) else getattr(item, "external_id", "")) or ""
    if prov and ext:
        return f"{prov}:{ext}"
    sid = _track_id(item)
    return f"netease:{sid}" if sid is not None else ""


def _history_entry(item: Any, history: dict[Any, dict[str, Any]]) -> dict[str, Any] | None:
    if not history:
        return None
    tkey = _track_key(item)
    if tkey and tkey in history:
        return history[tkey]
    sid = _track_id(item)
    if sid is not None and sid in history:
        return history[sid]
    return None


def _track_artists(item: Any) -> list[str]:
    raw = item.get("artists") if isinstance(item, dict) else getattr(item, "artists", None)
    if isinstance(raw, list):
        return [str(a).strip() for a in raw if str(a).strip()]
    if isinstance(raw, str) and raw.strip():
        return [raw.strip()]
    single = item.get("artist") if isinstance(item, dict) else getattr(item, "artist", None)
    if isinstance(single, str) and single.strip():
        return [single.strip()]
    return []


def _primary_artist_key(item: Any) -> str:
    artists = _track_artists(item)
    return _norm_key(artists[0]) if artists else ""


def _album_key(item: Any) -> str:
    raw = item.get("album") if isinstance(item, dict) else getattr(item, "album", "")
    return _norm_key(str(raw or ""))


def parse_timestamp_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
        # Distinguish millisecond epoch from second epoch cleanly.
        if num > 10_000_000_000:
            return num / 1000.0
        return num if num > 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        try:
            return parse_timestamp_seconds(float(text))
        except ValueError:
            return None


def recency_bucket(entry: dict[str, Any] | None, *, now_ts: float | None = None) -> str:
    """Classify a play_history record into unplayed / dusty / fresh / cooldown."""
    if not isinstance(entry, dict):
        return "unplayed"
    play_count = int(entry.get("play_count") or 0)
    last_ts = parse_timestamp_seconds(entry.get("last_played_at"))
    if play_count <= 0 or last_ts is None:
        return "unplayed"
    now = time.time() if now_ts is None else float(now_ts)
    age_days = max(0.0, (now - last_ts) / _SECONDS_PER_DAY)
    if age_days < COOLDOWN_DAYS:
        return "cooldown"
    if age_days > DUSTY_GEM_DAYS:
        return "dusty"
    return "fresh"


def _recency_coordinate_bias(entry: dict[str, Any] | None, *, now_ts: float) -> float:
    """Compute coordinate offset: negative promotes earlier, positive cools down later."""
    if not isinstance(entry, dict):
        return -0.12
    play_count = int(entry.get("play_count") or 0)
    last_ts = parse_timestamp_seconds(entry.get("last_played_at"))
    if play_count <= 0 or last_ts is None:
        return -0.12
    age_days = max(0.0, (now_ts - last_ts) / _SECONDS_PER_DAY)
    if age_days < COOLDOWN_DAYS:
        # Recently played tracks move progressively toward the tail of the round.
        freshness = 1.0 - (age_days / COOLDOWN_DAYS)
        return 0.24 * freshness + 0.04
    if age_days > DUSTY_GEM_DAYS:
        # Dusty gems (>30d unplayed) get a gentle promotion toward the front.
        return -0.10
    return 0.0


def _recency_tier(entry: dict[str, Any] | None, *, now_ts: float) -> int:
    bucket = recency_bucket(entry, now_ts=now_ts)
    if bucket == "unplayed":
        return 0
    if bucket == "dusty":
        return 1
    if bucket == "fresh":
        return 2
    return 3  # cooldown (<7d)


def _dither_within_artist(
    indexed_items: list[tuple[int, Any]],
    rng: random.Random,
    *,
    history: dict[int, dict[str, Any]],
    now_ts: float,
) -> list[tuple[int, Any]]:
    """Order an artist's tracks by recency tier while spreading albums evenly via Fiedler dithering."""
    if len(indexed_items) <= 1:
        return list(indexed_items)

    tier_groups: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for orig_idx, track in indexed_items:
        tier = _recency_tier(_history_entry(track, history), now_ts=now_ts)
        tier_groups[tier].append((orig_idx, track))

    result: list[tuple[int, Any]] = []
    for tier in sorted(tier_groups.keys()):
        group = tier_groups[tier]
        if len(group) <= 2:
            copy = list(group)
            rng.shuffle(copy)
            result.extend(copy)
            continue

        album_buckets: dict[str, list[tuple[int, Any]]] = defaultdict(list)
        for idx, track in group:
            akey = _album_key(track) or f"__no_album_{idx}"
            album_buckets[akey].append((idx, track))

        placed: list[tuple[float, float, int, Any]] = []
        for bucket in album_buckets.values():
            rng.shuffle(bucket)
            n = len(bucket)
            step = 1.0 / n
            phase = rng.uniform(0.0, step)
            for pos_idx, (orig_idx, track) in enumerate(bucket):
                jitter = rng.uniform(-0.35 * step, 0.35 * step)
                coord = (phase + pos_idx * step + jitter) % 1.0
                placed.append((coord, rng.random(), orig_idx, track))

        placed.sort(key=lambda item: (item[0], item[1]))
        result.extend((orig_idx, track) for _c, _t, orig_idx, track in placed)
    return result


def _repair_adjacent_artists(ordered: list[tuple[str, Any]]) -> list[Any]:
    """Eliminate back-to-back tracks by the same primary artist whenever feasible."""
    if len(ordered) <= 2:
        return [track for _artist, track in ordered]

    remaining = list(ordered)
    result: list[tuple[str, Any]] = []
    last_artist = ""

    while remaining:
        chosen = 0
        if last_artist:
            for idx, (artist_key, _track) in enumerate(remaining):
                if not artist_key or artist_key != last_artist:
                    chosen = idx
                    break
        picked = remaining.pop(chosen)
        result.append(picked)
        last_artist = picked[0]

    # Secondary tail repair: if the final track(s) collided with their predecessor
    # because the dominant artist remained at the end, relocate each colliding tail item
    # into the earliest non-adjacent interior gap.
    for _pass in range(len(result)):
        collision_idx = -1
        for i in range(1, len(result)):
            if result[i][0] and result[i][0] == result[i - 1][0]:
                collision_idx = i
                break
        if collision_idx < 0:
            break
        artist_to_move, track_to_move = result[collision_idx]
        moved = False
        for insert_at in range(len(result)):
            if insert_at == collision_idx or insert_at == collision_idx - 1:
                continue
            prev_artist = result[insert_at - 1][0] if insert_at > 0 else ""
            next_artist = result[insert_at][0] if insert_at < len(result) else ""
            if artist_to_move != prev_artist and artist_to_move != next_artist:
                item = result.pop(collision_idx)
                adjusted = insert_at if insert_at < collision_idx else insert_at - 1
                result.insert(adjusted, item)
                moved = True
                break
        if not moved:
            break

    return [track for _artist, track in result]


def balanced_shuffle(
    tracks: list[T],
    play_history: dict[int, dict[str, Any]] | None = None,
    *,
    seed: int | None = None,
    now_ts: float | None = None,
) -> list[T]:
    """Martin Fiedler Balanced Dithered Shuffle with Play Recency Cooldown & Artist Spread.

    - Buckets by ``primary_artist`` -> ``album`` and evenly dithers coordinates across ``[0, 1)``.
    - Downweights tracks played within the last 7 days (cooldown) and boosts Dusty Gems
      (>30 days unplayed or never played) without dropping or repeating any track.
    - Guarantees zero back-to-back tracks by the same primary artist whenever mathematically feasible.
    - Pass ``seed`` for deterministic testing.
    """
    if len(tracks) <= 1:
        return list(tracks)

    rng = random.Random(seed)
    now = time.time() if now_ts is None else float(now_ts)
    history = play_history or {}

    artist_buckets: dict[str, list[tuple[int, T]]] = defaultdict(list)
    artist_key_by_index: dict[int, str] = {}

    for idx, track in enumerate(tracks):
        akey = _primary_artist_key(track)
        artist_key_by_index[idx] = akey
        bucket_key = akey if akey else f"__solo_{idx}"
        artist_buckets[bucket_key].append((idx, track))

    scheduled: list[tuple[float, float, str, T]] = []
    for bucket_key, items in artist_buckets.items():
        ordered_bucket = _dither_within_artist(items, rng, history=history, now_ts=now)
        n = len(ordered_bucket)
        step = 1.0 / n
        phase = rng.uniform(0.0, step)
        scored_members: list[tuple[float, int, T]] = []
        for pos_idx, (orig_idx, track) in enumerate(ordered_bucket):
            entry = _history_entry(track, history)
            bias = _recency_coordinate_bias(entry, now_ts=now)
            base_coord = phase + pos_idx * step + rng.uniform(-0.28 * step, 0.28 * step)
            scored_members.append((base_coord + bias, orig_idx, track))

        for final_coord, orig_idx, track in scored_members:
            scheduled.append((final_coord, rng.random(), artist_key_by_index[orig_idx], track))

    scheduled.sort(key=lambda entry: (entry[0], entry[1]))
    return _repair_adjacent_artists([(akey, track) for _c, _t, akey, track in scheduled])
