from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import ipaddress
import math
import secrets
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import socket
import threading
import time
from typing import Any, AsyncIterator, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .amll_lyrics import fetch_amll_ttml_as_yrc
from .audio_service import AudioService, StreamTarget, stream_session
from .catalog_service import CatalogService, RulesService
from .channel_auth import BilibiliQrService, CHANNEL_AUTH_CONFIG, channel_capability
from .config import Settings, is_trusted_netease_api_base, validate_api_base
from .snapshots import download_snapshot
from .db import CatalogDatabase, VALID_FEEDBACK_ACTIONS, is_stub_song
from .lan_sync import LanSyncManager
from .desktop import DesktopLyricsController, _VALID_RESIZE_EDGES, bind_loopback_socket, launch_desktop_or_server
from .errors import AppError, UpstreamError
from .external_sources import (
    _NON_PUBLIC_HOST_SUFFIXES,
    _pinned_url,
    is_public_ip,
    is_safe_external_url,
    mime_type_for_format,
    playback_redirect_headers,
    validated_addresses,
)
from .instance_lock import DataDirectoryLock
from .local_metadata import compute_loudness_compensation
from .models import AccountSummary, Song
from .netease_client import NeteaseClient, NeteaseWebTransport, NoOpCookies, isolate_cookie_jar, Transport, recommendation_reads
from .netease_crypto import set_device_id
from .open_protocols import WebDAVEncryptedSync, _same_origin, self_hosted_addresses
from .operation_journal import OperationJournal
from .operations import OperationsService, positive_id as _positive_id, _positive_ids
from .playlist_migration import export_playlist_content, match_migration_entries, parse_playlist_import
from .recommend_service import RecommendService
from .resolver_v2 import (
    ResolverRegistry,
    fetch_resolver_lyric,
    fetch_resolver_manifest,
    fetch_subscription_json,
)
from .shuffle_engine import balanced_shuffle
from .sidecar_bridge import SidecarProcessManager
from .storage import (
    AudioDiskCache,
    ChannelCredentialVault,
    DEFAULT_DESKTOP_LYRICS_STYLE,
    DeviceIdentityStore,
    JsonStore,
    SUPPORTED_CHANNELS,
    SessionStore,
    SettingsStore,
    credential_protection_mode,
    sniff_audio_header_mime,
)
from .track_identity import canonical_track_key, parse_track_key
from .version import __version__

_STANDARD_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

def check_app_update():
    repo = "https://github.com/iiijiashu/Pruner-Studio"
    result = {"current_version": __version__, "latest_version": None, "has_update": False,
              "release_url": None, "published_at": None, "status": "unreachable",
              "message": "无法获取发布信息，请稍后重试。"}
    try:
        try:
            response = httpx.get("https://api.github.com/repos/iiijiashu/Pruner-Studio/releases/latest",
                                 timeout=5, headers={"User-Agent": f"Pruner-Studio/{__version__}"})
        except httpx.InvalidURL:
            with httpx.Client(trust_env=False, timeout=5) as fallback_client:
                response = fallback_client.get(
                    "https://api.github.com/repos/iiijiashu/Pruner-Studio/releases/latest",
                    headers={"User-Agent": f"Pruner-Studio/{__version__}"},
                )
        if response.status_code == 404:
            return {**result, "status": "no_releases", "message": "目前尚无公开发行版。"}
        response.raise_for_status()
        release = response.json()
        tag = release["tag_name"]
        if not re.fullmatch(r"v?(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}", tag):
            return {**result, "status": "invalid_version", "message": "发行版本号无法识别。"}
        latest = tag.removeprefix("v")
        newer = tuple(map(int, latest.split("."))) > tuple(map(int, __version__.split(".")))
        return {**result, "latest_version": latest, "has_update": newer,
                "release_url": f"{repo}/releases/tag/{tag}", "published_at": release.get("published_at"),
                "status": "ok", "message": f"发现新版本 {latest}" if newer else "已是最新版本"}
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return result



_ALLOWED_DESKTOP_COMMANDS = frozenset({
    "next", "prev", "toggle_play", "toggle_like", "unlock_desktop_lyrics",
    "toggle_desktop_lyrics_lock", "lyric_offset_minus", "lyric_offset_plus",
    "show_main_window", "open_lyrics_settings", "volume_up", "volume_down",
    "toggle_mute", "set_volume", "toggle_game_mode",
})
_ALLOWED_COVER_SIZES = (64, 96, 192, 384)
_COVER_MAX_BYTES = 2 * 1024 * 1024
_COVER_CACHE_MAX_ITEMS = 64
_LOCAL_AUDIO_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".opus"})
_LOCAL_AUDIO_MIME_BY_EXT = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".aac": "audio/aac",
    ".opus": "audio/opus",
}
_ALLOWED_STREAM_MIME_EXACT = frozenset(
    {"video/mp4", "video/webm", "application/ogg", "application/octet-stream", "binary/octet-stream"}
)
_STREAM_CHUNK_BYTES = 65_536
_STREAM_MAX_REDIRECTS = 5
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DNS_TLD_RE = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$")


@dataclass(frozen=True)
class ApiResponse:
    status: int
    payload: dict[str, Any] | bytes


def success(data: Any = None, status: int = 200) -> ApiResponse:
    return ApiResponse(status, {"ok": True, "data": data, "error": None})


def failure(error: AppError) -> ApiResponse:
    return ApiResponse(
        error.status,
        {"ok": False, "data": None, "error": {"code": error.code, "message": error.message}},
    )


class SyncEventBus:
    """Thread-safe fan-out bus bridging synchronous sync progress to async SSE clients."""

    def __init__(self, max_queue_size: int = 64):
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any] | None]] = set()
        self._max_queue_size = max_queue_size
        self._closed = False

    def subscribe(self) -> queue.Queue[dict[str, Any] | None]:
        subscriber: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=self._max_queue_size)
        with self._lock:
            if self._closed:
                subscriber.put_nowait(None)
            else:
                self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any] | None]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for subscriber in targets:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            targets = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in targets:
            try:
                subscriber.put_nowait(None)
            except queue.Full:
                pass


class PrunerApplication:
    def __init__(
        self,
        settings: Settings,
        client: NeteaseClient,
        session: SessionStore,
        catalog: CatalogService,
        rules: RulesService,
        audio: AudioService,
        operations: OperationsService,
        *,
        db: CatalogDatabase | None = None,
        user_settings: SettingsStore | None = None,
        recommend: RecommendService | None = None,
        channel_vault: ChannelCredentialVault | None = None,
        audio_cache: AudioDiskCache | None = None,
        desktop_lyrics: DesktopLyricsController | None = None,
        folder_picker: Callable[[], str | None] | None = None,
        wake_window: Callable[[], bool | None] | None = None,
    ):
        self.settings = settings
        self.client = client
        self.session = session
        self.catalog = catalog
        self.db = db or catalog.db
        self.user_settings = user_settings or SettingsStore(catalog.store)
        self.channel_vault = channel_vault or ChannelCredentialVault(catalog.store)
        self.audio_cache = audio_cache or AudioDiskCache(
            settings.data_dir,
            limit_mb_provider=lambda: int(self.user_settings.load().get("audio_cache_limit_mb", 512)),
        )
        self.rules = rules
        self.audio = audio
        self.operations = operations
        self.recommend = recommend or RecommendService(client, db=self.db, catalog=catalog)
        self.static_files = _build_static_allowlist(settings.web_dir)

        self._auth_lock = threading.RLock()
        self._session_generation: int = 1
        self._offline_account_id: str | None = None
        self._offline_generation = None
        self._cached_account: AccountSummary | None = None
        self._cached_account_cookie: str | None = None
        self._active_qr_keys: set[str] = set()

        self._sync_semaphore = threading.Semaphore(2)
        self._sync_bus = SyncEventBus()
        self.lan_sync = LanSyncManager(self, catalog.store)
        self._desktop_state_lock = threading.Lock()
        self._latest_desktop_lyrics_state: dict[str, Any] | None = None
        self._latest_desktop_lyrics_style: dict[str, Any] | None = None

        def _load_lyrics_bounds() -> dict[str, Any] | None:
            try:
                raw_bounds = self.user_settings.load().get("desktop_lyrics_bounds")
                return dict(raw_bounds) if isinstance(raw_bounds, dict) else None
            except Exception:
                return None

        def _save_lyrics_bounds(bounds: dict[str, int]) -> None:
            try:
                self.user_settings.save({"desktop_lyrics_bounds": bounds})
            except Exception:
                pass

        def _load_lyrics_style() -> dict[str, Any] | None:
            try:
                raw_style = self.user_settings.load().get("desktop_lyrics_style")
                return dict(raw_style) if isinstance(raw_style, dict) else None
            except Exception:
                return None

        def _load_game_mode() -> bool:
            try:
                return bool(self.user_settings.load().get("game_mode", False))
            except Exception:
                return False

        self.desktop_lyrics = desktop_lyrics or DesktopLyricsController(
            on_state_change=lambda st: self.publish_sse("desktop_lyrics", {"window": st}),
            bounds_provider=_load_lyrics_bounds,
            bounds_saver=_save_lyrics_bounds,
            style_provider=_load_lyrics_style,
            game_mode_provider=_load_game_mode,
        )
        self.wake_token = secrets.token_urlsafe(32)
        self.local_api_token = secrets.token_urlsafe(32)
        self._bootstrap_lock = threading.Lock()
        self._bootstrap_caps = {}
        self.browser_api_token = secrets.token_urlsafe(32)
        self.resolver_registry = ResolverRegistry(catalog.store)
        self.sidecar = SidecarProcessManager()
        self.webdav_sync = WebDAVEncryptedSync()
        if hasattr(self.audio, "external_resolver") and self.audio.external_resolver is not None:
            self.audio.external_resolver.resolver_registry = self.resolver_registry
            self.audio.external_resolver.sidecar_manager = self.sidecar
        self.folder_picker = folder_picker
        self.toggle_fullscreen: Callable[[], bool] | None = None
        self.get_fullscreen: Callable[[], bool] | None = None
        self.wake_window = wake_window
        self.apply_theme = None
        self.set_hotkeys_enabled = None
        self.channel_auth_window: Any = None
        self.bilibili_qr = BilibiliQrService()
        self._channel_probe_timeout_sec: float = 8.0
        self.lyrics_preview_subscribers = 0
        self._amll_fetcher: Any = None

        self._cover_lock = threading.Lock()
        self._cover_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    def _get_saved_desktop_lyrics_style(self) -> dict[str, Any] | None:
        with self._desktop_state_lock:
            if self._latest_desktop_lyrics_style is not None:
                return dict(self._latest_desktop_lyrics_style)
        try:
            loaded = self.user_settings.load()
        except Exception:
            return None
        style = loaded.get("desktop_lyrics_style")
        if isinstance(style, dict) and style != DEFAULT_DESKTOP_LYRICS_STYLE:
            return dict(style)
        return None

    def publish_sse(self, event_type: str, payload: dict[str, Any]) -> None:
        self._sync_bus.publish({"_event_type": event_type, **payload})

    @property
    def session_generation(self) -> int:
        with self._auth_lock:
            return self._session_generation

    def _session_snapshot(self) -> tuple[int, str | None, AccountSummary | None]:
        """Take an immutable session snapshot under _auth_lock and release immediately."""
        with self._auth_lock:
            cookie = None if self._offline_generation == self._session_generation else self.client.cookie
            cached = self._cached_account if (cookie and self._cached_account_cookie == cookie) else None
            return self._session_generation, cookie, cached

    def _client_for_cookie(self, cookie: str | None) -> NeteaseClient:
        with_cookie = getattr(self.client, "with_cookie", None)
        if callable(with_cookie):
            return with_cookie(cookie)
        return NeteaseClient(self.client.transport, cookie)

    def _record_account_if_current(self, expected_gen: int, cookie: str | None, account: AccountSummary) -> None:
        with self._auth_lock:
            if self._session_generation == expected_gen and self.client.cookie == cookie:
                self._offline_account_id = None
                self._offline_generation = None
                self._cached_account = account
                self._cached_account_cookie = cookie
                self.catalog.set_active_account(str(account.user_id))
        try:
            self.db.upsert_account(
                str(account.user_id),
                account.display_name,
                account.avatar_url,
                account.vip_type,
            )
        except Exception:
            pass

    def _assert_session_unchanged(self, expected_gen: int) -> None:
        with self._auth_lock:
            if self._session_generation != expected_gen:
                raise AppError("SESSION_CHANGED", "登录会话已变更，请重试", 409)

    def _resolve_account_unlocked(
        self,
        gen: int,
        cookie: str | None,
        cached: AccountSummary | None,
        scoped_client: NeteaseClient,
    ) -> AccountSummary:
        if cached is not None:
            return cached
        account = scoped_client.account()
        self._record_account_if_current(gen, cookie, account)
        return account

    def _active_account_id_best_effort(self, allow_remote: bool = False) -> str:
        gen, cookie, cached = self._session_snapshot()
        if gen == self._offline_generation or not cookie:
            return self._offline_account_id or self.catalog._active_account_id or self.session.active_account_id or "local"
        if cached is not None:
            return str(cached.user_id)
        if cookie and allow_remote:
            scoped = self._client_for_cookie(cookie)
            try:
                account = scoped.account()
                self._record_account_if_current(gen, cookie, account)
                return str(account.user_id)
            except AppError:
                pass
        return self.catalog._active_account_id or "local"

    def _discovery_scope(self):
        """Use the locally bound identity without spending a discovery read on /user/account."""
        with self._auth_lock:
            gen, cookie, cached = self._session_snapshot()
            if not cookie:
                aid = self._active_account_id_best_effort()
            elif cached:
                aid = str(cached.user_id)
            else:
                saved = self.session.active_account_id
                aid = saved if saved and self.session.load_account_cookie(saved) == cookie else None
            return gen, cookie, aid, self._client_for_cookie(cookie)

    def api(self, method: str, raw_path: str, body: Any = None, *, http_request=False) -> ApiResponse:
        response = self._scoped_api(method, raw_path, body, http_request=http_request)
        if method != "GET" and 200 <= response.status < 300 and self.lan_sync.bound_port:
            self.lan_sync.desktop_change(urlsplit(raw_path).path, body, response.payload["data"])
        return response

    def _scoped_api(self, method: str, raw_path: str, body: Any = None, *, http_request=False) -> ApiResponse:
        path = urlsplit(raw_path).path
        scoped = ((method, path) in {
            ("POST", "/api/operations/create-playlist"), ("POST", "/api/operations/like"),
            ("POST", "/api/operations/unlike"), ("PUT", "/api/playback/session"), ("POST", "/api/playback-session"),
            ("POST", "/api/playback/record"), ("DELETE", "/api/playback/history"),
        } or method == "POST" and re.fullmatch(r"/api/playlists/\d+/(?:add|remove)-tracks", path))
        optional_guard = (method, path) in {
            ("POST", "/api/feedback"), ("DELETE", "/api/feedback"), ("POST", "/api/catalog/import"),
            ("POST", "/api/recommend/fm/trash"),
        }
        if scoped or optional_guard:
            with self._auth_lock:
                try:
                    if path.startswith(("/api/operations/", "/api/playlists/")) or path == "/api/recommend/fm/trash":
                        self._require_online_write()
                    payload = _object(body, allow_none=True)
                    if optional_guard and method == "DELETE":
                        query = parse_qs(urlsplit(raw_path).query)
                        payload = {**{k: query[k][0] for k in ("expected_account_id", "session_generation") if k in query}, **payload}
                    generation = payload.get("session_generation")
                    if scoped and generation is None and http_request and self.client.cookie:
                        raise AppError("SESSION_GENERATION_MISMATCH", "缺少登录会话代数", 409)
                    self.operations._verify_session_generation(generation, self._session_generation)
                    aid = None
                    if not self._cached_account and ("expected_account_id" in payload or generation is not None):
                        # A cold identity check can read upstream. Reject invalid write IDs first.
                        if path in {"/api/operations/like", "/api/recommend/fm/trash"}:
                            _positive_id(payload.get("song_id"))
                        elif path in {"/api/operations/unlike", "/api/operations/create-playlist"} or path.startswith("/api/playlists/"):
                            ids = payload.get("song_ids")
                            if ids is not None or path != "/api/operations/create-playlist":
                                _positive_ids(ids, self.operations.max_batch_size)
                            if path == "/api/operations/unlike" or path.endswith("/remove-tracks"):
                                _positive_id(payload.get("source_playlist_id"), "SOURCE_PLAYLIST_ID_INVALID")
                            if path.startswith("/api/playlists/"):
                                _positive_id(path.split("/")[3], "PLAYLIST_ID_INVALID")
                    if optional_guard:
                        _, cookie, aid, client = self._discovery_scope()
                        account = None
                        if aid is None and cookie and (generation is not None or "expected_account_id" in payload):
                            account = client.account()
                            aid = str(account.user_id)
                        if "expected_account_id" in payload and str(payload["expected_account_id"]) != aid:
                            raise AppError("SESSION_GENERATION_MISMATCH", "操作账号已变更", 409)
                        if account:
                            self._record_account_if_current(self._session_generation, cookie, account)
                    elif "expected_account_id" in payload:
                        _, cookie, cached = self._session_snapshot()
                        aid = str((cached or self.client.account()).user_id) if cookie else self._active_account_id_best_effort()
                        if str(payload["expected_account_id"]) != aid:
                            raise AppError("SESSION_GENERATION_MISMATCH", "操作账号已变更", 409)
                    return self._dispatch_api(method, raw_path, body, write_account=aid)
                except AppError as exc:
                    return failure(exc)
        return self._dispatch_api(method, raw_path, body)

    def _require_online_write(self):
        if self._offline_generation == self._session_generation:
            raise AppError("NOT_AUTHENTICATED", "当前处于离线模式，请先登录或切换在线账号", 401)

    def _dispatch_api(self, method: str, raw_path: str, body: Any, *, write_account=None) -> ApiResponse:
        parsed = urlsplit(raw_path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if method == "POST" and path == "/api/lan-sync/handoff/ack":
                return success(self.lan_sync.acknowledge_desktop(_object(body).get("handoff_id")))
            if path.startswith("/api/lan-sync/"):
                return success(self.lan_sync.desktop(method, path[len("/api/lan-sync/"):], _object(body, allow_none=True)))
            if method == "GET" and path == "/api/health":
                return success({"status": "ok", "version": __version__})
            if method == "GET" and path == "/api/status":
                return self._status()

            if method == "GET" and path == "/api/accounts":
                with self._auth_lock:
                    authenticated = bool(self.client.cookie and self._cached_account_cookie == self.client.cookie)
                    offline = self._offline_generation == self._session_generation
                    if self.client.cookie and not authenticated and not offline:
                        try:
                            authenticated = self._status().payload["data"]["authenticated"]
                        except UpstreamError:
                            authenticated = False
                    return success({"active_account_id": self._active_account_id_best_effort() if offline else self.session.active_account_id,
                                    "authenticated": authenticated and not offline,
                                    "saved_accounts": self.session.list_saved_accounts(),
                                    "cached_accounts": self.db.list_cached_accounts()})
            if method == "POST" and path == "/api/accounts/offline":
                aid = _object(body).get("account_id")
                if not isinstance(aid, str) or not aid.strip() or len(aid) > 64 or aid.strip() == "unclaimed":
                    raise AppError("ACCOUNT_INVALID", "离线账号无效", 400)
                with self._auth_lock:
                    self._session_generation += 1
                    self._offline_generation = self._session_generation
                    self._offline_account_id = aid.strip()
                    self._cached_account = self._cached_account_cookie = None
                    self._active_qr_keys.clear()
                    self._clear_audio_cache()
                    self.catalog.set_active_account(self._offline_account_id)
                    return success({"active_account_id": self._offline_account_id,
                                    "session_generation": self._session_generation, "offline": True})
            if method == "POST" and path == "/api/accounts/switch":
                aid = str(_object(body).get("account_id") or "")
                with self._auth_lock:
                    cookie = self.session.load_account_cookie(aid)
                    if not cookie:
                        raise AppError("SESSION_NOT_FOUND", "没有可用的已保存会话", 404)
                    account = self.client.account(cookie_override=cookie)
                    if str(account.user_id) != aid:
                        raise AppError("SESSION_EXPIRED", "账号身份已改变，请重新扫码", 401)
                    self.session.set_active_account(aid)
                    self.client.set_cookie(cookie)
                    self._active_qr_keys.clear()
                    self._session_generation += 1
                    self._clear_transport_cache()
                    self._clear_audio_cache()
                    self._record_account_if_current(self._session_generation, cookie, account)
                    return success({"authenticated": True, "user": account.to_dict(),
                                    "active_account_id": aid, "session_generation": self._session_generation})
            match = re.fullmatch(r"/api/accounts/(\d+)", path)
            if method == "DELETE" and match:
                with self._auth_lock:
                    aid = match.group(1)
                    if aid == self.session.active_account_id:
                        self._revoke_session()
                    elif not self.session.remove_account_session(aid):
                        raise AppError("SESSION_NOT_FOUND", "未找到此账号", 404)
                    return success({"active_account_id": self.session.active_account_id})
            if method == "POST" and path == "/api/accounts/local":
                with self._auth_lock:
                    self.session.set_active_account(None)
                    self._revoke_session()
                    self.catalog.set_active_account("local")
                    return success({"session_generation": self._session_generation})
            if method == "GET" and path == "/api/app/update-check":
                return success(check_app_update())

            # --- Authentication ---
            if method == "POST" and path == "/api/auth/qr":
                with self._auth_lock:
                    result = self._client_for_cookie(None).create_qr()
                    self._active_qr_keys.clear()
                    self._active_qr_keys.add(result["key"])
                return success(result, 201)
            match = re.fullmatch(r"/api/auth/qr/([^/]+)", path)
            if method == "GET" and match:
                qr_key = unquote(match.group(1))
                with self._auth_lock:
                    if qr_key not in self._active_qr_keys:
                        raise AppError("QR_CANCELLED", "二维码已取消或失效", 409)
                    candidate = self._client_for_cookie(None)
                    result = candidate.poll_qr(qr_key)
                    if qr_key not in self._active_qr_keys:
                        raise AppError("QR_CANCELLED", "二维码已取消或失效", 409)
                    if result["state"] in {"authorized", "expired"}:
                        self._active_qr_keys.discard(qr_key)
                    if result["state"] == "authorized":
                        if not candidate.cookie:
                            raise AppError("SESSION_MISSING", "登录成功但会话未保存", 502)
                        acct = AccountSummary(**result["user"])
                        self._offline_account_id = None
                        self._offline_generation = None
                        self.session.save_account_session(acct, candidate.cookie)
                        self.client.set_cookie(candidate.cookie)
                        self._session_generation += 1
                        self._clear_transport_cache()
                        self._clear_audio_cache()
                        self._record_account_if_current(self._session_generation, candidate.cookie, acct)
                        sanitized = {k: v for k, v in result.items() if k != "cookie"}
                        sanitized["session_generation"] = self._session_generation
                        return success(sanitized)
                return success({k: v for k, v in result.items() if k != "cookie"})
            if method == "POST" and path == "/api/auth/logout":
                with self._auth_lock:
                    self._revoke_session()
                return success({"authenticated": False})

            # --- Playlists & Sync (Read/Sync decoupled from _auth_lock!) ---
            if method == "GET" and path == "/api/playlists":
                include_subscribed = _query_bool(query, "include_subscribed") or _query_bool(query, "all")
                gen, cookie, cached = self._session_snapshot()
                scoped = self._client_for_cookie(cookie)
                account = self._resolve_account_unlocked(gen, cookie, cached, scoped)
                if include_subscribed:
                    items = scoped.playlists(account, include_subscribed=True)
                else:
                    items = scoped.playlists(account)
                self._assert_session_unchanged(gen)
                return success({"playlists": [item.to_dict() for item in items]})

            match = re.fullmatch(r"/api/playlists/(\d+)/sync", path)
            if method == "POST" and match:
                playlist_id = _positive_id(match.group(1), "PLAYLIST_ID_INVALID")
                return self._sync_playlist(playlist_id)

            # --- Catalog & Import ---
            if method == "POST" and path == "/api/catalog/import":
                payload = _object(body)
                account_id = write_account or self._active_account_id_best_effort(allow_remote=False)
                return success(
                    self.catalog.import_catalog(
                        payload.get("content"),
                        str(payload.get("format") or ""),
                        str(payload.get("playlist_name") or "导入歌曲"),
                        mode=payload.get("mode", "merge"),
                        account_id=account_id,
                    ),
                    201,
                )
            if method == "GET" and path == "/api/catalog":
                return self._catalog_view(query)

            # --- Cloud Disk & Search (Decoupled from _auth_lock) ---
            if method == "GET" and path == "/api/cloud":
                gen, cookie, cached = self._session_snapshot()
                if not cookie:
                    raise AppError("NOT_AUTHENTICATED", "请先扫码登录", 401)
                scoped = self._client_for_cookie(cookie)
                account = self._resolve_account_unlocked(gen, cookie, cached, scoped)
                limit = _query_int(query, "limit", default=200, minimum=1, maximum=500)
                offset = _query_int(query, "offset", default=0, minimum=0, maximum=50_000)
                if offset:
                    songs = scoped.cloud_songs(limit=limit, offset=offset)
                    self._assert_session_unchanged(gen)
                    return success({"songs": [s.to_dict() for s in songs], "total": len(songs),
                                    "limit": limit, "offset": offset})
                songs = []
                declared_total = None
                for _ in range(251):
                    self._assert_session_unchanged(gen)
                    page, more, count = scoped.cloud_page_with_count(limit=200, offset=len(songs))
                    if count is not None:
                        if declared_total is not None and count != declared_total:
                            raise UpstreamError("CLOUD_SYNC_INCOMPLETE", "云盘分页数据不完整，已保留旧缓存", 502)
                        declared_total = count
                    songs.extend(page)
                    if not more:
                        break
                    scoped.pacer.settle()
                else:
                    raise AppError("CLOUD_LIMIT", "云盘超过同步上限，已保留原缓存", 413)
                self._assert_session_unchanged(gen)
                unique_count = len({song.id for song in songs})
                if unique_count != len(songs) or (declared_total is not None and unique_count != declared_total):
                    raise UpstreamError("CLOUD_SYNC_INCOMPLETE", "云盘分页数据不完整，已保留旧缓存", 502)
                self.catalog.sync_cloud(songs, account_id=str(account.user_id))
                visible = songs if _query_bool(query, "full") else songs[:limit]
                return success({"songs": [s.to_dict() for s in visible], "total": len(songs),
                                "limit": limit, "offset": offset})

            if method == "GET" and path == "/api/search":
                raw_q = _query_str(query, "q") or _query_str(query, "keywords")
                if not raw_q or len(raw_q) > 200:
                    raise AppError("SEARCH_QUERY_INVALID", "搜索关键词不能为空且不超过 200 个字符")
                limit = _query_int(query, "limit", default=30, minimum=1, maximum=50)
                offset = _query_int(query, "offset", default=0, minimum=0, maximum=1000)
                gen, cookie, _ = self._session_snapshot()
                scoped = self._client_for_cookie(cookie)
                try:
                    songs = scoped.search_songs(raw_q, limit=limit, offset=offset)
                except TypeError:
                    songs = scoped.search_songs(raw_q, limit=limit)
                self._assert_session_unchanged(gen)
                return success({"query": raw_q, "songs": [s.to_dict() for s in songs], "total": len(songs)})

            if method == "GET" and path == "/api/search/multi":
                return self._search_multi(query)

            # --- Multi-Channel Hub (DPAPI-Isolated, Zero Secret Exposure) ---
            if method == "GET" and path == "/api/channels":
                user_cfg = self.user_settings.load()
                return success(
                    {
                        "channels": self.channel_vault.public_summary(),
                        "quality_arbitration": user_cfg.get("quality_arbitration", "best_quality_race"),
                        "health": self.audio.external_resolver.health.snapshot() if hasattr(self.audio, "external_resolver") else {},
                        "protection_mode": credential_protection_mode(),
                        "resolvers": self.resolver_registry.list_public(),
                        "sidecar": self.sidecar.status(),
                    }
                )
            if method == "POST" and path == "/api/channels/test-all":
                return self._test_all_channels()
            if method == "POST" and path == "/api/channels/bilibili/qr/create":
                return self._create_bilibili_qr()
            if method == "GET" and path == "/api/channels/bilibili/qr/poll":
                return self._poll_bilibili_qr(_query_str(query, "qrcode_key"))
            match = re.fullmatch(r"/api/channels/([a-z0-9_]+)", path)
            if method == "PUT" and match:
                channel_id = match.group(1)
                payload = _object(body)
                updated = self.channel_vault.update_channel(channel_id, payload)
                self._clear_audio_cache()
                return success(updated)
            match = re.fullmatch(r"/api/channels/([a-z0-9_]+)/test", path)
            if method == "POST" and match:
                channel_id = match.group(1)
                return self._test_channel(channel_id)

            match = re.fullmatch(r"/api/songs/(\d+)/hydrate", path)
            if method == "POST" and match:
                song_id = _positive_id(match.group(1), "SONG_ID_INVALID")
                return self._hydrate_song(song_id)

            # --- Media: Lyrics, Audio, Candidates (Decoupled from _auth_lock) ---
            if method == "GET" and path == "/api/lyrics":
                return self._lyrics(query)
            if method == "GET" and path == "/api/audio":
                return self._audio(query)
            if method == "GET" and path == "/api/audio/candidates":
                return self._audio_candidates(query)

            # --- Playback History, Smart Shuffle & Insights ---
            if (method, path) in {("GET", "/api/playback/session"), ("PUT", "/api/playback/session"),
                                  ("GET", "/api/playback-session"), ("POST", "/api/playback-session")}:
                aid = write_account or self._active_account_id_best_effort(allow_remote=True)
                session = (self.db.get_playback_session(aid) if method == "GET"
                           else self.db.save_playback_session(aid, _object(body)))
                return success(session)
            if path == "/api/playback/history" and method in {"GET", "DELETE"}:
                aid = write_account or self._active_account_id_best_effort(allow_remote=True)
                if method == "GET":
                    items = self.db.list_play_history(aid,
                        _query_int(query, "limit", default=100, minimum=1, maximum=10000),
                        _query_int(query, "offset", default=0, minimum=0, maximum=2**31-1), _query_str(query,"q"))
                    return success({"items": items})
                params = _object(body, allow_none=True)
                key = params.get("track_key", query.get("track_key", [None])[0])
                if key is not None and (not isinstance(key, str) or not key):
                    raise ValueError("Invalid track key")
                return success({"deleted": self.db.delete_play_history(aid, key)})
            if method == "POST" and path == "/api/playback/record":
                payload = _object(body)
                account_id = write_account or self._active_account_id_best_effort(allow_remote=True)
                raw_song = payload.get("song") if isinstance(payload.get("song"), dict) else payload
                if not isinstance(raw_song, dict):
                    raise AppError("SONG_INVALID", "歌曲信息无效", 400)
                try:
                    listened_ms = int(payload.get("listen_ms", payload.get("listened_ms", 0)))
                except (TypeError, ValueError) as exc:
                    raise AppError("INVALID_INPUT", "播放时长参数无效", 400) from exc
                completed = payload.get("completed", False)
                if type(completed) is not bool or not isinstance(payload.get("event_id", ""), str):
                    raise ValueError("Invalid playback event")
                raw_song = {**raw_song, **payload}
                if any(not isinstance(raw_song.get(k, ""), str) for k in ("track_key", "provider", "local_path")):
                    raise ValueError("Invalid track identity")
                recorded = self.db.record_play(
                    account_id,
                    raw_song,
                    event_id=payload.get("event_id"), listened_ms=listened_ms,
                    completed=completed,
                )
                return success(recorded)

            if method == "POST" and path == "/api/queue/smart-shuffle":
                payload = _object(body)
                tracks = payload.get("tracks")
                if not isinstance(tracks, list) or len(tracks) > 10_000:
                    raise AppError("QUEUE_INVALID", "待洗牌队列必须是数组且不超过 10000 首", 400)
                raw_seed = payload.get("seed")
                seed = int(raw_seed) if (raw_seed is not None and not isinstance(raw_seed, bool)) else None
                account_id = self._active_account_id_best_effort(allow_remote=False)
                history_map = self.db.get_play_history_map(account_id)
                shuffled = balanced_shuffle(tracks, play_history=history_map, seed=seed)
                return success({"tracks": shuffled, "total": len(shuffled)})

            if method == "GET" and path == "/api/insights":
                requested_account = _query_str(query, "account_id")
                gen, cookie, cached = self._session_snapshot()
                if self._offline_account_id and (gen == self._offline_generation or not cookie):
                    account_id = self._offline_account_id
                elif cookie and cached is not None:
                    account_id = str(cached.user_id)
                elif not cookie and requested_account and requested_account != "unclaimed" and len(requested_account) <= 64:
                    account_id = requested_account
                else:
                    account_id = self._active_account_id_best_effort(allow_remote=False)
                return success(self.db.get_listening_insights(account_id))

            # --- Recommendations (Decoupled from _auth_lock) ---
            if method == "POST" and path in {"/api/recommend/smart-radio", "/api/recommend/explore-batch"}:
                return self._radio_batch(_object(body), explore=path.endswith("/explore-batch"))

            if method == "POST" and path == "/api/recommend/fm/trash":
                sid = _positive_id(_object(body).get("song_id"))
                if not self.user_settings.load()["sync_fm_trash_to_netease"]:
                    return success({"synced": False, "status": "disabled", "cooldown_sec": 0, "reason": "未开启垃圾桶同步"})
                if not self.client.cookie or self._offline_generation == self._session_generation:
                    return success({"synced": False, "status": "auth_required", "cooldown_sec": 0, "reason": "请先登录"})
                try:
                    self.client.fm_trash(sid)
                except AppError as exc:
                    cooldown = self.client.cooldown_seconds()
                    return success({"synced": False, "status": "auth_required" if exc.code == "AUTH_REQUIRED" else "cooldown" if cooldown else "error",
                                    "cooldown_sec": cooldown, "reason": exc.message})
                return success({"synced": True, "status": "ok", "cooldown_sec": 0, "reason": ""})

            if method == "GET" and path == "/api/recommend/daily/history":
                gen, cookie, aid, scoped = self._discovery_scope()
                with self._auth_lock:
                    explicit_offline = bool(self._offline_account_id and self._offline_generation == self._session_generation)
                aid = aid or self.client.cookie_key(cookie)
                result = self.recommend.daily_history(
                    aid,
                    scoped,
                    _query_str(query, "date") or None,
                    explicit_offline=explicit_offline,
                )
                self._assert_session_unchanged(gen)
                return success(result)

            if method == "GET" and path == "/api/recommend/smart-radio":
                return self._recommend_smart_radio(query)

            if method == "GET" and path == "/api/recommend/dusty-gems":
                return self._recommend_dusty_gems(query)

            if method == "GET" and path == "/api/recommend/daily":
                gen, cookie, aid, scoped = self._discovery_scope()
                account_id = aid or self.client.cookie_key(cookie)
                known = {int(i) for i in _query_str(query, "known_raw_ids").split(",") if i.isdigit()} if "known_raw_ids" in query else None
                result = self.recommend.daily(account_id, client_override=scoped, force_check=_query_bool(query, "force_check"), known_raw_ids=known)
                self._assert_session_unchanged(gen)
                return success(result)

            if method == "GET" and path == "/api/recommend/fm":
                gen, cookie, cached = self._session_snapshot()
                scoped = self._client_for_cookie(cookie)
                account_id = str(cached.user_id) if cached else self._active_account_id_best_effort(allow_remote=bool(cookie))
                result = self.recommend.personal_fm(account_id, client_override=scoped)
                self._assert_session_unchanged(gen)
                return success(result)

            if method == "GET" and path == "/api/recommend/intelligence":
                song_id = _query_positive_id_any(query, ("song_id", "id"), "SONG_ID_REQUIRED", "SONG_ID_INVALID")
                playlist_id = _query_positive_id_any(query, ("playlist_id", "pid"), "PLAYLIST_ID_REQUIRED", "PLAYLIST_ID_INVALID")
                gen, cookie, cached = self._session_snapshot()
                scoped = self._client_for_cookie(cookie)
                account_id = str(cached.user_id) if cached else self._active_account_id_best_effort(allow_remote=bool(cookie))
                sid = _query_optional_int(query, "sid" if "sid" in query else "start_music_id", minimum=1, maximum=2**63-1)
                count = _query_int(query, "count", default=30, minimum=1, maximum=100)
                result = self.recommend.intelligence(song_id, playlist_id, account_id, start_music_id=sid, count=count, client_override=scoped)
                self._assert_session_unchanged(gen)
                return success(result)

            if method == "GET" and path == "/api/recommend/similar":
                song_id = _query_positive_id_any(query, ("song_id", "id"), "SONG_ID_REQUIRED", "SONG_ID_INVALID")
                gen, cookie, cached = self._session_snapshot()
                scoped = self._client_for_cookie(cookie)
                account_id = str(cached.user_id) if cached else self._active_account_id_best_effort(allow_remote=bool(cookie))
                result = self.recommend.similar(song_id, account_id, client_override=scoped)
                self._assert_session_unchanged(gen)
                return success(result)

            # --- Explicit Feedback ---
            if method == "GET" and path == "/api/feedback":
                account_id = self._active_account_id_best_effort(allow_remote=False)
                action = _query_str(query, "action") or None
                limit = _query_int(query, "limit", default=200, minimum=1, maximum=2000)
                return success({"items": self.db.list_feedback(account_id, action=action, limit=limit)})

            if method == "POST" and path == "/api/feedback":
                payload = _object(body)
                song_id, local_key = _feedback_identity(payload.get("song_id"), payload.get("track_key"))
                action = str(payload.get("action") or "").strip().lower()
                if action not in VALID_FEEDBACK_ACTIONS:
                    raise AppError("FEEDBACK_ACTION_INVALID", "反馈动作无效")
                raw_context = payload.get("context", "")
                if isinstance(raw_context, (dict, list)):
                    context = json.dumps(raw_context, ensure_ascii=False)[:200]
                else:
                    context = str(raw_context or "")[:200]
                account_id = write_account or self._active_account_id_best_effort(allow_remote=False)
                record = self.db.add_feedback(account_id, song_id, action, context, track_key=local_key)
                return success(record, 201)

            if method == "DELETE" and path == "/api/feedback":
                payload = _object(body, allow_none=True)
                account_id = write_account or self._active_account_id_best_effort(allow_remote=False)
                raw_song_id = payload.get("song_id") if payload else None
                if raw_song_id is None and "song_id" in query:
                    raw_song_id = query["song_id"][0]
                key = payload.get("track_key", _query_str(query, "track_key") or None)
                song_id, local_key = _feedback_identity(raw_song_id, key) if raw_song_id is not None or key is not None else (None, "")
                raw_action = (payload.get("action") if payload else None) or _query_str(query, "action") or None
                if raw_action is not None:
                    raw_action = str(raw_action).strip().lower()
                    if raw_action not in VALID_FEEDBACK_ACTIONS:
                        raise AppError("FEEDBACK_ACTION_INVALID", "反馈动作无效")
                deleted = self.db.delete_feedback(account_id, song_id=song_id, action=raw_action, track_key=local_key)
                return success({"deleted": deleted})

            # --- Settings & Local Library ---
            if method == "GET" and path == "/api/settings":
                return success({**self.user_settings.load(), **self.user_settings.hotkey_status()})

            if method == "PUT" and path == "/api/settings":
                payload = _object(body)
                previous = self.user_settings.load()
                updated = self.user_settings.save(payload)
                if self.set_hotkeys_enabled and previous["global_hotkeys_enabled"] != updated["global_hotkeys_enabled"]:
                    self.set_hotkeys_enabled(updated["global_hotkeys_enabled"])
                if self.apply_theme and previous["appearance"]["theme"] != updated["appearance"]["theme"]:
                    self.apply_theme(updated["appearance"]["theme"])
                resolution_keys = ("quality_preference", "quality_arbitration", "enable_external_sources",
                                   "external_providers", "custom_external_api", "local_music_dirs")
                if any(previous.get(key) != updated.get(key) for key in resolution_keys):
                    self._clear_audio_resolution_cache()
                if "desktop_lyrics_style" in payload or "gpu_performance_mode" in payload or "game_mode" in payload:
                    style_snapshot = dict(updated["desktop_lyrics_style"])
                    with self._desktop_state_lock:
                        self._latest_desktop_lyrics_style = style_snapshot
                    self.desktop_lyrics.apply_style_runtime(style_snapshot, bool(updated["game_mode"]))
                    self.publish_sse("desktop_lyrics_style", {
                        "style": style_snapshot,
                        "gpu_performance_mode": updated["gpu_performance_mode"],
                        "game_mode": updated["game_mode"],
                    })
                return success({**updated, **self.user_settings.hotkey_status()})

            if method == "POST" and path == "/api/local-library/scan":
                payload = _object(body, allow_none=True)
                raw_dirs = payload.get("dirs") if payload else None
                if raw_dirs is not None:
                    if not isinstance(raw_dirs, list) or not all(isinstance(d, str) for d in raw_dirs) or len(raw_dirs) > 20:
                        raise AppError("LOCAL_DIRS_INVALID", "本地目录参数无效")
                    dirs = [d.strip() for d in raw_dirs if d.strip()]
                else:
                    dirs = list(self.user_settings.load().get("local_music_dirs") or [])
                scan_fn = getattr(self.audio, "scan_local_library", None)
                scan_result = scan_fn(dirs) if callable(scan_fn) else {"scanned_files": 0, "total_indexed": 0, "files": []}
                if isinstance(scan_result, dict):
                    count = int(scan_result.get("scanned_files", scan_result.get("total_indexed", scan_result.get("scanned_count", 0))) or 0)
                    return success({**scan_result, "scanned_count": count, "dirs": dirs})
                return success({"scanned_count": 0, "scanned_files": 0, "total_indexed": 0, "files": [], "dirs": dirs})

            if method == "GET" and path == "/api/local-library":
                q = _query_str(query, "q")
                limit = _query_int(query, "limit", default=200, minimum=1, maximum=5000)
                offset = _query_int(query, "offset", default=0, minimum=0, maximum=2**31-1)
                files = self.db.list_local_files(q, limit, offset)
                total = self.db.count_local_files(q)
                return success({"files": files, "total": total, "offset": offset, "limit": limit, "has_more": offset+len(files)<total})

            if method == "POST" and path == "/api/local-library/stream":
                payload = _object(body)
                return self._local_library_stream(payload.get("path"))

            # --- Audio Disk Cache ---
            if method == "GET" and path == "/api/cache/audio/stats":
                return success(self.audio_cache.stats())

            if method == "DELETE" and path == "/api/cache/audio":
                return success(self.audio_cache.clear())

            # --- Desktop Floating Lyrics & Live Playback Broadcast ---
            if method == "POST" and path == "/api/desktop/channel-auth-window":
                return self._open_channel_auth_window(_object(body))
            if method == "POST" and path == "/api/desktop/pick-folder":
                return success({"supported": bool(self.folder_picker), "path": self.folder_picker() if self.folder_picker else None})
            if method == "POST" and path == "/api/desktop/window/fullscreen":
                toggle = self.toggle_fullscreen
                return success({"supported": bool(toggle), "fullscreen": bool(toggle()) if toggle else False})

            if method == "POST" and path == "/api/desktop/window/activate":
                token = _object(body, allow_none=True).get("wake_token")
                if not isinstance(token, str) or not secrets.compare_digest(token.encode(), self.wake_token.encode()):
                    raise AppError("WAKE_DENIED", "窗口唤醒令牌无效", 403)
                return success({"activated": self.wake_window is not None and self.wake_window() is not False})
            if method == "GET" and path == "/api/desktop/lyrics-window":
                with self._desktop_state_lock:
                    count = self.lyrics_preview_subscribers
                return success({**self.desktop_lyrics.state(), "preview_subscribers": count})

            if method == "POST" and path == "/api/desktop/lyrics-window/drag":
                return success({"started": self.desktop_lyrics.start_native_drag()})
            if method == "POST" and path == "/api/desktop/lyrics-window/resize-start":
                payload = _object(body)
                raw_edge = payload.get("edge")
                edge = raw_edge.strip().lower() if isinstance(raw_edge, str) else ""
                if not edge or edge not in (_VALID_RESIZE_EDGES - {"move"}):
                    raise AppError("INVALID_INPUT", "edge 参数无效", 400)
                return success({"started": self.desktop_lyrics.start_interactive_geometry(edge), "edge": edge})
            if method == "POST" and path == "/api/desktop/lyrics-window/fit":
                payload = _object(body)
                raw_min_h = payload.get("min_height")
                if type(raw_min_h) is not int or raw_min_h < 64 or raw_min_h > 360:
                    raise AppError("INVALID_INPUT", "min_height 参数无效", 400)
                return success(self.desktop_lyrics.ensure_min_height(raw_min_h))
            if method == "POST" and path == "/api/desktop/lyrics-window/center":
                return success({"bounds": self.desktop_lyrics.snap_center()})
            if method == "POST" and path == "/api/desktop/lyrics-window":
                payload = _object(body)
                if "open" not in payload or not isinstance(payload.get("open"), bool):
                    raise AppError("INVALID_INPUT", "open 参数必须是布尔值", 400)
                locked_raw = payload.get("locked")
                if locked_raw is not None and not isinstance(locked_raw, bool):
                    raise AppError("INVALID_INPUT", "locked 参数必须是布尔值", 400)
                state_result = self.desktop_lyrics.set_window(payload["open"], locked=locked_raw)
                return success(state_result)

            if method == "POST" and path in {"/api/desktop/playback-state", "/api/desktop/lyrics"}:
                payload = _object(body)
                snapshot = dict(payload)
                with self._desktop_state_lock:
                    self._latest_desktop_lyrics_state = snapshot
                self.publish_sse("desktop_lyrics", snapshot)
                return success({"broadcast": True})

            if method == "POST" and path == "/api/desktop/command":
                payload = _object(body)
                raw_action = payload.get("action")
                if not isinstance(raw_action, str):
                    raise AppError("DESKTOP_COMMAND_INVALID", "不支持的桌面控制指令", 400)
                action = raw_action.strip()
                if action not in _ALLOWED_DESKTOP_COMMANDS:
                    raise AppError("DESKTOP_COMMAND_INVALID", "不支持的桌面控制指令", 400)
                sse_payload: dict[str, Any] = {"action": action}
                if action == "set_volume":
                    raw_val = payload.get("value")
                    if type(raw_val) not in (int, float) or not math.isfinite(float(raw_val)) or not (0.0 <= float(raw_val) <= 1.0):
                        raise AppError("DESKTOP_COMMAND_INVALID", "音量参数无效", 400)
                    sse_payload["value"] = round(float(raw_val), 4)
                if action == "unlock_desktop_lyrics":
                    self.desktop_lyrics.set_locked(False)
                elif action == "toggle_desktop_lyrics_lock":
                    self.desktop_lyrics.toggle_locked()
                if action in {"show_main_window", "open_lyrics_settings"} and self.wake_window:
                    self.wake_window()
                self.publish_sse("hotkey", sse_payload)
                return success({"action": action, "dispatched": True})

            # --- Cover validation when called through .api() directly ---
            if method == "GET" and path == "/api/cover":
                url = _query_str(query, "url")
                size = _query_int(query, "size", default=64, minimum=1, maximum=2048)
                validated_url = validate_cover_url(url, size)
                return success({"url": validated_url, "size": clamp_cover_size(size)})

            # --- Rules ---
            if method == "GET" and path == "/api/rules":
                return success({"rules": self.rules.get()})
            if method == "PUT" and path == "/api/rules":
                payload = _object(body)
                return success({"rules": self.rules.put(payload.get("rules"))})

            # --- Operations (Write operations hold _auth_lock so logout waits cleanly) ---
            if method == "POST" and path == "/api/operations/backup":
                payload = _object(body, allow_none=True)
                account_id = self._active_account_id_best_effort(allow_remote=False)
                return success(
                    self.operations.backup(
                        payload.get("ids") if payload else None,
                        account_id=account_id,
                    ).to_dict(),
                    201,
                )
            match = re.fullmatch(r"/api/operations/backup/([0-9a-f]{32})/download", path)
            if method == "GET" and match:
                aid = self._active_account_id_best_effort(allow_remote=False)
                return ApiResponse(200, download_snapshot(self.operations.store, match.group(1), aid))
            if method == "GET" and path == "/api/operations/pending":
                with self._auth_lock:
                    if self._offline_generation == self._session_generation or not self.client.cookie:
                        aid = self._offline_account_id or ""
                        pending = self.operations.pending_for(int(aid)) if aid.isascii() and aid.isdigit() else []
                    else:
                        pending = self.operations.pending_for(self.client.account().user_id)
                return success({"operations": pending})
            if method == "POST" and path == "/api/operations/like":
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.like_track(
                        payload.get("song_id"),
                        bool(payload.get("like", True)) if isinstance(payload.get("like", True), bool) else payload.get("like"),
                        expected_session_generation=payload.get("session_generation"),
                        current_session_generation=self._session_generation,
                    )
                return success(result.to_dict())
            if method == "POST" and path == "/api/operations/create-playlist":
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.create_playlist(
                        payload.get("name"),
                        payload.get("song_ids"),
                        payload.get("privacy", 0),
                        expected_session_generation=payload.get("session_generation"),
                        current_session_generation=self._session_generation,
                    )
                return success({**result.to_dict(), "playlist_id": result.resource_id}, 201)
            if method == "POST" and path == "/api/operations/unlike":
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.unlike(
                        payload.get("song_ids"),
                        payload.get("source_playlist_id"),
                        progress_callback=lambda event: self.publish_sse("operation_progress", event),
                        expected_session_generation=payload.get("session_generation"),
                        current_session_generation=self._session_generation,
                        manifest_hash=payload.get("manifest_hash"),
                    )
                return success(result.to_dict())
            match = re.fullmatch(r"/api/playlists/(\d+)/add-tracks", path)
            if method == "POST" and match:
                playlist_id = _positive_id(match.group(1), "PLAYLIST_ID_INVALID")
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.add_tracks(
                        playlist_id,
                        payload.get("song_ids"),
                        expected_session_generation=payload.get("session_generation"),
                        current_session_generation=self._session_generation,
                        progress_callback=lambda event: self.publish_sse("operation_progress", event),
                        manifest_hash=payload.get("manifest_hash"),
                    )
                return success({**result.to_dict(), "playlist_id": playlist_id})
            match = re.fullmatch(r"/api/playlists/(\d+)/remove-tracks", path)
            if method == "POST" and match:
                playlist_id = _positive_id(match.group(1), "PLAYLIST_ID_INVALID")
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.remove_tracks(
                        playlist_id,
                        payload.get("song_ids"),
                        payload.get("source_playlist_id"),
                        expected_session_generation=payload.get("session_generation"),
                        current_session_generation=self._session_generation,
                        progress_callback=lambda event: self.publish_sse("operation_progress", event),
                        manifest_hash=payload.get("manifest_hash"),
                    )
                return success(result.to_dict())
            match = re.fullmatch(r"/api/operations/([0-9a-f]{32})/(reconcile|ignore)", path)
            if method == "POST" and match:
                operation_id, action = match.groups()
                payload = _object(body, allow_none=True)
                with self._auth_lock:
                    self._require_online_write()
                    account = self.client.account()
                    if action == "reconcile":
                        result = self.operations.reconcile_pending(
                            account.user_id,
                            operation_id,
                            confirm_not_applied=bool(payload.get("confirm_not_applied", False)),
                        ).to_dict()
                    else:
                        result = self.operations.ignore_pending(account.user_id, operation_id)
                return success(result)
            if method == "POST" and path == "/api/operations/reconcile":
                payload = _object(body)
                with self._auth_lock:
                    self._require_online_write()
                    result = self.operations.reconcile(
                        payload.get("action"),
                        payload.get("playlist_id"),
                        payload.get("song_ids"),
                        payload.get("source_playlist_id"),
                        confirm_not_applied=bool(payload.get("confirm_not_applied", False)),
                    )
                return success(result.to_dict())

            # --- Canonical Favorites, Unified Hybrid Playlists, Route Cache & Candidate Blocklist ---
            if path == "/api/favorites":
                aid = self._active_account_id_best_effort(allow_remote=False)
                if method == "GET":
                    items = self.db.list_canonical_favorites(aid)
                    return success({"account_id": aid, "tracks": items, "total": len(items)})
                if method == "POST":
                    payload = _object(body)
                    raw_track = payload.get("track") if isinstance(payload.get("track"), dict) else payload
                    tkey = str(payload.get("track_key") or canonical_track_key(raw_track)).strip()
                    if not tkey:
                        raise AppError("TRACK_KEY_INVALID", "缺少有效的曲目标识 track_key", 400)
                    liked = bool(payload.get("liked", True))
                    res = self.db.set_canonical_favorite(aid, tkey, liked=liked, track=raw_track if isinstance(raw_track, dict) else None)
                    return success(res)

            if path == "/api/hybrid-playlists":
                aid = self._active_account_id_best_effort(allow_remote=False)
                if method == "GET":
                    include_tracks = _query_bool(query, "include_tracks")
                    pls = self.db.list_hybrid_playlists(aid, include_tracks=include_tracks)
                    return success({"account_id": aid, "playlists": pls})
                if method == "POST":
                    payload = _object(body)
                    name = str(payload.get("name") or "").strip()
                    if not name:
                        raise AppError("PLAYLIST_NAME_REQUIRED", "歌单名称不能为空", 400)
                    created = self.db.create_hybrid_playlist(
                        aid,
                        name,
                        description=str(payload.get("description") or ""),
                        cover_url=str(payload.get("cover_url") or ""),
                    )
                    initial_tracks = payload.get("tracks")
                    if isinstance(initial_tracks, list) and initial_tracks:
                        self.db.add_tracks_to_hybrid_playlist(aid, created["id"], initial_tracks)
                        created = self.db.get_hybrid_playlist(aid, created["id"]) or created
                    return success(created, 201)

            match = re.fullmatch(r"/api/hybrid-playlists/([^/]+)", path)
            if match:
                aid = self._active_account_id_best_effort(allow_remote=False)
                hpid = unquote(match.group(1))
                if method == "GET":
                    pl = self.db.get_hybrid_playlist(aid, hpid)
                    if pl is None:
                        raise AppError("PLAYLIST_NOT_FOUND", "找不到该混合歌单", 404)
                    return success(pl)
                if method == "DELETE":
                    deleted = self.db.delete_hybrid_playlist(aid, hpid)
                    if not deleted:
                        raise AppError("PLAYLIST_NOT_FOUND", "找不到该混合歌单", 404)
                    return success({"deleted": True, "playlist_id": hpid})

            match = re.fullmatch(r"/api/hybrid-playlists/([^/]+)/tracks", path)
            if match:
                aid = self._active_account_id_best_effort(allow_remote=False)
                hpid = unquote(match.group(1))
                payload = _object(body)
                if method == "POST":
                    tracks = payload.get("tracks")
                    if not isinstance(tracks, list) or not tracks:
                        raise AppError("TRACKS_REQUIRED", "请提供至少一首曲目", 400)
                    try:
                        res = self.db.add_tracks_to_hybrid_playlist(aid, hpid, tracks)
                    except KeyError as exc:
                        raise AppError("PLAYLIST_NOT_FOUND", "找不到该混合歌单", 404) from exc
                    return success(res)
                if method == "DELETE":
                    keys = payload.get("track_keys")
                    if not isinstance(keys, list) or not keys:
                        raise AppError("TRACK_KEYS_REQUIRED", "请提供待移除的曲目标识", 400)
                    return success(self.db.remove_tracks_from_hybrid_playlist(aid, hpid, keys))

            if path == "/api/tracks/route":
                aid = self._active_account_id_best_effort(allow_remote=False)
                if method == "GET":
                    tkey = _query_str(query, "track_key")
                    if not tkey:
                        raise AppError("TRACK_KEY_INVALID", "缺少 track_key", 400)
                    return success({"route": self.db.get_track_source_route(aid, tkey)})
                if method == "PUT":
                    payload = _object(body)
                    saved = self.db.save_track_source_route(
                        aid,
                        str(payload.get("track_key") or ""),
                        provider=str(payload.get("provider") or ""),
                        external_id=str(payload.get("external_id") or ""),
                        quality=str(payload.get("quality") or ""),
                        format_name=str(payload.get("format") or ""),
                        bitrate=int(payload.get("bitrate") or 0),
                        sample_rate=int(payload.get("sample_rate") or 0),
                        stream_url_hint=str(payload.get("stream_url_hint") or ""),
                    )
                    self._clear_audio_cache()
                    return success({"route": saved})
                if method == "DELETE":
                    tkey = _query_str(query, "track_key") or str(_object(body, allow_none=True).get("track_key") or "")
                    deleted = self.db.delete_track_source_route(aid, tkey)
                    self._clear_audio_cache()
                    return success({"deleted": deleted, "track_key": tkey})

            if path == "/api/tracks/blocklist":
                aid = self._active_account_id_best_effort(allow_remote=False)
                if method == "GET":
                    tkey = _query_str(query, "track_key")
                    if not tkey:
                        raise AppError("TRACK_KEY_INVALID", "缺少 track_key", 400)
                    return success({"items": self.db.list_blocked_candidates(aid, tkey)})
                if method == "POST":
                    payload = _object(body)
                    entry = self.db.block_track_candidate(
                        aid,
                        str(payload.get("track_key") or ""),
                        str(payload.get("candidate_key") or ""),
                        reason=str(payload.get("reason") or ""),
                    )
                    self._clear_audio_cache()
                    return success(entry, 201)
                if method == "DELETE":
                    payload = _object(body, allow_none=True)
                    tkey = str(payload.get("track_key") or _query_str(query, "track_key") or "")
                    ckey = str(payload.get("candidate_key") or _query_str(query, "candidate_key") or "")
                    removed = self.db.unblock_track_candidate(aid, tkey, ckey)
                    self._clear_audio_cache()
                    return success({"removed": removed, "track_key": tkey, "candidate_key": ckey})

            # --- OpenSubsonic / Navidrome Protocol ---
            if method == "GET" and path == "/api/opensubsonic/playlists":
                secret = self.channel_vault.get_channel_secret("opensubsonic")
                pls = self.audio.external_resolver.opensubsonic.list_playlists(
                    secret, self.audio.external_resolver._get_http_client()
                )
                return success({"playlists": pls})
            match = re.fullmatch(r"/api/opensubsonic/playlists/([^/]+)", path)
            if method == "GET" and match:
                secret = self.channel_vault.get_channel_secret("opensubsonic")
                pl = self.audio.external_resolver.opensubsonic.get_playlist_tracks(
                    unquote(match.group(1)), secret, self.audio.external_resolver._get_http_client()
                )
                return success(pl)
            if method == "GET" and path == "/api/opensubsonic/search":
                secret = self.channel_vault.get_channel_secret("opensubsonic")
                q_str = _query_str(query, "q")
                lim = _query_int(query, "limit", default=30, minimum=1, maximum=100)
                off = _query_int(query, "offset", default=0, minimum=0, maximum=10_000)
                tracks, has_more = self.audio.external_resolver.opensubsonic.search_tracks(
                    q_str, secret, self.audio.external_resolver._get_http_client(), limit=lim, offset=off
                )
                return success({"tracks": tracks, "has_more": has_more, "offset": off, "limit": lim})

            # --- WebDAV Library & Decentralized Encrypted Sync ---
            if method == "POST" and path == "/api/webdav/scan":
                secret = self.channel_vault.get_channel_secret("webdav")
                tracks = self.audio.external_resolver.webdav.scan_library(
                    secret, self.audio.external_resolver._get_http_client()
                )
                aid = self._active_account_id_best_effort(allow_remote=False)
                for wt in tracks:
                    try:
                        self.db.upsert_canonical_track(aid, wt)
                    except Exception:
                        pass
                return success({"scanned_count": len(tracks), "tracks": tracks[:200]})
            if method == "GET" and path == "/api/webdav/tracks":
                q_str = _query_str(query, "q")
                lim = _query_int(query, "limit", default=100, minimum=1, maximum=500)
                off = _query_int(query, "offset", default=0, minimum=0, maximum=100_000)
                tracks, has_more = self.audio.external_resolver.webdav.search_tracks(q_str, limit=lim, offset=off)
                return success({"tracks": tracks, "has_more": has_more, "offset": off, "limit": lim})
            if method == "POST" and path == "/api/webdav/sync/push":
                payload = _object(body)
                passphrase = str(payload.get("passphrase") or "")
                secret = self.channel_vault.get_channel_secret("webdav")
                aid = self._active_account_id_best_effort(allow_remote=False)
                device_id = DeviceIdentityStore(self.catalog.store).get_or_create_device_id()
                result = self.webdav_sync.push_incremental(
                    secret,
                    passphrase,
                    account_id=aid,
                    db=self.db,
                    rules_service=self.rules,
                    device_id=device_id,
                    http_client=self.audio.external_resolver._get_http_client(),
                )
                return success(result)
            if method == "POST" and path == "/api/webdav/sync/pull":
                payload = _object(body)
                passphrase = str(payload.get("passphrase") or "")
                since_seq = max(0, int(payload.get("since_seq") or 0))
                secret = self.channel_vault.get_channel_secret("webdav")
                aid = self._active_account_id_best_effort(allow_remote=False)
                result = self.webdav_sync.pull_incremental(
                    secret,
                    passphrase,
                    account_id=aid,
                    db=self.db,
                    rules_service=self.rules,
                    since_seq=since_seq,
                    http_client=self.audio.external_resolver._get_http_client(),
                )
                return success(result)

            # --- HTTP Resolver v2 Registry, Subscriptions & Sidecar Bridge ---
            if path == "/api/resolvers":
                if method == "GET":
                    return success(
                        {
                            "resolvers": self.resolver_registry.list_public(),
                            "subscriptions": self.resolver_registry.list_subscriptions(),
                            "health": self.audio.external_resolver.health.snapshot(),
                            "sidecar": self.sidecar.status(),
                        }
                    )
                if method == "POST":
                    created = self.resolver_registry.upsert(_object(body))
                    self._clear_audio_cache()
                    return success(created, 201)
            if method == "POST" and path == "/api/resolvers/subscribe":
                payload = _object(body)
                sub_url = str(payload.get("url") or "").strip()
                doc = payload.get("manifest_json") if isinstance(payload.get("manifest_json"), dict) else fetch_subscription_json(
                    sub_url, http_client=self.audio.external_resolver._get_http_client()
                )
                imported = self.resolver_registry.import_subscription_payload(sub_url, doc)
                self._clear_audio_cache()
                return success(imported)
            if method == "POST" and path == "/api/resolvers/probe":
                payload = _object(body)
                manifest = fetch_resolver_manifest(
                    str(payload.get("url") or ""),
                    self.audio.external_resolver._get_http_client(),
                    api_key=str(payload.get("api_key") or ""),
                )
                if manifest is None:
                    raise AppError("RESOLVER_MANIFEST_UNAVAILABLE", "目标接口未返回有效的 HTTP Resolver v2 /manifest", 502)
                return success({"ok": True, "manifest": manifest})
            match = re.fullmatch(r"/api/resolvers/([a-z0-9_.-]+)", path)
            if method == "DELETE" and match:
                removed = self.resolver_registry.remove(match.group(1))
                if not removed:
                    raise AppError("RESOLVER_NOT_FOUND", "找不到该 Resolver", 404)
                self._clear_audio_cache()
                return success({"deleted": True, "id": match.group(1)})

            if method == "GET" and path == "/api/sidecar/status":
                return success(self.sidecar.status())
            if method == "POST" and path == "/api/sidecar/start":
                payload = _object(body, allow_none=True)
                status_info = self.sidecar.start(
                    adapter_path=str(payload.get("adapter_path") or ""),
                    port=int(payload.get("port") or 0),
                )
                return success(status_info)
            if method == "POST" and path == "/api/sidecar/stop":
                return success(self.sidecar.stop())

            # --- Cross-Platform Playlist Export & Migration Assistant ---
            match = re.fullmatch(r"/api/playlists/([^/]+)/export", path)
            if method == "GET" and match:
                pid = unquote(match.group(1))
                fmt = _query_str(query, "format") or "json"
                aid = self._active_account_id_best_effort(allow_remote=False)
                playlist_obj: dict[str, Any] | None = None
                if pid.startswith("hybrid:"):
                    playlist_obj = self.db.get_hybrid_playlist(aid, pid)
                else:
                    cat = self.db.get_catalog_dict(aid)
                    entry = cat.get("playlists", {}).get(pid)
                    if isinstance(entry, dict):
                        playlist_obj = {**entry["summary"], "songs": entry["songs"]}
                if playlist_obj is None:
                    raise AppError("PLAYLIST_NOT_FOUND", "找不到要导出的歌单", 404)
                content_text, mime, ext = export_playlist_content(playlist_obj, fmt)
                return success(
                    {
                        "playlist_id": pid,
                        "name": playlist_obj.get("name", pid),
                        "format": ext,
                        "mime": mime,
                        "content": content_text,
                        "track_count": len(playlist_obj.get("songs") or []),
                    }
                )
            if method == "POST" and path == "/api/playlists/migrate/preview":
                payload = _object(body)
                parsed_import = parse_playlist_import(str(payload.get("content") or ""), str(payload.get("format") or "auto"))
                aid = self._active_account_id_best_effort(allow_remote=False)
                cat = self.db.get_catalog_dict(aid)
                catalog_tracks = [s for p in cat.get("playlists", {}).values() for s in p.get("songs", [])]
                catalog_tracks.extend(self.db.list_canonical_tracks(aid))
                for playlist in self.db.list_hybrid_playlists(aid, include_tracks=True):
                    catalog_tracks.extend(playlist["songs"])
                local_tracks = self.db.list_local_files(limit=5000)
                allow_online = bool(payload.get("allow_online", False))
                online_fn = (
                    lambda kw, limit=5: self.audio.external_resolver.search_multi_channel(
                        kw, limit=limit, netease_client=self.client if self.client.cookie else None
                    ).get("tracks", [])
                ) if allow_online else None
                matched = match_migration_entries(
                    parsed_import["entries"],
                    catalog_tracks=catalog_tracks,
                    local_tracks=local_tracks,
                    online_search_fn=online_fn,
                    allow_online=allow_online,
                )
                return success(
                    {
                        "detected_format": parsed_import["format"],
                        "suggested_name": parsed_import["playlist_name"] or "迁移歌单",
                        **matched,
                    }
                )
            if method == "POST" and path == "/api/playlists/migrate/commit":
                payload = _object(body)
                aid = self._active_account_id_best_effort(allow_remote=False)
                target_pid = str(payload.get("playlist_id") or "").strip()
                pl_name = str(payload.get("playlist_name") or "迁移歌单").strip() or "迁移歌单"
                tracks = payload.get("tracks")
                if not isinstance(tracks, list) or not tracks:
                    raise AppError("MIGRATION_EMPTY", "没有可写入的匹配曲目", 400)
                if not target_pid:
                    created = self.db.create_hybrid_playlist(aid, pl_name, description="跨平台歌单迁移导入")
                    target_pid = created["id"]
                added = self.db.add_tracks_to_hybrid_playlist(aid, target_pid, tracks)
                return success({"playlist": self.db.get_hybrid_playlist(aid, target_pid), **added}, 201)

            raise AppError("ROUTE_NOT_FOUND", "接口不存在", 404)
        except AppError as exc:
            return failure(exc)
        except (TypeError, ValueError):
            return failure(AppError("INVALID_INPUT", "输入数据无效", 400))
        except Exception:
            return failure(AppError("INTERNAL_ERROR", "程序内部错误", 500))

    def _sync_playlist(self, playlist_id: int) -> ApiResponse:
        gen, cookie, cached = self._session_snapshot()
        scoped = self._client_for_cookie(cookie)
        account = self._resolve_account_unlocked(gen, cookie, cached, scoped)
        # Allow syncing liked, self_created, AND subscribed playlists for local playback.
        all_playlists = scoped.playlists(account, include_subscribed=True)
        target = next((item for item in all_playlists if item.id == playlist_id), None)
        if target is None:
            raise AppError("PLAYLIST_NOT_FOUND", "找不到这个歌单", 404)

        def emit_progress(event: dict[str, Any]) -> None:
            self.publish_sse("sync_progress", {"type": "sync", **event, "account_id": str(account.user_id), "session_generation": gen})

        with self._sync_semaphore:
            self._assert_session_unchanged(gen)
            try:
                synced = self.catalog.sync(
                    target,
                    account_id=str(account.user_id),
                    client_override=scoped,
                    on_progress=emit_progress,
                )
            except AppError as exc:
                emit_progress({"stage": "error", "playlist_id": playlist_id, "error": exc.code, "message": exc.message})
                raise
        self._assert_session_unchanged(gen)
        return success(synced)

    def _catalog_view(self, query: dict[str, list[str]]) -> ApiResponse:
        gen, cookie, cached = self._session_snapshot()
        requested_account = _query_str(query, "account_id")
        if cookie and gen != self._offline_generation:
            if cached is not None:
                effective_account = str(cached.user_id)
            else:
                scoped = self._client_for_cookie(cookie)
                try:
                    account = self._resolve_account_unlocked(gen, cookie, None, scoped)
                    effective_account = str(account.user_id)
                except AppError as exc:
                    if exc.code in {"UPSTREAM_UNAVAILABLE", "UPSTREAM_HTTP_ERROR"} and requested_account and requested_account != "unclaimed":
                        effective_account = requested_account
                    elif exc.code == "SESSION_EXPIRED":
                        effective_account = requested_account if (requested_account and requested_account != "unclaimed") else "local"
                    else:
                        effective_account = self.catalog._active_account_id or "local"
        else:
            if self._offline_account_id is not None:
                effective_account = self._offline_account_id
            elif requested_account and requested_account != "unclaimed" and len(requested_account) <= 64:
                effective_account = requested_account
            else:
                effective_account = self._active_account_id_best_effort()

        playlist_id = _query_str(query, "playlist_id") or None
        q = _query_str(query, "q")
        filter_name = _query_str(query, "filter") or _query_str(query, "filter_name")
        sort = _query_str(query, "sort")
        direction = _query_str(query, "direction") or "asc"
        page = _query_optional_int(query, "page", minimum=1, maximum=100_000)
        page_size = _query_optional_int(query, "page_size", minimum=1, maximum=2000)

        view_data = self.catalog.view(
            account_id=effective_account,
            playlist_id=playlist_id,
            q=q,
            filter_name=filter_name,
            sort=sort,
            direction=direction,
            page=page,
            page_size=page_size,
        )
        with self._auth_lock:
            self._assert_session_unchanged(gen)
            if not self._offline_account_id:
                self.catalog.set_active_account(effective_account)
        return success({**view_data, "account_id": effective_account})

    def _lyrics(self, query: dict[str, list[str]]) -> ApiResponse:
        provider = _query_str(query, "provider").lower()
        external_id = _query_str(query, "external_id")
        track_key = _query_str(query, "track_key")
        title = _query_str(query, "title")
        artist = _query_str(query, "artist")
        if track_key and not provider:
            head, sep, tail = track_key.partition(":")
            head_l = head.strip().lower()
            if sep and head_l in {"gdstudio", "resolver", "resolver_v2"} and ":" in tail:
                sub, _, ext = tail.partition(":")
                sub_l = sub.strip().lower()
                ext = ext.strip()
                if sub_l and ext:
                    provider, external_id = f"{head_l}:{sub_l}", ext
            if not provider:
                p, ext = parse_track_key(track_key)
                if p == "netease" and ext.isdigit() and not _query_str(query, "id"):
                    query = {**query, "id": [ext]}
                elif p and p != "netease":
                    provider, external_id = p, ext

        if provider == "opensubsonic":
            secret = self.channel_vault.get_channel_secret("opensubsonic")
            ly = self.audio.external_resolver.opensubsonic.fetch_lyrics(
                secret,
                self.audio.external_resolver._get_http_client(),
                song_id=external_id,
                artist=artist,
                title=title,
            ) or {}
            return success(
                {
                    "id": 0,
                    "audio_id": 0,
                    "track_key": f"opensubsonic:{external_id}" if external_id else track_key,
                    "lyrics": str(ly.get("lrc") or ""),
                    "tlyrics": str(ly.get("tlyric") or ""),
                    "romalrc": str(ly.get("romalrc") or ""),
                    "yrc": str(ly.get("yrc") or ""),
                    "chorus_start": None,
                    "chorus_end": None,
                    "ttml_source": str(ly.get("source") or ""),
                }
            )
        raw_song_id = _query_str(query, "id")
        has_positive_song_id = raw_song_id.isdigit() and int(raw_song_id) > 0
        if provider and provider != "netease" and not has_positive_song_id:
            # Prioritize the Resolver v2 endpoint matching provider, then fall back to legacy custom and remaining endpoints.
            base_prov = provider.split(":", 1)[0]
            sub_prov = provider.split(":", 1)[1] if ":" in provider else ""
            preferred_ids = {provider, sub_prov} if base_prov in {"resolver", "resolver_v2"} and sub_prov else {provider}
            preferred_endpoints: list[tuple[str, str]] = []
            fallback_endpoints: list[tuple[str, str]] = []
            for rcfg in self.audio.external_resolver._active_v2_resolvers():
                caps = [str(c).lower() for c in (rcfg.get("capabilities") or [])]
                if caps and "lyric" not in caps:
                    continue
                rurl = str(rcfg.get("url") or "").strip()
                if not rurl:
                    continue
                entry = (rurl, str(rcfg.get("api_key") or ""))
                if str(rcfg.get("id") or "").lower() in preferred_ids:
                    preferred_endpoints.append(entry)
                else:
                    fallback_endpoints.append(entry)
            custom_api, custom_key = self.audio.external_resolver._effective_custom_api()
            if custom_api:
                if provider in {"custom", "custom_endpoint"}:
                    preferred_endpoints.append((custom_api, custom_key))
                else:
                    fallback_endpoints.append((custom_api, custom_key))
            lyric_endpoints = [*preferred_endpoints, *fallback_endpoints]
            ly = None
            http_client = self.audio.external_resolver._get_http_client()
            for ep_url, ep_key in lyric_endpoints:
                ly = fetch_resolver_lyric(
                    ep_url,
                    http_client,
                    track_id=external_id,
                    title=title,
                    artist=artist,
                    api_key=ep_key,
                )
                if ly:
                    break
            ly = ly or {}
            return success(
                {
                    "id": 0,
                    "audio_id": 0,
                    "track_key": track_key or f"{provider}:{external_id}",
                    "lyrics": str(ly.get("lrc") or ""),
                    "tlyrics": str(ly.get("tlyric") or ""),
                    "romalrc": str(ly.get("romalrc") or ""),
                    "yrc": str(ly.get("yrc") or ""),
                    "chorus_start": None,
                    "chorus_end": None,
                    "ttml_source": str(ly.get("source") or ""),
                }
            )

        song_id = _query_id(query)
        audio_id_raw = _query_str(query, "audio_id")
        audio_id = _positive_id(audio_id_raw, "SONG_ID_INVALID") if audio_id_raw else None

        gen, cookie, _ = self._session_snapshot()
        scoped = self._client_for_cookie(cookie)
        primary_id = audio_id if (audio_id is not None and audio_id != song_id) else song_id

        lyrics_data = self._call_client_lyrics(scoped, primary_id)
        if primary_id != song_id and not any(
            str(lyrics_data.get(k) or "").strip() for k in ("lyrics", "yrc", "tlyrics", "romalrc")
        ):
            lyrics_data = self._call_client_lyrics(scoped, song_id)

        chorus_start: int | None = None
        chorus_end: int | None = None
        lyrics_account = self._active_account_id_best_effort()
        cached_song = self.db.get_song(song_id, lyrics_account)
        if cached_song is not None and (cached_song.chorus_start is not None or cached_song.chorus_end is not None):
            chorus_start = cached_song.chorus_start
            chorus_end = cached_song.chorus_end
        elif hasattr(scoped, "chorus"):
            try:
                chorus_start, chorus_end = scoped.chorus(song_id)
                if chorus_start is not None or chorus_end is not None:
                    self.db.update_song_chorus(song_id, chorus_start, chorus_end, lyrics_account)
            except (AppError, AssertionError, Exception):
                chorus_start, chorus_end = None, None

        self._assert_session_unchanged(gen)
        yrc_text = str(lyrics_data.get("yrc") or "")
        lrc_text = str(lyrics_data.get("lyrics") or "")
        tlyric_text = str(lyrics_data.get("tlyrics") or "")
        roma_text = str(lyrics_data.get("romalrc") or "")
        ttml_source = ""

        if not yrc_text.strip() and bool(self.user_settings.load().get("enable_amll_ttml_fallback", True)):
            amll_data = self._maybe_fetch_amll_ttml(primary_id)
            if isinstance(amll_data, dict) and str(amll_data.get("yrc") or "").strip():
                yrc_text = str(amll_data.get("yrc") or "")
                if not lrc_text.strip() and str(amll_data.get("lrc") or "").strip():
                    lrc_text = str(amll_data.get("lrc") or "")
                if not tlyric_text.strip() and str(amll_data.get("tlyrics") or "").strip():
                    tlyric_text = str(amll_data.get("tlyrics") or "")
                if not roma_text.strip() and str(amll_data.get("romalrc") or "").strip():
                    roma_text = str(amll_data.get("romalrc") or "")
                ttml_source = str(amll_data.get("ttml_source") or "amll-ttml-db")

        return success(
            {
                "id": song_id,
                "audio_id": primary_id,
                "lyrics": lrc_text,
                "tlyrics": tlyric_text,
                "romalrc": roma_text,
                "yrc": yrc_text,
                "chorus_start": chorus_start,
                "chorus_end": chorus_end,
                "ttml_source": ttml_source,
            }
        )

    def _maybe_fetch_amll_ttml(self, song_id: int) -> dict[str, Any] | None:
        fetcher = self._amll_fetcher
        if fetcher is None:
            if not is_trusted_netease_api_base(self.settings.api_base):
                return None
            fetcher = fetch_amll_ttml_as_yrc
        try:
            return fetcher(song_id)
        except Exception:
            return None

    @staticmethod
    def _call_client_lyrics(client: NeteaseClient, song_id: int) -> dict[str, Any]:
        lyrics_v2 = getattr(client, "lyrics_v2", None)
        if callable(lyrics_v2):
            return lyrics_v2(song_id)
        return client.lyrics(song_id)

    def _audio(self, query: dict[str, list[str]]) -> ApiResponse:
        gen, cookie, _ = self._session_snapshot()
        try:
            with stream_session(gen):
                resolved = self._resolve_audio(query, cookie)
        except Exception:
            with self._auth_lock:
                if gen != self._session_generation:
                    raise AppError("SESSION_CHANGED", "登录会话已变更，请重试", 409) from None
            raise
        with self._auth_lock:
            if gen != self._session_generation:
                self.audio.revoke_stream_token(resolved.get("stream_token"))
                raise AppError("SESSION_CHANGED", "登录会话已变更，请重试", 409)
            return success(resolved)

    def _resolve_audio(self, query, cookie):
        provider = _query_str(query, "provider")
        external_id = _query_str(query, "external_id")
        if provider and external_id:
            title = _query_str(query, "title")
            raw_artist = _query_str(query, "artist") or _query_str(query, "artists")
            artists = [a.strip() for a in raw_artist.replace("；", ";").split(";") if a.strip()] if raw_artist else []
            duration = _query_int(query, "duration", default=0, minimum=0, maximum=86_400_000)
            stream_url_hint = _query_str(query, "stream_url_hint")
            v2_bases = [
                str(r.get("url") or "")
                for r in self.audio.external_resolver._active_v2_resolvers()
                if r.get("url")
            ]
            base = (self.user_settings.load().get("custom_external_api", "") if provider in {"custom", "custom_endpoint"}
                    else next((ch["api_base"] for ch in self.channel_vault.public_summary()
                               if ch.get("channel") == provider and ch.get("api_base")), ""))
            if stream_url_hint and not is_safe_external_url(stream_url_hint, loopback_bases=(base, *v2_bases)):
                raise AppError("STREAM_URL_REJECTED", "音频地址不被允许", 403)
            raw_song_id = _query_str(query, "id")
            song_id_val = int(raw_song_id) if raw_song_id.isdigit() else 0
            resolve_ext = getattr(self.audio, "resolve_external_track", None)
            if not callable(resolve_ext):
                raise AppError("EXTERNAL_AUDIO_UNSUPPORTED", "当前版本不支持跨渠道直连播放", 501)
            resolved = resolve_ext(
                provider=provider,
                external_id=external_id,
                title=title,
                artists=artists,
                duration_ms=duration,
                stream_url_hint=stream_url_hint,
                song_id=song_id_val,
            )
            return resolved

        song_id = _query_id(query)
        quality = _query_str(query, "quality") or None
        allow_external = _query_optional_bool(query, "allow_external")
        force_refresh = _query_bool(query, "force_refresh")
        candidate_id = _query_str(query, "candidate_id") or None
        song_hint = self._song_hint_from_query(song_id, query)

        scoped = self._client_for_cookie(cookie)
        resolve_kwargs: dict[str, Any] = {
            "quality": quality,
            "allow_external": allow_external,
            "force_refresh": force_refresh,
            "candidate_id": candidate_id,
            "client_override": scoped,
        }
        try:
            sig = inspect.signature(self.audio.resolve)
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if song_hint is not None and (has_var_kw or "song_hint" in sig.parameters):
                resolve_kwargs["song_hint"] = song_hint
            if has_var_kw or "account_id" in sig.parameters:
                resolve_kwargs["account_id"] = self._active_account_id_best_effort(allow_remote=False)
        except (TypeError, ValueError):
            pass
        resolved = self.audio.resolve(song_id, **resolve_kwargs)
        return resolved

    @staticmethod
    def _song_hint_from_query(song_id: int, query: dict[str, list[str]]) -> Song | None:
        title = _query_str(query, "title")
        if not title:
            return None
        raw_artist = _query_str(query, "artist") or _query_str(query, "artists")
        artists = [a.strip() for a in raw_artist.replace("；", ";").split(";") if a.strip()] if raw_artist else []
        duration = _query_int(query, "duration", default=0, minimum=0, maximum=86_400_000)
        return Song(id=song_id, title=title, artists=artists, duration=duration)

    def _search_multi(self, query: dict[str, list[str]]) -> ApiResponse:
        raw_q = _query_str(query, "q") or _query_str(query, "keywords")
        if not raw_q or len(raw_q) > 200:
            raise AppError("SEARCH_QUERY_INVALID", "搜索关键词不能为空且不超过 200 个字符")
        limit = _query_int(query, "limit", default=30, minimum=1, maximum=100)
        offset = _query_int(query, "offset", default=0, minimum=0, maximum=1000)
        raw_channels = _query_str(query, "channels")
        channels = [c.strip().lower() for c in raw_channels.split(",") if c.strip()] if raw_channels else None
        gen, cookie, _ = self._session_snapshot()
        scoped = self._client_for_cookie(cookie)
        resolver = getattr(self.audio, "external_resolver", None)
        search_fn = getattr(resolver, "search_multi_channel", None)
        if not callable(search_fn):
            # Clean fallback to NetEase search if multi-channel resolver is unavailable
            try:
                songs = scoped.search_songs(raw_q, limit=limit, offset=offset)
            except TypeError:
                songs = scoped.search_songs(raw_q, limit=limit)
            self._assert_session_unchanged(gen)
            tracks = [{**s.to_dict(), "provider": "netease", "available_channels": ["netease"]} for s in songs]
            return success(
                {
                    "query": raw_q,
                    "offset": offset,
                    "limit": limit,
                    "has_more": len(tracks) >= limit,
                    "channels_queried": ["netease"],
                    "tracks": tracks,
                    "channel_errors": {},
                }
            )
        custom_api = str(self.user_settings.load().get("custom_external_api") or "")
        search_kwargs: dict[str, Any] = {
            "channels": channels,
            "limit": limit,
            "netease_client": scoped,
            "custom_api": custom_api,
        }
        try:
            sig = inspect.signature(search_fn)
            if "offset" in sig.parameters or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            ):
                search_kwargs["offset"] = offset
        except (TypeError, ValueError):
            search_kwargs["offset"] = offset
        raw_cursors = _query_str(query, "cursors")
        if raw_cursors:
            try:
                cursors = json.loads(raw_cursors)
                if not isinstance(cursors, dict) or len(cursors) > 12 or any(
                    v is not None and (not isinstance(v, list) or len(v) != 2 or
                                       any(type(n) is not int or not 0 <= n <= 1000 for n in v))
                    for v in cursors.values()
                ):
                    raise ValueError
            except ValueError as exc:
                raise AppError("INVALID_CURSOR", "分页游标无效") from exc
            search_kwargs["cursors"] = cursors
        result = search_fn(raw_q, **search_kwargs)
        self._assert_session_unchanged(gen)
        if isinstance(result, dict):
            tracks = result.get("tracks") if isinstance(result.get("tracks"), list) else []
            normalized = {
                "query": str(result.get("query") or raw_q),
                "offset": int(result.get("offset", offset)),
                "limit": int(result.get("limit", limit)),
                "has_more": bool(result.get("has_more", len(tracks) >= limit)),
                "next_cursors": result.get("next_cursors"),
                "channels_queried": list(result.get("channels_queried") or []),
                "tracks": tracks,
                "channel_errors": dict(result.get("channel_errors") or {}),
            }
            return success(normalized)
        return success(result)

    def _channel_capability_for(self, channel_id: str, custom_api: str = "") -> str:
        try:
            secret = self.channel_vault.get_channel_secret(channel_id)
        except Exception:
            secret = None
        return channel_capability(channel_id, secret, custom_api=custom_api if channel_id == "custom" else "")

    def _probe_channel_result(self, channel_id: str, *, custom_api: str, revision: str) -> dict[str, Any]:
        resolver = getattr(self.audio, "external_resolver", None)
        test_fn = getattr(resolver, "test_channel", None)
        if not callable(test_fn):
            raise AppError("CHANNEL_TEST_UNSUPPORTED", "当前版本不支持渠道连通性测试", 501)
        result = dict(test_fn(channel_id, custom_api=custom_api))
        if not result.get("capability"):
            result["capability"] = self._channel_capability_for(channel_id, custom_api=custom_api)
        self.channel_vault.record_probe(channel_id, revision, result.get("ok") is True)
        return result

    def _probe_single_channel_safe(self, channel_id: str) -> dict[str, Any] | None:
        try:
            revision = self.channel_vault.get_channel_public(channel_id)["updated_at"]
            custom_api = str(self.user_settings.load().get("custom_external_api") or "")
            return self._probe_channel_result(channel_id, custom_api=custom_api, revision=revision)
        except Exception:
            return None

    def _test_channel(self, channel_id: str) -> ApiResponse:
        # Ensure channel_id is recognized (404 on unknown channel)
        revision = self.channel_vault.get_channel_public(channel_id)["updated_at"]
        custom_api = str(self.user_settings.load().get("custom_external_api") or "")
        result = self._probe_channel_result(channel_id, custom_api=custom_api, revision=revision)
        return success(result)

    def _test_all_channels(self) -> ApiResponse:
        resolver = getattr(self.audio, "external_resolver", None)
        test_fn = getattr(resolver, "test_channel", None)
        if not callable(test_fn):
            raise AppError("CHANNEL_TEST_UNSUPPORTED", "当前版本不支持渠道连通性测试", 501)
        custom_api = str(self.user_settings.load().get("custom_external_api") or "")
        initial_summary = self.channel_vault.public_summary()
        revisions = {row["channel"]: row["updated_at"] for row in initial_summary}
        capabilities = {
            row["channel"]: (
                "custom_endpoint"
                if (row["channel"] == "custom" and (custom_api or row["api_base"]))
                else (
                    "unconfigured"
                    if row["channel"] == "custom"
                    else (
                        "public_search"
                        if row["channel"] == "gdstudio"
                        else (
                            "custom_endpoint"
                            if row["api_base"]
                            else ("authenticated_search" if row["has_credential"] else "public_search")
                        )
                    )
                )
            )
            for row in initial_summary
        }
        timeout_sec = max(0.05, float(getattr(self, "_channel_probe_timeout_sec", 8.0)))

        results: dict[str, dict[str, Any]] = {}
        executor = ThreadPoolExecutor(max_workers=len(SUPPORTED_CHANNELS), thread_name_prefix="pruner-ch-probe")
        try:
            future_map = {
                executor.submit(test_fn, cid, custom_api=custom_api): cid
                for cid in SUPPORTED_CHANNELS
            }
            done, not_done = wait_futures(future_map.keys(), timeout=timeout_sec)
            for fut in done:
                cid = future_map[fut]
                cap = capabilities.get(cid, "unconfigured")
                try:
                    raw = dict(fut.result())
                    if not raw.get("capability"):
                        raw["capability"] = cap
                    raw.setdefault("channel", cid)
                    raw.setdefault("max_quality_hint", "")
                    results[cid] = raw
                except Exception:
                    results[cid] = {
                        "channel": cid,
                        "ok": False,
                        "latency_ms": 0,
                        "status": "error",
                        "capability": cap,
                        "message": "渠道连通性测试失败，请检查网络或配置",
                        "max_quality_hint": "",
                    }
            for fut in not_done:
                cid = future_map[fut]
                fut.cancel()
                results[cid] = {
                    "channel": cid,
                    "ok": False,
                    "latency_ms": int(timeout_sec * 1000),
                    "status": "timeout",
                    "capability": capabilities.get(cid, "unconfigured"),
                    "message": "渠道连通性测试超时",
                    "max_quality_hint": "",
                }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        ordered_results: dict[str, dict[str, Any]] = {cid: results[cid] for cid in SUPPORTED_CHANNELS}
        probe_outcomes = {
            cid: (revisions.get(cid, ""), ordered_results[cid].get("ok") is True)
            for cid in SUPPORTED_CHANNELS
        }
        channels_summary = self.channel_vault.record_probes(probe_outcomes)
        ok_count = sum(1 for item in ordered_results.values() if item.get("ok") is True)
        self.publish_sse(
            "channels",
            {
                "channel": "all",
                "status": "probed",
                "message": f"已完成全部渠道连通性检测（{ok_count}/{len(SUPPORTED_CHANNELS)} 可用）",
                "channels": channels_summary,
                "results": ordered_results,
            },
        )
        return success({"results": ordered_results, "channels": channels_summary})

    def _create_bilibili_qr(self) -> ApiResponse:
        return success(self.bilibili_qr.create_qr())

    def _poll_bilibili_qr(self, qrcode_key: str) -> ApiResponse:
        raw = self.bilibili_qr.poll_qr(qrcode_key)
        secrets_payload = raw.pop("secrets", None)
        response_data: dict[str, Any] = {
            "state": raw["state"],
            "code": raw["code"],
            "message": raw["message"],
            "channel": "bilibili",
        }
        if raw["state"] == "confirmed" and isinstance(secrets_payload, dict):
            self.channel_vault.update_channel(
                "bilibili",
                {
                    "enabled": True,
                    "sessdata": str(secrets_payload.get("sessdata") or ""),
                    "cookie": str(secrets_payload.get("cookie") or ""),
                },
            )
            self._clear_audio_cache()
            probe = self._probe_single_channel_safe("bilibili")
            channels_summary = self.channel_vault.public_summary()
            self.publish_sse(
                "channels",
                {
                    "channel": "bilibili",
                    "status": "authorized",
                    "message": raw["message"],
                    "channels": channels_summary,
                },
            )
            response_data["channels"] = channels_summary
            if probe is not None:
                response_data["probe"] = probe
        return success(response_data)

    def complete_channel_auth(self, channel_id: str, vault_payload: dict[str, Any]) -> dict[str, Any]:
        """Store captured official login credentials in DPAPI vault, probe, and broadcast SSE."""
        ch = str(channel_id or "").strip().lower()
        if ch not in CHANNEL_AUTH_CONFIG:
            raise AppError("CHANNEL_AUTH_UNSUPPORTED", f"渠道 {ch or 'unknown'} 不支持官方弹窗登录", 400)
        self.channel_vault.update_channel(ch, vault_payload)
        self._clear_audio_cache()
        self._probe_single_channel_safe(ch)
        channels_summary = self.channel_vault.public_summary()
        self.publish_sse(
            "channels",
            {
                "channel": ch,
                "status": "authorized",
                "message": "官方登录已完成，凭证已通过 DPAPI 加密保存",
                "channels": channels_summary,
            },
        )
        return self.channel_vault.get_channel_public(ch)

    def broadcast_channel_auth_status(self, channel_id: str, status: str, message: str) -> None:
        ch = str(channel_id or "").strip().lower()
        channels_summary = self.channel_vault.public_summary()
        self.publish_sse(
            "channels",
            {
                "channel": ch,
                "status": str(status or "cancelled"),
                "message": str(message or ""),
                "channels": channels_summary,
            },
        )

    def _open_channel_auth_window(self, payload: dict[str, Any]) -> ApiResponse:
        channel = str(payload.get("channel") or "").strip().lower()
        cfg = CHANNEL_AUTH_CONFIG.get(channel)
        if cfg is None:
            raise AppError("CHANNEL_AUTH_UNSUPPORTED", f"渠道 {channel or 'unknown'} 不支持官方弹窗登录", 400)
        login_url = cfg["login_url"]
        ctrl = self.channel_auth_window
        if ctrl is None or not callable(getattr(ctrl, "open_auth_window", None)):
            return success(
                {
                    "supported": False,
                    "opened": False,
                    "channel": channel,
                    "login_url": login_url,
                    "message": "当前为浏览器/无头模式，请使用桌面客户端内置登录窗口或手动配置凭证",
                }
            )
        opened = bool(ctrl.open_auth_window(channel))
        return success(
            {
                "supported": True,
                "opened": opened,
                "channel": channel,
                "login_url": login_url,
                "message": "已打开官方登录窗口，完成登录后将自动加密保存凭证" if opened else "无法打开登录窗口，请稍后重试",
            }
        )

    def _hydrate_song(self, song_id: int) -> ApiResponse:
        gen, cookie, cached = self._session_snapshot()
        scoped = self._client_for_cookie(cookie)
        account_id = str(cached.user_id) if cached is not None else self._active_account_id_best_effort(allow_remote=False)
        existing = self.db.get_song(song_id, account_id=account_id)
        try:
            fetched = scoped.song_detail(song_id)
        except AppError as exc:
            self._assert_session_unchanged(gen)
            return success(
                {
                    "hydrated": False,
                    "unavailable_metadata": True,
                    "reason": exc.message,
                    "song": existing.to_dict() if existing is not None else None,
                }
            )
        self._assert_session_unchanged(gen)
        if fetched is None or is_stub_song(fetched):
            return success(
                {
                    "hydrated": False,
                    "unavailable_metadata": True,
                    "reason": "上游未返回该曲目元数据（可能已下架或版权移除）",
                    "song": existing.to_dict() if existing is not None else None,
                }
            )
        self.db.upsert_song_metadata(account_id, fetched)
        updated = self.db.get_song(song_id, account_id=account_id) or fetched
        return success(
            {
                "hydrated": True,
                "unavailable_metadata": False,
                "reason": "",
                "song": updated.to_dict(),
            }
        )

    def _radio_batch(self, payload, *, explore=False):
        mode = payload.get("mode", "start")
        if mode not in ("start", "rearrange", "refill"):
            raise AppError("INVALID_INPUT", "电台模式无效", 400)
        limit = payload.get("limit", 20 if explore else 30)
        ratio = payload.get("explore_ratio", self.user_settings.load()["smart_radio_explore_ratio"])
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(ratio) not in (float, int)
            or not math.isfinite(float(ratio))
            or not 0.0 <= float(ratio) <= 1.0
        ):
            raise AppError("INVALID_INPUT", "电台批次参数无效", 400)
        session = payload.get("radio_session_id", "default")
        request = payload.get("request_id", secrets.token_hex(16))
        if not all(isinstance(k, str) and 0 < len(k) <= 200 for k in (session, request)):
            raise AppError("INVALID_INPUT", "电台会话或请求标识无效", 400)
        superseded = payload.get("superseded_request_ids", [])
        if (not isinstance(superseded, list) or len(superseded) > 50 or
                not all(isinstance(k, str) and k.strip() and len(k) <= 200 for k in superseded)):
            raise AppError("INVALID_INPUT", "已替代的请求标识无效", 400)
        ids = payload.get("exclude_ids", [])
        keys = payload.get("exclude_keys", [])
        replay = payload.get("allow_replay", False)
        if (not isinstance(keys, list) or not all(isinstance(k,str) for k in keys)
                or not isinstance(ids, list) or not all(type(i) is int for i in ids) or type(replay) is not bool):
            raise AppError("INVALID_INPUT", "电台排除参数无效", 400)
        with recommendation_reads(0 if replay and not explore else 2 if mode == "refill" or explore else 3) as budget:
            with self._auth_lock:
                gen, cookie, aid, scoped = self._discovery_scope()
                self.operations._verify_session_generation(payload.get("session_generation"), gen)
                account = scoped.account() if aid is None and scoped.cookie and not (replay and not explore) else None
                if aid is None:
                    aid = str(account.user_id) if account else self.client.cookie_key(cookie)
                if "expected_account_id" in payload and str(payload["expected_account_id"]) != aid:
                    raise AppError("SESSION_GENERATION_MISMATCH", "操作账号已变更", 409)
                if account:
                    self._record_account_if_current(gen, cookie, account)

            def verify():
                if gen != self._session_generation or cookie != self._session_snapshot()[1]:
                    raise AppError("SESSION_GENERATION_MISMATCH", "电台会话已变更", 409)

            def commit(batch):
                with self._auth_lock:
                    verify()
                    return self.db.commit_radio_batch(aid, batch)

            options = dict(client_override=scoped, mode=mode, explore_ratio=ratio, limit=limit, budget=budget,
                           exclude_ids=ids, exclude_keys=keys, allow_replay=replay)
            if explore:
                result = self.recommend.explore_batch(aid, **options)
            else:
                result = self.recommend.smart_radio(aid, radio_session_id=session, request_id=request, commit=commit,
                                                    superseded_request_ids=superseded, **options)
            with self._auth_lock:
                verify()
            return success(result)

    def _recommend_smart_radio(self, query: dict[str, list[str]]) -> ApiResponse:
        raw_ratio = _query_str(query, "explore_ratio")
        if raw_ratio:
            try:
                parsed_ratio = float(raw_ratio)
                if not math.isfinite(parsed_ratio):
                    raise ValueError("non-finite")
                explore_ratio = max(0.0, min(1.0, parsed_ratio))
            except ValueError as exc:
                raise AppError("INVALID_INPUT", "explore_ratio 参数无效", 400) from exc
        else:
            explore_ratio = float(self.user_settings.load().get("smart_radio_explore_ratio", 0.4))
        limit = _query_int(query, "limit", default=30, minimum=1, maximum=100)
        raw_exclude = _query_str(query, "exclude_ids")
        exclude_ids: set[int] = set()
        if raw_exclude:
            for part in raw_exclude.split(","):
                cleaned = part.strip()
                if cleaned.isdigit() and int(cleaned) > 0:
                    exclude_ids.add(int(cleaned))
        gen, cookie, aid, scoped = self._discovery_scope()
        account_id = aid or self.client.cookie_key(cookie)
        smart_fn = getattr(self.recommend, "smart_radio", None)
        if not callable(smart_fn):
            raise AppError("SMART_RADIO_UNSUPPORTED", "当前版本不支持真·心动电台", 501)
        result = smart_fn(
            account_id,
            explore_ratio=explore_ratio,
            exclude_ids=exclude_ids,
            limit=limit,
            client_override=scoped,
            modern_sources=True,
        )
        self._assert_session_unchanged(gen)
        return success(result)

    def _recommend_dusty_gems(self, query: dict[str, list[str]]) -> ApiResponse:
        limit = _query_int(query, "limit", default=30, minimum=1, maximum=100)
        gen, _, account_id, _ = self._discovery_scope()
        account_id = account_id or self._active_account_id_best_effort()
        dusty_fn = getattr(self.recommend, "dusty_gems", None)
        if not callable(dusty_fn):
            raise AppError("DUSTY_GEMS_UNSUPPORTED", "当前版本不支持沉睡宝藏推荐", 501)
        ids = {int(i) for i in _query_str(query, "exclude_ids").split(",") if i.isdigit()}
        raw_keys = query["exclude_key"] if "exclude_key" in query else _query_str(query, "exclude_keys").split(",")
        keys = {key for key in raw_keys if key}
        result = dusty_fn(account_id, limit=limit, exclude_ids=ids, exclude_keys=keys)
        self._assert_session_unchanged(gen)
        return success(result)

    def _audio_candidates(self, query: dict[str, list[str]]) -> ApiResponse:
        song_id = _query_id(query)
        allow_external = _query_optional_bool(query, "allow_external")
        song_hint = self._song_hint_from_query(song_id, query)
        gen, cookie, _ = self._session_snapshot()
        scoped = self._client_for_cookie(cookie)
        candidates_fn = getattr(self.audio, "candidates", None)
        if not callable(candidates_fn):
            raise AppError("CANDIDATES_UNSUPPORTED", "当前版本不支持候选音源列表", 501)
        cand_kwargs: dict[str, Any] = {
            "allow_external": allow_external,
            "client_override": scoped,
        }
        try:
            sig = inspect.signature(candidates_fn)
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if song_hint is not None and (has_var_kw or "song_hint" in sig.parameters):
                cand_kwargs["song_hint"] = song_hint
            if has_var_kw or "account_id" in sig.parameters:
                cand_kwargs["account_id"] = self._active_account_id_best_effort(allow_remote=False)
        except (TypeError, ValueError):
            pass
        result = candidates_fn(song_id, **cand_kwargs)
        self._assert_session_unchanged(gen)
        return success(result)

    def _local_library_stream(self, raw_path: Any) -> ApiResponse:
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            raise AppError("LOCAL_PATH_INVALID", "本地音频路径无效", 400)
        record = self.db.get_local_file(raw_path.strip())
        if record is None:
            raise AppError("LOCAL_FILE_NOT_INDEXED", "仅允许播放已扫描入库的本地音频文件", 403)
        file_path = Path(str(record["path"]))
        ext = file_path.suffix.lower()
        if ext not in _LOCAL_AUDIO_EXTENSIONS:
            raise AppError("LOCAL_FILE_REJECTED", "不支持的本地音频文件格式", 403)
        if not file_path.is_file():
            raise AppError("LOCAL_FILE_NOT_FOUND", "本地音频文件不存在或已被移动", 404)
        fmt = str(record.get("format") or ext.lstrip(".")).strip().lower()
        content_type = _LOCAL_AUDIO_MIME_BY_EXT.get(ext) or mimetypes.guess_type(file_path.name)[0] or "audio/mpeg"
        target = self.audio.register_stream_token(
            kind="local",
            session_generation=self.session_generation,
            path=file_path,
            content_type=content_type,
            format_name=fmt,
            provider="local_library",
        )
        stream_url = f"/api/stream/{target.token}"
        try:
            file_size = file_path.stat().st_size
        except OSError:
            file_size = 0
        loudness = compute_loudness_compensation(
            record.get("replaygain_track_gain_db"),
            record.get("replaygain_track_peak"),
            album_gain_db=record.get("replaygain_album_gain_db"),
            album_peak=record.get("replaygain_album_peak"),
        )
        cover_id = str(record.get("cover_id") or "")
        return success(
            {
                "id": 0,
                "track_key": f"local:{file_path}",
                "url": stream_url,
                "stream_url": stream_url,
                "stream_token": target.token,
                "level": "lossless" if fmt in {"flac", "wav"} else "local",
                "format": fmt,
                "bitrate": int(record.get("bitrate") or 0),
                "sample_rate": int(record.get("sample_rate") or 0),
                "size": file_size,
                "time_ms": int(record.get("duration_ms") or 0),
                "free_trial": False,
                "is_alternative": False,
                "source": "local",
                "source_provider": "local_library",
                "cover_url": f"/api/local-cover/{cover_id}" if cover_id else "",
                "loudness": loudness,
            }
        )

    def fetch_cover(
        self,
        raw_url: str,
        raw_size: int = 64,
        *,
        allow_external: bool = False,
    ) -> tuple[int, bytes, str]:
        """Proxy a validated HTTPS cover image with ZERO cookies, DNS pinning, and image MIME validation."""
        target_url = validate_cover_url(raw_url, raw_size, allow_external=allow_external)
        with self._cover_lock:
            cached = self._cover_cache.get(target_url)
            if cached is not None:
                self._cover_cache.move_to_end(target_url)
                return 200, cached[0], cached[1]

        headers = {
            "User-Agent": _STANDARD_BROWSER_UA,
            "Accept-Encoding": "identity",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8",
        }
        try:
            with httpx.Client(follow_redirects=False, timeout=8.0, trust_env=False,
                              limits=httpx.Limits(max_keepalive_connections=0)) as client:
                current_url = target_url
                for _ in range(3):
                    addresses = validated_addresses(current_url)
                    if not addresses:
                        raise AppError("COVER_URL_REJECTED", "封面地址不被允许", 403)
                    # Try at most one validated address per family (preferring IPv4 first on dual-stack CDNs)
                    # while staying strictly pinned to the validated DNS snapshot for this hop.
                    ordered_addrs: list[str] = []
                    for is_v6 in (False, True):
                        candidate_addr = next((a for a in addresses if (":" in a) is is_v6), None)
                        if candidate_addr is not None:
                            ordered_addrs.append(candidate_addr)
                    isolate_cookie_jar(client)
                    next_hop_url: str | None = None
                    fetched: tuple[bytes, str] | None = None
                    for idx, addr in enumerate(ordered_addrs):
                        pinned, authority, extensions = _pinned_url(current_url, (addr,))
                        try:
                            with client.stream(
                                "GET", pinned, headers={**headers, "Host": authority}, extensions=extensions
                            ) as response:
                                if response.is_redirect:
                                    location = response.headers.get("location", "").strip()
                                    if not location:
                                        raise UpstreamError("COVER_FETCH_FAILED", "封面重定向地址无效", 502)
                                    next_hop_url = validate_cover_url(
                                        urljoin(current_url, location),
                                        raw_size,
                                        allow_external=allow_external,
                                    )
                                    break
                                if response.status_code != 200:
                                    raise UpstreamError("COVER_FETCH_FAILED", "无法获取封面图片", 502)
                                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                                if content_type not in {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/avif"}:
                                    raise UpstreamError("COVER_CONTENT_INVALID", "封面地址未返回有效图片", 502)
                                length = response.headers.get("content-length", "")
                                encoding = (response.headers.get("content-encoding") or "identity").strip().lower()
                                if (length.isdigit() and int(length) > _COVER_MAX_BYTES) or encoding != "identity":
                                    raise UpstreamError("COVER_TOO_LARGE", "封面编码或大小不受支持", 502)
                                data = bytearray()
                                for chunk in response.iter_bytes(chunk_size=65536):
                                    if len(data) + len(chunk) > _COVER_MAX_BYTES:
                                        raise UpstreamError("COVER_TOO_LARGE", "封面图片过大", 502)
                                    data.extend(chunk)
                                if not data:
                                    raise UpstreamError("COVER_TOO_LARGE", "封面图片为空", 502)
                                fetched = (bytes(data), content_type)
                                break
                        except (httpx.ConnectError, httpx.ConnectTimeout):
                            if idx + 1 < len(ordered_addrs):
                                continue
                            raise
                    if next_hop_url is not None:
                        current_url = next_hop_url
                        continue
                    if fetched is not None:
                        body, content_type = fetched
                        break
                else:
                    raise UpstreamError("COVER_FETCH_FAILED", "封面重定向次数过多", 502)
        except AppError:
            raise
        except httpx.HTTPError as exc:
            raise UpstreamError("COVER_FETCH_FAILED", f"封面图片加载失败：{exc}", 502) from exc

        with self._cover_lock:
            self._cover_cache[target_url] = (body, content_type)
            self._cover_cache.move_to_end(target_url)
            while (len(self._cover_cache) > _COVER_CACHE_MAX_ITEMS
                   or sum(len(entry[0]) for entry in self._cover_cache.values()) > 32 * 1024**2):
                self._cover_cache.popitem(last=False)
        return 200, body, content_type

    def resolve_stream_target(self, token: str) -> StreamTarget:
        resolver = getattr(self.audio, "resolve_stream_token", None)
        if not callable(resolver):
            raise AppError("STREAM_TOKEN_NOT_FOUND", "音频流令牌不存在或已过期", 404)
        with self._auth_lock:
            target = resolver(token, expected_generation=self._session_generation)
        if target is None:
            raise AppError("STREAM_TOKEN_NOT_FOUND", "音频流令牌不存在或已过期", 404)
        return target

    def _clear_transport_cache(self) -> None:
        clear = getattr(self.client.transport, "clear_caches", None)
        if callable(clear):
            clear()

    def _clear_audio_resolution_cache(self) -> None:
        self.audio.clear_resolution_caches()

    def _clear_audio_cache(self) -> None:
        clear = getattr(self.audio, "clear_caches", None)
        if callable(clear):
            clear()

    def _revoke_session(self) -> None:
        self._active_qr_keys.clear()
        self._offline_account_id = None
        self._offline_generation = None
        self.session.clear()
        self.client.set_cookie(None)
        self._cached_account = None
        self._cached_account_cookie = None
        self._session_generation += 1
        self.catalog.set_active_account(None)
        self._clear_transport_cache()
        self._clear_audio_cache()

    def _status(self) -> ApiResponse:
        cached_accounts = self.db.list_cached_accounts()
        with self._auth_lock:
            gen = self._session_generation
            if self._offline_account_id is not None:
                return success({"authenticated": False, "offline": True, "active_account_id": self._offline_account_id,
                                "user": None, "session_generation": gen, "cached_accounts": cached_accounts,
                                "pending_operations": []})
            if not self.client.cookie:
                return success(
                    {
                        "authenticated": False,
                        "user": None,
                        "session_generation": gen,
                        "cached_accounts": cached_accounts,
                        "pending_operations": [],
                    }
                )
            try:
                account = self.client.account()
            except AppError as exc:
                if exc.code == "SESSION_EXPIRED":
                    self._revoke_session()
                    return success(
                        {
                            "authenticated": False,
                            "user": None,
                            "session_generation": self._session_generation,
                            "cached_accounts": cached_accounts,
                            "pending_operations": [],
                        }
                    )
                if exc.code == "UPSTREAM_UNAVAILABLE":
                    if self._offline_generation != gen:
                        self._session_generation += 1
                        gen = self._session_generation
                        self._clear_audio_cache()
                    self._offline_generation = gen
                    return success(
                        {
                            "authenticated": False,
                            "offline": True,
                            "user": None,
                            "session_generation": gen,
                            "cached_accounts": cached_accounts,
                            "pending_operations": [],
                            "reason": exc.message,
                        }
                    )
                raise
            if self._offline_generation == gen:
                self._clear_audio_cache()
                self._session_generation += 1
                gen = self._session_generation
            self._offline_account_id = None
            self._offline_generation = None
            if self.session.active_account_id != str(account.user_id):
                self.session.save_account_session(account, self.client.cookie)
            self._cached_account = account
            self._cached_account_cookie = self.client.cookie
            self.catalog.set_active_account(str(account.user_id))
            self.db.upsert_account(
                str(account.user_id),
                account.display_name,
                account.avatar_url,
                account.vip_type,
            )
            pending = self.operations.pending_for(account.user_id)
            cached_accounts = self.db.list_cached_accounts()
        return success(
            {
                "authenticated": True,
                "user": account.to_dict(),
                "risk_control_cooldown_sec": self.client.cooldown_seconds(),
                "pending_operations": pending,
                "session_generation": gen,
                "cached_accounts": cached_accounts,
            }
        )

    def launcher_url(self, url: str) -> str:
        with self._bootstrap_lock:
            now = time.monotonic()
            self._bootstrap_caps = {k: expiry for k, expiry in self._bootstrap_caps.items() if expiry > now}
            cap = secrets.token_urlsafe(32)
            self._bootstrap_caps[cap] = now + 120
        return url + "?bootstrap=" + cap

    def consume_bootstrap(self, cap: str) -> bool:
        with self._bootstrap_lock:
            return self._bootstrap_caps.pop(cap, 0) > time.monotonic()

    def static(self, raw_path: str) -> tuple[int, bytes, str]:
        path = urlsplit(raw_path).path
        relative = "index.html" if path == "/" else unquote(path).lstrip("/")
        target = self.static_files.get(relative)
        if target is None:
            return 404, b"Not found", "text/plain; charset=utf-8"
        try:
            content = target.read_bytes()
        except OSError:
            return 404, b"Not found", "text/plain; charset=utf-8"
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        return 200, content, content_type


def build_application(settings: Settings | None = None, transport: Transport | None = None) -> PrunerApplication:
    selected = settings or Settings.default()
    if transport is None:
        # Enforce trusted HTTPS NetEase api_base strictly in real runtime.
        validate_api_base(selected.api_base, allow_test_hosts=False)
    store = JsonStore(selected.data_dir)
    device_identity = DeviceIdentityStore(store)
    set_device_id(device_identity.get_or_create_device_id())
    db = CatalogDatabase(selected.data_dir)
    session = SessionStore(store)
    user_settings = SettingsStore(store)
    channel_vault = ChannelCredentialVault(store)
    audio_cache = AudioDiskCache(
        selected.data_dir,
        limit_mb_provider=lambda: int(user_settings.load().get("audio_cache_limit_mb", 512)),
    )
    client = NeteaseClient(transport or NeteaseWebTransport(selected.api_base), session.load_cookie())
    catalog = CatalogService(store, client, selected.max_batch_size, db=db)
    rules = RulesService(store)
    audio_kwargs: dict[str, Any] = {
        "settings_provider": user_settings.load,
        "db": db,
    }
    if "credential_vault" in inspect.signature(AudioService.__init__).parameters:
        audio_kwargs["credential_vault"] = channel_vault
    audio = AudioService(
        client,
        catalog,
        selected.alternative_duration_tolerance_ms,
        **audio_kwargs,
    )
    recommend = RecommendService(client, db=db, catalog=catalog)
    journal = OperationJournal(store)
    operations = OperationsService(store, client, catalog, journal, selected.max_batch_size)
    return PrunerApplication(
        selected,
        client,
        session,
        catalog,
        rules,
        audio,
        operations,
        db=db,
        user_settings=user_settings,
        recommend=recommend,
        channel_vault=channel_vault,
        audio_cache=audio_cache,
    )


def create_asgi_app(
    application: PrunerApplication,
    bound_port: int,
    *,
    require_local_token: bool = False,
) -> Starlette:
    """Build the single-worker Starlette ASGI application for loopback serving."""

    def _has_valid_local_token(request: Request) -> bool:
        expected = getattr(application, "local_api_token", "")
        if not expected:
            return True
        header_tok = (request.headers.get("x-pruner-token") or "").strip()
        if header_tok and header_tok.isascii() and secrets.compare_digest(header_tok, expected):
            return True
        return bool(header_tok and header_tok.isascii()
                    and secrets.compare_digest(header_tok, application.browser_api_token))

    def _json_response(resp: ApiResponse) -> Response:
        if isinstance(resp.payload, bytes):
            return Response(resp.payload, media_type="application/json", headers={"Cache-Control": "no-store"})
        return JSONResponse(
            content=resp.payload,
            status_code=resp.status,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _read_json_body(request: Request) -> Any:
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        if (not content_type and request.headers.get("content-length", "0") == "0"
                and not request.headers.get("transfer-encoding")
                and request.url.path in {"/api/desktop/window/activate", "/api/playback/history"}):
            return None
        if content_type != "application/json":
            raise AppError("CONTENT_TYPE_INVALID", "请求内容类型必须是 application/json", 415)
        limit = (application.settings.import_max_body_bytes if request.url.path == "/api/catalog/import"
                 else application.settings.max_body_bytes)
        raw_length = request.headers.get("content-length")
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise AppError("CONTENT_LENGTH_INVALID", "请求长度无效", 400) from exc
            if length < 0 or length > limit:
                raise AppError("BODY_TOO_LARGE", "请求内容过大", 413)
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise AppError("BODY_TOO_LARGE", "请求内容过大", 413)
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError("JSON_INVALID", "请求 JSON 格式无效", 400) from exc

    async def _handle_cover(request: Request) -> Response:
        raw_url = request.query_params.get("url") or ""
        raw_size_str = request.query_params.get("size") or "64"
        try:
            raw_size = int(raw_size_str)
        except ValueError:
            return _json_response(failure(AppError("COVER_SIZE_INVALID", "封面尺寸参数无效", 400)))
        try:
            status, body, content_type = await asyncio.to_thread(application.fetch_cover, raw_url, raw_size)
        except AppError as exc:
            return _json_response(failure(exc))
        return Response(
            content=body,
            status_code=status,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Security-Policy": "sandbox",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _handle_stream(request: Request) -> Response:
        token = request.path_params.get("token") or ""
        try:
            target = application.resolve_stream_target(token)
        except AppError as exc:
            return _json_response(failure(exc))

        range_header = request.headers.get("range")
        is_head = request.method == "HEAD"

        if target.kind == "local":
            return _stream_local_file(target, range_header, head_only=is_head)
        if target.kind == "remote":
            return await _stream_remote_audio(target, application, range_header, head_only=is_head)
        return _json_response(failure(AppError("STREAM_KIND_INVALID", "不支持的音频流类型", 400)))

    async def _handle_events(request: Request) -> Response:
        preview = request.query_params.get("lyrics_preview") == "1"

        def update_preview_count(delta):
            with application._desktop_state_lock:
                application.lyrics_preview_subscribers += delta
                count = application.lyrics_preview_subscribers
                snapshot = dict(application._latest_desktop_lyrics_state or {})
            window = application.desktop_lyrics.state()
            application.publish_sse("desktop_lyrics", {**snapshot, "window": window, "locked": window["locked"],
                                                       "preview_subscribers": count})

        async def event_stream() -> AsyncIterator[bytes]:
            subscriber = application._sync_bus.subscribe()
            if preview:
                update_preview_count(1)
            idle_ticks = 0
            try:
                yield b'event: connected\ndata: {"ok": true}\n\n'
                with application._desktop_state_lock:
                    initial_lyrics = (
                        dict(application._latest_desktop_lyrics_state)
                        if application._latest_desktop_lyrics_state is not None
                        else None
                    )
                replay = {**(initial_lyrics or {}), "window": application.desktop_lyrics.state()}
                replay["preview_subscribers"] = application.lyrics_preview_subscribers
                replay["locked"] = replay["window"]["locked"]
                init_payload = json.dumps(replay, ensure_ascii=False).encode("utf-8")
                yield b"event: desktop_lyrics\ndata: " + init_payload + b"\n\n"
                initial_style = application._get_saved_desktop_lyrics_style()
                if initial_style is not None:
                    style_payload = json.dumps({"style": initial_style}, ensure_ascii=False).encode("utf-8")
                    yield b"event: desktop_lyrics_style\ndata: " + style_payload + b"\n\n"
                if callable(application.get_fullscreen):
                    fs_payload = json.dumps({"fullscreen": bool(application.get_fullscreen())}, ensure_ascii=False).encode("utf-8")
                    yield b"event: window_fullscreen\ndata: " + fs_payload + b"\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.to_thread(subscriber.get, True, 0.5)
                    except queue.Empty:
                        idle_ticks += 1
                        if idle_ticks >= 30:
                            idle_ticks = 0
                            yield b": keepalive\n\n"
                        continue
                    if event is None:
                        break
                    idle_ticks = 0
                    ev_type = str(event.get("_event_type") or "sync")
                    body_obj = {k: v for k, v in event.items() if k != "_event_type"}
                    payload = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
                    yield f"event: {ev_type}\ndata: ".encode("utf-8") + payload + b"\n\n"
            finally:
                application._sync_bus.unsubscribe(subscriber)
                if preview:
                    update_preview_count(-1)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _dispatch(request: Request) -> Response:
        rejected = validate_local_request(request.headers, bound_port)
        if rejected is not None:
            return _json_response(failure(rejected))

        method = request.method.upper()
        if method == "OPTIONS":
            return _json_response(failure(AppError("METHOD_NOT_ALLOWED", "不支持跨域请求", 405)))

        path = request.url.path
        if (
            require_local_token
            and path.startswith("/api/")
            and path not in {"/api/health", "/api/desktop/window/activate"}
            and not _has_valid_local_token(request)
        ):
            return _json_response(
                failure(AppError("LOCAL_AUTH_REQUIRED", "缺少有效的本机访问令牌", 401))
            )

        if path == "/api/cover" and method == "GET":
            return await _handle_cover(request)
        if path.startswith("/api/local-cover/") and method == "GET":
            cover_id = unquote(path[len("/api/local-cover/"):]).strip()
            cover_cache = getattr(getattr(application.audio, "external_resolver", None), "local_scanner", None)
            cached_cover = cover_cache.cover_cache.get(cover_id) if cover_cache is not None and hasattr(cover_cache, "cover_cache") else None
            if cached_cover is None:
                return _json_response(failure(AppError("COVER_NOT_FOUND", "找不到本地嵌入封面", 404)))
            body_bytes, mime_type = cached_cover
            return Response(
                content=body_bytes,
                status_code=200,
                media_type=mime_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Content-Security-Policy": "sandbox",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        if path.startswith("/api/opensubsonic/cover/") and method == "GET":
            art_id = unquote(path[len("/api/opensubsonic/cover/"):]).strip()
            raw_size_str = request.query_params.get("size") or "384"
            try:
                size_val = max(64, min(1024, int(raw_size_str)))
            except ValueError:
                return _json_response(failure(AppError("COVER_SIZE_INVALID", "封面尺寸参数无效", 400)))
            try:
                secret = application.channel_vault.get_channel_secret("opensubsonic")
                body_bytes, mime_type = await asyncio.to_thread(
                    application.audio.external_resolver.opensubsonic.fetch_cover,
                    art_id,
                    secret,
                    application.audio.external_resolver._get_http_client(),
                    size=size_val,
                )
            except AppError as exc:
                return _json_response(failure(exc))
            return Response(
                content=body_bytes,
                status_code=200,
                media_type=mime_type,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Content-Security-Policy": "sandbox",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        if path == "/api/events" and method == "GET":
            return await _handle_events(request)
        if path.startswith("/api/stream/") and method in {"GET", "HEAD"}:
            request.path_params["token"] = unquote(path[len("/api/stream/"):])
            return await _handle_stream(request)

        if path.startswith("/api/"):
            if method not in {"GET", "POST", "PUT", "DELETE"}:
                return _json_response(failure(AppError("METHOD_NOT_ALLOWED", "请求方法不支持", 405)))
            body: Any = None
            if method in {"POST", "PUT", "DELETE"}:
                try:
                    body = await _read_json_body(request)
                except AppError as exc:
                    return _json_response(failure(exc))
            if path == "/api/desktop/window/activate" and "x-pruner-wake-token" in request.headers:
                body = {"wake_token": request.headers["x-pruner-wake-token"]}
            raw_path = path + (f"?{request.url.query}" if request.url.query else "")
            response = await asyncio.to_thread(application.api, method, raw_path, body, http_request=True)
            return _json_response(response)

        if method not in {"GET", "HEAD"}:
            return _json_response(failure(AppError("METHOD_NOT_ALLOWED", "请求方法不支持", 405)))
        status, content, content_type = application.static(path)
        is_html = status == 200 and content_type.startswith("text/html")
        if is_html and "bootstrap" in request.query_params:
            if method != "GET" or not application.consume_bootstrap(request.query_params["bootstrap"]):
                return _json_response(failure(AppError("BOOTSTRAP_EXPIRED", "请从应用重新打开窗口", 403)))
            content = (
                '<!doctype html><meta charset="utf-8"><meta name="pruner-browser-session" content="'
                + application.browser_api_token
                + '"><script src="/bootstrap.js" defer></script>'
            ).encode()
        resp = Response(
            content=b"" if method == "HEAD" else content,
            status_code=status,
            media_type=content_type,
            headers={
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, must-revalidate",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )
        return resp

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            application._sync_bus.close()

    return Starlette(
        debug=False,
        lifespan=lifespan,
        routes=[
            Route("/{path:path}", _dispatch, methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"]),
            Route("/", _dispatch, methods=["GET", "HEAD", "OPTIONS"]),
        ],
    )


def _parse_single_byte_range(range_header: str, total_size: int) -> tuple[int, int] | None:
    """Parse HTTP single byte Range header; returns (start, end) inclusive or None if invalid."""
    if not range_header or total_size <= 0:
        return None
    header = range_header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header[6:].split(",", 1)[0].strip()
    if "-" not in spec:
        return None
    start_s, end_s = spec.split("-", 1)
    try:
        if start_s == "":
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start = max(0, total_size - suffix)
            end = total_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s != "" else total_size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= total_size:
        return None
    return start, min(end, total_size - 1)


def _stream_disk_path(
    file_path: Path,
    content_type: str,
    range_header: str | None,
    *,
    head_only: bool,
    cache_hit: bool = False,
    register_stream=None,
) -> Response:
    if register_stream is not None:
        from .lan_sync import stream_indexed_file
        response = stream_indexed_file(file_path, content_type, range_header, head_only=head_only, register_stream=register_stream)
        if cache_hit:
            response.headers["X-Audio-Cache"] = "HIT"
        return response
    file_size = file_path.stat().st_size
    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if cache_hit:
        base_headers["X-Audio-Cache"] = "HIT"
    if range_header:
        parsed = _parse_single_byte_range(range_header, file_size)
        if parsed is None:
            return Response(
                content=b"",
                status_code=416,
                headers={**base_headers, "Content-Range": f"bytes */{file_size}"},
            )
        start, end = parsed
        length = end - start + 1
        headers = {
            **base_headers,
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        }
        status_code = 206
    else:
        start, end = 0, max(0, file_size - 1)
        length = file_size
        headers = {**base_headers, "Content-Length": str(length)}
        status_code = 200

    if head_only:
        return Response(content=b"", status_code=status_code, media_type=content_type, headers=headers)

    def iter_file() -> Any:
        remaining = length
        with file_path.open("rb") as handle:
            handle.seek(start)
            while remaining > 0:
                chunk = handle.read(min(_STREAM_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        iter_file(),
        status_code=status_code,
        media_type=content_type,
        headers=headers,
    )


def _stream_local_file(target: StreamTarget, range_header: str | None, *, head_only: bool) -> Response:
    file_path = target.path
    if not isinstance(file_path, Path) or not file_path.is_file():
        return JSONResponse(
            content=failure(AppError("LOCAL_FILE_NOT_FOUND", "本地音频文件不存在", 404)).payload,
            status_code=404,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    if file_path.suffix.lower() not in _LOCAL_AUDIO_EXTENSIONS:
        return JSONResponse(
            content=failure(AppError("LOCAL_FILE_REJECTED", "不允许流式访问非音频文件", 403)).payload,
            status_code=403,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return _stream_disk_path(file_path, target.content_type, range_header, head_only=head_only)


def _is_allowed_stream_mime(mime: str) -> bool:
    cleaned = (mime or "").split(";")[0].strip().lower()
    if not cleaned:
        return False
    return cleaned.startswith("audio/") or cleaned in _ALLOWED_STREAM_MIME_EXACT


def _parse_content_range_start(content_range: str) -> int | None:
    if not content_range:
        return None
    match = re.match(r"^\s*bytes\s+(\d+)-", content_range, re.I)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _preferred_target_audio_mime(target: StreamTarget) -> str | None:
    fmt = (target.format or "").strip().lower()
    if fmt:
        inferred = mime_type_for_format(fmt)
        if inferred and inferred not in {"application/octet-stream", "binary/octet-stream"}:
            return inferred
    ct = (target.content_type or "").split(";")[0].strip().lower()
    if ct and ct not in {"application/octet-stream", "binary/octet-stream"} and _is_allowed_stream_mime(ct):
        return ct
    return None


def _is_byte_zero_stream_response(status_code: int, content_range: str, range_header: str | None) -> bool:
    if status_code == 200:
        return True
    if status_code == 206:
        cr_start = _parse_content_range_start(content_range)
        if cr_start is not None:
            return cr_start == 0
        if not range_header:
            return True
        return bool(re.match(r"^\s*bytes\s*=\s*0-", range_header, re.I))
    return False


def _is_remote_stream_url_allowed(
    url: str,
    custom_api_url: str = "",
    extra_api_bases: list[str] | None = None,
    resolver_fn=None,
) -> bool:
    return is_safe_external_url(url, loopback_bases=(custom_api_url, *(extra_api_bases or [])), resolver_fn=resolver_fn)


async def _stream_remote_audio(
    target: StreamTarget,
    application: PrunerApplication,
    range_header: str | None,
    *,
    head_only: bool,
    register_stream: Any = None,
) -> Response:
    custom_api = str(application.user_settings.load().get("custom_external_api") or "")
    extra_bases = [
        str(ch.get("api_base") or "")
        for ch in application.channel_vault.public_summary()
        if ch.get("api_base")
    ]
    resolver_bases = [
        str(r.get("url") or "")
        for r in application.resolver_registry.list_public()
        if r.get("url")
    ]
    sidecar_cfg = application.sidecar.active_resolver_config()
    if isinstance(sidecar_cfg, dict) and sidecar_cfg.get("url"):
        resolver_bases.append(str(sidecar_cfg["url"]))
    bases = (custom_api, *extra_bases, *resolver_bases)
    self_hosted_origin = str(getattr(target, "allow_self_hosted_origin", "") or "").strip()

    async def _resolve_hop_addresses(url_to_check: str) -> tuple[tuple[str, ...], bool]:
        same_self_hosted = bool(self_hosted_origin and _same_origin(self_hosted_origin, url_to_check))
        if self_hosted_origin:
            return await asyncio.to_thread(self_hosted_addresses, self_hosted_origin, url_to_check), same_self_hosted
        addrs = await asyncio.to_thread(validated_addresses, url_to_check, loopback_bases=bases)
        return addrs, False

    addresses, is_self_hosted = await _resolve_hop_addresses(target.url)
    if not addresses:
        return JSONResponse(
            content=failure(AppError("STREAM_URL_REJECTED", "外部音频流地址不被允许", 403)).payload,
            status_code=403,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    cache_key = target.cache_key or f"{target.provider}:{target.url}"
    cached = application.audio_cache.get(cache_key)
    if cached is not None:
        cached_path, cached_ct, _cached_size = cached
        return _stream_disk_path(cached_path, cached_ct, range_header, head_only=head_only, cache_hit=True, register_stream=register_stream)

    def _clean_outbound_headers() -> dict[str, str]:
        # STRICT ZERO-COOKIE ISOLATION: never forward NetEase cookies; allow Authorization ONLY for configured self-hosted origin.
        headers: dict[str, str] = {}
        for key, value in (target.headers or {}).items():
            kl = key.lower()
            if kl in {"cookie", "host", "content-length"}:
                continue
            if kl == "authorization" and not is_self_hosted:
                continue
            headers[key] = str(value)
        if "user-agent" not in {k.lower() for k in headers}:
            headers["User-Agent"] = _STANDARD_BROWSER_UA
        if range_header:
            headers["Range"] = range_header
        return headers

    client = httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        cookies=NoOpCookies(),
        limits=httpx.Limits(max_keepalive_connections=0),
        timeout=httpx.Timeout(10.0, read=30.0),
    )
    isolate_cookie_jar(client)
    hop_headers = _clean_outbound_headers()
    current_url = target.url
    upstream: httpx.Response | None = None
    try:
        for _hop in range(_STREAM_MAX_REDIRECTS + 1):
            pinned, authority, extensions = _pinned_url(current_url, addresses)
            req = client.build_request("HEAD" if head_only else "GET", pinned,
                                       headers={**hop_headers, "Host": authority}, extensions=extensions)
            req.headers.pop("cookie", None)
            upstream = await client.send(req, stream=True)
            if upstream.status_code in {301, 302, 303, 307, 308}:
                location = upstream.headers.get("location") or ""
                await upstream.aclose()
                upstream = None
                if not location:
                    await client.aclose()
                    return JSONResponse(
                        content=failure(AppError("STREAM_UPSTREAM_INVALID", "外部音频流重定向缺少目标地址", 502)).payload,
                        status_code=502,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                    )
                next_url = urljoin(current_url, location)
                addresses, _next_self_hosted = await _resolve_hop_addresses(next_url)
                if not addresses:
                    await client.aclose()
                    return JSONResponse(
                        content=failure(AppError("STREAM_URL_REJECTED", "外部音频流重定向到不允许的地址", 403)).payload,
                        status_code=403,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                    )
                hop_headers = playback_redirect_headers(current_url, next_url, hop_headers)
                current_url = next_url
                continue
            break
    except httpx.HTTPError:
        if upstream is not None:
            await upstream.aclose()
        await client.aclose()
        return JSONResponse(
            content=failure(AppError("STREAM_UPSTREAM_FAILED", "外部音频流连接失败", 502)).payload,
            status_code=502,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    if upstream is None:
        await client.aclose()
        return JSONResponse(
            content=failure(AppError("STREAM_UPSTREAM_INVALID", "外部音频流重定向次数过多", 502)).payload,
            status_code=502,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    raw_ct = upstream.headers.get("content-type")
    upstream_ct = (raw_ct or "").split(";")[0].strip().lower()
    ambiguous = upstream_ct in {"", "application/octet-stream", "binary/octet-stream"}
    if ambiguous:
        upstream_ct = "application/octet-stream"
    if upstream.status_code not in {200, 206, 416}:
        upstream_status = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        if upstream_status in {401, 403}:
            err = AppError("STREAM_UPSTREAM_FORBIDDEN", "外部音频流链接已过期或被拒绝访问", 502)
        elif upstream_status in {404, 410}:
            err = AppError("STREAM_UPSTREAM_NOT_FOUND", "外部音频流资源不存在或已失效", 502)
        elif upstream_status >= 500:
            err = AppError("STREAM_UPSTREAM_FAILED", f"外部音频流源站异常 (HTTP {upstream_status})", 502)
        else:
            err = AppError("STREAM_UPSTREAM_INVALID", "外部音频流返回了无效响应", 502)
        return JSONResponse(
            content=failure(err).payload,
            status_code=502,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    if not _is_allowed_stream_mime(upstream_ct):
        await upstream.aclose()
        await client.aclose()
        return JSONResponse(
            content=failure(AppError("STREAM_UPSTREAM_INVALID", "外部音频流返回了无效响应", 502)).payload,
            status_code=502,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    target_mime = _preferred_target_audio_mime(target)
    resolved_ct = upstream_ct
    if ambiguous and target_mime:
        resolved_ct = target_mime
    elif upstream_ct == "audio/mpeg" and target_mime and target_mime != "audio/mpeg":
        resolved_ct = target_mime

    resp_headers: dict[str, str] = {
        "Accept-Ranges": upstream.headers.get("accept-ranges") or "bytes",
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    expected_length = 0
    for header_name in ("content-length", "content-range"):
        if header_name in upstream.headers:
            resp_headers[header_name.title()] = upstream.headers[header_name]
            if header_name == "content-length":
                try:
                    expected_length = int(upstream.headers[header_name])
                except ValueError:
                    expected_length = 0

    if head_only or upstream.status_code == 416:
        status_code = upstream.status_code
        await upstream.aclose()
        await client.aclose()
        return Response(content=b"", status_code=status_code, media_type=resolved_ct, headers=resp_headers)

    cache_limit = application.audio_cache.limit_bytes()
    content_range = upstream.headers.get("content-range", "")
    is_byte_zero = _is_byte_zero_stream_response(upstream.status_code, content_range, range_header)
    full_entity = upstream.status_code == 200
    if upstream.status_code == 206:
        span = re.fullmatch(r"bytes 0-(\d+)/(\d+)", content_range, re.I)
        if span:
            end, total = map(int, span.groups())
            full_entity = end + 1 == total and expected_length in (0, total)
            expected_length = total
    full_request = not range_header or range_header.strip().lower() in {"bytes=0-", f"bytes=0-{expected_length-1}"}
    should_cache = full_request and full_entity and 0 < cache_limit and expected_length <= cache_limit

    chunks = upstream.aiter_bytes(chunk_size=_STREAM_CHUNK_BYTES)
    prefix = b""
    should_sniff = is_byte_zero and (ambiguous or upstream_ct == "audio/mpeg" or not upstream_ct.startswith("audio/"))
    if should_sniff:
        prefix_parts: list[bytes] = []
        prefix_len = 0
        try:
            while prefix_len < 64:
                piece = await anext(chunks, None)
                if piece is None:
                    break
                if piece:
                    prefix_parts.append(piece)
                    prefix_len += len(piece)
        except httpx.HTTPError:
            prefix_parts = []
        prefix = b"".join(prefix_parts)
        sniffed_mime = sniff_audio_header_mime(prefix)
        signature = sniffed_mime is not None
        error_body = prefix.lstrip().startswith((b"<", b"{", b"["))
        if ambiguous:
            if not prefix or error_body or (full_entity and not signature):
                await upstream.aclose()
                await client.aclose()
                return JSONResponse(
                    content=failure(AppError("STREAM_UPSTREAM_INVALID", "外部音频流格式无效", 502)).payload,
                    status_code=502,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                )
            if sniffed_mime:
                resolved_ct = sniffed_mime
            should_cache = should_cache and signature
        else:
            if error_body and not signature:
                await upstream.aclose()
                await client.aclose()
                return JSONResponse(
                    content=failure(AppError("STREAM_UPSTREAM_INVALID", "外部音频流返回了无效内容", 502)).payload,
                    status_code=502,
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                )
            if sniffed_mime:
                resolved_ct = sniffed_mime

    async def buffered_chunks():
        if prefix:
            yield prefix
        async for chunk in chunks:
            yield chunk

    async def iter_remote() -> AsyncIterator[bytes]:
        temp_path: Path | None = None
        temp_handle: Any = None
        cache_aborted = False
        written = 0
        completed_cleanly = False
        try:
            async for chunk in buffered_chunks():
                if not chunk:
                    continue
                if should_cache and not cache_aborted and temp_handle is None:
                    try:
                        temp_path = application.audio_cache.create_temp_path()
                        temp_handle = temp_path.open("wb")
                    except OSError:
                        temp_path = None
                        temp_handle = None
                        cache_aborted = True
                if temp_handle is not None:
                    written += len(chunk)
                    if written > cache_limit:
                        try:
                            temp_handle.close()
                        except OSError:
                            pass
                        temp_handle = None
                        cache_aborted = True
                        if temp_path is not None:
                            try:
                                temp_path.unlink()
                            except OSError:
                                pass
                            temp_path = None
                    else:
                        try:
                            temp_handle.write(chunk)
                        except OSError:
                            try:
                                temp_handle.close()
                            except OSError:
                                pass
                            temp_handle = None
                            cache_aborted = True
                yield chunk
            completed_cleanly = True
        finally:
            if temp_handle is not None:
                try:
                    temp_handle.flush()
                    os.fsync(temp_handle.fileno())
                except OSError:
                    completed_cleanly = False
                finally:
                    try:
                        temp_handle.close()
                    except OSError:
                        pass
            if temp_path is not None:
                if completed_cleanly and temp_handle is not None and written > 0 and expected_length in (0, written):
                    application.audio_cache.commit_temp_file(
                        cache_key,
                        temp_path,
                        expected_length,
                        resolved_ct,
                        format_name=target.format,
                    )
                else:
                    try:
                        if temp_path.exists():
                            temp_path.unlink()
                    except OSError:
                        pass
            await upstream.aclose()
            await client.aclose()

    cancelled = threading.Event()

    async def iter_remote_guarded() -> AsyncIterator[bytes]:
        async for chunk in iter_remote():
            if cancelled.is_set():
                raise asyncio.CancelledError
            yield chunk

    if register_stream is None:
        return StreamingResponse(
            iter_remote(),
            status_code=upstream.status_code,
            media_type=resolved_ct,
            headers=resp_headers,
        )

    class GuardedRemoteStreamResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            unregister = None
            try:
                task = asyncio.current_task()
                loop = asyncio.get_running_loop()

                def abort_stream() -> None:
                    cancelled.set()
                    if task is not None:
                        loop.call_soon_threadsafe(task.cancel)

                try:
                    unregister = register_stream(abort_stream)
                except AppError as exc:
                    await upstream.aclose()
                    await client.aclose()
                    await JSONResponse({"error": exc.code}, status_code=exc.status)(scope, receive, send)
                    return
                await super().__call__(scope, receive, send)
            finally:
                if unregister is not None:
                    unregister()

    return GuardedRemoteStreamResponse(
        iter_remote_guarded(),
        status_code=upstream.status_code,
        media_type=resolved_ct,
        headers=resp_headers,
    )


def make_handler(application: PrunerApplication) -> type[BaseHTTPRequestHandler]:
    """Backwards-compatible stdlib HTTP handler preserved for direct unit tests."""

    class Handler(BaseHTTPRequestHandler):
        server_version = f"PrunerStudio/{__version__}"

        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        def do_OPTIONS(self) -> None:
            self._json_response(failure(AppError("METHOD_NOT_ALLOWED", "不支持跨域请求", 405)))

        def _dispatch(self) -> None:
            rejected = validate_local_request(self.headers, self.server.server_address[1])
            if rejected is not None:
                self._json_response(failure(rejected))
                return
            if urlsplit(self.path).path.startswith("/api/"):
                body: Any = None
                if self.command in {"POST", "PUT", "DELETE"}:
                    try:
                        body = self._read_json()
                    except AppError as exc:
                        self._json_response(failure(exc))
                        return
                if urlsplit(self.path).path == "/api/desktop/window/activate" and "X-Pruner-Wake-Token" in self.headers:
                    body = {"wake_token": self.headers["X-Pruner-Wake-Token"]}
                self._json_response(application.api(self.command, self.path, body, http_request=True))
                return
            if self.command != "GET":
                self._json_response(failure(AppError("METHOD_NOT_ALLOWED", "请求方法不支持", 405)))
                return
            status, content, content_type = application.static(self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(content)

        def _read_json(self) -> Any:
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                raise AppError("CONTENT_TYPE_INVALID", "请求内容类型必须是 application/json", 415)
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return None
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise AppError("CONTENT_LENGTH_INVALID", "请求长度无效", 400) from exc
            limit = (application.settings.import_max_body_bytes if urlsplit(self.path).path == "/api/catalog/import"
                     else application.settings.max_body_bytes)
            if length < 0 or length > limit:
                raise AppError("BODY_TOO_LARGE", "请求内容过大", 413)
            raw = self.rfile.read(length)
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AppError("JSON_INVALID", "请求 JSON 格式无效", 400) from exc

        def _json_response(self, response: ApiResponse) -> None:
            raw = response.payload if isinstance(response.payload, bytes) else json.dumps(response.payload, ensure_ascii=False).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def serve(settings: Settings | None = None, transport: Transport | None = None) -> None:
    selected = settings or Settings.default()
    if selected.host != "127.0.0.1":
        raise ValueError("Pruner Studio may only bind to 127.0.0.1")
    canonical_data_dir = Path(os.path.normcase(os.path.realpath(os.path.expanduser(os.fspath(selected.data_dir)))))
    canonical_data_dir.mkdir(parents=True, exist_ok=True)
    selected = replace(selected, data_dir=canonical_data_dir)
    with DataDirectoryLock(selected.data_dir):
        application = build_application(selected, transport)
        sock, bound_port = bind_loopback_socket(selected.host, selected.port)
        asgi_app = create_asgi_app(application, bound_port, require_local_token=True)
        try:
            application.lan_sync.start_if_enabled()
            launch_desktop_or_server(
                asgi_app,
                sock,
                selected.data_dir,
                application=application,
                web_dir=selected.web_dir,
                minimize_to_tray_provider=lambda: bool(application.user_settings.load().get("minimize_to_tray", True)),
                hotkeys_enabled_provider=lambda: bool(application.user_settings.load().get("global_hotkeys_enabled", True)),
                on_hotkey=lambda action: application.publish_sse("hotkey", {"action": action}),
                desktop_lyrics_controller=application.desktop_lyrics,
            )
        finally:
            application.sidecar.stop()
            application.lan_sync.stop()
            sock.close()
            (selected.data_dir / ".instance.json").unlink(missing_ok=True)


class LoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = False
    block_on_close = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _get_header(headers: Any, name: str) -> str:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return str(value) if value is not None else ""


def validate_local_request(headers: Any, port: int) -> AppError | None:
    """Reject requests that did not come from this application's own page."""
    host = _get_header(headers, "Host").strip().lower()
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:
        return AppError("REQUEST_ORIGIN_REJECTED", "仅接受本机页面发起的请求", 403)
    fetch_site = _get_header(headers, "Sec-Fetch-Site").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return AppError("REQUEST_ORIGIN_REJECTED", "仅接受本机页面发起的请求", 403)
    origin = _get_header(headers, "Origin")
    if origin:
        parsed = urlsplit(origin.strip())
        try:
            origin_port = parsed.port
        except ValueError:
            return AppError("REQUEST_ORIGIN_REJECTED", "仅接受本机页面发起的请求", 403)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "http" or hostname not in {"127.0.0.1", "localhost", "::1"} or origin_port != port:
            return AppError("REQUEST_ORIGIN_REJECTED", "仅接受本机页面发起的请求", 403)
    return None


def clamp_cover_size(size: int) -> int:
    return min(_ALLOWED_COVER_SIZES, key=lambda bucket: abs(bucket - size))


def _has_forbidden_url_char(value: str, *, forbid_space: bool = False) -> bool:
    limit = 0x20 if forbid_space else 0x1F
    for ch in value:
        code = ord(ch)
        if code <= limit or 0x7F <= code <= 0x9F or ch in ("\u2028", "\u2029"):
            return True
    return False


def validate_cover_url(raw_url: str, size: int = 64, *, allow_external: bool = False) -> str:
    """Validate HTTPS cover URLs. Defaults to NetEase CDN (*.126.net); with allow_external=True
    also permits SSRF-checked public HTTPS cover URLs for cross-platform LAN proxying."""
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise AppError("COVER_URL_REQUIRED", "缺少封面地址", 400)
    cleaned = raw_url.strip()
    if len(cleaned) > 1024 or _has_forbidden_url_char(cleaned, forbid_space=True):
        raise AppError("COVER_URL_REJECTED", "封面地址无效", 403)
    parsed = urlsplit(cleaned)
    if parsed.scheme != "https":
        raise AppError("COVER_URL_REJECTED", "仅允许 HTTPS 封面地址", 403)
    if parsed.username is not None or parsed.password is not None:
        raise AppError("COVER_URL_REJECTED", "封面地址不允许包含凭据", 403)
    try:
        port = parsed.port
    except ValueError as exc:
        raise AppError("COVER_URL_REJECTED", "封面地址端口无效", 403) from exc
    if port not in {None, 443}:
        raise AppError("COVER_URL_REJECTED", "封面地址仅允许标准 HTTPS 端口", 403)
    hostname = (parsed.hostname or "").lower()
    is_netease = _is_trusted_cover_host(hostname)
    if not is_netease and not (allow_external and _is_public_cover_host(hostname)):
        raise AppError(
            "COVER_URL_REJECTED",
            "封面地址域名不被允许" if allow_external else "仅允许代理网易云官方封面域名",
            403,
        )
    path = parsed.path or "/"
    decoded_path = unquote(path)
    decoded_query = unquote(parsed.query)
    if (
        "\\" in path
        or "\\" in decoded_path
        or "\\" in parsed.query
        or "\\" in decoded_query
        or ".." in path.split("/")
        or ".." in decoded_path.split("/")
        or _has_forbidden_url_char(decoded_path)
        or _has_forbidden_url_char(decoded_query)
    ):
        raise AppError("COVER_URL_REJECTED", "封面路径无效", 403)
    if is_netease:
        clamped = clamp_cover_size(int(size))
        return f"https://{hostname}{path}?param={clamped}y{clamped}"
    host_part = f"[{hostname}]" if ":" in hostname else hostname
    return f"https://{host_part}{path}" + (f"?{parsed.query}" if parsed.query else "")


def _is_trusted_cover_host(hostname: str) -> bool:
    if not hostname or len(hostname) > 253:
        return False
    if not (hostname.endswith(".music.126.net") or hostname.endswith(".126.net")):
        return False
    labels = hostname.split(".")
    if len(labels) < 3:
        return False
    return all(bool(_DNS_LABEL_RE.match(label)) for label in labels)


def _is_public_cover_host(hostname: str) -> bool:
    if not hostname or len(hostname) > 253 or hostname.endswith(".") or ".." in hostname:
        return False
    if hostname == "localhost" or hostname.endswith(_NON_PUBLIC_HOST_SUFFIXES):
        return False
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        labels = hostname.split(".")
        if len(labels) < 2:
            return False
        if not all(bool(_DNS_LABEL_RE.match(label)) for label in labels):
            return False
        return bool(_DNS_TLD_RE.match(labels[-1]))
    return is_public_ip(ip)


def _build_static_allowlist(web_dir: Path) -> dict[str, Path]:
    allowed_suffixes = {
        ".html", ".css", ".js", ".mjs", ".svg", ".png", ".jpg", ".jpeg",
        ".webp", ".ico", ".woff", ".woff2", ".ttf",
    }
    if not web_dir.is_dir():
        return {}
    result: dict[str, Path] = {}
    root = web_dir.resolve()
    for target in root.rglob("*"):
        if not target.is_file() or target.suffix.lower() not in allowed_suffixes:
            continue
        resolved = target.resolve()
        if resolved.is_relative_to(root):
            result[resolved.relative_to(root).as_posix()] = resolved
    return result


def _object(value: Any, allow_none: bool = False) -> dict[str, Any]:
    if value is None and allow_none:
        return {}
    if not isinstance(value, dict):
        raise AppError("JSON_OBJECT_REQUIRED", "请求内容必须是 JSON 对象")
    return value


def _feedback_identity(song_id, key):
    if key is None:
        return _positive_id(song_id), ""
    if isinstance(key, str):
        cleaned = key.strip()
        if cleaned.startswith("netease:"):
            sid = _positive_id(cleaned[len("netease:"):])
            if song_id is None or sid == _positive_id(song_id):
                return sid, ""
        elif ":" in cleaned:
            provider, ext_id = cleaned.split(":", 1)
            if provider and ext_id and song_id in (None, 0):
                return 0, cleaned
    raise AppError("TRACK_KEY_INVALID", "曲目标识与歌曲 ID 不一致", 400)


def _query_id(query: dict[str, list[str]]) -> int:
    values = query.get("id")
    if not values or len(values) != 1:
        raise AppError("SONG_ID_REQUIRED", "缺少歌曲 ID")
    return _positive_id(values[0])


def _query_positive_id_any(
    query: dict[str, list[str]],
    names: tuple[str, ...],
    missing_code: str,
    invalid_code: str,
) -> int:
    for name in names:
        values = query.get(name)
        if values and len(values) == 1 and values[0].strip():
            return _positive_id(values[0], invalid_code)
    raise AppError(missing_code, "缺少必要 ID 参数")


def _query_str(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name)
    if not values:
        return ""
    return str(values[0]).strip()


def _query_bool(query: dict[str, list[str]], name: str) -> bool:
    return _query_str(query, name).lower() in {"1", "true", "yes"}


def _query_optional_bool(query: dict[str, list[str]], name: str) -> bool | None:
    raw = _query_str(query, name).lower()
    if not raw:
        return None
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    raise AppError("INVALID_INPUT", f"布尔参数 {name} 无效")


def _query_int(query: dict[str, list[str]], name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = _query_str(query, name)
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError as exc:
        raise AppError("INVALID_INPUT", f"整数参数 {name} 无效") from exc
    return max(minimum, min(maximum, val))


def _query_optional_int(query: dict[str, list[str]], name: str, *, minimum: int, maximum: int) -> int | None:
    raw = _query_str(query, name)
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError as exc:
        raise AppError("INVALID_INPUT", f"整数参数 {name} 无效") from exc
    if val < minimum or val > maximum:
        raise AppError("INVALID_INPUT", f"整数参数 {name} 超出范围")
    return val
