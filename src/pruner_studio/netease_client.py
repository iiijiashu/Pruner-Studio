from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import date
import base64
import hashlib
import random
import io
import json
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Protocol

import httpx

from .errors import AppError, UpstreamError
from .models import AccountSummary, PlaylistSummary, Song
from .netease_crypto import (
    DEFAULT_PC_APPVER,
    build_eapi_cookie,
    build_eapi_header,
    build_player_url_v1_payload,
    eapi_decrypt_response,
    eapi_encrypt,
    normalize_quality_level,
)


class NoOpCookies(httpx.Cookies):
    """Thread-safe zero-state CookieJar preventing any cross-request/account cookie leakage."""

    def extract_cookies(self, response: httpx.Response) -> None:  # pragma: no cover - intentional no-op
        return

    def set_cookie_header(self, request: httpx.Request) -> None:
        return

    def set(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        return

    def update(self, cookies: Any = None) -> None:
        return

def isolate_cookie_jar(client):
    # HTTPX 0.28 copies even a Cookies subclass passed to its public constructor/setter.
    jar = getattr(client, "cookies", None)
    if isinstance(jar, httpx.Cookies) and not isinstance(jar, NoOpCookies):
        client._cookies = NoOpCookies()
    return client


TRUSTED_NETEASE_HOSTS = frozenset(
    {
        "music.163.com",
        "interface.music.163.com",
        "interface3.music.163.com",
    }
)
_TEST_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "fake.invalid"})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 3
_MAX_RESPONSE_BYTES = 4_194_304
_MAX_ERROR_BYTES = 1_048_576
_UNSET = object()


class ReadBudgetReached(Exception):
    """No HTTP request was sent."""


class ReadBudget:
    def __init__(self, limit):
        self.remaining = limit

    def consume(self):
        if not self.remaining:
            raise ReadBudgetReached()
        self.remaining -= 1


_active_read_budget = ContextVar("recommend_read_budget", default=None)


@contextmanager
def recommendation_reads(limit):
    budget = ReadBudget(limit)
    token = _active_read_budget.set(budget)
    try:
        yield budget
    finally:
        _active_read_budget.reset(token)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    data: dict[str, Any]
    set_cookie: str = ""


class Transport(Protocol):
    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        cookie: str | None = None,
    ) -> TransportResponse: ...


def _is_trusted_redirect(orig_url: str, next_url: str) -> tuple[bool, bool, str]:
    """Validate an upstream redirect target.

    Returns ``(allowed, strip_cookie, reason)``.
    - Strips ``Cookie`` on ANY cross-host/origin redirect.
    - Blocks non-HTTPS redirects (except exact same-origin loopback/test hosts).
    - Blocks redirects to hosts outside ``TRUSTED_NETEASE_HOSTS`` (unless exact same origin).
    """
    orig = urllib.parse.urlsplit(orig_url)
    target = urllib.parse.urlsplit(next_url)
    if target.scheme not in {"http", "https"} or not target.netloc:
        return False, True, "上游重定向地址无效"
    orig_host = (orig.hostname or "").lower()
    target_host = (target.hostname or "").lower()
    same_origin = orig.scheme.lower() == target.scheme.lower() and orig.netloc.lower() == target.netloc.lower()
    strip_cookie = orig.netloc.lower() != target.netloc.lower() or orig.scheme.lower() != target.scheme.lower()
    if target.scheme.lower() != "https":
        if not (same_origin and orig_host in _TEST_LOOPBACK_HOSTS):
            return False, True, "已拦截非 HTTPS 上游重定向"
    if not same_origin and target_host not in TRUSTED_NETEASE_HOSTS:
        return False, True, "已拦截指向非信任域名的上游重定向"
    return True, strip_cookie, ""


class UrllibTransport:
    """Pooled HTTPX transport keeping the legacy UrllibTransport name & signature."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        *,
        http_client: httpx.Client | None = None,
    ):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("api_base must be an HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client_lock = threading.Lock()
        self._client: httpx.Client | None = http_client
        self._owns_client = http_client is None

    def _get_client(self) -> httpx.Client:
        with self._client_lock:
            if self._client is None:
                limits = httpx.Limits(
                    max_connections=16,
                    max_keepalive_connections=8,
                    keepalive_expiry=30.0,
                )
                timeout = httpx.Timeout(self.timeout, connect=min(self.timeout, 10.0))
                try:
                    self._client = httpx.Client(
                        timeout=timeout,
                        limits=limits,
                        follow_redirects=False,
                        cookies=NoOpCookies(),
                        http2=False,
                    )
                except httpx.InvalidURL:
                    self._client = httpx.Client(
                        timeout=timeout,
                        limits=limits,
                        follow_redirects=False,
                        trust_env=False,
                        cookies=NoOpCookies(),
                        http2=False,
                    )
                self._owns_client = True
            return self._client

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None and self._owns_client:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        cookie: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> TransportResponse:
        values = {key: str(value) for key, value in (params or {}).items()}
        encoded = urllib.parse.urlencode(values).encode("utf-8")
        current_method = method.upper()
        current_url = f"{self.base_url}/{path.lstrip('/')}"
        current_body: bytes | None = None
        if current_method == "GET" and encoded:
            current_url = f"{current_url}?{encoded.decode('ascii')}"
        elif current_method != "GET":
            current_body = encoded
        current_headers = {
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
        }
        if current_body is not None:
            current_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        if cookie:
            current_headers["Cookie"] = _format_cookie_header(cookie)
        else:
            current_headers["Cookie"] = f"os=pc; appver={DEFAULT_PC_APPVER}"
        if extra_headers:
            current_headers.update(extra_headers)

        client = isolate_cookie_jar(self._get_client())
        stripped = False
        set_cookies: list[str] = []
        is_eapi = path.startswith("/eapi/")

        try:
            for redirect_index in range(_MAX_REDIRECTS + 1):
                if redirect_index and _active_read_budget.get() is not None:
                    _active_read_budget.get().consume()
                req = httpx.Request(
                    current_method,
                    current_url,
                    content=current_body,
                    headers=current_headers,
                )
                if stripped:
                    req.headers.pop("cookie", None)
                with closing(client.send(req, auth=None, stream=True, follow_redirects=False)) as response:
                    status = response.status_code
                    set_cookies.extend(response.headers.get_list("set-cookie"))
                    location = response.headers.get("location")
                    if status in _REDIRECT_STATUSES and location:
                        if path == "/api/radio/trash/add":
                            raise UpstreamError("UPSTREAM_REDIRECT_BLOCKED", "垃圾桶写入返回重定向，未再次发送")
                        if redirect_index >= _MAX_REDIRECTS:
                            raise UpstreamError("UPSTREAM_REDIRECT_LIMIT", "上游重定向次数过多")
                        next_url = urllib.parse.urljoin(current_url, location)
                        allowed, strip_cookie, reason = _is_trusted_redirect(current_url, next_url)
                        if not allowed:
                            raise UpstreamError("UPSTREAM_REDIRECT_BLOCKED", reason)
                        if strip_cookie:
                            stripped = True
                            for key in list(current_headers):
                                if key.lower() == "cookie":
                                    current_headers.pop(key, None)
                        if status in {301, 302, 303} and current_method not in {"GET", "HEAD"}:
                            current_method = "GET"
                            current_body = None
                            for key in list(current_headers):
                                if key.lower() == "content-type":
                                    current_headers.pop(key, None)
                        current_url = next_url
                        continue

                    max_bytes = _MAX_RESPONSE_BYTES if 200 <= status < 300 else _MAX_ERROR_BYTES
                    raw = _read_bounded_stream(response, max_bytes)
                    if status < 200 or status >= 300:
                        try:
                            payload = _decode_json_payload(raw, is_eapi=is_eapi)
                        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                            payload = {}
                        return TransportResponse(
                            status=status,
                            data=payload,
                            set_cookie=_join_set_cookie(set_cookies),
                        )
                    payload = _decode_json_payload(raw, is_eapi=is_eapi)
                    return TransportResponse(
                        status=status,
                        data=payload,
                        set_cookie=_join_set_cookie(set_cookies),
                    )
        except UpstreamError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError) as exc:
            raise UpstreamError("UPSTREAM_UNAVAILABLE", f"网易云服务不可用：{exc}") from exc
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise UpstreamError("UPSTREAM_INVALID_RESPONSE", "网易云服务返回了无效数据") from exc

        raise UpstreamError("UPSTREAM_REDIRECT_LIMIT", "上游重定向次数过多")


def _read_bounded_stream(response: httpx.Response, limit_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        remaining = limit_bytes - total
        if remaining <= 0:
            break
        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            total += remaining
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _decode_json_payload(raw: bytes, *, is_eapi: bool = False) -> Any:
    if is_eapi:
        return eapi_decrypt_response(raw)
    return json.loads(raw.decode("utf-8"))


class NeteaseWebTransport(UrllibTransport):
    """Direct pooled adapter for the NetEase Web & EAPI endpoints."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 15.0,
        *,
        http_client: httpx.Client | None = None,
    ):
        super().__init__(base_url, timeout, http_client=http_client)
        self._interface_transport = UrllibTransport(
            "https://interface.music.163.com",
            timeout,
            http_client=http_client,
        )
        self._playlist_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._playlist_generations: dict[int, int] = {}
        self._global_generation = 0
        self._playlist_lock = threading.RLock()

    def clear_caches(self) -> None:
        with self._playlist_lock:
            self._playlist_cache.clear()
            self._global_generation += 1

    def close(self) -> None:
        super().close()
        self._interface_transport.close()

    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        cookie: str | None = None,
    ) -> TransportResponse:
        values = dict(params or {})
        if path == "/login/qr/key":
            response = super().request("/api/login/qrcode/unikey", {"type": 1}, method="POST", cookie=cookie)
            payload = _response_mapping(response)
            nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            key = payload.get("unikey") or nested.get("unikey")
            return TransportResponse(
                response.status,
                {"code": payload.get("code", 200), "data": {"unikey": key}},
                response.set_cookie,
            )
        if path == "/login/qr/create":
            key = str(values.get("key") or "")
            qr_url = f"https://music.163.com/login?codekey={urllib.parse.quote(key)}"
            try:
                import qrcode

                image = qrcode.make(qr_url)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                qr_image = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            except Exception as exc:
                raise UpstreamError("QR_IMAGE_FAILED", "本地二维码生成失败") from exc
            return TransportResponse(200, {"code": 200, "data": {"qrurl": qr_url, "qrimg": qr_image}})
        if path == "/login/qr/check":
            return super().request(
                "/api/login/qrcode/client/login",
                {"key": values.get("key", ""), "type": 1, "timestamp": values.get("timestamp", "")},
                method="POST",
                cookie=cookie,
            )
        if path == "/user/account":
            return super().request("/api/nuser/account/get", values, cookie=cookie)
        if path == "/user/playlist":
            return super().request("/api/user/playlist", values, cookie=cookie)
        if path == "/playlist/track/all":
            return self._fetch_playlist_tracks(values, cookie=cookie)
        if path == "/lyric":
            return super().request(
                "/api/song/lyric",
                {"os": "osx", "id": values.get("id"), "lv": -1, "kv": -1, "tv": -1},
                cookie=cookie,
            )
        if path == "/lyric/new":
            return self._interface_transport.request(
                "/api/song/lyric/v1",
                {
                    "id": values.get("id"),
                    "cp": values.get("cp", "false"),
                    "tv": values.get("tv", 0),
                    "lv": values.get("lv", 0),
                    "rv": values.get("rv", 0),
                    "kv": values.get("kv", 0),
                    "yv": values.get("yv", 0),
                    "ytv": values.get("ytv", 0),
                    "yrv": values.get("yrv", 0),
                },
                method="POST",
                cookie=cookie,
            )
        if path == "/song/chorus":
            song_id = int(values.get("id", 0))
            return self._interface_transport.request(
                "/api/song/chorus",
                {"ids": json.dumps([song_id])},
                method="POST",
                cookie=cookie,
            )
        if path == "/song/detail":
            song_id = int(values.get("id", 0))
            return super().request(
                "/api/v3/song/detail",
                {"c": json.dumps([{"id": song_id}]), "ids": json.dumps([song_id])},
                method="POST",
                cookie=cookie,
            )
        if path == "/song/url/v1":
            raw_ids = values.get("ids")
            if isinstance(raw_ids, list):
                song_ids = [int(item) for item in raw_ids if str(item).isdigit()]
            elif isinstance(raw_ids, str):
                try:
                    parsed_ids = json.loads(raw_ids)
                    song_ids = [int(item) for item in parsed_ids] if isinstance(parsed_ids, list) else []
                except (ValueError, TypeError):
                    song_ids = []
            else:
                song_ids = [int(values.get("id", 0))]
            level = normalize_quality_level(values.get("level"), default="jymaster")
            header_dict = build_eapi_header(cookie)
            eapi_payload = build_player_url_v1_payload(song_ids, level=level, cookie=cookie, header=header_dict)
            encrypted = eapi_encrypt("/api/song/enhance/player/url/v1", eapi_payload)
            eapi_cookie = build_eapi_cookie(cookie, header_dict)
            return self._interface_transport.request(
                "/eapi/song/enhance/player/url/v1",
                encrypted,
                method="POST",
                cookie=eapi_cookie,
            )
        if path == "/cloudsearch":
            return super().request(
                "/api/search/get/web",
                {
                    "s": values.get("keywords", ""),
                    "type": values.get("type", 1),
                    "offset": max(0, min(1_000, _safe_int(values.get("offset", 0)))),
                    "limit": max(1, min(100, _safe_int(values.get("limit", 30)) or 30)),
                },
                cookie=cookie,
            )
        if path == "/likelist":
            return super().request("/api/song/like/get", {"uid": values.get("uid")}, cookie=cookie)
        if path == "/like":
            like_flag = "true" if str(values.get("like", "false")).lower() == "true" else "false"
            return super().request(
                "/api/radio/like",
                {"trackId": values.get("id"), "like": like_flag, "time": 25},
                cookie=cookie,
            )
        if path == "/playlist/create":
            return super().request("/api/playlist/create", values, method="POST", cookie=cookie)
        if path == "/playlist/tracks":
            track_values = [int(item) for item in str(values.get("tracks", "")).split(",") if item.strip().isdigit()]
            playlist_id = int(values.get("pid", 0))
            with self._playlist_lock:
                self._playlist_cache.pop(playlist_id, None)
                self._playlist_generations[playlist_id] = self._playlist_generations.get(playlist_id, 0) + 1
            return super().request(
                "/api/playlist/manipulate/tracks",
                {
                    "op": values.get("op", "add"),
                    "pid": values.get("pid"),
                    "trackIds": json.dumps(track_values),
                    "tracks": json.dumps(track_values),
                },
                method="POST",
                cookie=cookie,
            )
        if path == "/user/cloud":
            return super().request(
                "/api/v1/cloud/get",
                {"limit": values.get("limit", 200), "offset": values.get("offset", 0)},
                method="POST",
                cookie=cookie,
            )
        if path == "/recommend/songs":
            return super().request("/api/v3/discovery/recommend/songs", {}, method="POST", cookie=cookie)
        if path == "/personal_fm":
            return super().request("/api/v1/radio/get", {}, method="POST", cookie=cookie)
        if path == "/playmode/intelligence/list":
            return super().request(
                "/api/playmode/intelligence/list",
                {
                    "songId": values.get("id"),
                    "type": "fromPlayOne",
                    "playlistId": values.get("pid"),
                    "startMusicId": values.get("sid") or values.get("id"),
                    "count": values.get("count", 30),
                },
                method="POST",
                cookie=cookie,
            )
        discovery_routes = {
            "/simi/playlist": "/api/discovery/simiPlaylist",
            "/recommend/resource": "/api/v1/discovery/recommend/resource",
            "/artist/top/song": "/api/artist/top/song",
            "/history/recommend/songs": "/api/discovery/recommend/songs/history/recent",
            "/history/recommend/songs/detail": "/api/discovery/recommend/songs/history/detail",
            "/fm_trash": "/api/radio/trash/add",
            "/playlist/sample": "/api/v6/playlist/detail",
            "/playlist/sample/details": "/api/v3/song/detail",
        }
        if path in discovery_routes:
            return super().request(discovery_routes[path], values, method="POST", cookie=cookie)
        if path == "/simi/song":
            return super().request(
                "/api/v1/discovery/simiSong",
                {
                    "songid": values.get("id"),
                    "limit": values.get("limit", 50),
                    "offset": values.get("offset", 0),
                },
                method="POST",
                cookie=cookie,
            )
        raise UpstreamError("UPSTREAM_ROUTE_UNSUPPORTED", f"不支持的网易云调用：{path}")

    def _fetch_playlist_tracks(self, values: dict[str, Any], *, cookie: str | None) -> TransportResponse:
        playlist_id = int(values.get("id", 0))
        force_fresh = str(values.get("fresh", "")).lower() in {"1", "true", "yes"}

        cached_songs: list[dict[str, Any]] | None = None
        with self._playlist_lock:
            generation = (self._global_generation, self._playlist_generations.get(playlist_id, 0))
            if force_fresh:
                self._playlist_cache.pop(playlist_id, None)
            else:
                cached = self._playlist_cache.get(playlist_id)
                if cached and time.monotonic() - cached[0] < 30:
                    cached_songs = cached[1]

        if cached_songs is not None:
            all_songs = cached_songs
            response = TransportResponse(200, {"code": 200})
        else:
            response = super().request(
                "/api/v6/playlist/detail",
                {"id": playlist_id, "n": 100_000, "s": 0},
                cookie=cookie,
            )
            payload = _response_mapping(response)
            playlist = payload.get("playlist")
            if not isinstance(playlist, dict):
                raise UpstreamError("TRACKS_INVALID", "歌曲列表格式无效")
            raw_tracks = playlist.get("tracks") if isinstance(playlist.get("tracks"), list) else []
            raw_track_ids = playlist.get("trackIds")
            if not isinstance(raw_track_ids, list):
                raise UpstreamError("TRACKS_INCOMPLETE", "网易云未返回完整 trackIds，已中止以防歌单数据截断")
            track_ids = [
                item.get("id")
                for item in raw_track_ids
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            ]
            if force_fresh and (len(set(track_ids)) != len(raw_track_ids) or any(type(i) is not int or i <= 0 for i in track_ids)):
                raise UpstreamError("TRACKS_INCOMPLETE", "歌单曲目标识无效或重复")
            declared_count = playlist.get("trackCount")
            if isinstance(declared_count, int) and not isinstance(declared_count, bool) and declared_count >= 0:
                if force_fresh and len(track_ids) != declared_count:
                    raise UpstreamError(
                        "TRACKS_INCOMPLETE",
                        f"歌单曲目清单长度与声明数量不一致（{len(track_ids)}/{declared_count}）",
                    )
                if not force_fresh and declared_count > 0 and len(track_ids) < declared_count:
                    raise UpstreamError(
                        "TRACKS_INCOMPLETE",
                        f"网易云返回的歌单曲目清单不完整（{len(track_ids)}/{declared_count}）",
                    )

            privileges_by_id: dict[int, dict[str, Any]] = {}
            _collect_privileges(privileges_by_id, payload.get("privileges"))

            songs_by_id: dict[int, dict[str, Any]] = {
                item["id"]: dict(item)
                for item in raw_tracks
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            }
            missing_ids = list(dict.fromkeys(song_id for song_id in track_ids if song_id not in songs_by_id))
            chunks = [missing_ids[index : index + 500] for index in range(0, len(missing_ids), 500)]

            def fetch_chunk(chunk: list[int]) -> tuple[list[dict[str, Any]], Any]:
                details = super(NeteaseWebTransport, self).request(
                    "/api/v3/song/detail",
                    {"c": json.dumps([{"id": song_id} for song_id in chunk]), "ids": json.dumps(chunk)},
                    method="POST",
                    cookie=cookie,
                )
                detail_payload = _response_mapping(details)
                if detail_payload.get("code") != 200 or not isinstance(detail_payload.get("songs"), list):
                    raise UpstreamError("TRACKS_INVALID", "网易云未返回完整歌曲详情")
                return detail_payload["songs"], detail_payload.get("privileges")

            def fetch_chunk_resilient(chunk: list[int]) -> None:
                try:
                    chunk_songs, chunk_privs = fetch_chunk(chunk)
                    _merge_detail_chunk(songs_by_id, privileges_by_id, chunk_songs, chunk_privs)
                except AppError as exc:
                    if exc.code in {"UPSTREAM_RISK_CONTROL", "RISK_CONTROL_COOLDOWN"} or len(chunk) <= 1:
                        raise
                    sub_size = 50 if len(chunk) > 50 else (10 if len(chunk) > 10 else 1)
                    for sub_idx in range(0, len(chunk), sub_size):
                        if sub_idx:
                            time.sleep(random.uniform(0.05, 0.12))
                        fetch_chunk_resilient(chunk[sub_idx : sub_idx + sub_size])
                    return

                still_missing = [sid for sid in chunk if sid not in songs_by_id]
                if still_missing and len(chunk) > 1:
                    sub_size = 50 if len(chunk) > 50 else (10 if len(chunk) > 10 else 1)
                    if sub_size >= len(still_missing) and len(still_missing) > 1:
                        sub_size = max(1, len(still_missing) // 2)
                    for sub_idx in range(0, len(still_missing), sub_size):
                        sub_chunk = still_missing[sub_idx : sub_idx + sub_size]
                        time.sleep(random.uniform(0.05, 0.12))
                        try:
                            sub_songs, sub_privs = fetch_chunk(sub_chunk)
                            _merge_detail_chunk(songs_by_id, privileges_by_id, sub_songs, sub_privs)
                        except AppError as exc:
                            if exc.code in {"UPSTREAM_RISK_CONTROL", "RISK_CONTROL_COOLDOWN"}:
                                raise
                            if len(sub_chunk) > 1:
                                fetch_chunk_resilient(sub_chunk)

            # The parent read holds one account-wide permit, including its detail requests.
            for index, chunk in enumerate(chunks):
                if index:
                    time.sleep(random.uniform(0.08, 0.18))
                fetch_chunk_resilient(chunk)

            all_songs = []
            for song_id in track_ids:
                item = songs_by_id.get(song_id)
                if item is None:
                    continue
                merged_song = dict(item)
                priv = privileges_by_id.get(song_id)
                if priv:
                    existing_priv = merged_song.get("privilege") if isinstance(merged_song.get("privilege"), dict) else {}
                    merged_song["privilege"] = {**existing_priv, **priv}
                all_songs.append(merged_song)
            if len(all_songs) != len(track_ids):
                raise UpstreamError("TRACKS_INCOMPLETE", "网易云返回的歌单歌曲不完整，请稍后重试")

            with self._playlist_lock:
                current_gen = (self._global_generation, self._playlist_generations.get(playlist_id, 0))
                if current_gen == generation:
                    self._playlist_cache[playlist_id] = (time.monotonic(), all_songs)
                    while len(self._playlist_cache) > 4:
                        oldest = min(self._playlist_cache, key=lambda key: self._playlist_cache[key][0])
                        self._playlist_cache.pop(oldest)

        try:
            offset = max(0, int(values.get("offset", 0)))
            limit = max(1, min(500, int(values.get("limit", 500))))
        except (TypeError, ValueError):
            offset, limit = 0, 500
        songs = _slice_playlist_tracks(all_songs, limit, offset)
        return TransportResponse(response.status, {**response.data, "songs": songs}, response.set_cookie)


def _collect_privileges(target: dict[int, dict[str, Any]], raw_privileges: Any) -> None:
    if not isinstance(raw_privileges, list):
        return
    for item in raw_privileges:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            target[item["id"]] = dict(item)


def _merge_detail_chunk(
    songs_by_id: dict[int, dict[str, Any]],
    privileges_by_id: dict[int, dict[str, Any]],
    chunk_songs: list[dict[str, Any]],
    chunk_privs: Any,
) -> None:
    for item in chunk_songs:
        if isinstance(item, dict) and isinstance(item.get("id"), int):
            songs_by_id[item["id"]] = dict(item)
    _collect_privileges(privileges_by_id, chunk_privs)


def _join_set_cookie(headers: list[str]) -> str:
    pairs: list[str] = []
    for header in headers:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:
            continue
        pairs.extend(f"{name}={morsel.value}" for name, morsel in parsed.items())
    return "; ".join(pairs)


def _format_cookie_header(cookie: str) -> str:
    keys: set[str] = set()
    for part in cookie.split(";"):
        if "=" in part:
            keys.add(part.split("=", 1)[0].strip().lower())
    prefixes: list[str] = []
    if "os" not in keys:
        prefixes.append("os=pc")
    if "appver" not in keys:
        prefixes.append(f"appver={DEFAULT_PC_APPVER}")
    if prefixes:
        return "; ".join(prefixes) + f"; {cookie}"
    return cookie


def _check_risk_control(response: TransportResponse) -> None:
    payload = response.data if isinstance(response.data, dict) else {}
    message = f"{payload.get('message', '')} {payload.get('msg', '')}".lower()
    if (response.status in {405, 429, 503}
            or str(payload.get("code")) in {"-460", "-462", "-447", "405", "429", "503"}
            or any(word in message for word in ("频繁", "风控", "cheating", "too fast", "risk", "拥挤", "验证"))):
        raise UpstreamError("UPSTREAM_RISK_CONTROL", "网易云触发限流或安全验证，已暂停本账号写入", 429)


def _response_mapping(response: TransportResponse) -> dict[str, Any]:
    _check_risk_control(response)
    if not isinstance(response.data, dict):
        raise UpstreamError("UPSTREAM_INVALID_RESPONSE", "网易云服务返回了无效数据")
    return response.data


def _slice_playlist_tracks(items: Any, limit: int, offset: int) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [item for item in items[offset : offset + limit] if isinstance(item, dict)]


def normalize_cookie_header(value: str) -> str:
    """Keep only cookie name/value pairs, never Set-Cookie attributes."""
    if not value or "\r" in value or "\n" in value:
        raise UpstreamError("SESSION_INVALID", "登录服务返回了无效会话")
    parsed = SimpleCookie()
    try:
        parsed.load(value)
    except Exception as exc:
        raise UpstreamError("SESSION_INVALID", "登录服务返回了无效会话") from exc
    result = "; ".join(f"{name}={morsel.value}" for name, morsel in parsed.items())
    if not result:
        raise UpstreamError("SESSION_INVALID", "登录服务返回了无效会话")
    return result


DEFAULT_RISK_COOLDOWN_SEC = 60.0


class NeteaseRequestPacer:
    """Share write serialization and cooldowns across clients bound to the same cookie."""

    def __init__(self, sleep_fn=time.sleep, monotonic_fn=time.monotonic, rng_fn=random.uniform):
        self.sleep_fn = sleep_fn
        self.clock = monotonic_fn
        self.uniform = rng_fn
        self._lock = threading.Lock()
        self._reads = {}
        self.max_concurrent_reads = 2
        self._writes = {}
        self._last_write = {}
        self.risk_cooldown_until = {}

    def cooldown_status(self, account_key):
        with self._lock:
            return max(0.0, self.risk_cooldown_until.get(account_key, 0) - self.clock())

    def settle(self):
        self.sleep_fn(self.uniform(1.0, 1.5))

    def check(self, key):
        if self.cooldown_status(key):
            raise AppError("RISK_CONTROL_COOLDOWN", "网易云风控冷却中，请稍后再试", 429)

    @contextmanager
    def request(self, key, path):
        write = path in {"/like", "/playlist/tracks", "/playlist/create", "/fm_trash"}
        with self._lock:
            locks = self._writes if write else self._reads
            if key not in locks:
                locks[key] = threading.Lock() if write else threading.BoundedSemaphore(self.max_concurrent_reads)
            lock = locks[key]
        with lock:
            # A sent write can still be verified once, even during write cooldown.
            if path not in {"/playlist/track/all", "/user/playlist", "/likelist"}:
                self.check(key)
            if write:
                if key in self._last_write:
                    interval = self.uniform(0.45, 0.85) if path == "/like" else self.uniform(0.60, 1.10)
                    delay = self._last_write[key] + interval - self.clock()
                    if delay > 0:
                        self.sleep_fn(delay)
                    self.check(key)
            try:
                yield
            except AppError as exc:
                if exc.code == "UPSTREAM_RISK_CONTROL":
                    with self._lock:
                        self.risk_cooldown_until[key] = self.clock() + DEFAULT_RISK_COOLDOWN_SEC
                raise
            finally:
                if write:
                    self._last_write[key] = self.clock()


class NeteaseClient:
    """Typed client for NetEase operations used by Pruner Studio."""

    def __init__(self, transport: Transport, cookie: str | None = None, pacer: NeteaseRequestPacer | None = None):
        self.pacer = pacer or NeteaseRequestPacer()
        self.transport = transport
        self.cookie = cookie

    @staticmethod
    def cookie_key(cookie: str | None) -> str:
        return hashlib.sha256(cookie.encode()).hexdigest() if cookie else "default"

    def check_write_allowed(self) -> None:
        self.pacer.check(self.cookie_key(self.cookie))

    def cooldown_seconds(self) -> float:
        return self.pacer.cooldown_status(self.cookie_key(self.cookie))

    def set_cookie(self, cookie: str | None) -> None:
        self.cookie = cookie

    def with_cookie(self, cookie: str | None) -> NeteaseClient:
        """Create a lightweight snapshot client bound to a fixed cookie."""
        return NeteaseClient(self.transport, cookie, self.pacer)

    def _call(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        accepted_codes: set[int] | None = None,
        cookie_override: Any = _UNSET,
    ) -> tuple[dict[str, Any], str]:
        effective_cookie = self.cookie if cookie_override is _UNSET else cookie_override
        with self.pacer.request(self.cookie_key(effective_cookie), path):
            if _active_read_budget.get() is not None:
                _active_read_budget.get().consume()
            response = self.transport.request(path, params, method=method, cookie=effective_cookie)
            if path == "/fm_trash" and (response.status == 401 or
                                        isinstance(response.data, dict) and response.data.get("code") in (301, 401)):
                raise AppError("AUTH_REQUIRED", "登录状态已失效，请重新登录", 401)
            _check_risk_control(response)
        if path.startswith("/history/recommend/songs") and response.status in (401, 403):
            raise AppError("AUTH_REQUIRED" if response.status == 401 else "VIP_REQUIRED", "历史推荐访问受限", response.status)
        if response.status < 200 or response.status >= 300:
            raise UpstreamError("UPSTREAM_HTTP_ERROR", f"网易云服务返回 HTTP {response.status}")
        payload = response.data
        if not isinstance(payload, dict):
            raise UpstreamError("UPSTREAM_INVALID_RESPONSE", "网易云服务返回了无效数据")
        code = payload.get("code")
        valid = accepted_codes or {200}
        if code not in valid:
            message = payload.get("message") or payload.get("msg") or "网易云操作未成功"
            raise UpstreamError("UPSTREAM_REJECTED", str(message))
        return payload, response.set_cookie

    def create_qr(self) -> dict[str, str]:
        key_payload, _ = self._call("/login/qr/key", {"timestamp": int(time.time() * 1000)})
        key = key_payload.get("data", {}).get("unikey")
        if not isinstance(key, str) or not key:
            raise UpstreamError("QR_KEY_MISSING", "未能创建登录二维码")
        qr_payload, _ = self._call("/login/qr/create", {"key": key, "qrimg": "true"})
        qr_data = qr_payload.get("data", {})
        url = qr_data.get("qrurl")
        image = qr_data.get("qrimg")
        if not isinstance(url, str) or not url:
            raise UpstreamError("QR_IMAGE_MISSING", "未能创建登录二维码")
        result = {"key": key, "qr_url": url}
        if isinstance(image, str) and image.startswith("data:image/"):
            result["qr_image"] = image
        return result

    def poll_qr(self, key: str) -> dict[str, Any]:
        if not key or len(key) > 256:
            raise AppError("INVALID_QR_KEY", "二维码标识无效")
        payload, header_cookie = self._call(
            "/login/qr/check",
            {"key": key, "timestamp": int(time.time() * 1000)},
            accepted_codes={800, 801, 802, 803},
        )
        code = payload["code"]
        states = {800: "expired", 801: "waiting", 802: "confirmed", 803: "authorized"}
        result: dict[str, Any] = {"code": code, "state": states[code], "message": str(payload.get("message", ""))}
        if code == 803:
            raw_cookie = payload.get("cookie") or header_cookie
            if not isinstance(raw_cookie, str) or not raw_cookie:
                raise UpstreamError("SESSION_MISSING", "登录成功但未收到会话")
            candidate_cookie = normalize_cookie_header(raw_cookie)
            # Verify candidate cookie atomically BEFORE mutating self.cookie.
            verified_user = self.account(cookie_override=candidate_cookie)
            self.cookie = candidate_cookie
            result["user"] = verified_user.to_dict()
        return result

    def account(self, *, cookie_override: str | None = None) -> AccountSummary:
        effective_cookie = cookie_override if cookie_override is not None else self.cookie
        if not effective_cookie:
            raise AppError("NOT_AUTHENTICATED", "请先扫码登录", 401)
        payload, _ = self._call(
            "/user/account",
            {"timestamp": int(time.time() * 1000)},
            accepted_codes={200, 301},
            cookie_override=effective_cookie,
        )
        if payload.get("code") == 301:
            raise AppError("SESSION_EXPIRED", "登录已失效，请重新扫码", 401)
        profile = payload.get("profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("userId"), int):
            raise UpstreamError("ACCOUNT_INVALID", "网易云返回的账号信息格式无效")
        account_obj = payload.get("account") if isinstance(payload.get("account"), dict) else {}
        vip_type = _safe_int(account_obj.get("vipType")) or _safe_int(profile.get("vipType"))
        return AccountSummary(
            user_id=profile["userId"],
            display_name=str(profile.get("nickname") or "网易云用户"),
            avatar_url=str(profile.get("avatarUrl") or ""),
            vip_type=vip_type,
        )

    def playlists(
        self,
        account: AccountSummary | None = None,
        *,
        include_subscribed: bool = False,
    ) -> list[PlaylistSummary]:
        owner = account or self.account()
        payload, _ = self._call("/user/playlist", {"uid": owner.user_id, "limit": 1000, "offset": 0})
        items = payload.get("playlist")
        if not isinstance(items, list):
            raise UpstreamError("PLAYLISTS_INVALID", "歌单列表格式无效")
        result: list[PlaylistSummary] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
            is_owner = creator.get("userId") == owner.user_id
            if not is_owner and not include_subscribed:
                continue
            playlist_id = item.get("id")
            if not isinstance(playlist_id, int) or playlist_id <= 0:
                continue
            if is_owner:
                kind = "liked" if item.get("specialType") == 5 else "self_created"
                if kind == "liked" and (type(item.get("trackCount")) is not int or item["trackCount"] < 0):
                    raise UpstreamError("TRACKS_INCOMPLETE", "喜欢列表缺少曲目数量")
            else:
                kind = "subscribed"
            result.append(
                PlaylistSummary(
                    id=playlist_id,
                    name=str(item.get("name") or "未命名歌单"),
                    track_count=max(0, int(item.get("trackCount") or 0)),
                    kind=kind,
                    privacy="private" if int(item.get("privacy") or 0) else "public",
                    cover_url=str(item.get("coverImgUrl") or ""),
                )
            )
        return result

    def playlist_tracks(self, playlist_id: int, *, fresh: bool = False) -> list[Song]:
        tracks: list[Song] = []
        offset = 0
        limit = 500
        while True:
            payload, _ = self._call(
                "/playlist/track/all",
                {"id": playlist_id, "limit": limit, "offset": offset, "fresh": fresh and offset == 0},
            )
            page = payload.get("songs")
            if not isinstance(page, list):
                raise UpstreamError("TRACKS_INVALID", "歌曲列表格式无效")
            privileges_by_id: dict[int, dict[str, Any]] = {}
            _collect_privileges(privileges_by_id, payload.get("privileges"))
            for item in page:
                if not isinstance(item, dict):
                    continue
                song_id = item.get("id")
                priv_override = privileges_by_id.get(song_id) if isinstance(song_id, int) else None
                tracks.append(_song_from_api(item, playlist_id, privilege_override=priv_override))
            if len(page) < limit:
                break
            offset += len(page)
            self.pacer.sleep_fn(self.pacer.uniform(0.08, 0.18))
            if offset > 100_000:
                raise UpstreamError("TRACKS_TOO_LARGE", "歌单歌曲数量超出支持范围")
        return tracks

    def lyrics(self, song_id: int) -> dict[str, Any]:
        payload, _ = self._call("/lyric", {"id": song_id})
        return {
            "id": song_id,
            "lyrics": _extract_lyric_text(payload, "lrc"),
            "tlyrics": _extract_lyric_text(payload, "tlyric"),
        }

    def lyrics_v2(self, song_id: int) -> dict[str, Any]:
        payload: dict[str, Any] | None = None
        try:
            payload, _ = self._call(
                "/lyric/new",
                {
                    "id": song_id,
                    "cp": "false",
                    "tv": 0,
                    "lv": 0,
                    "rv": 0,
                    "kv": 0,
                    "yv": 0,
                    "ytv": 0,
                    "yrv": 0,
                },
            )
        except (AssertionError, UpstreamError):
            payload, _ = self._call("/lyric", {"id": song_id})
            return _extract_lyrics_bundle(song_id, payload)

        result = _extract_lyrics_bundle(song_id, payload)
        if not result["lyrics"].strip() and not result["yrc"].strip():
            try:
                fallback_payload, _ = self._call("/lyric", {"id": song_id})
                fallback = _extract_lyrics_bundle(song_id, fallback_payload)
                for key in ("lyrics", "tlyrics", "romalrc", "yrc"):
                    if not result[key].strip() and fallback[key].strip():
                        result[key] = fallback[key]
            except (AssertionError, UpstreamError):
                pass
        return result

    def chorus(self, song_id: int) -> tuple[int | None, int | None]:
        payload, _ = self._call("/song/chorus", {"id": song_id})
        items = payload.get("chorus")
        if not isinstance(items, list):
            return None, None
        for entry in items:
            if not isinstance(entry, dict):
                continue
            start = _safe_int(entry.get("startTime"))
            end = _safe_int(entry.get("endTime"))
            if start >= 0 and end > start:
                return start, end
        return None, None

    def song_detail(self, song_id: int) -> Song | None:
        payload, _ = self._call("/song/detail", {"id": song_id})
        songs = payload.get("songs")
        if not isinstance(songs, list) or not songs or not isinstance(songs[0], dict):
            return None
        privileges_by_id: dict[int, dict[str, Any]] = {}
        _collect_privileges(privileges_by_id, payload.get("privileges"))
        return _song_from_api(songs[0], None, privilege_override=privileges_by_id.get(song_id))

    def audio_url(
        self,
        song_id: int,
        level: str = "jymaster",
        *,
        allow_free_trial: bool = False,
    ) -> dict[str, Any] | None:
        normalized_level = normalize_quality_level(level, default="jymaster")
        payload, _ = self._call("/song/url/v1", {"id": song_id, "level": normalized_level})
        entries = payload.get("data")
        if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
            return None
        item = entries[0]
        url = item.get("url")
        free_trial = bool(item.get("freeTrialInfo"))
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return None
        if free_trial and not allow_free_trial:
            return None
        audio_format = str(item.get("type") or item.get("encodeType") or "").strip().lower()
        return {
            "id": int(item.get("id") or song_id),
            "url": url,
            "level": str(item.get("level") or "unknown"),
            "format": audio_format,
            "bitrate": _safe_int(item.get("br")),
            "sample_rate": _safe_int(item.get("sr")),
            "size": _safe_int(item.get("size")),
            "time_ms": _safe_int(item.get("time")),
            "free_trial": free_trial,
        }

    def search_songs_page(self, query: str, limit: int = 30, offset: int = 0) -> tuple[list[Song], bool]:
        safe_limit = max(1, min(100, _safe_int(limit) or 30))
        safe_offset = max(0, min(1_000, _safe_int(offset)))
        params: dict[str, Any] = {"keywords": query, "type": 1, "limit": safe_limit}
        if safe_offset:
            params["offset"] = safe_offset
        payload, _ = self._call("/cloudsearch", params)
        result_obj = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        items = result_obj.get("songs", []) if isinstance(result_obj.get("songs"), list) else []
        privileges_by_id: dict[int, dict[str, Any]] = {}
        _collect_privileges(privileges_by_id, result_obj.get("privileges") or payload.get("privileges"))
        songs: list[Song] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            song_id = item.get("id")
            priv_override = privileges_by_id.get(song_id) if isinstance(song_id, int) else None
            try:
                songs.append(_song_from_api(item, None, privilege_override=priv_override))
            except UpstreamError:
                continue
        raw_has_more = result_obj.get("hasMore")
        raw_song_count = result_obj.get("songCount")
        if isinstance(raw_has_more, bool):
            has_more = raw_has_more
        elif isinstance(raw_song_count, int) and not isinstance(raw_song_count, bool):
            has_more = (safe_offset + len(songs)) < raw_song_count
        else:
            has_more = len(songs) >= safe_limit
        return songs, has_more

    def search_songs(self, query: str, limit: int = 30, offset: int = 0) -> list[Song]:
        songs, _ = self.search_songs_page(query, limit=limit, offset=offset)
        return songs

    def liked_ids(self, user_id: int) -> set[int]:
        payload, _ = self._call("/likelist", {"uid": user_id})
        ids = payload.get("ids")
        if not isinstance(ids, list) or any(type(sid) is not int or sid <= 0 for sid in ids):
            raise UpstreamError("LIKED_LIST_INVALID", "喜欢列表格式无效")
        return set(ids)

    def request_like(self, song_id: int, like: bool = True) -> None:
        self._call(
            "/like",
            {"id": song_id, "like": "true" if like else "false", "timestamp": int(time.time() * 1000)},
            method="POST",
        )

    def request_unlike(self, song_id: int) -> None:
        self.request_like(song_id, like=False)

    def request_create_playlist(self, name: str, privacy: int = 0) -> int:
        payload, _ = self._call("/playlist/create", {"name": name, "privacy": privacy}, method="POST")
        playlist = payload.get("playlist")
        playlist_id = playlist.get("id") if isinstance(playlist, dict) else payload.get("id")
        if not isinstance(playlist_id, int) or playlist_id <= 0:
            raise UpstreamError("CREATE_PLAYLIST_INVALID", "创建歌单后未返回有效 ID")
        return playlist_id

    def request_add_tracks(self, playlist_id: int, song_ids: list[int]) -> None:
        if not song_ids:
            return
        self._call(
            "/playlist/tracks",
            {"op": "add", "pid": playlist_id, "tracks": ",".join(str(value) for value in song_ids)},
            method="POST",
        )

    def request_remove_tracks(self, playlist_id: int, song_ids: list[int]) -> None:
        if not song_ids:
            return
        self._call(
            "/playlist/tracks",
            {"op": "del", "pid": playlist_id, "tracks": ",".join(str(value) for value in song_ids)},
            method="POST",
        )

    def cloud_songs(self, limit: int = 200, offset: int = 0) -> list[Song]:
        return self.cloud_songs_page(limit, offset)[0]

    def cloud_songs_page(self, limit: int, offset: int) -> tuple[list[Song], bool]:
        songs, more, _ = self.cloud_page_with_count(limit, offset)
        return songs, more

    def cloud_page_with_count(self, limit, offset):
        payload, _ = self._call("/user/cloud", {"limit": max(1, min(1000, int(limit))), "offset": max(0, int(offset))})
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise UpstreamError("CLOUD_RESPONSE_INVALID", "云盘分页数据无效")
        songs: list[Song] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise UpstreamError("CLOUD_RESPONSE_INVALID", "云盘歌曲数据无效")
            simple = entry.get("simpleSong") if isinstance(entry.get("simpleSong"), dict) else {}
            song_id = simple.get("id") if isinstance(simple.get("id"), int) and simple["id"] > 0 else entry.get("songId")
            if not isinstance(song_id, int) or song_id <= 0:
                raise UpstreamError("CLOUD_RESPONSE_INVALID", "云盘歌曲数据无效")
            merged = dict(simple)
            merged["id"] = song_id
            if not merged.get("name"):
                merged["name"] = entry.get("songName") or entry.get("fileName") or "云盘歌曲"
            if not merged.get("ar") and not merged.get("artists") and entry.get("artist"):
                merged["ar"] = [{"name": str(entry["artist"])}]
            if not merged.get("al") and not merged.get("album") and entry.get("album"):
                merged["al"] = {"name": str(entry["album"])}
            priv = dict(merged.get("privilege") or {}) if isinstance(merged.get("privilege"), dict) else {}
            priv["cs"] = True
            priv.setdefault("st", 0)
            merged["privilege"] = priv
            try:
                songs.append(
                    _song_from_api(
                        merged,
                        "cloud",
                        source_playlist_name="网易云音乐云盘",
                        force_cloud=True,
                    )
                )
            except UpstreamError as exc:
                raise UpstreamError("CLOUD_RESPONSE_INVALID", "云盘歌曲数据无效") from exc
        total = payload.get("count")
        more = payload.get("hasMore")
        if not isinstance(more, bool):
            more = offset + len(entries) < total if type(total) is int else len(entries) >= limit
        if (more and not entries) or (not more and type(total) is int and offset + len(entries) < total):
            raise UpstreamError("CLOUD_SYNC_INCOMPLETE", "云盘分页数据不完整，已保留旧缓存", 502)
        return songs, more, total if type(total) is int else None

    def daily_recommend_songs(self) -> list[Song]:
        payload, _ = self._call("/recommend/songs", {})
        data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        items = data_obj.get("dailySongs")
        if not isinstance(items, list):
            items = payload.get("recommend") if isinstance(payload.get("recommend"), list) else []
        return _parse_song_list(items, None)

    def personal_fm(self) -> list[Song]:
        payload, _ = self._call("/personal_fm", {})
        items = payload.get("data")
        if not isinstance(items, list):
            return []
        return _parse_song_list(items, None)

    def intelligence_list(
        self,
        song_id: int,
        playlist_id: int,
        *,
        start_music_id: int | None = None,
        count: int = 30,
    ) -> list[Song]:
        payload, _ = self._call(
            "/playmode/intelligence/list",
            {
                "id": song_id,
                "pid": playlist_id,
                "sid": start_music_id or song_id,
                "count": max(1, min(100, int(count))),
            },
        )
        entries = payload.get("data")
        if not isinstance(entries, list):
            return []
        raw_songs: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            song_info = entry.get("songInfo")
            if isinstance(song_info, dict):
                raw_songs.append(song_info)
            elif isinstance(entry.get("id"), int):
                raw_songs.append(entry)
        return _parse_song_list(raw_songs, None)

    @staticmethod
    def _playlist_summaries(rows):
        return [{"id": row["id"], "name": str(row.get("name") or ""),
                 "track_count": _safe_int(row.get("trackCount"))}
                for row in rows or [] if isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0]

    def similar_playlists(self, song_id: int, limit: int = 10) -> list[dict]:
        payload, _ = self._call("/simi/playlist", {"songid": song_id, "limit": limit})
        return self._playlist_summaries(payload.get("playlists"))[:limit]

    def recommend_playlists(self) -> list[dict]:
        payload, _ = self._call("/recommend/resource")
        return self._playlist_summaries(payload.get("recommend"))

    def artist_top_songs(self, artist_id: int) -> list[Song]:
        payload, _ = self._call("/artist/top/song", {"id": artist_id})
        return _parse_song_list((payload.get("songs") or [])[:50], None)

    def _history(self, path, params=None):
        payload, _ = self._call(path, params, accepted_codes={200, 301, 401, 403, -2})
        if payload["code"] in {301, 401}:
            raise AppError("AUTH_REQUIRED", "请先登录网易云", 401)
        if payload["code"] in {403, -2}:
            raise AppError("VIP_REQUIRED", "历史日推需要会员权限", 403)
        return payload.get("data") or {}

    def daily_history_dates(self) -> dict:
        data = self._history("/history/recommend/songs")
        dates = []
        for item in data.get("dates") or []:
            try:
                dates.append(date.fromisoformat(item).isoformat())
            except (TypeError, ValueError):
                continue
        return {"dates": dates, "status": "ok" if dates else "empty", "reason": "" if dates else "暂无历史日推"}

    def daily_history_songs(self, date_str: str) -> list[Song]:
        data = self._history("/history/recommend/songs/detail", {"date": date_str})
        return _parse_song_list(data.get("songs") or data.get("dailySongs") or [], None)

    def fm_trash(self, song_id: int) -> None:
        self._call("/fm_trash", {"songId": song_id}, method="POST")

    def playlist_tracks_sample(self, playlist_id: int, limit: int = 60) -> list[Song]:
        limit = max(1, min(60, limit))
        payload, _ = self._call("/playlist/sample", {"id": playlist_id, "n": limit, "s": 0})
        playlist = payload.get("playlist") or {}
        privileges = {}
        _collect_privileges(privileges, payload.get("privileges"))
        songs = {s.id: s for s in _parse_song_list(playlist.get("tracks", [])[:limit], playlist_id, privileges)}
        ids = [r["id"] for r in playlist.get("trackIds", []) if isinstance(r, dict)
               and type(r.get("id")) is int and r["id"] > 0 and r["id"] not in songs][:limit-len(songs)]
        budget = _active_read_budget.get()
        if ids and (budget is None or budget.remaining):
            detail, _ = self._call("/playlist/sample/details", {"c": json.dumps([{"id": sid} for sid in ids])})
            _collect_privileges(privileges, detail.get("privileges"))
            songs.update((s.id, s) for s in _parse_song_list(detail.get("songs", []), playlist_id, privileges))
        return list(songs.values())[:limit]

    def similar_songs(self, song_id: int) -> list[Song]:
        payload, _ = self._call("/simi/song", {"id": song_id})
        items = payload.get("songs")
        if not isinstance(items, list):
            return []
        return _parse_song_list(items, None)


def _extract_lyric_text(payload: dict[str, Any], key: str) -> str:
    section = payload.get(key)
    if isinstance(section, dict):
        return str(section.get("lyric") or "")
    return ""


def _extract_lyrics_bundle(song_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": song_id,
        "lyrics": _extract_lyric_text(payload, "lrc"),
        "tlyrics": _extract_lyric_text(payload, "tlyric") or _extract_lyric_text(payload, "ytlrc"),
        "romalrc": _extract_lyric_text(payload, "romalrc") or _extract_lyric_text(payload, "yromalrc"),
        "yrc": _extract_lyric_text(payload, "yrc"),
    }


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_song_list(items: list[Any], source_playlist_id: int | str | None, privileges=None) -> list[Song]:
    songs: list[Song] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            songs.append(_song_from_api(item, source_playlist_id, privilege_override=(privileges or {}).get(item.get("id"))))
        except UpstreamError:
            continue
    return songs


def _song_from_api(
    item: dict[str, Any],
    source_playlist_id: int | str | None,
    *,
    privilege_override: dict[str, Any] | None = None,
    source_playlist_name: str = "",
    force_cloud: bool = False,
) -> Song:
    song_id = item.get("id")
    if not isinstance(song_id, int) or song_id <= 0:
        raise UpstreamError("SONG_INVALID", "歌曲数据缺少有效 ID")
    artist_items = item.get("ar") if isinstance(item.get("ar"), list) else item.get("artists", [])
    artists = [str(artist.get("name")) for artist in artist_items if isinstance(artist, dict) and artist.get("name")]
    album_data = item.get("al") if isinstance(item.get("al"), dict) else item.get("album", {})
    duration = item.get("dt", item.get("duration", 0))
    duration_ms = _safe_int(duration)

    raw_priv = item.get("privilege") if isinstance(item.get("privilege"), dict) else {}
    privilege = {**raw_priv, **privilege_override} if privilege_override else raw_priv

    fee = _optional_int(privilege.get("fee") if privilege.get("fee") is not None else item.get("fee"))
    privilege_st = _optional_int(privilege.get("st"))
    max_br_level = str(
        privilege.get("maxBrLevel")
        or privilege.get("playMaxBrLevel")
        or privilege.get("plLevel")
        or ""
    ).strip().lower()
    is_cloud = bool(force_cloud or privilege.get("cs"))

    st_value = privilege_st if privilege_st is not None else 0
    playable = st_value >= 0 and fee != 4
    if not playable:
        availability = "unavailable"
    elif fee == 1:
        availability = "vip"
    else:
        availability = "available"

    return Song(
        id=song_id,
        title=str(item.get("name") or "未知歌曲"),
        artists=artists,
        artist_ids=[a["id"] for a in artist_items if isinstance(a, dict) and type(a.get("id")) is int and a["id"] > 0],
        album=str(album_data.get("name") or "") if isinstance(album_data, dict) else "",
        duration=duration_ms,
        cover_url=str(album_data.get("picUrl") or "") if isinstance(album_data, dict) else "",
        source_playlist_id=source_playlist_id,
        availability=availability,
        fee=fee,
        privilege_st=privilege_st,
        max_br_level=max_br_level,
        is_cloud=is_cloud,
        source_playlist_name=source_playlist_name,
    )
