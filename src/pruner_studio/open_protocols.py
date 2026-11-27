"""First-class OpenSubsonic/Navidrome & WebDAV music providers + WebDAV AES-256-GCM encrypted sync."""
from __future__ import annotations

import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import ipaddress
import socket
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2
import httpx

from .errors import AppError
from .local_metadata import SUPPORTED_AUDIO_EXTENSIONS, parse_replaygain_db, parse_replaygain_peak
from .netease_client import isolate_cookie_jar
from .db import PLAY_COUNTERS
from .storage import validate_self_hosted_api
from .track_identity import normalize_canonical_track, normalize_isrc, normalize_mbid

_SUBSONIC_API_VERSION = "1.16.1"
_SUBSONIC_CLIENT_NAME = "PrunerStudio"
_SYNC_AAD = b"pruner-webdav-sync-v1"
_SYNC_PBKDF2_ITERATIONS = 200_000
_SYNC_MAX_BYTES = 16 * 1024 * 1024
_PROPFIND_MAX_BYTES = 4 * 1024 * 1024
_COVER_MAX_BYTES = 8 * 1024 * 1024
_UNSAFE_XML_BYTES_RE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_UNSAFE_XML_STR_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_WEBDAV_XML_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:resourcetype/><d:getcontentlength/><d:getcontenttype/><d:getlastmodified/><d:getetag/>"
    "</d:prop></d:propfind>"
).encode("utf-8")


def _safe_xml_root(raw: bytes) -> ET.Element:
    """Parse XML with DTD/ENTITY declarations explicitly rejected across the full bounded payload."""
    if len(raw) > _PROPFIND_MAX_BYTES:
        raise ValueError("XML payload exceeds maximum allowed size")
    if _UNSAFE_XML_BYTES_RE.search(raw):
        raise ValueError("XML DTD/ENTITY declarations are forbidden")
    # Also defend against UTF-16/UTF-32 encoded DTD/ENTITY declarations.
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or raw[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        for enc in ("utf-16", "utf-32"):
            try:
                decoded = raw.decode(enc)
                if _UNSAFE_XML_STR_RE.search(decoded):
                    raise ValueError("XML DTD/ENTITY declarations are forbidden")
                break
            except UnicodeDecodeError:
                continue
    return ET.fromstring(raw)


def _same_origin(base_url: str, target_url: str) -> bool:
    try:
        base, target = httpx.URL(base_url), httpx.URL(target_url)
        return (base.scheme, base.raw_host, base.port) == (target.scheme, target.raw_host, target.port)
    except (httpx.InvalidURL, ValueError):
        return False


def self_hosted_addresses(base_url: str, url: str) -> tuple[str, ...]:
    from .external_sources import is_public_ip, validated_addresses
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return ()
        if not _same_origin(base_url, url):
            return validated_addresses(url) if parsed.scheme == "https" else ()
        try:
            ips = [ipaddress.ip_address(parsed.hostname)]
        except ValueError:
            ips = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)]
    except (OSError, ValueError):
        return ()
    private_ranges = tuple(map(ipaddress.ip_network, ("10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","fc00::/7")))
    if all(is_public_ip(ip) or ip.is_loopback or any(ip in network for network in private_ranges) for ip in ips):
        return tuple(str(ip) for ip in ips)
    return ()


def _self_hosted_request(
    http_client: httpx.Client,
    method: str,
    url: str,
    *,
    base_url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    timeout: float = 8.0,
    max_bytes: int = _SYNC_MAX_BYTES,
) -> httpx.Response:
    validated_base = validate_self_hosted_api(base_url)
    if not validated_base:
        raise AppError("PROTOCOL_NOT_CONFIGURED", "尚未配置服务器地址", 400)
    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36",
        **(headers or {}),
    }
    current_method = method.upper()
    current_url = url
    current_params = params
    current_content = content
    from .external_sources import _pinned_request
    isolate_cookie_jar(http_client)
    for _ in range(4):
        addresses = self_hosted_addresses(validated_base, current_url)
        if not addresses:
            raise AppError("PROTOCOL_ADDRESS_REJECTED", "服务器地址不允许访问", 403)
        if not _same_origin(validated_base, current_url):
            req_headers = {k: v for k,v in req_headers.items() if k.lower() not in
                           {"authorization", "cookie", "x-api-key", "x-resolver-token"}}
        req = _pinned_request(current_method, current_url, addresses, req_headers, timeout,
                              params=current_params, content=current_content)
        with closing(http_client.send(req, auth=None, follow_redirects=False, stream=True)) as resp:
            if resp.status_code in {301, 302, 303, 307, 308} and resp.headers.get("location"):
                next_url = urljoin(current_url, resp.headers["location"])
                if current_method not in {"GET", "HEAD"} and (
                    resp.status_code == 303 or not _same_origin(validated_base, next_url)
                ):
                    raise AppError("PROTOCOL_REDIRECT_REJECTED", "服务器重定向无法安全完成操作", 502)
                current_url = next_url
                current_params = None
                continue
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise AppError("PROTOCOL_RESPONSE_TOO_LARGE", "服务器响应体积超过安全上限", 413)
                chunks.append(chunk)
            return httpx.Response(
                status_code=resp.status_code,
                headers={k:v for k,v in resp.headers.items() if k not in {"content-encoding","content-length"}},
                content=b"".join(chunks),
                request=resp.request,
            )
    raise AppError("PROTOCOL_REDIRECT_LIMIT", "服务器重定向次数过多", 502)


# ---------------------------------------------------------------------------
# First-Class OpenSubsonic / Navidrome Provider
# ---------------------------------------------------------------------------


class OpenSubsonicProvider:
    """First-class OpenSubsonic / Navidrome provider using token+salt MD5 auth."""

    name = "opensubsonic"

    @staticmethod
    def _auth_params(secret: dict[str, Any]) -> tuple[str, dict[str, str]]:
        base_url = validate_self_hosted_api(str(secret.get("api_base") or ""))
        if not base_url:
            raise AppError("OPENSUBSONIC_NOT_CONFIGURED", "尚未配置 OpenSubsonic / Navidrome 服务器地址", 400)
        username = str(secret.get("username") or secret.get("userid") or "").strip()
        password = str(secret.get("password") or "").strip()
        api_key = str(secret.get("api_key") or secret.get("token") or "").strip()
        params: dict[str, str] = {
            "v": _SUBSONIC_API_VERSION,
            "c": _SUBSONIC_CLIENT_NAME,
            "f": "json",
        }
        if username:
            params["u"] = username
        if password:
            salt = secrets.token_hex(8)
            token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
            params["t"] = token
            params["s"] = salt
        elif api_key:
            params["apiKey"] = api_key
        else:
            raise AppError("OPENSUBSONIC_CREDENTIALS_MISSING", "缺少 OpenSubsonic 密码或 API Key", 400)
        return base_url, params

    def _call_json(
        self,
        endpoint: str,
        secret: dict[str, Any],
        http_client: httpx.Client,
        *,
        extra_params: dict[str, Any] | None = None,
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        base_url, params = self._auth_params(secret)
        if extra_params:
            for k, v in extra_params.items():
                if v is not None:
                    params[str(k)] = str(v)
        url = f"{base_url}/rest/{endpoint.lstrip('/')}"
        try:
            resp = _self_hosted_request(
                http_client,
                "GET",
                url,
                base_url=base_url,
                params=params,
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise AppError("OPENSUBSONIC_UNREACHABLE", f"无法连接 OpenSubsonic 服务器：{exc}", 502) from exc
        if resp.status_code != 200:
            raise AppError("OPENSUBSONIC_HTTP_ERROR", f"OpenSubsonic 返回 HTTP {resp.status_code}", 502)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise AppError("OPENSUBSONIC_INVALID_JSON", "OpenSubsonic 返回了无效 JSON", 502) from exc
        body = payload.get("subsonic-response") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            raise AppError("OPENSUBSONIC_INVALID_PAYLOAD", "OpenSubsonic 响应缺少 subsonic-response", 502)
        if body.get("status") != "ok":
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            msg = str(err.get("message") or "认证失败或接口拒绝访问")
            code = int(err.get("code") or 0)
            status_code = 401 if code in {40, 41, 42, 43, 44, 50} else 502
            raise AppError("OPENSUBSONIC_API_ERROR", f"OpenSubsonic 错误：{msg}", status_code)
        return body

    @staticmethod
    def _song_to_canonical(raw: dict[str, Any]) -> dict[str, Any]:
        sid = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        artist = str(raw.get("artist") or "").strip()
        artists_raw = raw.get("artists")
        if isinstance(artists_raw, list) and artists_raw:
            artists = [str(a.get("name") if isinstance(a, dict) else a).strip() for a in artists_raw if a]
            artists = [a for a in artists if a]
        elif artist:
            artists = [artist]
        else:
            artists = []
        duration_sec = int(raw.get("duration") or 0)
        duration_ms = max(0, duration_sec * 1000)
        bitrate_kbps = int(raw.get("bitRate") or 0)
        bitrate = bitrate_kbps * 1000 if 0 < bitrate_kbps <= 10000 else bitrate_kbps
        sample_rate = int(raw.get("samplingRate") or 0)
        suffix = str(raw.get("suffix") or "").strip().lower()
        cover_art = str(raw.get("coverArt") or "").strip()
        rg = raw.get("replayGain") if isinstance(raw.get("replayGain"), dict) else {}
        isrcs = raw.get("isrc")
        isrc_val = isrcs[0] if isinstance(isrcs, list) and isrcs else (isrcs if isinstance(isrcs, str) else "")
        return normalize_canonical_track(
            {
                "id": 0,
                "provider": "opensubsonic",
                "source": "opensubsonic",
                "source_provider": "opensubsonic",
                "external_id": sid,
                "track_key": f"opensubsonic:{sid}" if sid else "",
                "title": title,
                "artists": artists,
                "album": str(raw.get("album") or "").strip(),
                "duration": duration_ms,
                "duration_ms": duration_ms,
                "format": suffix,
                "bitrate": bitrate,
                "sample_rate": sample_rate,
                "size": int(raw.get("size") or 0),
                "cover_art_id": cover_art,
                "cover_url": f"/api/opensubsonic/cover/{quote(cover_art, safe='')}" if cover_art else "",
                "isrc": normalize_isrc(isrc_val),
                "mbid": normalize_mbid(raw.get("musicBrainzId") or ""),
                "replaygain_track_gain_db": parse_replaygain_db(rg.get("trackGain")),
                "replaygain_track_peak": parse_replaygain_peak(rg.get("trackPeak")),
                "replaygain_album_gain_db": parse_replaygain_db(rg.get("albumGain")),
                "replaygain_album_peak": parse_replaygain_peak(rg.get("albumPeak")),
                "available_channels": ["opensubsonic"],
            }
        )

    def ping(self, secret: dict[str, Any], http_client: httpx.Client, *, timeout: float = 6.0) -> dict[str, Any]:
        started = time.monotonic()
        body = self._call_json("ping.view", secret, http_client, timeout=timeout)
        latency_ms = int((time.monotonic() - started) * 1000)
        return {
            "channel": self.name,
            "ok": True,
            "latency_ms": latency_ms,
            "status": "connected",
            "capability": "custom_endpoint",
            "server_type": str(body.get("type") or "opensubsonic"),
            "server_version": str(body.get("serverVersion") or body.get("version") or ""),
            "open_subsonic": bool(body.get("openSubsonic", True)),
            "message": f"已连接 {body.get('type') or 'OpenSubsonic'} ({body.get('serverVersion') or body.get('version') or 'v1'})",
            "max_quality_hint": "flac",
        }

    def search_tracks(
        self,
        keyword: str,
        secret: dict[str, Any],
        http_client: httpx.Client,
        *,
        limit: int = 30,
        offset: int = 0,
        timeout: float = 8.0,
    ) -> tuple[list[dict[str, Any]], bool]:
        per_page = max(1, min(100, int(limit or 30)))
        off = max(0, int(offset or 0))
        body = self._call_json(
            "search3.view",
            secret,
            http_client,
            extra_params={
                "query": str(keyword or "").strip(),
                "songCount": per_page,
                "songOffset": off,
                "artistCount": 0,
                "albumCount": 0,
            },
            timeout=timeout,
        )
        sr = body.get("searchResult3") if isinstance(body.get("searchResult3"), dict) else {}
        songs = sr.get("song") if isinstance(sr.get("song"), list) else []
        tracks = [self._song_to_canonical(s) for s in songs if isinstance(s, dict) and s.get("id")]
        return tracks, len(songs) >= per_page

    def list_playlists(self, secret: dict[str, Any], http_client: httpx.Client, *, timeout: float = 8.0) -> list[dict[str, Any]]:
        body = self._call_json("getPlaylists.view", secret, http_client, timeout=timeout)
        pl_wrap = body.get("playlists") if isinstance(body.get("playlists"), dict) else {}
        items = pl_wrap.get("playlist") if isinstance(pl_wrap.get("playlist"), list) else []
        out = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            cover_art = str(item.get("coverArt") or "").strip()
            out.append(
                {
                    "id": f"opensubsonic:{item['id']}",
                    "external_id": str(item["id"]),
                    "name": str(item.get("name") or "未命名歌单"),
                    "track_count": int(item.get("songCount") or 0),
                    "duration": int(item.get("duration") or 0) * 1000,
                    "kind": "opensubsonic",
                    "cover_url": f"/api/opensubsonic/cover/{quote(cover_art, safe='')}" if cover_art else "",
                }
            )
        return out

    def get_playlist_tracks(
        self,
        playlist_id: str,
        secret: dict[str, Any],
        http_client: httpx.Client,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        ext_id = str(playlist_id or "").removeprefix("opensubsonic:")
        body = self._call_json("getPlaylist.view", secret, http_client, extra_params={"id": ext_id}, timeout=timeout)
        pl = body.get("playlist") if isinstance(body.get("playlist"), dict) else {}
        entries = pl.get("entry") if isinstance(pl.get("entry"), list) else []
        tracks = [self._song_to_canonical(s) for s in entries if isinstance(s, dict) and s.get("id")]
        return {
            "id": f"opensubsonic:{ext_id}",
            "name": str(pl.get("name") or ext_id),
            "kind": "opensubsonic",
            "track_count": len(tracks),
            "songs": tracks,
        }

    def get_song(self, song_id: str, secret: dict[str, Any], http_client: httpx.Client, *, timeout: float = 8.0) -> dict[str, Any] | None:
        ext_id = str(song_id or "").removeprefix("opensubsonic:")
        if not ext_id:
            return None
        body = self._call_json("getSong.view", secret, http_client, extra_params={"id": ext_id}, timeout=timeout)
        raw = body.get("song")
        return self._song_to_canonical(raw) if isinstance(raw, dict) and raw.get("id") else None

    def build_stream_url(self, song_id: str, secret: dict[str, Any]) -> tuple[str, dict[str, str]]:
        ext_id = str(song_id or "").removeprefix("opensubsonic:")
        if not ext_id:
            raise AppError("OPENSUBSONIC_SONG_INVALID", "缺少 OpenSubsonic 曲目 ID", 400)
        base_url, params = self._auth_params(secret)
        params["id"] = ext_id
        query = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in params.items())
        return f"{base_url}/rest/stream.view?{query}", {}

    def fetch_cover(
        self,
        cover_art_id: str,
        secret: dict[str, Any],
        http_client: httpx.Client,
        *,
        size: int = 384,
        timeout: float = 8.0,
    ) -> tuple[bytes, str]:
        base_url, params = self._auth_params(secret)
        params["id"] = str(cover_art_id or "").strip()
        params["size"] = str(max(64, min(1024, int(size or 384))))
        url = f"{base_url}/rest/getCoverArt.view"
        resp = _self_hosted_request(
            http_client, "GET", url, base_url=base_url, params=params, timeout=timeout, max_bytes=_COVER_MAX_BYTES
        )
        if resp.status_code != 200 or not resp.content:
            raise AppError("OPENSUBSONIC_COVER_FAILED", "无法获取 OpenSubsonic 封面", 502)
        ct = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip().lower()
        if not ct.startswith("image/"):
            raise AppError("OPENSUBSONIC_COVER_INVALID", "OpenSubsonic 封面格式无效", 502)
        return resp.content, ct

    def fetch_lyrics(
        self,
        secret: dict[str, Any],
        http_client: httpx.Client,
        *,
        song_id: str = "",
        artist: str = "",
        title: str = "",
        timeout: float = 6.0,
    ) -> dict[str, Any] | None:
        ext_id = str(song_id or "").removeprefix("opensubsonic:").strip()
        if ext_id:
            try:
                body = self._call_json("getLyricsBySongId.view", secret, http_client, extra_params={"id": ext_id}, timeout=timeout)
                sl = body.get("lyricsList") if isinstance(body.get("lyricsList"), dict) else {}
                structured = sl.get("structuredLyrics") if isinstance(sl.get("structuredLyrics"), list) else []
                for item in structured:
                    if not isinstance(item, dict):
                        continue
                    lines = item.get("line") if isinstance(item.get("line"), list) else []
                    lrc_lines = []
                    for ln in lines:
                        if not isinstance(ln, dict):
                            continue
                        val = str(ln.get("value") or "").strip()
                        if not val:
                            continue
                        start = ln.get("start")
                        if isinstance(start, (int, float)):
                            sec = max(0.0, float(start) / 1000.0)
                            mm, ss = divmod(sec, 60.0)
                            lrc_lines.append(f"[{int(mm):02d}:{ss:05.2f}]{val}")
                        else:
                            lrc_lines.append(val)
                    if lrc_lines:
                        return {"lrc": "\n".join(lrc_lines), "tlyric": "", "romalrc": "", "yrc": "", "source": "opensubsonic"}
            except AppError:
                pass
        if artist or title:
            try:
                body = self._call_json(
                    "getLyrics.view",
                    secret,
                    http_client,
                    extra_params={"artist": artist, "title": title},
                    timeout=timeout,
                )
                ly = body.get("lyrics") if isinstance(body.get("lyrics"), dict) else {}
                text = str(ly.get("value") or "").strip()
                if text:
                    return {"lrc": text, "tlyric": "", "romalrc": "", "yrc": "", "source": "opensubsonic"}
            except AppError:
                pass
        return None


# ---------------------------------------------------------------------------
# First-Class WebDAV Music Provider
# ---------------------------------------------------------------------------


def _webdav_auth_headers(secret: dict[str, Any]) -> tuple[str, dict[str, str]]:
    base_url = validate_self_hosted_api(str(secret.get("api_base") or ""))
    if not base_url:
        raise AppError("WEBDAV_NOT_CONFIGURED", "尚未配置 WebDAV 服务器地址", 400)
    username = str(secret.get("username") or secret.get("userid") or "").strip()
    password = str(secret.get("password") or "").strip()
    token = str(secret.get("token") or secret.get("api_key") or "").strip()
    headers: dict[str, str] = {}
    if username or password:
        raw = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {raw}"
    elif token:
        headers["Authorization"] = f"Bearer {token}"
    return base_url, headers


def _join_webdav_url(base_url: str, sub_path: str = "") -> str:
    clean_base = base_url.rstrip("/")
    clean_sub = "/" + "/".join(quote(seg, safe="") for seg in str(sub_path or "").strip("/").split("/") if seg)
    if clean_sub == "/":
        return clean_base + "/"
    return clean_base + clean_sub


class WebDAVProvider:
    """First-class WebDAV audio library scanner and Range-streaming provider."""

    name = "webdav"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._indexed_tracks: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}

    def ping(self, secret: dict[str, Any], http_client: httpx.Client, *, timeout: float = 6.0) -> dict[str, Any]:
        base_url, headers = _webdav_auth_headers(secret)
        target = _join_webdav_url(base_url, str(secret.get("music_path") or ""))
        started = time.monotonic()
        try:
            resp = _self_hosted_request(
                http_client,
                "PROPFIND",
                target,
                base_url=base_url,
                headers={**headers, "Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
                content=_WEBDAV_XML_BODY,
                timeout=timeout,
            )
            if resp.status_code == 405:
                resp = _self_hosted_request(http_client, "OPTIONS", target, base_url=base_url, headers=headers, timeout=timeout)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise AppError("WEBDAV_UNREACHABLE", f"无法连接 WebDAV 服务器：{exc}", 502) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code in {401, 403}:
            raise AppError("WEBDAV_AUTH_FAILED", "WebDAV 认证失败，请检查用户名与密码", 401)
        if resp.status_code not in {200, 204, 207}:
            raise AppError("WEBDAV_HTTP_ERROR", f"WebDAV 返回 HTTP {resp.status_code}", 502)
        return {
            "channel": self.name,
            "ok": True,
            "latency_ms": latency_ms,
            "status": "connected",
            "capability": "custom_endpoint",
            "message": f"WebDAV 已连通 (HTTP {resp.status_code})",
            "max_quality_hint": "flac",
        }

    def scan_library(
        self,
        secret: dict[str, Any],
        http_client: httpx.Client,
        *,
        max_depth: int = 3,
        max_files: int = 2000,
        timeout: float = 10.0,
    ) -> list[dict[str, Any]]:
        base_url, headers = _webdav_auth_headers(secret)
        root_url = _join_webdav_url(base_url, str(secret.get("music_path") or ""))
        queue: list[tuple[str, int]] = [(root_url, 0)]
        visited: set[str] = set()
        tracks: list[dict[str, Any]] = []

        while queue and len(tracks) < max_files:
            current_url, depth = queue.pop(0)
            norm_path = unquote(urlsplit(current_url).path).rstrip("/")
            if norm_path in visited:
                continue
            visited.add(norm_path)
            resp = _self_hosted_request(
                http_client,
                "PROPFIND",
                current_url,
                base_url=base_url,
                headers={**headers, "Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
                content=_WEBDAV_XML_BODY,
                timeout=timeout,
                max_bytes=_PROPFIND_MAX_BYTES,
            )
            if resp.status_code not in {200, 207}:
                if depth == 0:
                    raise AppError("WEBDAV_SCAN_FAILED", f"WebDAV 目录枚举失败 (HTTP {resp.status_code})", 502)
                continue
            try:
                root_el = _safe_xml_root(resp.content)
            except Exception as exc:
                if depth == 0:
                    raise AppError("WEBDAV_XML_INVALID", f"WebDAV 返回无效 XML：{exc}", 502) from exc
                continue

            for response_el in root_el.iter():
                if not response_el.tag.endswith("}response") and response_el.tag != "response":
                    continue
                href = ""
                is_collection = False
                size = 0
                for child in response_el.iter():
                    tag = child.tag.rsplit("}", 1)[-1].lower()
                    if tag == "href" and child.text and not href:
                        href = child.text.strip()
                    elif tag == "collection":
                        is_collection = True
                    elif tag == "getcontentlength" and child.text and child.text.strip().isdigit():
                        size = int(child.text.strip())
                if not href:
                    continue
                item_url = urljoin(current_url, href)
                if not _same_origin(base_url, item_url):
                    continue
                item_norm_path = unquote(urlsplit(item_url).path).rstrip("/")
                if item_norm_path == norm_path:
                    continue
                if is_collection:
                    if depth < max_depth and item_norm_path not in visited:
                        queue.append((item_url if item_url.endswith("/") else item_url + "/", depth + 1))
                    continue
                parsed_path = unquote(urlsplit(item_url).path)
                p = Path(parsed_path)
                ext = p.suffix.lower()
                if ext not in SUPPORTED_AUDIO_EXTENSIONS:
                    continue
                stem = p.stem
                if " - " in stem:
                    artist, title = [part.strip() for part in stem.split(" - ", 1)]
                else:
                    artist, title = p.parent.name if p.parent and p.parent.name else "", stem
                wid = hashlib.sha256(parsed_path.encode("utf-8")).hexdigest()[:24]
                tracks.append(
                    normalize_canonical_track(
                        {
                            "id": 0,
                            "provider": "webdav",
                            "source": "webdav",
                            "source_provider": "webdav",
                            "external_id": wid,
                            "track_key": f"webdav:{wid}",
                            "title": title or stem,
                            "artists": [artist] if artist else [],
                            "album": p.parent.name if p.parent else "",
                            "duration": 0,
                            "format": ext.lstrip("."),
                            "size": size,
                            "webdav_url": item_url,
                            "webdav_path": parsed_path,
                            "available_channels": ["webdav"],
                        }
                    )
                )
                if len(tracks) >= max_files:
                    break

        with self._lock:
            self._indexed_tracks = tracks
            self._by_id = {t["external_id"]: t for t in tracks}
        return list(tracks)

    def seed_tracks(self, tracks: list[dict[str, Any]]) -> None:
        """Restore persisted WebDAV track metadata into the in-memory lookup table."""
        with self._lock:
            for item in tracks:
                if not isinstance(item, dict):
                    continue
                ext = str(item.get("external_id") or "").strip()
                if ext and item.get("webdav_url"):
                    self._by_id[ext] = dict(item)
            self._indexed_tracks = list(self._by_id.values())

    def list_tracks(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._indexed_tracks)

    def get_track(self, external_id: str) -> dict[str, Any] | None:
        ext = str(external_id or "").removeprefix("webdav:").strip()
        with self._lock:
            t = self._by_id.get(ext)
            return dict(t) if t else None

    def search_tracks(self, keyword: str, *, limit: int = 30, offset: int = 0) -> tuple[list[dict[str, Any]], bool]:
        q = str(keyword or "").strip().lower()
        with self._lock:
            pool = list(self._indexed_tracks)
        if q:
            matched = [
                t
                for t in pool
                if q in str(t.get("title") or "").lower()
                or q in str(t.get("artist") or "").lower()
                or q in str(t.get("album") or "").lower()
            ]
        else:
            matched = pool
        page = matched[offset : offset + limit]
        return page, (offset + len(page)) < len(matched)

    def build_stream_request(
        self,
        external_id: str,
        secret: dict[str, Any],
        *,
        fallback_track: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, str]]:
        track = self.get_track(external_id) or fallback_track
        if not track or not track.get("webdav_url"):
            raise AppError("WEBDAV_TRACK_NOT_FOUND", "未在 WebDAV 曲库索引中找到该曲目，请先扫描 WebDAV 曲库", 404)
        base_url, headers = _webdav_auth_headers(secret)
        target_url = str(track["webdav_url"])
        if not _same_origin(base_url, target_url):
            raise AppError("WEBDAV_ORIGIN_MISMATCH", "WebDAV 曲目地址与当前配置服务器不一致", 403)
        return target_url, headers


# ---------------------------------------------------------------------------
# Decentralized Client-Side Encrypted Incremental Sync over WebDAV (AES-256-GCM)
# ---------------------------------------------------------------------------


def encrypt_sync_envelope(payload: dict[str, Any], passphrase: str, *, iterations: int = _SYNC_PBKDF2_ITERATIONS) -> dict[str, Any]:
    """Encrypt a JSON payload client-side using PBKDF2-HMAC-SHA256 + AES-256-GCM."""
    if not isinstance(passphrase, str) or len(passphrase) < 6:
        raise AppError("SYNC_PASSPHRASE_WEAK", "端到端同步口令长度至少为 6 位", 400)
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(plaintext) > (_SYNC_MAX_BYTES - 1024) * 3 // 4:
        raise AppError("SYNC_PAYLOAD_TOO_LARGE", "同步数据超过 16 MiB 上限", 413)
    salt = secrets.token_bytes(16)
    key = PBKDF2(passphrase.encode("utf-8"), salt, dkLen=32, count=iterations, hmac_hash_module=SHA256)
    nonce = secrets.token_bytes(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(_SYNC_AAD)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return {
        "v": 1,
        "alg": "PBKDF2-HMAC-SHA256+AES-256-GCM",
        "iter": iterations,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "aad": _SYNC_AAD.decode("ascii"),
    }


def decrypt_sync_envelope(envelope: dict[str, Any], passphrase: str) -> dict[str, Any]:
    """Decrypt and verify an AES-256-GCM WebDAV sync envelope."""
    if not isinstance(envelope, dict) or envelope.get("v") != 1:
        raise AppError("SYNC_ENVELOPE_INVALID", "加密同步信封格式无效", 400)
    if not isinstance(passphrase, str) or not passphrase:
        raise AppError("SYNC_PASSPHRASE_REQUIRED", "需要端到端同步口令", 400)
    try:
        iterations = int(envelope.get("iter") or _SYNC_PBKDF2_ITERATIONS)
        if not 10_000 <= iterations <= 2_000_000:
            raise ValueError("Invalid PBKDF2 iteration count")
        salt = base64.b64decode(str(envelope["salt"]), validate=True)
        nonce = base64.b64decode(str(envelope["nonce"]), validate=True)
        tag = base64.b64decode(str(envelope["tag"]), validate=True)
        ciphertext = base64.b64decode(str(envelope["ciphertext"]), validate=True)
        if len(salt) < 16 or len(nonce) != 12 or len(tag) != 16:
            raise ValueError("Invalid cryptographic parameter lengths")
        key = PBKDF2(passphrase.encode("utf-8"), salt, dkLen=32, count=iterations, hmac_hash_module=SHA256)
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        cipher.update(_SYNC_AAD)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        decoded = json.loads(plaintext.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("Decoded sync payload is not an object")
        return decoded
    except AppError:
        raise
    except Exception as exc:
        raise AppError("SYNC_DECRYPT_FAILED", "端到端同步解密失败：口令错误或远端数据被篡改", 400) from exc


class WebDAVEncryptedSync:
    """Decentralized E2EE incremental event-log & snapshot sync over WebDAV."""

    def __init__(self, *, iterations: int = _SYNC_PBKDF2_ITERATIONS):
        self.iterations = iterations

    @staticmethod
    def _sync_root(secret: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
        base_url, headers = _webdav_auth_headers(secret)
        sync_sub = str(secret.get("sync_path") or "/pruner-sync").strip() or "/pruner-sync"
        return base_url, _join_webdav_url(base_url, sync_sub), headers

    def _ensure_dir(self, base_url: str, dir_url: str, headers: dict[str, str], http_client: httpx.Client) -> None:
        base_path = urlsplit(base_url).path.rstrip("/")
        target_path = urlsplit(dir_url).path.rstrip("/")
        rel = target_path[len(base_path):].strip("/") if target_path.startswith(base_path) else target_path.strip("/")
        segments = [seg for seg in rel.split("/") if seg]
        accum = ""
        for seg in segments:
            accum = f"{accum}/{seg}"
            seg_url = _join_webdav_url(base_url, accum)
            resp = _self_hosted_request(http_client, "MKCOL", seg_url, base_url=base_url, headers=headers, timeout=8.0)
            if resp.status_code not in {200, 201, 204, 301, 405}:
                raise AppError("WEBDAV_MKCOL_FAILED", f"无法创建 WebDAV 同步目录 (HTTP {resp.status_code})", 502)

    def _get_json(self, base_url: str, url: str, headers: dict[str, str], http_client: httpx.Client) -> tuple[dict[str, Any] | None, str]:
        resp = _self_hosted_request(http_client, "GET", url, base_url=base_url, headers=headers, timeout=10.0, max_bytes=_SYNC_MAX_BYTES)
        if resp.status_code == 404:
            return None, ""
        if resp.status_code != 200:
            raise AppError("WEBDAV_GET_FAILED", f"读取 WebDAV 同步文件失败 (HTTP {resp.status_code})", 502)
        if len(resp.content) > _SYNC_MAX_BYTES:
            raise AppError("SYNC_PAYLOAD_TOO_LARGE", "远端同步文件过大", 413)
        try:
            parsed = json.loads(resp.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError("SYNC_ENVELOPE_INVALID", "远端同步文件不是有效的 JSON", 502) from exc
        if not isinstance(parsed, dict):
            raise AppError("SYNC_ENVELOPE_INVALID", "远端同步文件结构无效", 502)
        return parsed, str(resp.headers.get("etag") or "")

    def _put_json(
        self,
        base_url: str,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        http_client: httpx.Client,
        *,
        if_match: str = "",
        create_only: bool = False,
    ) -> None:
        put_headers = {**headers, "Content-Type": "application/json; charset=utf-8"}
        if if_match:
            put_headers["If-Match"] = if_match
        if create_only:
            put_headers["If-None-Match"] = "*"
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _SYNC_MAX_BYTES:
            raise AppError("SYNC_PAYLOAD_TOO_LARGE", "同步信封超过大小限制", 413)
        resp = _self_hosted_request(
            http_client,
            "PUT",
            url,
            base_url=base_url,
            headers=put_headers,
            content=encoded,
            timeout=12.0,
        )
        if resp.status_code == 412:
            raise AppError("WEBDAV_SYNC_CONFLICT", "远端同步清单已被其他设备更新，请先拉取合并后再推送", 409)
        if resp.status_code not in {200, 201, 204}:
            raise AppError("WEBDAV_PUT_FAILED", f"写入 WebDAV 同步文件失败 (HTTP {resp.status_code})", 502)

    def _scope(self, secret, aid):
        base, root, headers = self._sync_root(secret)
        scope = hashlib.sha256(aid.encode()).hexdigest()
        return base, root.rstrip("/") + "/" + scope, headers

    def _manifest(self, base, root, headers, client, password, aid):
        raw, etag = self._get_json(base, root + "/manifest.enc.json", headers, client)
        if raw is None:
            return None, ""
        manifest = decrypt_sync_envelope(raw, password)
        if manifest.get("account_id") != aid:
            raise AppError("SYNC_ACCOUNT_MISMATCH", "同步数据属于其他账号", 409)
        return manifest, etag

    def _load_committed(self, base, root, headers, client, password, aid, manifest, since):
        if not manifest:
            return []
        head = int(manifest["seq"])
        snap = int(manifest["snapshot_seq"])
        if not 1 <= snap <= head:
            raise AppError("SYNC_SEQUENCE_GAP", "同步快照序号无效", 503)
        entries = []
        if since < snap:
            entries.append((snap, manifest.get("snapshot"), "snapshot"))
            since = snap
        next_seq = since + 1
        for ref in manifest["events"]:
            seq = int(ref["seq"])
            if seq <= since:
                continue
            if seq != next_seq:
                raise AppError("SYNC_SEQUENCE_GAP", "远端同步序列不连续", 503)
            entries.append((seq, ref.get("file"), "event"))
            next_seq += 1
        if next_seq != head + 1:
            raise AppError("SYNC_SEQUENCE_GAP", "远端缺少同步事件", 503)
        result = []
        for seq, name, kind in entries:
            if not isinstance(name,str) or not re.fullmatch(r"[a-z0-9-]+\.enc\.json", name):
                raise AppError("SYNC_OBJECT_INVALID", "同步对象名无效", 503)
            raw, _ = self._get_json(base, root + "/" + name, headers, client)
            if raw is None:
                raise AppError("SYNC_OBJECT_MISSING", "远端同步对象尚不可用", 503)
            obj = decrypt_sync_envelope(raw,password)
            if obj.get("account_id") != aid:
                raise AppError("SYNC_ACCOUNT_MISMATCH", "同步对象属于其他账号", 409)
            if obj.get("seq") != seq or obj.get("kind") != kind:
                raise AppError("SYNC_SEQUENCE_GAP", "远端同步对象序号不匹配", 503)
            result.append(obj)
        return result

    @staticmethod
    def _collect(aid, db, rules_service):
        return {"play_history": db.list_sync_play_history(aid), "feedback": db.list_feedback(aid,limit=2000),
                "favorites": db.list_canonical_favorites(aid, limit=None),
                "hybrid_playlists": db.list_hybrid_playlists(aid,include_tracks=True),
                "deletions": db.list_sync_deletions(aid), "rules": rules_service.get() if rules_service else [],
                "playback_session": db.get_playback_session(aid)}

    @staticmethod
    def _merge(events):
        history, feedback, favorites, playlists, members, deletions, rules = {}, {}, {}, {}, {}, {}, {}
        session = None
        def latest(pool, key, row, field):
            if key not in pool or row[field] >= pool[key][field]:
                pool[key] = row

        for event in events:
            for row in event["play_history"]:
                key = row["track_key"]
                previous = history.get(key, row)
                metadata = row if row["last_played_at"] >= previous["last_played_at"] else previous
                components = dict(previous.get("play_components", {}))
                incoming = row.get("play_components", {"legacy": {field: row[field] for field in PLAY_COUNTERS}})
                for device, counters in incoming.items():
                    prior = components.get(device, {})
                    components[device] = {field: max(prior.get(field, 0), counters[field]) for field in PLAY_COUNTERS}
                totals = {field: sum(counters[field] for counters in components.values()) for field in PLAY_COUNTERS}
                history[key] = {**metadata, **totals, "play_components": components}
            for row in event["feedback"]:
                key = row.get("client_event_id") or "webdav-fb:" + hashlib.sha256(json.dumps(
                    [row.get(k) for k in ("track_key","action","created_at","id")]).encode()).hexdigest()
                feedback[key] = {**row,"client_event_id":key}
            for row in event["favorites"]:
                latest(favorites,row["track_key"],row,"added_at")
            for row in event["hybrid_playlists"]:
                pid = row["id"]
                latest(playlists,pid,row,"updated_at")
                for song in row["songs"]:
                    latest(members,(pid,song["track_key"]),song,"added_at")
            for row in event.get("deletions",[]):
                latest(deletions,(row["kind"],row["parent"],row["item"]),row,"deleted_at")
            for rule in event["rules"]:
                rules[rule["id"]] = rule
            incoming = event.get("playback_session")
            if incoming and (not session or incoming["updated_at"] > session["updated_at"]):
                session = incoming

        def survives(kind, parent, key, timestamp):
            removal = deletions.get((kind,parent,key))
            return not removal or timestamp > removal["deleted_at"]

        output_pl = {}
        for pid,row in playlists.items():
            if survives("playlist","",pid,row["created_at"]):
                output_pl[pid] = {**row,"songs":[]}
        for (pid,key),row in members.items():
            if pid in output_pl and survives("member",pid,key,row["added_at"]) and survives("playlist","",pid,row["added_at"]):
                output_pl[pid]["songs"].append(row)
        return {"play_history":list(history.values()), "feedback":list(feedback.values()),
                "favorites":[row for key,row in favorites.items() if survives("favorite","",key,row["added_at"])],
                "hybrid_playlists":list(output_pl.values()),"rules":list(rules.values()),
                "deletions":list(deletions.values()),"playback_session":session}

    def push_incremental(self, secret, passphrase, *, account_id, db, rules_service, device_id, http_client):
        base,root,headers = self._scope(secret,account_id)
        self._ensure_dir(base,root,headers,http_client)
        manifest,etag = self._manifest(base,root,headers,http_client,passphrase,account_id)
        if manifest and not re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', etag):
            raise AppError("WEBDAV_ETAG_REQUIRED","远端未提供可用于并发更新的 ETag",409)
        seq = manifest["seq"] + 1 if manifest else 1
        with db._lock:
            local = self._collect(account_id,db,rules_service)
        def upload(kind, state):
            name = f"{kind}-{seq}-{secrets.token_hex(16)}.enc.json"
            obj = {**state,"kind":kind,"seq":seq,"account_id":account_id,"device_id":device_id}
            self._put_json(base,root+"/"+name,encrypt_sync_envelope(obj,passphrase,iterations=self.iterations),
                           headers,http_client,create_only=True)
            return name

        snap = manifest["snapshot_seq"] if manifest else 0
        if not manifest or seq - snap >= 10:
            committed = self._load_committed(base,root,headers,http_client,passphrase,account_id,manifest,0)
            snapshot = upload("snapshot",self._merge([*committed,local]))
            snap,events,event_file = seq,[],snapshot
        else:
            snapshot = manifest["snapshot"]
            event_file = upload("event",local)
            events = [*manifest["events"],{"seq":seq,"file":event_file}]
        new = {"version":2,"account_id":account_id,"seq":seq,"events":events,"snapshot":snapshot,"snapshot_seq":snap}
        self._put_json(base,root+"/manifest.enc.json",encrypt_sync_envelope(new,passphrase,iterations=self.iterations),
                       headers,http_client,if_match=etag,create_only=manifest is None)
        return {"direction":"push","seq":seq,"snapshot_seq":snap,"event_file":event_file,
                "counts":{k:len(v) for k,v in local.items() if isinstance(v,list)}}

    def pull_incremental(self, secret, passphrase, *, account_id, db, rules_service, since_seq=0, http_client):
        base,root,headers = self._scope(secret,account_id)
        manifest,_ = self._manifest(base,root,headers,http_client,passphrase,account_id)
        head = manifest["seq"] if manifest else 0
        if head < since_seq:
            raise AppError("SYNC_SEQUENCE_GAP","远端序号早于本机进度",409)
        events = self._load_committed(base,root,headers,http_client,passphrase,account_id,manifest,since_seq)
        if not events:
            return {"direction":"pull","seq":head,"applied_events":0}
        with db._lock:
            local = self._collect(account_id,db,rules_service)
            merged = self._merge([local,*events])
            validated_rules = rules_service.validate(merged["rules"]) if rules_service else []
            session = merged["playback_session"]
            restore = bool(session and session != local["playback_session"])
            db.apply_sync_state(account_id,merged,restore)
            if rules_service and validated_rules != local["rules"]:
                rules_service.put(validated_rules)
        return {"direction":"pull","seq":head,"applied_events":len(events),"session_restored":restore,
                "merged_favorites":len(merged["favorites"]),"merged_playlists":len(merged["hybrid_playlists"]),
                "merged_rules":len(validated_rules)}
