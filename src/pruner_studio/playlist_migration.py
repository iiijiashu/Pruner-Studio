"""Cross-platform playlist export (JSON v2 / M3U / TXT) & Migration Assistant with confidence scoring."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
from typing import Any

from .errors import AppError
from .external_sources import TrackQuery, evaluate_match
from .track_identity import canonical_track_key, normalize_canonical_track, normalize_isrc, normalize_mbid, parse_track_key

_LINE_PREFIX_RE = re.compile(r"^\s*(?:\d{1,4}[\.\)\]]\s*|[-*•]\s*)")
_EXTINF_RE = re.compile(r"^#EXTINF:(-?\d+(?:\.\d+)?)\s*,(.*)$", re.IGNORECASE)


def _safe_duration_ms(raw_val: Any) -> int:
    try:
        num = float(raw_val or 0)
        if not math.isfinite(num) or num <= 0:
            return 0
        return min(86_400_000, int(round(num)))
    except (TypeError, ValueError, OverflowError):
        return 0


def export_playlist_content(playlist: dict[str, Any], fmt: str = "json") -> tuple[str, str, str]:
    """Export a playlist to JSON (canonical v2), M3U8, or plain text. Returns (body, mime, ext)."""
    mode = str(fmt or "json").strip().lower()
    name = str(playlist.get("name") or "Playlist").strip()
    songs = [normalize_canonical_track(s) for s in (playlist.get("songs") or []) if isinstance(s, dict)]

    if mode == "json":
        doc = {
            "schema": "pruner-playlist-v2",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "playlist": {
                "id": str(playlist.get("id") or ""),
                "name": name,
                "kind": str(playlist.get("kind") or "hybrid"),
                "track_count": len(songs),
            },
            "tracks": [
                {
                    "track_key": t["track_key"],
                    "provider": t["provider"],
                    "external_id": t["external_id"],
                    "id": t["id"],
                    "title": t["title"],
                    "artists": t["artists"],
                    "album": t["album"],
                    "duration_ms": t["duration_ms"],
                    "isrc": t.get("isrc", ""),
                    "mbid": t.get("mbid", ""),
                    "local_path": t.get("local_path", ""),
                }
                for t in songs
            ],
        }
        return json.dumps(doc, ensure_ascii=False, indent=2), "application/json; charset=utf-8", "json"

    if mode in ("m3u", "m3u8"):
        lines = ["#EXTM3U", f"#PLAYLIST:{name}"]
        for t in songs:
            sec = max(-1, int(round((t.get("duration_ms") or 0) / 1000.0))) if t.get("duration_ms") else -1
            artist = ", ".join(t.get("artists") or [])
            display = f"{artist} - {t['title']}" if artist else t["title"]
            lines.append(f"#EXTINF:{sec},{display}")
            if t.get("track_key"):
                lines.append(f"#EXT-X-PRUNER-KEY:{t['track_key']}")
            if t.get("isrc"):
                lines.append(f"#EXT-X-PRUNER-ISRC:{t['isrc']}")
            lines.append(t.get("local_path") or t.get("track_key") or t["title"])
        return "\n".join(lines) + "\n", "audio/x-mpegurl; charset=utf-8", "m3u8"

    if mode in ("txt", "text"):
        lines = []
        for t in songs:
            artist = ", ".join(t.get("artists") or [])
            lines.append(f"{t['title']} - {artist}" if artist else t["title"])
        return "\n".join(lines) + ("\n" if lines else ""), "text/plain; charset=utf-8", "txt"

    raise AppError("EXPORT_FORMAT_UNSUPPORTED", f"不支持的导出格式：{fmt}", 400)


def parse_playlist_import(content: str, fmt: str = "auto") -> dict[str, Any]:
    """Parse JSON (v2 or legacy list), M3U/M3U8, or plain text playlist into migration source rows."""
    raw = str(content or "").lstrip("\ufeff").strip()
    if not raw:
        raise AppError("IMPORT_EMPTY", "导入内容不能为空", 400)

    mode = str(fmt or "auto").strip().lower()
    if mode == "auto":
        if raw.startswith(("{", "[")):
            mode = "json"
        elif "#EXTM3U" in raw[:128].upper() or "#EXTINF:" in raw[:512].upper():
            mode = "m3u"
        else:
            mode = "txt"

    playlist_name = ""
    entries: list[dict[str, Any]] = []

    if mode == "json":
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise AppError("IMPORT_JSON_INVALID", "JSON 歌单格式无效", 400) from exc
        if isinstance(doc, dict):
            pl_meta = doc.get("playlist") if isinstance(doc.get("playlist"), dict) else {}
            playlist_name = str(pl_meta.get("name") or doc.get("name") or "").strip()
            raw_tracks = doc.get("tracks") or doc.get("songs") or doc.get("items") or []
        elif isinstance(doc, list):
            raw_tracks = doc
        else:
            raise AppError("IMPORT_JSON_INVALID", "无法识别 JSON 歌单结构", 400)
        for item in raw_tracks[:2000]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            if not title:
                continue
            raw_artists = item.get("artists")
            if isinstance(raw_artists, list):
                artists = [str(a.get("name") if isinstance(a, dict) else a).strip() for a in raw_artists if a]
                artists = [a for a in artists if a]
            elif item.get("artist"):
                artists = [str(item["artist"]).strip()]
            else:
                artists = []
            entries.append(
                {
                    "title": title,
                    "artists": artists,
                    "album": str(item.get("album") or "").strip(),
                    "duration_ms": _safe_duration_ms(item.get("duration_ms") or item.get("duration") or 0),
                    "track_key": str(item.get("track_key") or "").strip(),
                    "isrc": normalize_isrc(item.get("isrc")),
                    "mbid": normalize_mbid(item.get("mbid")),
                    "raw_line": f"{title} - {', '.join(artists)}".strip(" -"),
                }
            )
    elif mode in ("m3u", "m3u8"):
        pending_title = ""
        pending_artists: list[str] = []
        pending_dur_ms = 0
        pending_key = ""
        pending_isrc = ""
        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.upper().startswith("#PLAYLIST:"):
                playlist_name = line.split(":", 1)[1].strip()
                continue
            if line.upper().startswith("#EXT-X-PRUNER-KEY:"):
                pending_key = line.split(":", 1)[1].strip()
                continue
            if line.upper().startswith("#EXT-X-PRUNER-ISRC:"):
                pending_isrc = normalize_isrc(line.split(":", 1)[1].strip())
                continue
            m = _EXTINF_RE.match(line)
            if m:
                try:
                    sec = float(m.group(1))
                    pending_dur_ms = _safe_duration_ms(sec * 1000.0) if math.isfinite(sec) and sec > 0 else 0
                except (ValueError, OverflowError):
                    pending_dur_ms = 0
                label = m.group(2).strip()
                if " - " in label:
                    art, tit = [p.strip() for p in label.split(" - ", 1)]
                    pending_artists, pending_title = ([art] if art else []), tit
                else:
                    pending_artists, pending_title = [], label
                continue
            if line.startswith("#"):
                continue
            # Path or URI line finishes current entry
            title = pending_title
            artists = list(pending_artists)
            if not title:
                stem = line.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
                if "." in stem:
                    stem = stem.rsplit(".", 1)[0]
                if " - " in stem:
                    art, tit = [p.strip() for p in stem.split(" - ", 1)]
                    artists, title = ([art] if art else []), tit
                else:
                    title = stem
            entries.append(
                {
                    "title": title,
                    "artists": artists,
                    "album": "",
                    "duration_ms": pending_dur_ms,
                    "track_key": pending_key or (f"local:{line}" if (":\\" in line or line.startswith("/")) else ""),
                    "isrc": pending_isrc,
                    "mbid": "",
                    "raw_line": line,
                }
            )
            pending_title = ""
            pending_artists = []
            pending_dur_ms = 0
            pending_key = ""
            pending_isrc = ""
    else:
        for raw_line in raw.splitlines()[:2000]:
            line = _LINE_PREFIX_RE.sub("", raw_line.strip()).strip()
            if not line or line.startswith("#"):
                continue
            title, artists = line, []
            for sep in (" - ", " – ", " — ", " / ", "\t"):
                if sep in line:
                    left, right = [p.strip() for p in line.split(sep, 1)]
                    if left and right:
                        title, artists = left, [a.strip() for a in re.split(r"[,/&、]", right) if a.strip()]
                        break
            entries.append(
                {
                    "title": title,
                    "artists": artists,
                    "album": "",
                    "duration_ms": 0,
                    "track_key": "",
                    "isrc": "",
                    "mbid": "",
                    "raw_line": raw_line.strip(),
                }
            )

    return {"format": mode, "playlist_name": playlist_name, "entries": entries}


def match_migration_entries(
    entries: list[dict[str, Any]],
    *,
    catalog_tracks: list[dict[str, Any]],
    local_tracks: list[dict[str, Any]],
    online_search_fn: Any = None,
    allow_online: bool = False,
) -> dict[str, Any]:
    """Match imported playlist entries against local + catalog + optional multi-channel online search."""
    pool: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for raw in [*local_tracks, *catalog_tracks]:
        norm = normalize_canonical_track(raw)
        key = norm.get("track_key") or canonical_track_key(norm)
        if key and key not in seen_keys:
            seen_keys.add(key)
            pool.append(norm)

    by_key = {t["track_key"]: t for t in pool if t.get("track_key")}
    by_isrc = {t["isrc"]: t for t in pool if t.get("isrc")}
    by_mbid = {t["mbid"]: t for t in pool if t.get("mbid")}

    results: list[dict[str, Any]] = []
    stats = {"exact": 0, "high": 0, "medium": 0, "unmatched": 0}

    for idx, entry in enumerate(entries):
        matched_track: dict[str, Any] | None = None
        confidence = "unmatched"
        score = 0.0
        reason = ""
        alternatives: list[dict[str, Any]] = []

        # 1. Exact key / ISRC / MBID lookup
        provider, external_id = parse_track_key(entry.get("track_key"))
        entry_key = f"{provider}:{external_id}"
        if entry_key in by_key:
            matched_track = by_key[entry_key]
            confidence, score, reason = "exact", 1.0, "track_key"
        elif entry.get("isrc") and entry["isrc"] in by_isrc:
            matched_track = by_isrc[entry["isrc"]]
            confidence, score, reason = "exact", 1.0, "isrc"
        elif entry.get("mbid") and entry["mbid"] in by_mbid:
            matched_track = by_mbid[entry["mbid"]]
            confidence, score, reason = "exact", 1.0, "mbid"

        # 2. Local & catalog fuzzy/strict metadata match
        if matched_track is None:
            query = TrackQuery(
                title=entry["title"],
                artists=tuple(entry.get("artists") or []),
                album=entry.get("album") or "",
                duration_ms=int(entry.get("duration_ms") or 0),
            )
            scored: list[tuple[bool, float, dict[str, Any]]] = []
            for cand in pool:
                strict, sc = evaluate_match(query, cand["title"], cand["artists"], cand["duration_ms"])
                if not strict and entry.get("artists") and len(entry["artists"]) == 1:
                    # Also handle swapped "Artist - Title" plain-text lines cleanly!
                    swapped_q = TrackQuery(title=entry["artists"][0], artists=(entry["title"],), duration_ms=query.duration_ms)
                    s2, sc2 = evaluate_match(swapped_q, cand["title"], cand["artists"], cand["duration_ms"])
                    if sc2 > sc:
                        strict, sc = s2, sc2
                if sc >= 0.45:
                    scored.append((strict, sc, cand))
            scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            if scored:
                top_strict, top_score, top_cand = scored[0]
                if top_strict or top_score >= 0.82:
                    matched_track = top_cand
                    confidence, score, reason = "high", round(top_score, 3), "library_match"
                elif top_score >= 0.6:
                    matched_track = top_cand
                    confidence, score, reason = "medium", round(top_score, 3), "library_fuzzy"
                alternatives = [c for _, _, c in scored[:5]]

        # 3. Optional online multi-channel search fallback for unmatched items
        if matched_track is None and allow_online and callable(online_search_fn):
            kw = f"{entry['title']} {' '.join(entry.get('artists') or [])}".strip()
            try:
                online_res = online_search_fn(kw, limit=5) or []
            except Exception:
                online_res = []
            query = TrackQuery(
                title=entry["title"],
                artists=tuple(entry.get("artists") or []),
                album=entry.get("album") or "",
                duration_ms=int(entry.get("duration_ms") or 0),
            )
            online_scored: list[tuple[bool, float, dict[str, Any]]] = []
            for raw_cand in online_res[:8]:
                norm_c = normalize_canonical_track(raw_cand)
                strict, sc = evaluate_match(query, norm_c["title"], norm_c["artists"], norm_c["duration_ms"])
                if sc >= 0.4:
                    online_scored.append((strict, sc, norm_c))
            online_scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
            if online_scored:
                top_strict, top_score, top_cand = online_scored[0]
                if top_strict or top_score >= 0.75:
                    matched_track = top_cand
                    confidence, score, reason = ("high" if top_strict else "medium"), round(top_score, 3), "online_match"
                alternatives = [c for _, _, c in online_scored[:5]]

        stats[confidence] += 1
        results.append(
            {
                "index": idx,
                "source": entry,
                "confidence": confidence,
                "score": score,
                "reason": reason,
                "matched_track": matched_track,
                "alternatives": alternatives,
            }
        )

    return {"items": results, "summary": {**stats, "total": len(results)}}
