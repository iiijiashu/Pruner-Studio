"""Platform-agnostic canonical track identity (provider:external_id) and metadata normalization."""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .models import Song

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_COMPOSITE_ROOTS = frozenset({"gdstudio", "resolver", "resolver_v2"})
_COMPOSITE_RE = re.compile(r"(gdstudio|resolver|resolver_v2)[:.]([a-z0-9][a-z0-9_.-]{0,47})")
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
_MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def normalize_provider(value: Any, default: str = "netease") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    if raw == "local_library":
        return "local"
    if raw == "custom_endpoint":
        return "custom"
    match = _COMPOSITE_RE.fullmatch(raw)
    if match:
        return ":".join(match.groups())
    head, separator, _ = raw.partition(".")
    if ":" in raw or (separator and head in _COMPOSITE_ROOTS):
        return default
    cleaned = re.sub(r"[^a-z0-9_.-]", "", raw)
    return cleaned[:32] if cleaned and _PROVIDER_RE.match(cleaned) else default


def normalize_isrc(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(value or "").strip()).upper()
    return cleaned if _ISRC_RE.match(cleaned) else ""


def normalize_mbid(value: Any) -> str:
    cleaned = str(value or "").strip().lower()
    return cleaned if _MBID_RE.match(cleaned) else ""


def make_track_key(provider: str, external_id: str | int) -> str:
    prov = normalize_provider(provider)
    ext = str(external_id or "").strip()
    if not ext:
        raise ValueError("external_id must not be empty")
    return f"{prov}:{ext}"


def parse_track_key(track_key: Any) -> tuple[str, str]:
    raw = str(track_key or "").strip()
    if not raw:
        return "", ""
    if ":" not in raw:
        if raw.isdigit() and int(raw) > 0:
            return "netease", raw
        return "", ""
    provider, ext = raw.split(":", 1)
    if provider.lower() in _COMPOSITE_ROOTS and ":" in ext:
        sub, ext = ext.split(":", 1)
        provider = f"{provider}:{sub}"
    prov = normalize_provider(provider, default="")
    ext = ext.strip()
    if not prov or not ext:
        return "", ""
    return prov, ext


def is_valid_track_key(track_key: Any) -> bool:
    prov, ext = parse_track_key(track_key)
    return bool(prov and ext)


def canonical_track_key(track: Any) -> str:
    """Resolve a deterministic canonical `provider:external_id` key from a Song or dict."""
    if isinstance(track, Song):
        if track.track_key and is_valid_track_key(track.track_key):
            return ":".join(parse_track_key(track.track_key))
        if track.provider and track.provider != "netease" and track.external_id:
            return make_track_key(track.provider, track.external_id)
        if track.local_path:
            return f"local:{track.local_path}"
        if track.id and int(track.id) > 0:
            return f"netease:{int(track.id)}"
        return ""

    if not isinstance(track, dict):
        return ""

    explicit = str(track.get("track_key") or "").strip()
    if explicit:
        prov, ext = parse_track_key(explicit)
        return f"{prov}:{ext}" if prov and ext else ""

    local_path = str(track.get("local_path") or track.get("path") or "").strip()
    provider_raw = str(track.get("provider") or track.get("source_provider") or track.get("source") or "").strip()
    provider = normalize_provider(provider_raw, default="") if provider_raw else ""

    if provider == "local" or (not provider and local_path):
        ext = local_path or str(track.get("external_id") or track.get("file_id") or "").strip()
        if ext:
            return f"local:{ext}"

    external_id = str(track.get("external_id") or track.get("provider_track_id") or "").strip()
    if provider and provider != "netease" and external_id:
        return make_track_key(provider, external_id)

    raw_id = track.get("id", track.get("song_id"))
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
        return f"netease:{raw_id}"
    if isinstance(raw_id, str) and raw_id.strip().isdigit() and int(raw_id.strip()) > 0:
        return f"netease:{int(raw_id.strip())}"

    if external_id:
        return make_track_key(provider or "external", external_id)

    title = str(track.get("title") or track.get("name") or "").strip()
    if title:
        artists = track.get("artists")
        if isinstance(artists, list):
            artist_text = ",".join(str(a).strip() for a in artists if str(a).strip())
        else:
            artist_text = str(track.get("artist") or "").strip()
        digest = hashlib.sha256(f"{title.lower()}|{artist_text.lower()}".encode("utf-8")).hexdigest()[:20]
        return f"{provider or 'external'}:{digest}"
    return ""


def normalize_canonical_track(raw: dict[str, Any] | Song) -> dict[str, Any]:
    """Produce a normalized canonical track dictionary compatible with legacy NetEase & multi-source paths."""
    src = raw.to_dict() if isinstance(raw, Song) else dict(raw or {})
    tkey = canonical_track_key(src)
    prov, ext = parse_track_key(tkey)
    if not prov:
        prov = normalize_provider(src.get("provider") or src.get("source_provider") or "netease")

    raw_artists = src.get("artists")
    if isinstance(raw_artists, list):
        artists = [str(a).strip() for a in raw_artists if str(a).strip()]
    elif isinstance(src.get("artist"), str) and src["artist"].strip():
        artists = [src["artist"].strip()]
    else:
        artists = []

    raw_id = src.get("id", src.get("song_id", 0))
    try:
        song_id = int(raw_id) if raw_id is not None and not isinstance(raw_id, bool) else 0
    except (TypeError, ValueError):
        song_id = 0
    if prov == "netease" and song_id <= 0 and ext.isdigit():
        song_id = int(ext)
    if prov != "netease" and song_id < 0:
        song_id = 0

    duration = src.get("duration")
    if duration is None:
        duration = src.get("duration_ms", 0)
    try:
        duration_ms = max(0, int(duration or 0))
    except (TypeError, ValueError):
        duration_ms = 0

    title = str(src.get("title") or src.get("name") or "").strip()
    album = str(src.get("album") or "").strip()
    cover_url = str(src.get("cover_url") or "").strip()
    if not cover_url and src.get("cover_id"):
        cover_url = f"/api/local-cover/{src['cover_id']}"

    local_path = str(src.get("local_path") or src.get("path") or (ext if prov == "local" else "")).strip()
    isrc = normalize_isrc(src.get("isrc"))
    mbid = normalize_mbid(src.get("mbid"))

    out = dict(src)
    out.update(
        {
            "id": song_id,
            "song_id": song_id,
            "track_key": tkey,
            "provider": prov,
            "external_id": ext or (str(song_id) if song_id > 0 else ""),
            "title": title,
            "artists": artists,
            "artist": ", ".join(artists),
            "album": album,
            "duration": duration_ms,
            "duration_ms": duration_ms,
            "cover_url": cover_url,
            "local_path": local_path,
            "isrc": isrc,
            "mbid": mbid,
        }
    )
    return out


def candidate_identity_key(provider: str, external_id: str | int) -> str:
    """Stable blocklist key for a candidate resolution source (provider:external_id)."""
    prov = normalize_provider(provider, default="external")
    ext = str(external_id or "").strip()
    return f"{prov}:{ext}" if ext else prov
