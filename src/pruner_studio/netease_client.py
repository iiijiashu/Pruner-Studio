from __future__ import annotations

import json
import base64
import io
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any, Protocol

from .errors import AppError, UpstreamError
from .models import AccountSummary, PlaylistSummary, Song
from .version import __version__


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


class UrllibTransport:
    def __init__(self, base_url: str, timeout: float = 15.0):
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("api_base must be an HTTP(S) URL")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        cookie: str | None = None,
    ) -> TransportResponse:
        values = {key: str(value) for key, value in (params or {}).items()}
        encoded = urllib.parse.urlencode(values).encode("utf-8")
        url = f"{self.base_url}/{path.lstrip('/')}"
        data = None
        if method.upper() == "GET" and encoded:
            url = f"{url}?{encoded.decode('ascii')}"
        elif method.upper() != "GET":
            data = encoded
        headers = {
            "Accept": "application/json",
            "User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Pruner-Studio/{__version__}",
            "Referer": "https://music.163.com/",
            "Origin": "https://music.163.com",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        if cookie:
            headers["Cookie"] = f"os=pc; appver=8.9.70; {cookie}"
        else:
            headers["Cookie"] = "os=pc; appver=8.9.70"
        request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(4_194_304)
                payload = json.loads(raw.decode("utf-8"))
                return TransportResponse(
                    status=response.status,
                    data=payload,
                    set_cookie=_join_set_cookie(response.headers.get_all("Set-Cookie", [])),
                )
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read(1_048_576).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            return TransportResponse(status=exc.code, data=payload)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpstreamError("UPSTREAM_UNAVAILABLE", f"网易云服务不可用：{exc.reason if hasattr(exc, 'reason') else exc}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpstreamError("UPSTREAM_INVALID_RESPONSE", "网易云服务返回了无效数据") from exc


class NeteaseWebTransport(UrllibTransport):
    """Small direct adapter for the web endpoints used by this application.

    It deliberately implements only Pruner Studio's required calls so the packaged
    application does not need a separately installed API gateway.
    """

    def __init__(self, base_url: str, timeout: float = 15.0):
        super().__init__(base_url, timeout)
        self._playlist_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._playlist_lock = threading.RLock()

    def clear_caches(self) -> None:
        with self._playlist_lock:
            self._playlist_cache.clear()

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
            playlist_id = int(values.get("id", 0))
            with self._playlist_lock:
                force_fresh = str(values.get("fresh", "")).lower() in {"1", "true", "yes"}
                if force_fresh:
                    self._playlist_cache.pop(playlist_id, None)
                cached = self._playlist_cache.get(playlist_id)
                if cached and time.monotonic() - cached[0] < 30:
                    all_songs = cached[1]
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
                    track_ids = [item.get("id") for item in playlist.get("trackIds", []) if isinstance(item, dict) and isinstance(item.get("id"), int)]
                    songs_by_id = {item.get("id"): item for item in raw_tracks if isinstance(item, dict) and isinstance(item.get("id"), int)}
                    missing_ids = [song_id for song_id in track_ids if song_id not in songs_by_id]
                    for index in range(0, len(missing_ids), 500):
                        chunk = missing_ids[index : index + 500]
                        details = super().request(
                            "/api/v3/song/detail",
                            {"c": json.dumps([{"id": song_id} for song_id in chunk]), "ids": json.dumps(chunk)},
                            method="POST",
                            cookie=cookie,
                        )
                        detail_payload = _response_mapping(details)
                        if detail_payload.get("code") != 200 or not isinstance(detail_payload.get("songs"), list):
                            raise UpstreamError("TRACKS_INVALID", "网易云未返回完整歌曲详情")
                        songs_by_id.update(
                            {item.get("id"): item for item in detail_payload["songs"] if isinstance(item, dict) and isinstance(item.get("id"), int)}
                        )
                    if track_ids:
                        all_songs = [songs_by_id[song_id] for song_id in track_ids if song_id in songs_by_id]
                        if len(all_songs) != len(track_ids):
                            raise UpstreamError("TRACKS_INCOMPLETE", "网易云返回的歌单歌曲不完整，请稍后重试")
                    else:
                        all_songs = [item for item in raw_tracks if isinstance(item, dict)]
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
        if path == "/lyric":
            return super().request(
                "/api/song/lyric",
                {"os": "osx", "id": values.get("id"), "lv": -1, "kv": -1, "tv": -1},
                cookie=cookie,
            )
        if path == "/song/url/v1":
            interface = UrllibTransport("https://interface.music.163.com", self.timeout)
            return interface.request(
                "/api/song/enhance/player/url/v1",
                {"ids": json.dumps([int(values.get("id", 0))]), "level": values.get("level", "standard"), "encodeType": "mp3"},
                method="POST",
                cookie=cookie,
            )
        if path == "/cloudsearch":
            return super().request(
                "/api/search/get/web",
                {"s": values.get("keywords", ""), "type": values.get("type", 1), "offset": 0, "limit": values.get("limit", 30)},
                cookie=cookie,
            )
        if path == "/likelist":
            return super().request("/api/song/like/get", {"uid": values.get("uid")}, cookie=cookie)
        if path == "/like":
            return super().request(
                "/api/radio/like",
                {"trackId": values.get("id"), "like": "false", "time": 25},
                cookie=cookie,
            )
        if path == "/playlist/create":
            return super().request("/api/playlist/create", values, method="POST", cookie=cookie)
        if path == "/playlist/tracks":
            track_values = [int(item) for item in str(values.get("tracks", "")).split(",") if item.strip().isdigit()]
            playlist_id = int(values.get("pid", 0))
            # Invalidate before attempting the write. A transport failure can happen
            # after NetEase applied the mutation, so any subsequent reconciliation
            # must read remote state instead of trusting the 30-second cache.
            with self._playlist_lock:
                self._playlist_cache.pop(playlist_id, None)
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
        raise UpstreamError("UPSTREAM_ROUTE_UNSUPPORTED", f"不支持的网易云调用：{path}")


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


def _response_mapping(response: TransportResponse) -> dict[str, Any]:
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


class NeteaseClient:
    """Typed client for the narrow NetEase operations used by Pruner Studio."""

    def __init__(self, transport: Transport, cookie: str | None = None):
        self.transport = transport
        self.cookie = cookie

    def set_cookie(self, cookie: str | None) -> None:
        self.cookie = cookie

    def _call(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        accepted_codes: set[int] | None = None,
    ) -> tuple[dict[str, Any], str]:
        response = self.transport.request(path, params, method=method, cookie=self.cookie)
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
            self.cookie = normalize_cookie_header(raw_cookie)
            result["user"] = self.account().to_dict()
        return result

    def account(self) -> AccountSummary:
        if not self.cookie:
            raise AppError("NOT_AUTHENTICATED", "请先扫码登录", 401)
        payload, _ = self._call(
            "/user/account",
            {"timestamp": int(time.time() * 1000)},
            accepted_codes={200, 301},
        )
        if payload.get("code") == 301:
            raise AppError("SESSION_EXPIRED", "登录已失效，请重新扫码", 401)
        profile = payload.get("profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("userId"), int):
            raise UpstreamError("ACCOUNT_INVALID", "网易云返回的账号信息格式无效")
        return AccountSummary(
            user_id=profile["userId"],
            display_name=str(profile.get("nickname") or "网易云用户"),
            avatar_url=str(profile.get("avatarUrl") or ""),
        )

    def playlists(self, account: AccountSummary | None = None) -> list[PlaylistSummary]:
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
            if creator.get("userId") != owner.user_id:
                continue
            playlist_id = item.get("id")
            if not isinstance(playlist_id, int) or playlist_id <= 0:
                continue
            is_liked = item.get("specialType") == 5
            result.append(
                PlaylistSummary(
                    id=playlist_id,
                    name=str(item.get("name") or "未命名歌单"),
                    track_count=max(0, int(item.get("trackCount") or 0)),
                    kind="liked" if is_liked else "self_created",
                    privacy="private" if int(item.get("privacy") or 0) else "public",
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
            tracks.extend(_song_from_api(item, playlist_id) for item in page if isinstance(item, dict))
            if len(page) < limit:
                break
            offset += len(page)
            if offset > 100_000:
                raise UpstreamError("TRACKS_TOO_LARGE", "歌单歌曲数量超出支持范围")
        return tracks

    def lyrics(self, song_id: int) -> dict[str, Any]:
        payload, _ = self._call("/lyric", {"id": song_id})
        lyric = payload.get("lrc", {}).get("lyric", "") if isinstance(payload.get("lrc"), dict) else ""
        translated = payload.get("tlyric", {}).get("lyric", "") if isinstance(payload.get("tlyric"), dict) else ""
        return {"id": song_id, "lyrics": str(lyric), "tlyrics": str(translated)}

    def audio_url(self, song_id: int) -> dict[str, Any] | None:
        payload, _ = self._call("/song/url/v1", {"id": song_id, "level": "exhigh"})
        entries = payload.get("data")
        if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
            return None
        item = entries[0]
        url = item.get("url")
        if item.get("freeTrialInfo") or not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return None
        return {"id": int(item.get("id") or song_id), "url": url, "level": str(item.get("level") or "unknown")}

    def search_songs(self, query: str, limit: int = 30) -> list[Song]:
        payload, _ = self._call("/cloudsearch", {"keywords": query, "type": 1, "limit": limit})
        items = payload.get("result", {}).get("songs", []) if isinstance(payload.get("result"), dict) else []
        return [_song_from_api(item, None) for item in items if isinstance(item, dict)]

    def liked_ids(self, user_id: int) -> set[int]:
        payload, _ = self._call("/likelist", {"uid": user_id})
        ids = payload.get("ids")
        if not isinstance(ids, list):
            raise UpstreamError("LIKED_LIST_INVALID", "喜欢列表格式无效")
        return {value for value in ids if isinstance(value, int) and value > 0}

    def request_unlike(self, song_id: int) -> None:
        self._call("/like", {"id": song_id, "like": "false", "timestamp": int(time.time() * 1000)}, method="POST")

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


def _song_from_api(item: dict[str, Any], source_playlist_id: int | None) -> Song:
    song_id = item.get("id")
    if not isinstance(song_id, int) or song_id <= 0:
        raise UpstreamError("SONG_INVALID", "歌曲数据缺少有效 ID")
    artist_items = item.get("ar") if isinstance(item.get("ar"), list) else item.get("artists", [])
    artists = [str(artist.get("name")) for artist in artist_items if isinstance(artist, dict) and artist.get("name")]
    album_data = item.get("al") if isinstance(item.get("al"), dict) else item.get("album", {})
    duration = item.get("dt", item.get("duration", 0))
    try:
        duration_ms = max(0, int(duration or 0))
    except (TypeError, ValueError):
        duration_ms = 0
    fee = item.get("fee")
    privilege = item.get("privilege") if isinstance(item.get("privilege"), dict) else {}
    playable = privilege.get("st", 0) >= 0 and fee != 4
    return Song(
        id=song_id,
        title=str(item.get("name") or "未知歌曲"),
        artists=artists,
        album=str(album_data.get("name") or "") if isinstance(album_data, dict) else "",
        duration=duration_ms,
        cover_url=str(album_data.get("picUrl") or "") if isinstance(album_data, dict) else "",
        source_playlist_id=source_playlist_id,
        availability="available" if playable else "unavailable",
    )
