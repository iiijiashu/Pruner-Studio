from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import secrets
import threading
import time
import unicodedata
from typing import Any, Callable

from .catalog_service import CatalogService
from .errors import propagate_risk_control, AppError
from .local_metadata import compute_loudness_compensation
from .track_identity import canonical_track_key, candidate_identity_key
from .external_sources import (
    DEFAULT_EXTERNAL_PROVIDERS,
    ExternalAudioResolver,
    ExternalCandidate,
    LocalTrackMatch,
    TrackQuery,
    evaluate_match,
    is_verified_lossless,
    mime_type_for_format,
    sanitize_outbound_headers,
    score_audio_quality,
)
from .models import Song
from .netease_client import NeteaseClient, UrllibTransport
from .netease_crypto import normalize_quality_level

_UNSUPPORTED_BROWSER_FORMATS = frozenset({"dolby", "ac3", "eac3", "ec3", "alac", "ape", "dsd", "dsf", "dff", "wma"})
_FALLBACK_QUALITY = {
    "sky": "lossless",
    "jymaster": "exhigh",
    "hires": "exhigh",
    "jyeffect": "exhigh",
    "lossless": "exhigh",
}


def normalize_credit(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def is_strict_match(source: Song, candidate: Song, duration_tolerance_ms: int) -> bool:
    if normalize_credit(source.title) != normalize_credit(candidate.title):
        return False
    source_artist = normalize_credit(source.artists[0]) if source.artists else ""
    candidate_artist = normalize_credit(candidate.artists[0]) if candidate.artists else ""
    if not source_artist or source_artist != candidate_artist:
        return False
    if source.duration <= 0 or candidate.duration <= 0:
        return False
    return abs(source.duration - candidate.duration) <= duration_tolerance_ms


_stream_generation = ContextVar("stream_generation", default=None)


@contextmanager
def stream_session(generation):
    token = _stream_generation.set(generation)
    try:
        yield
    finally:
        _stream_generation.reset(token)


@dataclass(frozen=True)
class StreamTarget:
    token: str
    kind: str  # "local" | "remote"
    path: Path | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "audio/mpeg"
    format: str = ""
    provider: str = ""
    allow_cookies: bool = False
    allow_self_hosted_origin: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    cache_key: str = ""
    session_generation: int | None = None


def _is_browser_playable_format(audio_format: str) -> bool:
    cleaned = (audio_format or "").strip().lower()
    if not cleaned:
        return True
    return cleaned not in _UNSUPPORTED_BROWSER_FORMATS


def self_hosted_cache_scope(secret):
    import json
    fields = ("api_base", "username", "userid", "password", "api_key", "token", "updated_at")
    values = [str(secret.get(field) or "") for field in fields]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _session_cache_key(cookie: str | None) -> str:
    if not cookie:
        return "anon"
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:16]


def _payload_quality_score(payload: dict[str, Any]) -> int:
    return score_audio_quality(
        format_name=str(payload.get("format") or ""),
        level=str(payload.get("level") or ""),
        bitrate=int(payload.get("bitrate") or 0),
        sample_rate=int(payload.get("sample_rate") or 0),
        bit_depth=int(payload.get("bit_depth") or 0),
    )


class AudioService:
    """Unified multi-tier VIP + Cloud + In-NetEase + Local + Multi-Channel Best-Quality Audio Arbitrator."""

    def __init__(
        self,
        client: NeteaseClient,
        catalog: CatalogService,
        duration_tolerance_ms: int = 5_000,
        *,
        settings_provider: Callable[[], dict[str, Any]] | Any | None = None,
        external_resolver: ExternalAudioResolver | None = None,
        db: Any = None,
        credential_vault: Any = None,
        cache_ttl_sec: float = 600.0,
        cache_max_entries: int = 200,
        token_ttl_sec: float = 1_800.0,
        token_max_entries: int = 100,
        cloud_cache_ttl_sec: float = 300.0,
    ):
        self.client = client
        self.catalog = catalog
        self.db = db
        self.credential_vault = credential_vault
        self.duration_tolerance_ms = duration_tolerance_ms
        self._settings_provider = settings_provider
        self._has_custom_external_resolver = external_resolver is not None
        if external_resolver is not None:
            self.external_resolver = external_resolver
            if credential_vault is not None and self.external_resolver.credential_vault is None:
                self.external_resolver.credential_vault = credential_vault
        else:
            self.external_resolver = ExternalAudioResolver(db=db, credential_vault=credential_vault)
        self._cache_ttl_sec = cache_ttl_sec
        self._cache_max_entries = cache_max_entries
        self._token_ttl_sec = token_ttl_sec
        self._token_max_entries = token_max_entries
        self._cloud_cache_ttl_sec = cloud_cache_ttl_sec

        self._lock = threading.RLock()
        self._audio_cache: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()
        self._stream_tokens: OrderedDict[str, StreamTarget] = OrderedDict()
        self._cloud_cache: dict[str, tuple[float, list[Song]]] = {}
        self._candidate_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def clear_resolution_caches(self) -> None:
        self.clear_caches(include_tokens=False)

    def clear_caches(self, include_tokens: bool = True) -> None:
        with self._lock:
            self._audio_cache.clear()
            if include_tokens:
                self._stream_tokens.clear()
            self._cloud_cache.clear()
            self._candidate_cache.clear()

    def scan_local_library(self, dirs: list[str | Path] | None = None) -> dict[str, Any]:
        target_dirs = dirs
        if target_dirs is None:
            settings = self._read_settings()
            raw_dirs = settings.get("local_music_dirs")
            target_dirs = list(raw_dirs) if isinstance(raw_dirs, list) else []
        result = self.external_resolver.scan_local_dirs(target_dirs)
        with self._lock:
            self._audio_cache.clear()
        return result

    def register_stream_token(
        self,
        *,
        kind: str,
        path: Path | None = None,
        url: str = "",
        headers: dict[str, str] | None = None,
        content_type: str = "audio/mpeg",
        format_name: str = "",
        provider: str = "",
        cache_key: str = "",
        allow_self_hosted_origin: str = "",
        session_generation: int | None = None,
    ) -> StreamTarget:
        now = time.monotonic()
        token = secrets.token_urlsafe(24)
        if kind == "remote":
            clean_headers = sanitize_outbound_headers(headers)
            if allow_self_hosted_origin and headers:
                for k, v in headers.items():
                    if k.lower() == "authorization" and v and "\r" not in str(v) and "\n" not in str(v):
                        clean_headers["Authorization"] = str(v)
            clean_headers.pop("Cookie", None)
            clean_headers.pop("cookie", None)
        else:
            clean_headers = {}
        target = StreamTarget(
            token=token,
            kind=kind,
            path=path,
            url=url,
            headers=clean_headers,
            content_type=content_type or "audio/mpeg",
            format=format_name,
            provider=provider,
            cache_key=cache_key,
            session_generation=_stream_generation.get() if session_generation is None else session_generation,
            allow_cookies=False,
            allow_self_hosted_origin=allow_self_hosted_origin,
            created_at=now,
            expires_at=now + self._token_ttl_sec,
        )
        with self._lock:
            self._prune_expired_tokens_locked(now)
            self._stream_tokens[token] = target
            self._stream_tokens.move_to_end(token)
            while len(self._stream_tokens) > self._token_max_entries:
                self._stream_tokens.popitem(last=False)
        return target

    def revoke_stream_token(self, token: str):
        with self._lock:
            self._stream_tokens.pop(token, None)

    def resolve_stream_token(self, token: str, expected_generation: int | None = None) -> StreamTarget | None:
        if not token or not isinstance(token, str):
            return None
        now = time.monotonic()
        with self._lock:
            target = self._stream_tokens.get(token)
            if target is None:
                return None
            if now >= target.expires_at or (expected_generation is not None and target.session_generation != expected_generation):
                self._stream_tokens.pop(token, None)
                return None
            self._stream_tokens.move_to_end(token)
            return target

    def _prune_expired_tokens_locked(self, now: float) -> None:
        expired = [tok for tok, entry in self._stream_tokens.items() if now >= entry.expires_at]
        for tok in expired:
            self._stream_tokens.pop(tok, None)

    def _read_settings(self) -> dict[str, Any]:
        provider = self._settings_provider
        if provider is None:
            return {}
        try:
            if callable(provider):
                value = provider()
            else:
                loader = getattr(provider, "load", None) or getattr(provider, "get", None)
                value = loader() if callable(loader) else {}
            return dict(value) if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _can_call_external_network(self, active_client: NeteaseClient, explicit_allow: bool | None) -> bool:
        if explicit_allow is False:
            return False
        if explicit_allow is True:
            return True
        if self._has_custom_external_resolver:
            return True
        if isinstance(active_client.transport, UrllibTransport):
            return True
        if getattr(self.external_resolver.resolve, "__func__", None) is not ExternalAudioResolver.resolve:
            return True
        if getattr(self.external_resolver.search_candidates, "__func__", None) is not ExternalAudioResolver.search_candidates:
            return True
        return False

    def _lookup_song(
        self,
        song_id: int,
        active_client: NeteaseClient | None = None,
        *,
        account_id: str | None = None,
    ) -> Song | None:
        if self.db is not None:
            getter = getattr(self.db, "get_song", None)
            if callable(getter):
                candidate_accounts: list[str | None] = []
                for aid in (account_id, getattr(self.catalog, "_active_account_id", None), "local", None):
                    if aid not in candidate_accounts:
                        candidate_accounts.append(aid)
                for aid in candidate_accounts:
                    try:
                        found = getter(song_id, account_id=aid)
                        if isinstance(found, Song):
                            return found
                    except Exception:
                        continue
        if self.catalog is not None:
            found = self.catalog.song(song_id)
            if found is not None:
                return found
        if active_client is not None:
            detail_fn = getattr(active_client, "song_detail", None)
            if callable(detail_fn):
                try:
                    return detail_fn(song_id)
                except (AppError, AssertionError) as exc:
                    propagate_risk_control(exc)
                    return None
        return None

    def _get_cached_audio(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._audio_cache.get(key)
            if entry is None:
                return None
            expires_at, payload = entry
            if now >= expires_at:
                self._audio_cache.pop(key, None)
                return None
            stream_token = payload.get("stream_token")
            if isinstance(stream_token, str) and stream_token:
                target = self._stream_tokens.get(stream_token)
                if target is None or now >= target.expires_at:
                    self._audio_cache.pop(key, None)
                    return None
            self._audio_cache.move_to_end(key)
            return dict(payload)

    def _put_cached_audio(self, key: tuple[Any, ...], payload: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            self._audio_cache[key] = (now + self._cache_ttl_sec, dict(payload))
            self._audio_cache.move_to_end(key)
            while len(self._audio_cache) > self._cache_max_entries:
                self._audio_cache.popitem(last=False)

    def _proxy_netease(self, payload, song_id, requested_level):
        url = payload["url"]
        level = payload.get("level") or requested_level
        fmt = str(payload.get("format") or "mp3")
        target = self.register_stream_token(kind="remote", url=url, format_name=fmt,
                                            content_type=mime_type_for_format(fmt), provider="netease",
                                            cache_key=f"netease:{song_id}:{level}")
        return {**payload, "url": f"/api/stream/{target.token}", "direct_url": url, "stream_token": target.token}

    def _resolve_netease_track(
        self,
        active_client: NeteaseClient,
        song_id: int,
        preferred_level: str,
    ) -> dict[str, Any] | None:
        first_attempt: dict[str, Any] | None = None
        try:
            first_attempt = active_client.audio_url(song_id, level=preferred_level, allow_free_trial=True)
        except AppError as exc:
            propagate_risk_control(exc)
            first_attempt = None

        if (
            first_attempt is not None
            and not first_attempt.get("free_trial")
            and _is_browser_playable_format(str(first_attempt.get("format") or ""))
        ):
            return self._proxy_netease(first_attempt, song_id, preferred_level)

        fallback_level = _FALLBACK_QUALITY.get(preferred_level)
        if not fallback_level:
            return None
        try:
            second_attempt = active_client.audio_url(song_id, level=fallback_level, allow_free_trial=True)
        except AppError as exc:
            propagate_risk_control(exc)
            return None
        if (
            second_attempt is not None
            and not second_attempt.get("free_trial")
            and _is_browser_playable_format(str(second_attempt.get("format") or ""))
        ):
            return self._proxy_netease(second_attempt, song_id, fallback_level)
        return None

    def _get_cloud_songs(self, active_client: NeteaseClient) -> list[Song]:
        if not active_client.cookie:
            return []
        session_key = _session_cache_key(active_client.cookie)
        now = time.monotonic()
        with self._lock:
            cached = self._cloud_cache.get(session_key)
            if cached and now < cached[0]:
                return list(cached[1])
        try:
            songs = active_client.cloud_songs(limit=500, offset=0)
        except (AppError, AssertionError) as exc:
            propagate_risk_control(exc)
            return []
        with self._lock:
            self._cloud_cache[session_key] = (now + self._cloud_cache_ttl_sec, list(songs))
            if len(self._cloud_cache) > 4:
                oldest = min(self._cloud_cache, key=lambda key: self._cloud_cache[key][0])
                self._cloud_cache.pop(oldest, None)
        return songs

    def _try_cloud_disk(
        self,
        active_client: NeteaseClient,
        song_id: int,
        source: Song | None,
        preferred_level: str,
    ) -> dict[str, Any] | None:
        if not active_client.cookie:
            return None
        cloud_songs = self._get_cloud_songs(active_client)
        cloud_match = next((item for item in cloud_songs if item.id == song_id), None)
        if cloud_match is None and source is not None:
            cloud_match = next(
                (item for item in cloud_songs if is_strict_match(source, item, self.duration_tolerance_ms)),
                None,
            )
        if cloud_match is None:
            return None
        cloud_audio = self._resolve_netease_track(active_client, cloud_match.id, preferred_level)
        if not cloud_audio:
            return None
        return {
            **cloud_audio,
            "is_alternative": cloud_match.id != song_id,
            "source": "cloud",
            "source_provider": "netease_cloud",
            "alternative_info": cloud_match.to_dict() if cloud_match.id != song_id else None,
        }

    def _try_netease_alternative(
        self,
        active_client: NeteaseClient,
        source: Song,
        preferred_level: str,
    ) -> dict[str, Any] | None:
        query = " ".join([source.title, source.artists[0] if source.artists else ""]).strip()
        if not query:
            return None
        try:
            netease_candidates = active_client.search_songs(query)
        except (AppError, AssertionError) as exc:
            propagate_risk_control(exc)
            return None
        for candidate in netease_candidates:
            if candidate.id == source.id or not is_strict_match(source, candidate, self.duration_tolerance_ms):
                continue
            audio = self._resolve_netease_track(active_client, candidate.id, preferred_level)
            if audio:
                return {
                    **audio,
                    "is_alternative": True,
                    "source": "alternative",
                    "source_provider": "netease",
                    "alternative_info": candidate.to_dict(),
                }
        return None

    def _build_local_payload(self, source: Song, match: LocalTrackMatch) -> dict[str, Any]:
        mime = mime_type_for_format(match.format, match.path)
        target = self.register_stream_token(
            kind="local",
            path=match.path,
            content_type=mime,
            format_name=match.format,
            provider="local_library",
        )
        stream_url = f"/api/stream/{target.token}"
        try:
            size = match.path.stat().st_size
        except OSError:
            size = 0
        loudness = compute_loudness_compensation(
            getattr(match.record, "replaygain_track_gain_db", None),
            getattr(match.record, "replaygain_track_peak", None),
            album_gain_db=getattr(match.record, "replaygain_album_gain_db", None),
            album_peak=getattr(match.record, "replaygain_album_peak", None),
        )
        cover_id = getattr(match.record, "cover_id", "")
        return {
            "id": source.id,
            "track_key": source.resolved_track_key() if hasattr(source, "resolved_track_key") else f"local:{match.path}",
            "url": stream_url,
            "stream_url": stream_url,
            "stream_token": target.token,
            "level": "lossless" if match.format in {"flac", "wav"} else "local",
            "format": match.format,
            "bitrate": match.bitrate,
            "sample_rate": match.sample_rate,
            "size": size,
            "time_ms": match.duration_ms or source.duration,
            "free_trial": False,
            "is_alternative": True,
            "source": "local",
            "source_provider": "local_library",
            "cover_url": f"/api/local-cover/{cover_id}" if cover_id else "",
            "loudness": loudness,
            "match_score": round(match.match_score, 3),
            "alternative_info": {
                "id": source.id,
                "title": match.title,
                "artists": [match.artist] if match.artist else list(source.artists),
                "album": match.album,
                "duration": match.duration_ms,
                "duration_ms": match.duration_ms,
                "file_name": match.path.name,
            },
        }

    def _build_external_payload(
        self,
        source: Song,
        candidate: ExternalCandidate,
        *,
        force_proxy: bool = False,
        is_alternative: bool = True,
        external_id: str = "",
    ) -> dict[str, Any]:
        stable_id = external_id or candidate.provider_track_id or source.id
        needs_proxy = bool(
            force_proxy or candidate.needs_proxy or candidate.headers or not candidate.url.startswith("https://")
        )
        if candidate.source_provider in {"opensubsonic", "webdav"}:
            stable_id = f"{candidate.extra['cache_scope']}:{stable_id}"
        token_value: str | None = None
        if needs_proxy:
            content_type = str(candidate.extra.get("content_type") or mime_type_for_format(candidate.format))
            target = self.register_stream_token(
                kind="remote",
                url=candidate.url,
                headers=candidate.headers,
                content_type=content_type,
                format_name=candidate.format,
                provider=candidate.source_provider,
                cache_key=(f"{candidate.source_provider}:{stable_id}:"
                           f"{candidate.level or candidate.format}:{candidate.bitrate}"
                           if stable_id else ""),
                allow_self_hosted_origin=str(candidate.extra.get("allow_self_hosted_origin") or ""),
            )
            token_value = target.token
            playback_url = f"/api/stream/{target.token}"
        else:
            playback_url = candidate.url

        if candidate.level:
            level = candidate.level
        elif candidate.format == "flac":
            level = "lossless"
        elif candidate.bitrate >= 300_000:
            level = "exhigh"
        else:
            level = "standard"

        loudness = compute_loudness_compensation(
            candidate.extra.get("replaygain_track_gain_db"),
            candidate.extra.get("replaygain_track_peak"),
        )
        result: dict[str, Any] = {
            "id": source.id,
            "track_key": canonical_track_key(source) or f"{candidate.source_provider}:{stable_id}",
            "url": playback_url,
            "stream_url": playback_url,
            "level": level,
            "format": candidate.format,
            "bitrate": candidate.bitrate,
            "sample_rate": candidate.sample_rate,
            "size": candidate.size,
            "time_ms": candidate.duration_ms or source.duration,
            "free_trial": False,
            "is_alternative": is_alternative,
            "source": "external",
            "source_provider": candidate.source_provider,
            "candidate_key": candidate_identity_key(candidate.source_provider, candidate.provider_track_id),
            "loudness": loudness,
            "match_score": round(candidate.match_score, 3),
            "alternative_info": {
                "id": source.id,
                "title": candidate.title,
                "artists": candidate.artists,
                "album": candidate.album,
                "duration": candidate.duration_ms,
                "duration_ms": candidate.duration_ms,
                "provider": candidate.source_provider,
            },
        }
        if token_value is not None:
            result["stream_token"] = token_value
        return result

    def _blocked_candidate_set(self, account_id: str | None, track_key: str) -> set[str]:
        if self.db is None or not track_key or not hasattr(self.db, "list_blocked_candidates"):
            return set()
        try:
            rows = self.db.list_blocked_candidates(str(account_id or "local"), track_key)
            return {str(r.get("candidate_key") or "").strip() for r in rows if r.get("candidate_key")}
        except Exception:
            return set()

    def _is_payload_blocked(self, payload: dict[str, Any], blocked: set[str]) -> bool:
        if not blocked:
            return False
        prov = str(payload.get("source_provider") or "").strip()
        ckey = str(payload.get("candidate_key") or "").strip()
        if ckey and ckey in blocked:
            return True
        if prov and prov in blocked:
            return True
        return False

    def _remember_source_route(self, account_id: str | None, track_key: str, payload: dict[str, Any]) -> None:
        if self.db is None or not track_key or not hasattr(self.db, "save_track_source_route"):
            return
        prov = str(payload.get("source_provider") or "").strip()
        if not prov or prov == "netease":
            return
        try:
            ckey = str(payload.get("candidate_key") or "")
            ext_id = ckey.split(":", 1)[1] if ":" in ckey else ""
            self.db.save_track_source_route(
                str(account_id or "local"),
                track_key,
                provider=prov,
                external_id=ext_id,
                quality=str(payload.get("level") or ""),
                format_name=str(payload.get("format") or ""),
                bitrate=int(payload.get("bitrate") or 0),
                sample_rate=int(payload.get("sample_rate") or 0),
            )
        except Exception:
            pass

    def _race_higher_quality(
        self,
        *,
        song_id: int,
        source: Song,
        active_client: NeteaseClient,
        preferred_level: str,
        allow_external_net: bool,
        enabled_providers: list[str],
        custom_api: str,
        include_netease_alt: bool,
        blocked_candidates: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Concurrently gather verified full-track alternatives from Cloud, Local, NetEase Alt, and Multi-Channel."""
        blocked = blocked_candidates or set()
        contenders: list[dict[str, Any]] = []

        # Local lossless check is fast in-memory/disk; run immediately.
        local_match = self.external_resolver.match_local(source, self.duration_tolerance_ms)
        if local_match is not None:
            local_payload = self._build_local_payload(source, local_match)
            if not self._is_payload_blocked(local_payload, blocked):
                contenders.append(local_payload)

        tasks: list[Callable[[], dict[str, Any] | None]] = []
        if active_client.cookie:
            tasks.append(lambda: self._try_cloud_disk(active_client, song_id, source, preferred_level))
        if include_netease_alt:
            tasks.append(lambda: self._try_netease_alternative(active_client, source, preferred_level))
        if allow_external_net:
            def _ext_task() -> dict[str, Any] | None:
                kwargs: dict[str, Any] = {
                    "enabled_providers": enabled_providers,
                    "custom_api": custom_api,
                    "duration_tolerance_ms": self.duration_tolerance_ms,
                    "verify_stream": True,
                    "best_quality_race": True,
                }
                if blocked:
                    kwargs["blocked_candidates"] = blocked
                ext_match = self.external_resolver.resolve(source, **kwargs)
                if ext_match is None:
                    return None
                payload = self._build_external_payload(source, ext_match)
                return None if self._is_payload_blocked(payload, blocked) else payload
            tasks.append(_ext_task)

        if len(tasks) == 1:
            try:
                res = tasks[0]()
                if res is not None and not self._is_payload_blocked(res, blocked):
                    contenders.append(res)
            except Exception as exc:
                propagate_risk_control(exc)
                pass
        elif len(tasks) > 1:
            with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
                futures = [executor.submit(copy_context().run, fn) for fn in tasks]
                for fut in futures:
                    try:
                        res = fut.result(timeout=5.5)
                        if res is not None and not self._is_payload_blocked(res, blocked):
                            contenders.append(res)
                    except Exception as exc:
                        propagate_risk_control(exc)
                        continue
        return contenders

    def resolve(
        self,
        song_id: int,
        *,
        quality: str | None = None,
        allow_external: bool | None = None,
        force_refresh: bool = False,
        candidate_id: str | None = None,
        song_hint: Song | None = None,
        client_override: NeteaseClient | None = None,
        quality_arbitration: str | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        active_client = client_override or self.client
        effective_account_id = account_id or getattr(self.catalog, "_active_account_id", None)
        settings = self._read_settings()
        preferred_level = normalize_quality_level(
            quality or settings.get("quality_preference"),
            default="jymaster",
        )
        external_enabled = (
            bool(allow_external)
            if allow_external is not None
            else bool(settings.get("enable_external_sources", True))
        )
        arbitration = str(
            quality_arbitration or settings.get("quality_arbitration") or "best_quality_race"
        ).strip().lower()
        if arbitration not in {"best_quality_race", "netease_first"}:
            arbitration = "best_quality_race"

        raw_providers = settings.get("external_providers")
        if isinstance(raw_providers, list):
            enabled_providers = [str(item) for item in raw_providers if str(item).strip()]
        else:
            enabled_providers = list(DEFAULT_EXTERNAL_PROVIDERS)
        custom_api = str(settings.get("custom_external_api") or "").strip()

        if candidate_id:
            return self._resolve_specific_candidate(
                song_id,
                candidate_id,
                preferred_level=preferred_level,
                song_hint=song_hint,
                active_client=active_client,
                custom_api=custom_api,
            )

        track_key = f"netease:{song_id}"
        blocked_candidates = self._blocked_candidate_set(effective_account_id, track_key)

        cache_key = (
            _stream_generation.get(),
            effective_account_id,
            song_id,
            preferred_level,
            external_enabled,
            arbitration,
            _session_cache_key(active_client.cookie),
            tuple(sorted(blocked_candidates)),
        )
        if not force_refresh:
            cached = self._get_cached_audio(cache_key)
            if cached is not None:
                return cached

        allow_ext_net = external_enabled and self._can_call_external_network(active_client, allow_external)

        # Tier 1: Original NetEase VIP/SVIP resolution.
        original = self._resolve_netease_track(active_client, song_id, preferred_level)
        if original:
            orig_payload: dict[str, Any] = {
                **original,
                "track_key": track_key,
                "is_alternative": False,
                "source": "original",
                "source_provider": "netease",
                "loudness": compute_loudness_compensation(None, None),
                "alternative_info": None,
            }
            if is_verified_lossless(original) or arbitration == "netease_first":
                self._put_cached_audio(cache_key, orig_payload)
                return orig_payload

            orig_br = int(original.get("bitrate") or 0)
            source_for_race = song_hint or self._lookup_song(song_id, active_client, account_id=effective_account_id)
            if source_for_race is not None and orig_br < 500_000:
                contenders = self._race_higher_quality(
                    song_id=song_id,
                    source=source_for_race,
                    active_client=active_client,
                    preferred_level=preferred_level,
                    allow_external_net=allow_ext_net,
                    enabled_providers=enabled_providers,
                    custom_api=custom_api,
                    include_netease_alt=False,
                    blocked_candidates=blocked_candidates,
                )
                orig_score = _payload_quality_score(orig_payload)
                best_upgrade: dict[str, Any] | None = None
                best_score = orig_score
                for cand in contenders:
                    cand_score = _payload_quality_score(cand)
                    if cand_score > best_score + 100:
                        best_score = cand_score
                        best_upgrade = cand
                if best_upgrade is not None:
                    best_upgrade["quality_upgraded_from"] = {
                        "source": "original",
                        "source_provider": "netease",
                        "level": str(original.get("level") or ""),
                        "format": str(original.get("format") or ""),
                        "bitrate": orig_br,
                    }
                    self._remember_source_route(effective_account_id, track_key, best_upgrade)
                    self._put_cached_audio(cache_key, best_upgrade)
                    return best_upgrade

            self._put_cached_audio(cache_key, orig_payload)
            return orig_payload

        source = song_hint or self._lookup_song(song_id, active_client, account_id=effective_account_id)

        if arbitration == "best_quality_race" and source is not None:
            contenders = self._race_higher_quality(
                song_id=song_id,
                source=source,
                active_client=active_client,
                preferred_level=preferred_level,
                allow_external_net=allow_ext_net,
                enabled_providers=enabled_providers,
                custom_api=custom_api,
                include_netease_alt=True,
                blocked_candidates=blocked_candidates,
            )
            if contenders:
                source_rank = {"local": 4, "cloud": 3, "alternative": 2, "external": 1}
                contenders.sort(
                    key=lambda item: (_payload_quality_score(item), source_rank.get(str(item.get("source")), 0)),
                    reverse=True,
                )
                winner = contenders[0]
                self._remember_source_route(effective_account_id, track_key, winner)
                self._put_cached_audio(cache_key, winner)
                return winner
            raise AppError("AUDIO_UNAVAILABLE", "没有找到严格匹配的可播放音源", 404)

        # Classic netease_first waterfall (also used when source is None for Cloud exact-ID match).
        cloud_payload = self._try_cloud_disk(active_client, song_id, source, preferred_level)
        if cloud_payload is not None:
            self._put_cached_audio(cache_key, cloud_payload)
            return cloud_payload

        if source is None:
            raise AppError("SONG_NOT_IN_CATALOG", "本地曲库中找不到这首歌", 404)

        alt_payload = self._try_netease_alternative(active_client, source, preferred_level)
        if alt_payload is not None:
            self._put_cached_audio(cache_key, alt_payload)
            return alt_payload

        local_match = self.external_resolver.match_local(source, self.duration_tolerance_ms)
        if local_match is not None:
            payload = self._build_local_payload(source, local_match)
            if not self._is_payload_blocked(payload, blocked_candidates):
                self._remember_source_route(effective_account_id, track_key, payload)
                self._put_cached_audio(cache_key, payload)
                return payload

        if allow_ext_net:
            ext_kwargs: dict[str, Any] = {
                "enabled_providers": enabled_providers,
                "custom_api": custom_api,
                "duration_tolerance_ms": self.duration_tolerance_ms,
                "verify_stream": True,
                "best_quality_race": False,
            }
            if blocked_candidates:
                ext_kwargs["blocked_candidates"] = blocked_candidates
            ext_match = self.external_resolver.resolve(source, **ext_kwargs)
            if ext_match is not None:
                payload = self._build_external_payload(source, ext_match)
                if not self._is_payload_blocked(payload, blocked_candidates):
                    self._remember_source_route(effective_account_id, track_key, payload)
                    self._put_cached_audio(cache_key, payload)
                    return payload

        raise AppError("AUDIO_UNAVAILABLE", "没有找到严格匹配的可播放音源", 404)

    def resolve_external_track(
        self,
        *,
        provider: str,
        external_id: str,
        title: str = "",
        artists: list[str] | None = None,
        duration_ms: int = 0,
        stream_url_hint: str = "",
        song_id: int = 0,
    ) -> dict[str, Any]:
        """Resolve a cross-channel, OpenSubsonic, or WebDAV track directly through an isolated StreamTarget proxy."""
        prov = str(provider or "").strip().lower()
        if prov == "opensubsonic":
            secret = self.external_resolver._channel_secret("opensubsonic") or {}
            url, headers = self.external_resolver.opensubsonic.build_stream_url(external_id, secret)
            base_origin = str(secret.get("api_base") or "")
            song_meta = None
            try:
                song_meta = self.external_resolver.opensubsonic.get_song(
                    external_id, secret, self.external_resolver._get_http_client(), timeout=5.0
                )
            except Exception:
                song_meta = None
            fmt = str((song_meta or {}).get("format") or "unknown").lower()
            mime = mime_type_for_format(fmt)
            target = self.register_stream_token(
                kind="remote",
                url=url,
                headers=headers,
                content_type=mime,
                format_name=fmt,
                provider="opensubsonic",
                cache_key=f"opensubsonic:{self_hosted_cache_scope(secret)}:{external_id}:{fmt}",
                allow_self_hosted_origin=base_origin,
            )
            loudness = compute_loudness_compensation(
                (song_meta or {}).get("replaygain_track_gain_db"),
                (song_meta or {}).get("replaygain_track_peak"),
                album_gain_db=(song_meta or {}).get("replaygain_album_gain_db"),
                album_peak=(song_meta or {}).get("replaygain_album_peak"),
            )
            return {
                "id": max(0, int(song_id or 0)),
                "track_key": f"opensubsonic:{external_id}",
                "url": f"/api/stream/{target.token}",
                "stream_url": f"/api/stream/{target.token}",
                "stream_token": target.token,
                "level": "lossless" if fmt in {"flac", "wav"} else ("standard" if fmt == "unknown" else "exhigh"),
                "format": fmt,
                "bitrate": int((song_meta or {}).get("bitrate") or 0),
                "sample_rate": int((song_meta or {}).get("sample_rate") or 0),
                "size": int((song_meta or {}).get("size") or 0),
                "time_ms": int((song_meta or {}).get("duration_ms") or duration_ms or 0),
                "free_trial": False,
                "is_alternative": song_id > 0,
                "source": "opensubsonic",
                "source_provider": "opensubsonic",
                "loudness": loudness,
                "cover_url": str((song_meta or {}).get("cover_url") or ""),
            }

        if prov == "webdav":
            secret = self.external_resolver._channel_secret("webdav") or {}
            track_meta = self.external_resolver.webdav.get_track(external_id)
            if track_meta is None and self.db is not None and hasattr(self.db, "get_canonical_track"):
                tkey = f"webdav:{str(external_id).removeprefix('webdav:')}"
                for acct in dict.fromkeys((str(getattr(self.catalog, "_active_account_id", "") or ""), "local")):
                    if not acct:
                        continue
                    found = self.db.get_canonical_track(acct, tkey)
                    if isinstance(found, dict) and found.get("webdav_url"):
                        track_meta = found
                        self.external_resolver.webdav.seed_tracks([found])
                        break
            url, headers = self.external_resolver.webdav.build_stream_request(
                external_id, secret, fallback_track=track_meta
            )
            track_meta = track_meta or {}
            fmt = str(track_meta.get("format") or "mp3").lower()
            mime = mime_type_for_format(fmt)
            target = self.register_stream_token(
                kind="remote",
                url=url,
                headers=headers,
                content_type=mime,
                format_name=fmt,
                provider="webdav",
                cache_key=f"webdav:{self_hosted_cache_scope(secret)}:{external_id}:{fmt}",
                allow_self_hosted_origin=str(secret.get("api_base") or ""),
            )
            return {
                "id": max(0, int(song_id or 0)),
                "track_key": f"webdav:{external_id}",
                "url": f"/api/stream/{target.token}",
                "stream_url": f"/api/stream/{target.token}",
                "stream_token": target.token,
                "level": "lossless" if fmt in {"flac", "wav"} else "local",
                "format": fmt,
                "bitrate": 0,
                "sample_rate": 0,
                "size": int(track_meta.get("size") or 0),
                "time_ms": int(track_meta.get("duration_ms") or duration_ms or 0),
                "free_trial": False,
                "is_alternative": song_id > 0,
                "source": "webdav",
                "source_provider": "webdav",
                "loudness": compute_loudness_compensation(None, None),
            }

        settings = self._read_settings()
        custom_api = str(settings.get("custom_external_api") or "").strip()
        resolved = self.external_resolver.resolve_direct_channel(
            provider,
            external_id,
            title=title,
            artists=artists,
            duration_ms=duration_ms,
            stream_url_hint=stream_url_hint,
            custom_api=custom_api,
            verify_stream=True,
        )
        if resolved is None:
            raise AppError("AUDIO_UNAVAILABLE", "所选跨渠道音源不可用或流探活失败", 404)
        source = Song(
            id=max(0, int(song_id or 0)),
            title=resolved.title or title or external_id,
            artists=list(resolved.artists or artists or []),
            album=resolved.album,
            duration=resolved.duration_ms or max(0, int(duration_ms or 0)),
            cover_url=resolved.cover_url,
        )
        return self._build_external_payload(
            source,
            resolved,
            force_proxy=True,
            is_alternative=song_id > 0,
            external_id=external_id,
        )

    def _resolve_specific_candidate(
        self,
        song_id: int,
        candidate_id: str,
        *,
        preferred_level: str,
        song_hint: Song | None,
        active_client: NeteaseClient,
        custom_api: str,
    ) -> dict[str, Any]:
        if candidate_id.startswith("netease:"):
            raw_id = candidate_id.split(":", 1)[1]
            if not raw_id.isdigit():
                raise AppError("CANDIDATE_INVALID", "无效的音源候选标识", 400)
            target_id = int(raw_id)
            audio = self._resolve_netease_track(active_client, target_id, preferred_level)
            if not audio:
                raise AppError("AUDIO_UNAVAILABLE", "所选网易云音源当前不可播放", 404)
            cached_meta = self._get_cached_candidate(candidate_id)
            alt_info = cached_meta.to_dict() if isinstance(cached_meta, Song) else None
            return {
                **audio,
                "is_alternative": target_id != song_id,
                "source": "alternative" if target_id != song_id else "original",
                "source_provider": "netease",
                "alternative_info": alt_info if target_id != song_id else None,
            }

        cached_obj = self._get_cached_candidate(candidate_id)
        source = song_hint or self._lookup_song(song_id, active_client) or Song(id=song_id, title="", artists=[])
        if isinstance(cached_obj, LocalTrackMatch):
            return self._build_local_payload(source, cached_obj)
        if isinstance(cached_obj, ExternalCandidate):
            from_custom = cached_obj.source_provider == "custom_endpoint" or bool(
                cached_obj.extra.get("from_custom_endpoint")
            )
            allow_loopback = custom_api if from_custom else False
            resolved = self.external_resolver.resolve_candidate(
                cached_obj,
                verify_stream=True,
                allow_loopback=allow_loopback,
            )
            if resolved is None:
                raise AppError("AUDIO_UNAVAILABLE", "所选外部音源不可用或流探活失败", 404)
            return self._build_external_payload(source, resolved)
        raise AppError("CANDIDATE_NOT_FOUND", "候选音源已过期，请重新获取候选列表", 404)

    def _put_cached_candidate(self, candidate_id: str, value: Any) -> None:
        now = time.monotonic()
        with self._lock:
            self._candidate_cache[candidate_id] = (now + 600.0, value)
            self._candidate_cache.move_to_end(candidate_id)
            while len(self._candidate_cache) > 300:
                self._candidate_cache.popitem(last=False)

    def _get_cached_candidate(self, candidate_id: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            entry = self._candidate_cache.get(candidate_id)
            if entry is None:
                return None
            expires_at, value = entry
            if now >= expires_at:
                self._candidate_cache.pop(candidate_id, None)
                return None
            return value

    def candidates(
        self,
        song_id: int,
        *,
        allow_external: bool | None = None,
        song_hint: Song | None = None,
        client_override: NeteaseClient | None = None,
        account_id: str | None = None,
    ) -> dict[str, Any]:
        """Return NetEase, local, and multi-channel external candidates for manual switching."""
        active_client = client_override or self.client
        settings = self._read_settings()
        external_enabled = (
            bool(allow_external)
            if allow_external is not None
            else bool(settings.get("enable_external_sources", True))
        )
        source = song_hint or self._lookup_song(song_id, active_client, account_id=account_id)
        if source is None:
            raise AppError("SONG_NOT_IN_CATALOG", "本地曲库中找不到这首歌", 404)

        query_obj = TrackQuery.from_song(source)
        items: list[dict[str, Any]] = []

        # 1. Local file match if present.
        local_match = self.external_resolver.match_local(source, self.duration_tolerance_ms)
        if local_match is not None:
            cid = f"local:{hashlib.sha256(str(local_match.path).encode('utf-8')).hexdigest()[:12]}"
            self._put_cached_candidate(cid, local_match)
            items.append(
                {
                    "candidate_id": cid,
                    "source": "local",
                    "source_provider": "local_library",
                    "id": source.id,
                    "title": local_match.title,
                    "artists": [local_match.artist] if local_match.artist else list(source.artists),
                    "album": local_match.album,
                    "duration": local_match.duration_ms,
                    "duration_ms": local_match.duration_ms,
                    "format": local_match.format,
                    "bitrate": local_match.bitrate,
                    "strict_match": True,
                    "match_score": round(local_match.match_score, 3),
                    "playable": True,
                }
            )

        # 2. In-NetEase search candidates.
        keyword = query_obj.search_keyword
        if keyword:
            try:
                search_results = active_client.search_songs(keyword, limit=12)
            except (AppError, AssertionError) as exc:
                propagate_risk_control(exc)
                search_results = []
            for cand in search_results:
                strict = is_strict_match(source, cand, self.duration_tolerance_ms)
                _, score = evaluate_match(
                    query_obj,
                    cand.title,
                    cand.artists,
                    cand.duration,
                    duration_tolerance_ms=self.duration_tolerance_ms,
                )
                cid = f"netease:{cand.id}"
                self._put_cached_candidate(cid, cand)
                items.append(
                    {
                        "candidate_id": cid,
                        "source": "original" if cand.id == source.id else "netease",
                        "source_provider": "netease",
                        "id": cand.id,
                        "title": cand.title,
                        "artists": list(cand.artists),
                        "album": cand.album,
                        "duration": cand.duration,
                        "duration_ms": cand.duration,
                        "format": "",
                        "bitrate": 0,
                        "strict_match": strict,
                        "match_score": round(score, 3),
                        "playable": cand.availability != "unavailable",
                    }
                )

        # 3. External multi-channel candidates.
        if external_enabled and self._can_call_external_network(active_client, allow_external):
            raw_providers = settings.get("external_providers")
            if isinstance(raw_providers, list):
                enabled_providers = [str(item) for item in raw_providers if str(item).strip()]
            else:
                enabled_providers = list(DEFAULT_EXTERNAL_PROVIDERS)
            custom_api = str(settings.get("custom_external_api") or "").strip()
            ext_candidates = self.external_resolver.search_candidates(
                source,
                enabled_providers=enabled_providers,
                custom_api=custom_api,
                duration_tolerance_ms=self.duration_tolerance_ms,
                limit=10,
            )
            for cand in ext_candidates:
                cid = f"external:{cand.source_provider}:{cand.provider_track_id}"
                self._put_cached_candidate(cid, cand)
                items.append(
                    {
                        "candidate_id": cid,
                        "source": "external",
                        "source_provider": cand.source_provider,
                        "id": source.id,
                        "title": cand.title,
                        "artists": list(cand.artists),
                        "album": cand.album,
                        "duration": cand.duration_ms,
                        "duration_ms": cand.duration_ms,
                        "format": cand.format,
                        "bitrate": cand.bitrate,
                        "strict_match": cand.strict_match,
                        "match_score": round(cand.match_score, 3),
                        "playable": True,
                    }
                )

        items.sort(
            key=lambda entry: (
                entry["source"] == "local",
                entry["strict_match"],
                entry["playable"],
                entry["match_score"],
            ),
            reverse=True,
        )
        return {"song_id": song_id, "candidates": items}
