from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
import html
from contextlib import closing
from copy import copy
import ipaddress
import socket
import json
import mimetypes
import os
import re
import struct
import threading
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from .channel_auth import channel_capability
from .local_metadata import LocalCoverCache, parse_audio_metadata
from .models import Song
from .netease_client import NoOpCookies, isolate_cookie_jar
from .open_protocols import OpenSubsonicProvider, WebDAVProvider
from .resolver_v2 import ChannelHealthTracker, fetch_resolver_manifest

_EXTERNAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_EXTERNAL_PROVIDERS = ("kugou_lite", "migu", "qq", "kuwo", "bilibili", "gdstudio")
ALL_EXTERNAL_CHANNELS = ("kugou_lite", "migu", "qq", "kuwo", "bilibili", "gdstudio", "custom", "opensubsonic", "webdav")
GDSTUDIO_SUB_SOURCES = ("kuwo", "kugou", "migu", "joox", "bilibili", "netease")
SUPPORTED_LOCAL_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".opus"})
_LOCAL_FORMAT_PRIORITY = {"flac": 6, "wav": 5, "opus": 4, "m4a": 3, "aac": 3, "ogg": 2, "mp3": 1}
_MAX_LOCAL_FILES = 50_000
_MAX_PROBE_REDIRECTS = 3
_MULTI_SEARCH_DEDUP_TOLERANCE_MS = 3_000

_SEARCH_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="pruner-search")
atexit.register(_SEARCH_EXECUTOR.shutdown, False)

_CHANNEL_TRUSTED_HOST_SUFFIXES: dict[str, tuple[str, ...]] = {
    "kugou_lite": ("kugou.com", ".kugou.com"),
    "migu": ("migu.cn", ".migu.cn"),
    "qq": ("qq.com", ".qq.com"),
    "kuwo": ("kuwo.cn", ".kuwo.cn"),
    "bilibili": ("bilibili.com", ".bilibili.com"),
}

_VARIANT_TOKENS = (
    "live",
    "现场",
    "remix",
    "dj",
    "伴奏",
    "纯音乐",
    "instrumental",
    "karaoke",
    "ktv",
    "cover",
    "翻唱",
    "慢摇",
    "片段",
    "试听",
    "铃声",
    "acoustic",
    "unplugged",
    "nightcore",
    "spedup",
    "串烧",
    "翻弹",
    "reaction",
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TRACK_PREFIX_RE = re.compile(r"^\s*\d{1,3}\s*[-._)\]]\s*")
_PAREN_SUFFIX_RE = re.compile(r"\s*[\(\[（【][^)\]）】]*[\)\]）】]\s*")
_CONTENT_RANGE_TOTAL_RE = re.compile(r"/(\d+)$")

_AUDIO_MIME_TO_FORMAT = {
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
}
_FORMAT_TO_MIME = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "wav": "audio/wav",
    "webm": "audio/webm",
}
_LOSSLESS_LEVEL_NAMES = frozenset({"jymaster", "hires", "lossless", "sky", "jyeffect", "viper_atmos", "sq", "zq", "hr"})


@dataclass(frozen=True)
class TrackQuery:
    """Credential-free track metadata passed to local & external resolvers."""

    title: str
    artists: tuple[str, ...]
    album: str = ""
    duration_ms: int = 0

    @classmethod
    def from_song(cls, song: Song) -> TrackQuery:
        return cls(
            title=str(song.title or "").strip(),
            artists=tuple(str(artist).strip() for artist in (song.artists or []) if str(artist).strip()),
            album=str(song.album or "").strip(),
            duration_ms=max(0, int(song.duration or 0)),
        )

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    @property
    def search_keyword(self) -> str:
        parts = [self.title]
        if self.primary_artist:
            parts.append(self.primary_artist)
        return " ".join(part for part in parts if part).strip()


@dataclass(frozen=True)
class LocalTrackRecord:
    path: Path
    title: str
    artist: str
    album: str
    duration_ms: int
    format: str
    bitrate: int
    sample_rate: int
    mtime: float
    alt_title: str = ""
    alt_artist: str = ""
    cover_id: str = ""
    isrc: str = ""
    mbid: str = ""
    replaygain_track_gain_db: float | None = None
    replaygain_track_peak: float | None = None
    replaygain_album_gain_db: float | None = None
    replaygain_album_peak: float | None = None
    file_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "track_key": f"local:{self.path}",
            "provider": "local",
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "format": self.format,
            "bitrate": self.bitrate,
            "sample_rate": self.sample_rate,
            "mtime": self.mtime,
            "cover_id": self.cover_id,
            "cover_url": f"/api/local-cover/{self.cover_id}" if self.cover_id else "",
            "isrc": self.isrc,
            "mbid": self.mbid,
            "replaygain_track_gain_db": self.replaygain_track_gain_db,
            "replaygain_track_peak": self.replaygain_track_peak,
            "replaygain_album_gain_db": self.replaygain_album_gain_db,
            "replaygain_album_peak": self.replaygain_album_peak,
            "file_size": self.file_size,
        }


@dataclass(frozen=True)
class LocalTrackMatch:
    record: LocalTrackRecord
    match_score: float

    @property
    def path(self) -> Path:
        return self.record.path

    @property
    def title(self) -> str:
        return self.record.title

    @property
    def artist(self) -> str:
        return self.record.artist

    @property
    def album(self) -> str:
        return self.record.album

    @property
    def duration_ms(self) -> int:
        return self.record.duration_ms

    @property
    def format(self) -> str:
        return self.record.format

    @property
    def bitrate(self) -> int:
        return self.record.bitrate

    @property
    def sample_rate(self) -> int:
        return self.record.sample_rate


@dataclass(frozen=True)
class ExternalCandidate:
    title: str
    artists: list[str]
    album: str
    duration_ms: int
    source_provider: str
    provider_track_id: str
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    format: str = ""
    bitrate: int = 0
    sample_rate: int = 0
    bit_depth: int = 0
    size: int = 0
    match_score: float = 0.0
    strict_match: bool = False
    needs_proxy: bool = False
    level: str = ""
    cover_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamProbeResult:
    ok: bool
    final_url: str = ""
    content_type: str = ""
    format: str = ""
    size: int = 0
    reason: str = ""
    final_headers: dict[str, str] = field(default_factory=dict)


def score_audio_quality(
    format_name: str = "",
    level: str = "",
    bitrate: int = 0,
    sample_rate: int = 0,
    bit_depth: int = 0,
) -> int:
    """Objective quality ranking for cross-channel arbitration (0..1000)."""
    fmt = (format_name or "").strip().lower()
    lvl = (level or "").strip().lower()
    br = max(0, int(bitrate or 0))
    sr = max(0, int(sample_rate or 0))
    bd = max(0, int(bit_depth or 0))

    if lvl in {"jymaster", "viper_atmos"}:
        return 980 + min(20, sr // 10_000)
    if lvl in {"hires", "zq", "hr"} or (fmt in {"flac", "wav"} and (sr >= 88_200 or bd >= 24)):
        return 920 + min(40, (sr // 4_000) + (bd if bd >= 24 else 0))
    if lvl in {"sky", "jyeffect"}:
        return 880
    if fmt in {"flac", "wav"} or lvl in {"lossless", "sq"} or br >= 700_000:
        sr_bonus = min(40, sr // 2_400) if sr > 0 else 0
        br_bonus = min(60, br // 25_000) if br > 0 else 20
        return 740 + sr_bonus + br_bonus
    if lvl in {"exhigh", "hq", "320"} or br >= 256_000:
        return 380 + min(70, br // 6_000)
    if lvl in {"higher", "192"} or br >= 160_000:
        return 260 + min(60, br // 5_000)
    if lvl in {"standard", "pq", "128"} or br >= 96_000:
        return 180 + min(50, br // 4_000)
    if fmt in {"m4a", "aac", "ogg", "mp3"}:
        return 160
    return 100


def is_verified_lossless(audio: dict[str, Any] | None) -> bool:
    if not isinstance(audio, dict):
        return False
    fmt = str(audio.get("format") or "").strip().lower()
    lvl = str(audio.get("level") or "").strip().lower()
    br = _safe_int(audio.get("bitrate"))
    return fmt in {"flac", "wav"} or lvl in _LOSSLESS_LEVEL_NAMES or br >= 700_000


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def _base_title(value: str) -> str:
    stripped = _PAREN_SUFFIX_RE.sub(" ", value or "").strip()
    return normalize_text(stripped)


def _has_disallowed_variant(query_title: str, candidate_title: str) -> bool:
    q_norm = unicodedata.normalize("NFKC", query_title or "").casefold()
    c_norm = unicodedata.normalize("NFKC", candidate_title or "").casefold()
    q_compact = normalize_text(query_title)
    c_compact = normalize_text(candidate_title)
    for token in _VARIANT_TOKENS:
        in_cand = token in c_norm or token in c_compact
        in_query = token in q_norm or token in q_compact
        if in_cand and not in_query:
            return True
    return False


def evaluate_match(
    query: TrackQuery,
    cand_title: str,
    cand_artists: list[str],
    cand_duration_ms: int,
    *,
    duration_tolerance_ms: int = 5_000,
    allow_title_embedded_artist: bool = False,
) -> tuple[bool, float]:
    """Score a candidate track and decide whether it is safe for strict auto-play."""
    q_title = normalize_text(query.title)
    c_title = normalize_text(cand_title)
    if not q_title or not c_title:
        return False, 0.0

    q_artist = normalize_text(query.primary_artist)
    norm_cand_artists = [normalize_text(item) for item in cand_artists if normalize_text(item)]

    exact_title = q_title == c_title
    base_title_match = _base_title(query.title) == _base_title(cand_title) and bool(_base_title(query.title))
    title_overlap = q_title in c_title or c_title in q_title

    artist_exact = bool(q_artist) and any(
        q_artist == ca or (len(q_artist) >= 2 and q_artist in ca) for ca in norm_cand_artists
    )
    embedded_strict = False
    if allow_title_embedded_artist and not exact_title and q_artist:
        cleaned = _PAREN_SUFFIX_RE.sub(" ", cand_title).strip()
        for sep in (" - ", " – ", " — ", "-", "—", "/"):
            if sep in cleaned:
                left, right = [part.strip() for part in cleaned.split(sep, 1)]
                nl, nr = normalize_text(left), normalize_text(right)
                if (nl == q_artist and nr == q_title) or (nl == q_title and nr == q_artist):
                    embedded_strict = True
                    exact_title = True
                    artist_exact = True
                    break

    duration_ok = True
    duration_score = 0.1
    if query.duration_ms > 0 and cand_duration_ms > 0:
        diff = abs(query.duration_ms - cand_duration_ms)
        duration_ok = diff <= duration_tolerance_ms
        if diff <= 1_000:
            duration_score = 0.15
        elif diff <= duration_tolerance_ms:
            duration_score = 0.12
        elif diff <= duration_tolerance_ms * 3:
            duration_score = 0.04
        else:
            duration_score = 0.0

    has_variant = _has_disallowed_variant(query.title, cand_title)

    strict = bool(
        exact_title
        and (artist_exact or (not q_artist and not norm_cand_artists))
        and duration_ok
        and not has_variant
    )
    if embedded_strict and (not duration_ok or has_variant):
        strict = False

    score = 0.0
    if exact_title:
        score += 0.55
    elif base_title_match:
        score += 0.40
    elif title_overlap:
        score += 0.25
    if artist_exact:
        score += 0.30
    elif q_artist and q_artist in c_title:
        score += 0.15
    score += duration_score
    if has_variant:
        score = max(0.0, score - 0.25)
    score = min(1.0, round(score, 4))
    return strict, score


def sanitize_outbound_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Ensure NO credentials/cookies ever leave via unauthenticated/CDN external audio requests."""
    cleaned: dict[str, str] = {"User-Agent": _EXTERNAL_USER_AGENT}
    for key, value in (headers or {}).items():
        lower = str(key).strip().lower()
        if lower in {"host", "connection", "cookie", "authorization", "proxy-authorization", "x-csrf-token"}:
            continue
        if "\r" in str(value) or "\n" in str(value):
            continue
        cleaned[str(key)] = str(value)
    return cleaned


def _resolve_addresses(host, port, **kwargs):
    return socket.getaddrinfo(host, port, **kwargs)


_NON_PUBLIC_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".localdomain")
_NAT64_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only for globally routable unicast IPs (excluding NAT64/6to4/Teredo/mapped)."""
    if isinstance(ip, ipaddress.IPv6Address):
        if (
            ip.ipv4_mapped is not None
            or ip.sixtofour is not None
            or ip.teredo is not None
            or any(ip in prefix for prefix in _NAT64_PREFIXES)
        ):
            return False
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def validated_addresses(url: str, *, allow_loopback: str | bool = False, resolver_fn=None,
                         loopback_bases=()) -> tuple[str, ...]:
    """Accept public destinations, or the exact configured loopback endpoint."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
            return ()
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        configured = (allow_loopback, *loopback_bases)
        for configured_base in configured:
            if isinstance(configured_base, str) and is_loopback_endpoint(configured_base):
                base = urllib.parse.urlsplit(configured_base)
                if host == base.hostname and parsed.scheme == base.scheme and port == (base.port or (443 if base.scheme == "https" else 80)):
                    return ("127.0.0.1" if host == "localhost" else host,)
        if host == "localhost" or host.endswith(_NON_PUBLIC_HOST_SUFFIXES):
            return ()
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            resolve = resolver_fn or _resolve_addresses
            addresses = [ipaddress.ip_address(info[4][0]) for info in resolve(host, port, type=socket.SOCK_STREAM)]
        if addresses and all(is_public_ip(ip) for ip in addresses):
            return tuple(str(ip) for ip in addresses)
        return ()
    except (ValueError, OSError, UnicodeError):
        return ()


def _pinned_url(url, addresses):
    original = httpx.URL(url)
    return (original.copy_with(host=addresses[0]), original.netloc.decode("ascii"),
            {"sni_hostname": original.raw_host.decode("ascii")})


def _pinned_request(method, url, addresses, headers, timeout, **kwargs):
    pinned, authority, extensions = _pinned_url(url, addresses)
    # Build independently of the shared client's cookie jar and default credentials.
    return httpx.Request(method, pinned, headers={**headers, "Host": authority, "Connection": "close"},
                         extensions={**extensions, "timeout": httpx.Timeout(timeout).as_dict()}, **kwargs)


def is_safe_external_url(url: str, **options) -> bool:
    return bool(validated_addresses(url, **options))


def is_loopback_endpoint(url: str) -> bool:
    cleaned = (url or "").strip()
    if not cleaned:
        return False
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").strip().lower()
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_custom_external_endpoint(url: str) -> str:
    """Validate a user-supplied custom external audio resolver URL."""
    cleaned = (url or "").strip()
    if not cleaned:
        return ""
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("自定义外部音源地址必须是有效的 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("自定义外部音源地址不允许包含内嵌凭据")
    is_loopback = is_loopback_endpoint(cleaned)
    if parsed.scheme == "http" and not is_loopback:
        raise ValueError("非本机自定义外部音源地址必须使用 HTTPS")
    if not is_loopback and not is_safe_external_url(cleaned, allow_loopback=False):
        raise ValueError("自定义外部音源地址指向了不允许的内网地址")
    return cleaned


def _url_matches_channel_scope(url: str, channel_id: str, validated_api_base: str = "") -> bool:
    """Strict host allowlist controlling where a channel's own credential may be sent."""
    if channel_id == "gdstudio":
        return False
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return False
    target_host = (parsed.hostname or "").strip().lower()
    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if validated_api_base:
        base = urllib.parse.urlsplit(validated_api_base)
        base_host = (base.hostname or "").strip().lower()
        base_port = base.port or (443 if base.scheme == "https" else 80)
        if parsed.scheme == base.scheme and target_host == base_host and target_port == base_port:
            return True

    if parsed.scheme != "https":
        return False
    suffixes = _CHANNEL_TRUSTED_HOST_SUFFIXES.get(channel_id)
    if not suffixes:
        return False
    return any(target_host == s or (s.startswith(".") and target_host.endswith(s)) for s in suffixes)


def _scoped_channel_request(
    http_client: httpx.Client,
    method: str,
    url: str,
    *,
    channel_id: str,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    secret_headers: dict[str, str] | None = None,
    validated_api_base: str = "",
    timeout: float = 5.0,
) -> httpx.Response | None:
    """Send a channel request with strict host-scoped credentials & redirect credential stripping."""
    allow_loopback = validated_api_base
    req_headers = sanitize_outbound_headers(headers)
    if secret_headers and _url_matches_channel_scope(url, channel_id, validated_api_base):
        for k, v in secret_headers.items():
            if v and "\r" not in str(v) and "\n" not in str(v):
                req_headers[str(k)] = str(v)

    current_url = url
    current_method = method.upper()
    current_params = params
    current_json = json_body
    try:
        for _ in range(_MAX_PROBE_REDIRECTS + 1):
            addresses = validated_addresses(current_url, loopback_bases=(allow_loopback,))
            if not addresses:
                return None
            request = _pinned_request(current_method, current_url, addresses, req_headers, timeout,
                                      params=current_params, json=current_json)
            response = isolate_cookie_jar(http_client).send(request, auth=None, follow_redirects=False)
            location = response.headers.get("location")
            if response.status_code in {301, 302, 303, 307, 308} and location:
                next_url = urllib.parse.urljoin(current_url, location)
                orig_parsed = urllib.parse.urlsplit(current_url)
                next_parsed = urllib.parse.urlsplit(next_url)
                if (
                    orig_parsed.scheme.lower() != next_parsed.scheme.lower()
                    or orig_parsed.netloc.lower() != next_parsed.netloc.lower()
                ):
                    for key in list(req_headers):
                        if key in (secret_headers or {}) or key.lower() in {"cookie", "authorization", "proxy-authorization", "x-api-key", "x-resolver-token"}:
                            req_headers.pop(key, None)
                if response.status_code in {301, 302, 303} and current_method not in {"GET", "HEAD"}:
                    current_method = "GET"
                    current_json = None
                current_params = None
                current_url = next_url
                continue
            logical_request = copy(response.request)
            logical = httpx.URL(current_url)
            logical_request.url = logical.copy_with(query=response.request.url.query or None)
            response.request = logical_request
            return response
    except (httpx.HTTPError, TimeoutError, OSError):
        return None
    return None


def _detect_format_from_bytes(head: bytes, content_type: str, url: str) -> str:
    if len(head) >= 4:
        if head[:4] == b"fLaC":
            return "flac"
        if head[:4] == b"OggS":
            return "ogg"
        if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
            return "wav"
        if len(head) >= 8 and head[4:8] == b"ftyp":
            return "m4a"
        if head[:3] == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
            return "mp3"
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in _AUDIO_MIME_TO_FORMAT:
        return _AUDIO_MIME_TO_FORMAT[ct]
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower().lstrip(".")
    if suffix in _FORMAT_TO_MIME:
        return suffix
    return ""


def _looks_like_text_error(head: bytes, content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct.startswith(("text/", "image/")) or ct in {
        "application/json",
        "application/xml",
        "application/xhtml+xml",
        "application/javascript",
    }:
        return True
    sample = head.lstrip()[:32].lower()
    return sample.startswith((b"<!doctype", b"<html", b"<?xml", b'{"', b"[{"))


def playback_redirect_headers(current_url, next_url, headers):
    old, new = map(urllib.parse.urlsplit, (current_url, next_url))
    if (old.scheme.lower(), old.netloc.lower()) == (new.scheme.lower(), new.netloc.lower()):
        return headers
    return {k: v for k, v in headers.items() if k.lower() in {"user-agent", "accept", "accept-language", "range"}}


def probe_audio_url(
    url: str,
    http_client: httpx.Client,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 4.0,
    allow_loopback: str | bool = False,
) -> StreamProbeResult:
    """Verify an external audio URL is reachable and actually serves audio bytes (strict zero-Cookie)."""
    req_headers = sanitize_outbound_headers(headers)
    req_headers["Range"] = "bytes=0-4095"
    current_url = url
    try:
        for _ in range(_MAX_PROBE_REDIRECTS + 1):
            addresses = validated_addresses(current_url, loopback_bases=(allow_loopback,))
            if not addresses:
                return StreamProbeResult(False, reason="unsafe_url" if current_url == url else "unsafe_redirect")
            request = _pinned_request("GET", current_url, addresses, req_headers, timeout)
            with closing(isolate_cookie_jar(http_client).send(request, auth=None, follow_redirects=False, stream=True)) as response:
                status = response.status_code
                location = response.headers.get("location")
                if status in {301, 302, 303, 307, 308} and location:
                    next_url = urllib.parse.urljoin(current_url, location)
                    req_headers = playback_redirect_headers(current_url, next_url, req_headers)
                    current_url = next_url
                    continue
                if status not in {200, 206}:
                    return StreamProbeResult(False, final_url=current_url, reason=f"http_{status}")
                content_type = response.headers.get("content-type", "")
                head_chunks: list[bytes] = []
                read_len = 0
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    take = min(len(chunk), 4096 - read_len)
                    head_chunks.append(chunk[:take])
                    read_len += take
                    if read_len >= 4096:
                        break
                head = b"".join(head_chunks)
                if not head or _looks_like_text_error(head, content_type):
                    return StreamProbeResult(False, final_url=current_url, content_type=content_type, reason="not_audio")
                total_size = 0
                content_range = response.headers.get("content-range", "")
                range_match = _CONTENT_RANGE_TOTAL_RE.search(content_range)
                if range_match:
                    total_size = int(range_match.group(1))
                elif response.headers.get("content-length", "").isdigit():
                    total_size = int(response.headers["content-length"])
                if status == 200 and 0 < total_size < 1024:
                    return StreamProbeResult(False, final_url=current_url, content_type=content_type, reason="too_small")
                detected_format = _detect_format_from_bytes(head, content_type, current_url)
                if not detected_format and not content_type.lower().startswith(("audio/", "application/octet-stream")):
                    return StreamProbeResult(False, final_url=current_url, content_type=content_type, reason="unknown_media")
                resolved_ct = content_type.split(";")[0].strip() or _FORMAT_TO_MIME.get(detected_format, "audio/mpeg")
                return StreamProbeResult(
                    ok=True,
                    final_url=current_url,
                    final_headers={k: v for k, v in req_headers.items() if k.lower() != "range"},
                    content_type=resolved_ct,
                    format=detected_format,
                    size=total_size,
                )
    except (httpx.HTTPError, TimeoutError, OSError):
        return StreamProbeResult(False, reason="probe_network_error")
    return StreamProbeResult(False, reason="redirect_limit")


# ---------------------------------------------------------------------------
# Pure-Python Local Audio Metadata Reader & Indexer
# ---------------------------------------------------------------------------


def _parse_filename_parts(path: Path) -> tuple[str, str, str, str]:
    stem = _TRACK_PREFIX_RE.sub("", path.stem).strip()
    parent_hint = path.parent.name.strip() if path.parent and path.parent.name else ""
    for sep in (" - ", " – ", " — "):
        if sep in stem:
            left, right = [part.strip() for part in stem.split(sep, 1)]
            if left and right:
                return right, left, left, right
    return stem, parent_hint, "", ""


def _parse_flac_metadata(path: Path, file_size: int) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    with path.open("rb") as handle:
        if handle.read(4) != b"fLaC":
            return meta
        while True:
            header = handle.read(4)
            if len(header) < 4:
                break
            is_last = bool(header[0] & 0x80)
            block_type = header[0] & 0x7F
            length = int.from_bytes(header[1:4], "big")
            if length > 4_194_304:
                break
            block = handle.read(length)
            if len(block) < length:
                break
            if block_type == 0 and len(block) >= 18:
                packed = int.from_bytes(block[10:18], "big")
                sample_rate = (packed >> 44) & 0xFFFFF
                total_samples = packed & 0xFFFFFFFFF
                if sample_rate > 0 and total_samples > 0:
                    duration_ms = int((total_samples * 1000) / sample_rate)
                    meta["sample_rate"] = sample_rate
                    meta["duration_ms"] = duration_ms
                    if duration_ms > 0:
                        meta["bitrate"] = int((file_size * 8 * 1000) / duration_ms)
            elif block_type == 4 and len(block) >= 8:
                offset = 0
                vendor_len = struct.unpack_from("<I", block, offset)[0]
                offset += 4 + vendor_len
                if offset + 4 <= len(block):
                    comment_count = struct.unpack_from("<I", block, offset)[0]
                    offset += 4
                    for _ in range(min(comment_count, 128)):
                        if offset + 4 > len(block):
                            break
                        entry_len = struct.unpack_from("<I", block, offset)[0]
                        offset += 4
                        if offset + entry_len > len(block):
                            break
                        raw_entry = block[offset : offset + entry_len].decode("utf-8", errors="ignore")
                        offset += entry_len
                        if "=" in raw_entry:
                            k, v = raw_entry.split("=", 1)
                            ku = k.strip().upper()
                            if ku == "TITLE" and not meta.get("title"):
                                meta["title"] = v.strip()
                            elif ku in {"ARTIST", "ALBUMARTIST"} and not meta.get("artist"):
                                meta["artist"] = v.strip()
                            elif ku == "ALBUM" and not meta.get("album"):
                                meta["album"] = v.strip()
            if is_last:
                break
    return meta


def _parse_wav_metadata(path: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    with path.open("rb") as handle:
        riff = handle.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            return meta
        byte_rate = 0
        data_size = 0
        for _ in range(32):
            chunk_hdr = handle.read(8)
            if len(chunk_hdr) < 8:
                break
            chunk_id = chunk_hdr[:4]
            chunk_size = struct.unpack("<I", chunk_hdr[4:8])[0]
            if chunk_id == b"fmt " and chunk_size >= 16:
                fmt = handle.read(16)
                if len(fmt) == 16:
                    _, _channels, sample_rate, byte_rate = struct.unpack("<HHII", fmt[:12])
                    meta["sample_rate"] = sample_rate
                    meta["bitrate"] = byte_rate * 8
                if chunk_size > 16:
                    handle.seek(chunk_size - 16, os.SEEK_CUR)
            elif chunk_id == b"data":
                data_size = chunk_size
                break
            else:
                handle.seek(chunk_size + (chunk_size % 2), os.SEEK_CUR)
        if byte_rate > 0 and data_size > 0:
            meta["duration_ms"] = int((data_size * 1000) / byte_rate)
    return meta


def _decode_id3_text(raw: bytes) -> str:
    if not raw:
        return ""
    encoding = raw[0]
    body = raw[1:]
    try:
        if encoding == 0:
            return body.decode("latin-1", errors="ignore").rstrip("\x00").strip()
        if encoding == 1:
            return body.decode("utf-16", errors="ignore").rstrip("\x00").strip()
        if encoding == 2:
            return body.decode("utf-16-be", errors="ignore").rstrip("\x00").strip()
        if encoding == 3:
            return body.decode("utf-8", errors="ignore").rstrip("\x00").strip()
    except Exception:
        return ""
    return ""


def _parse_id3v2_metadata(path: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    with path.open("rb") as handle:
        header = handle.read(10)
        if len(header) < 10 or header[:3] != b"ID3":
            return meta
        version = header[3]
        if version not in {3, 4}:
            return meta
        tag_size = (
            ((header[6] & 0x7F) << 21)
            | ((header[7] & 0x7F) << 14)
            | ((header[8] & 0x7F) << 7)
            | (header[9] & 0x7F)
        )
        raw_tag = handle.read(min(tag_size, 524_288))
        pos = 0
        while pos + 10 <= len(raw_tag):
            frame_id = raw_tag[pos : pos + 4].decode("latin-1", errors="ignore")
            if not frame_id.strip("\x00"):
                break
            if version == 4:
                frame_size = (
                    ((raw_tag[pos + 4] & 0x7F) << 21)
                    | ((raw_tag[pos + 5] & 0x7F) << 14)
                    | ((raw_tag[pos + 6] & 0x7F) << 7)
                    | (raw_tag[pos + 7] & 0x7F)
                )
            else:
                frame_size = struct.unpack_from(">I", raw_tag, pos + 4)[0]
            pos += 10
            if frame_size <= 0 or pos + frame_size > len(raw_tag):
                break
            frame_data = raw_tag[pos : pos + frame_size]
            pos += frame_size
            if frame_id == "TIT2" and not meta.get("title"):
                meta["title"] = _decode_id3_text(frame_data)
            elif frame_id == "TPE1" and not meta.get("artist"):
                meta["artist"] = _decode_id3_text(frame_data)
            elif frame_id == "TALB" and not meta.get("album"):
                meta["album"] = _decode_id3_text(frame_data)
            elif frame_id == "TLEN" and not meta.get("duration_ms"):
                text_val = _decode_id3_text(frame_data)
                if text_val.isdigit():
                    meta["duration_ms"] = int(text_val)
    return meta


def read_local_track_record(
    path: Path,
    *,
    cover_cache: LocalCoverCache | None = None,
) -> LocalTrackRecord | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    ext = path.suffix.lower()
    if ext not in SUPPORTED_LOCAL_EXTENSIONS:
        return None
    fmt = ext.lstrip(".")
    tag_meta: dict[str, Any] = {}
    try:
        tag_meta = parse_audio_metadata(path, stat.st_size, cover_cache=cover_cache)
    except Exception:
        tag_meta = {}

    fb_title, fb_artist, alt_title, alt_artist = _parse_filename_parts(path)
    title = str(tag_meta.get("title") or fb_title or path.stem).strip()
    artist = str(tag_meta.get("artist") or fb_artist or "").strip()
    album = str(tag_meta.get("album") or "").strip()
    duration_ms = max(0, int(tag_meta.get("duration_ms") or 0))
    bitrate = max(0, int(tag_meta.get("bitrate") or 0))
    sample_rate = max(0, int(tag_meta.get("sample_rate") or 0))
    return LocalTrackRecord(
        path=path,
        title=title,
        artist=artist,
        album=album,
        duration_ms=duration_ms,
        format=fmt,
        bitrate=bitrate,
        sample_rate=sample_rate,
        mtime=stat.st_mtime,
        alt_title=alt_title if not tag_meta.get("title") else "",
        alt_artist=alt_artist if not tag_meta.get("artist") else "",
        cover_id=str(tag_meta.get("cover_id") or ""),
        isrc=str(tag_meta.get("isrc") or ""),
        mbid=str(tag_meta.get("mbid") or ""),
        replaygain_track_gain_db=tag_meta.get("replaygain_track_gain_db"),
        replaygain_track_peak=tag_meta.get("replaygain_track_peak"),
        replaygain_album_gain_db=tag_meta.get("replaygain_album_gain_db"),
        replaygain_album_peak=tag_meta.get("replaygain_album_peak"),
        file_size=int(stat.st_size),
    )


class LocalLibraryScanner:
    """Scans and strictly matches local lossless/audio directories with mtime incremental caching."""

    def __init__(self, db: Any = None, *, cover_cache: LocalCoverCache | None = None):
        self.db = db
        if cover_cache is not None:
            self.cover_cache = cover_cache
        elif db is not None and hasattr(db, "data_dir"):
            self.cover_cache = LocalCoverCache(db.data_dir)
        else:
            self.cover_cache = LocalCoverCache(None)
        self._lock = threading.RLock()
        self._records: list[LocalTrackRecord] = []
        self._revision = 0
        self._last_dirs: tuple[str, ...] = ()

    def records(self) -> list[LocalTrackRecord]:
        with self._lock:
            if not self._records and self.db is not None:
                self._load_from_db_locked()
            return list(self._records)

    def _load_from_db_locked(self) -> None:
        loader = getattr(self.db, "list_local_files", None)
        if not callable(loader):
            return
        try:
            try:
                rows = []
                while len(rows) < _MAX_LOCAL_FILES:
                    limit = min(5000, _MAX_LOCAL_FILES - len(rows))
                    page = loader(limit=limit, offset=len(rows))
                    rows.extend(page)
                    if len(page) < limit:
                        break
            except TypeError:
                rows = loader()
        except Exception:
            return
        loaded: list[LocalTrackRecord] = []
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("path"):
                continue
            path = Path(str(row["path"]))
            loaded.append(
                LocalTrackRecord(
                    path=path,
                    title=str(row.get("title") or path.stem),
                    artist=str(row.get("artist") or ""),
                    album=str(row.get("album") or ""),
                    duration_ms=max(0, int(row.get("duration_ms") or 0)),
                    format=str(row.get("format") or path.suffix.lower().lstrip(".")),
                    bitrate=max(0, int(row.get("bitrate") or 0)),
                    sample_rate=max(0, int(row.get("sample_rate") or 0)),
                    mtime=float(row.get("mtime") or 0.0),
                    cover_id=str(row.get("cover_id") or ""),
                    isrc=str(row.get("isrc") or ""),
                    mbid=str(row.get("mbid") or ""),
                    replaygain_track_gain_db=row.get("replaygain_track_gain_db"),
                    replaygain_track_peak=row.get("replaygain_track_peak"),
                    replaygain_album_gain_db=row.get("replaygain_album_gain_db"),
                    replaygain_album_peak=row.get("replaygain_album_peak"),
                    file_size=max(0, int(row.get("file_size") or 0)),
                )
            )
        self._records = loaded

    def scan(self, dirs: list[str | Path]) -> dict[str, Any]:
        with self._lock:
            previous_list = self.records()
        previous_by_path = {r.path: r for r in previous_list}

        readable = []
        unreadable = {}
        file_errors: list[dict[str, str]] = []
        scanned = {}
        reused_count = 0
        for raw in dict.fromkeys(dirs):
            root = Path(raw).expanduser().absolute()
            if not root.is_dir():
                unreadable[str(root)] = "目录不存在或不可访问"
                continue

            def walk_error(exc):
                raise exc

            try:
                for folder, _, filenames in os.walk(root, onerror=walk_error):
                    for name in filenames:
                        target = Path(folder) / name
                        if target.suffix.lower() not in SUPPORTED_LOCAL_EXTENSIONS or target in scanned:
                            continue
                        if len(scanned) >= _MAX_LOCAL_FILES:
                            raise OSError("达到单次扫描文件上限")
                        # Incremental mtime + size check with single-file fault tolerance
                        try:
                            st = target.stat()
                        except OSError as file_exc:
                            file_errors.append({"file": str(target), "reason": str(file_exc)})
                            continue
                        prev = previous_by_path.get(target)
                        if (
                            prev is not None
                            and prev.file_size > 0
                            and prev.file_size == int(st.st_size)
                            and abs(prev.mtime - st.st_mtime) < 1e-6
                        ):
                            scanned[target] = prev
                            reused_count += 1
                            continue
                        try:
                            record = read_local_track_record(target, cover_cache=self.cover_cache)
                        except Exception as file_exc:
                            file_errors.append({"file": str(target), "reason": str(file_exc)})
                            continue
                        if record is None:
                            file_errors.append({"file": str(target), "reason": "无法读取音频文件"})
                            continue
                        scanned[target] = record
                readable.append(root)
            except OSError as exc:
                unreadable[str(root)] = str(exc)

        protected = [Path(p) for p in unreadable]
        with self._lock:
            previous = self.records()
            retained = {r.path: r for r in previous
                        if not any(r.path.is_relative_to(p) for p in readable)
                        or any(r.path.is_relative_to(p) for p in protected)}
            retained.update(scanned)
            total = len(retained)
            if self.db is not None:
                self.db.update_local_files_for_dirs(readable, [r.to_dict() for r in scanned.values()], protected)
                total = self.db.count_local_files()
            self._records = list(retained.values())
            self._last_dirs = tuple(str(p) for p in readable)
            if readable or scanned:
                self._revision += 1
            status = ("empty_dirs" if not dirs else "ok" if not unreadable else
                      "partial_failure" if readable else "unreadable_dirs")
            return {"status": status, "scanned_count": len(scanned), "scanned_files": len(scanned),
                    "reused_unchanged": reused_count, "file_errors": file_errors[:50],
                    "total_indexed": total, "dirs": list(map(str, dirs)), "revision": self._revision,
                    "unreadable_dirs": list(unreadable), "errors": [{"dir": p, "reason": e} for p, e in unreadable.items()],
                    "files": [r.to_dict() for r in self._records[:200]]}

    def match(self, query: TrackQuery, duration_tolerance_ms: int = 5_000) -> LocalTrackMatch | None:
        candidates = self.records()
        best: tuple[tuple[int, float, int], LocalTrackMatch] | None = None
        for record in candidates:
            if not record.path.is_file():
                continue
            strict, score = evaluate_match(
                query,
                record.title,
                [record.artist] if record.artist else [],
                record.duration_ms,
                duration_tolerance_ms=duration_tolerance_ms,
            )
            if not strict and record.alt_title:
                strict, score = evaluate_match(
                    query,
                    record.alt_title,
                    [record.alt_artist] if record.alt_artist else [],
                    record.duration_ms,
                    duration_tolerance_ms=duration_tolerance_ms,
                )
            if not strict:
                continue
            rank = (_LOCAL_FORMAT_PRIORITY.get(record.format, 0), score, record.bitrate)
            if best is None or rank > best[0]:
                best = (rank, LocalTrackMatch(record=record, match_score=score))
        return best[1] if best is not None else None


# ---------------------------------------------------------------------------
# Multi-Channel External Providers (STRICT PER-CHANNEL CREDENTIAL ISOLATION)
# ---------------------------------------------------------------------------


def _validated_channel_api_base(secret: dict[str, Any] | None) -> str:
    raw = str((secret or {}).get("api_base") or "").strip()
    if not raw:
        return ""
    try:
        return validate_custom_external_endpoint(raw).rstrip("/")
    except ValueError:
        return ""


def _check_search_response(response):
    if response is None or response.status_code in (408, 429) or response.status_code >= 500:
        raise RuntimeError("渠道搜索暂时不可用")


class KugouLiteProvider:
    """KuGou Lite (酷狗概念版) multi-quality search & URL resolver (strictly scoped credentials)."""

    name = "kugou_lite"
    public_search_url = "https://songsearch.kugou.com/song_search_v2"
    direct_play_url = "https://wwwapi.kugou.com/yy/index.php"

    def _build_secret_headers_and_params(self, secret: dict[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
        if not isinstance(secret, dict):
            return {}, {}
        cookie_parts: list[str] = []
        raw_cookie = str(secret.get("cookie") or "").strip()
        if raw_cookie:
            cookie_parts.append(raw_cookie)
        token = str(secret.get("token") or "").strip()
        userid = str(secret.get("userid") or "").strip()
        dfid = str(secret.get("dfid") or "").strip()
        if token and "token=" not in raw_cookie:
            cookie_parts.append(f"token={token}")
        if userid and "userid=" not in raw_cookie:
            cookie_parts.append(f"userid={userid}")
        if dfid and "kg_dfid=" not in raw_cookie:
            cookie_parts.append(f"kg_dfid={dfid}")
        secret_headers = {"Cookie": "; ".join(cookie_parts)} if cookie_parts else {}
        extra_params: dict[str, str] = {}
        if token:
            extra_params["token"] = token
        if userid:
            extra_params["userid"] = userid
        if dfid:
            extra_params["dfid"] = dfid
        return secret_headers, extra_params

    def search_page(
        self,
        keyword: str,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        limit: int = 30,
        offset: int = 0,
        timeout: float = 5.0,
    ) -> tuple[list[ExternalCandidate], bool]:
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return [], False
        per_page = max(1, min(100, int(limit or 30)))
        off = max(0, min(1_000, int(offset or 0)))
        page = (off // per_page) + 1
        api_base = _validated_channel_api_base(secret)
        secret_headers, auth_params = self._build_secret_headers_and_params(secret)

        if api_base:
            resp = _scoped_channel_request(
                http_client,
                "GET",
                f"{api_base}/search",
                channel_id=self.name,
                params={"keywords": cleaned, "pagesize": per_page, "page": page, "platform": "lite", **auth_params},
                secret_headers=secret_headers,
                validated_api_base=api_base,
                timeout=timeout,
            )
        else:
            resp = _scoped_channel_request(
                http_client,
                "GET",
                self.public_search_url,
                channel_id=self.name,
                params={"keyword": cleaned, "page": page, "pagesize": per_page, "platform": "WebFilter"},
                headers={"Referer": "https://www.kugou.com/"},
                secret_headers=secret_headers,
                timeout=timeout,
            )
        _check_search_response(resp)
        if resp.status_code != 200:
            return [], False
        try:
            payload = resp.json()
        except ValueError:
            return [], False
        data_obj = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        raw_items = data_obj.get("lists") or data_obj.get("info") or (payload.get("songs") if isinstance(payload, dict) else None)
        if not isinstance(raw_items, list):
            return [], False

        candidates: list[ExternalCandidate] = []
        for item in raw_items[:per_page]:
            if not isinstance(item, dict):
                continue
            file_hash = str(
                item.get("SQFileHash")
                or item.get("ResFileHash")
                or item.get("HQFileHash")
                or item.get("FileHash")
                or item.get("hash")
                or ""
            ).strip()
            base_hash = str(item.get("FileHash") or item.get("hash") or file_hash).strip()
            if not file_hash:
                continue
            title = _HTML_TAG_RE.sub("", html.unescape(str(item.get("SongName") or item.get("songname") or item.get("name") or ""))).strip()
            if not title:
                continue
            singer_raw = _HTML_TAG_RE.sub("", html.unescape(str(item.get("SingerName") or item.get("singername") or item.get("artist") or ""))).strip()
            artists = [part.strip() for part in re.split(r"[、&/,]", singer_raw) if part.strip()]
            duration_ms = _parse_duration_ms(item.get("Duration") or item.get("duration") or 0)
            has_sq = bool(str(item.get("SQFileHash") or item.get("ResFileHash") or "").strip())
            candidates.append(
                ExternalCandidate(
                    title=title,
                    artists=artists,
                    album=html.unescape(str(item.get("AlbumName") or item.get("album_name") or "")).strip(),
                    duration_ms=duration_ms,
                    source_provider=self.name,
                    provider_track_id=base_hash,
                    format="flac" if has_sq else "",
                    bitrate=999_000 if has_sq else 0,
                    match_score=1.0,
                    strict_match=False,
                    needs_proxy=True,
                    cover_url=str(item.get("Image") or item.get("cover") or "").replace("{size}", "240"),
                    extra={
                        "sq_hash": str(item.get("SQFileHash") or "").strip(),
                        "res_hash": str(item.get("ResFileHash") or "").strip(),
                        "hq_hash": str(item.get("HQFileHash") or "").strip(),
                        "album_id": str(item.get("AlbumID") or item.get("album_id") or "").strip(),
                        "from_custom_endpoint": bool(api_base),
                    },
                )
            )
        total = _safe_int(data_obj.get("total"))
        has_more = (off + len(raw_items) < total) if total > 0 else (len(raw_items) >= per_page)
        return candidates, has_more

    def search(
        self,
        query: TrackQuery,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
        duration_tolerance_ms: int = 5_000,
    ) -> list[ExternalCandidate]:
        raw, _ = self.search_page(query.search_keyword, http_client, secret=secret, limit=8, offset=0, timeout=timeout)
        scored: list[ExternalCandidate] = []
        for cand in raw:
            strict, score = evaluate_match(query, cand.title, cand.artists, cand.duration_ms, duration_tolerance_ms=duration_tolerance_ms)
            if score < 0.25:
                continue
            scored.append(
                ExternalCandidate(
                    title=cand.title,
                    artists=cand.artists,
                    album=cand.album,
                    duration_ms=cand.duration_ms,
                    source_provider=cand.source_provider,
                    provider_track_id=cand.provider_track_id,
                    format=cand.format,
                    bitrate=cand.bitrate,
                    match_score=score,
                    strict_match=strict,
                    needs_proxy=cand.needs_proxy,
                    cover_url=cand.cover_url,
                    extra=cand.extra,
                )
            )
        return scored

    def resolve_url(
        self,
        candidate: ExternalCandidate,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> ExternalCandidate | None:
        api_base = _validated_channel_api_base(secret)
        allow_loopback = api_base
        secret_headers, auth_params = self._build_secret_headers_and_params(secret)

        if api_base:
            quality_ladder = (
                ("viper_atmos", "flac", candidate.extra.get("res_hash") or candidate.extra.get("sq_hash") or candidate.provider_track_id),
                ("flac", "flac", candidate.extra.get("sq_hash") or candidate.provider_track_id),
                ("high", "mp3", candidate.extra.get("hq_hash") or candidate.provider_track_id),
                ("128", "mp3", candidate.provider_track_id),
            )
            for quality_key, default_fmt, target_hash in quality_ladder:
                if not target_hash:
                    continue
                resp = _scoped_channel_request(
                    http_client,
                    "GET",
                    f"{api_base}/song/url",
                    channel_id=self.name,
                    params={
                        "hash": target_hash,
                        "quality": quality_key,
                        "platform": "lite",
                        **({"album_id": candidate.extra["album_id"]} if candidate.extra.get("album_id") else {}),
                        **auth_params,
                    },
                    secret_headers=secret_headers,
                    validated_api_base=api_base,
                    timeout=timeout,
                )
                if resp is None or resp.status_code != 200:
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                data_block = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                raw_url = data_block.get("url") or payload.get("url")
                if isinstance(raw_url, list):
                    raw_url = next((u for u in raw_url if isinstance(u, str) and u.startswith(("http://", "https://"))), "")
                url = str(raw_url or "").strip()
                if not is_safe_external_url(url, allow_loopback=allow_loopback):
                    continue
                # Reject obvious 30s/60s trial clips when song duration is known and long.
                time_len_ms = _parse_duration_ms(data_block.get("timeLength") or data_block.get("duration") or 0)
                if candidate.duration_ms >= 90_000 and 0 < time_len_ms < candidate.duration_ms * 0.5:
                    continue
                fmt = str(data_block.get("extName") or data_block.get("format") or default_fmt).strip().lower()
                if ".flac" in url.lower():
                    fmt = "flac"
                br = _safe_int(data_block.get("bitRate") or data_block.get("bitrate"))
                if br == 0:
                    br = 999_000 if fmt == "flac" else (320_000 if quality_key == "high" else 128_000)
                elif br <= 4000:
                    br *= 1000
                return ExternalCandidate(
                    title=candidate.title,
                    artists=candidate.artists,
                    album=candidate.album,
                    duration_ms=time_len_ms or candidate.duration_ms,
                    source_provider=self.name,
                    provider_track_id=candidate.provider_track_id,
                    url=url,
                    headers={"Referer": "https://www.kugou.com/", "User-Agent": _EXTERNAL_USER_AGENT},
                    format=fmt,
                    bitrate=br,
                    sample_rate=_safe_int(data_block.get("sampleRate") or data_block.get("sample_rate")),
                    size=_safe_int(data_block.get("fileSize") or data_block.get("size")),
                    match_score=candidate.match_score,
                    strict_match=candidate.strict_match,
                    needs_proxy=True,
                    level=quality_key if quality_key in {"viper_atmos", "flac"} else ("exhigh" if br >= 300_000 else "standard"),
                    cover_url=candidate.cover_url,
                    extra={**candidate.extra, "from_custom_endpoint": True},
                )
            return None

        # Direct KuGou lookup (works best with user KuGou dfid/cookie, strictly scoped to *.kugou.com).
        target_hash = candidate.extra.get("sq_hash") or candidate.provider_track_id
        resp = _scoped_channel_request(
            http_client,
            "GET",
            self.direct_play_url,
            channel_id=self.name,
            params={
                "r": "play/getdata",
                "hash": target_hash,
                "dfid": auth_params.get("dfid", "-"),
                "album_id": candidate.extra.get("album_id", ""),
            },
            headers={"Referer": "https://www.kugou.com/"},
            secret_headers=secret_headers,
            timeout=timeout,
        )
        if resp is None or resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
        data_block = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        url = str(data_block.get("play_url") or data_block.get("play_backup_url") or "").strip()
        if not is_safe_external_url(url, allow_loopback=False):
            return None
        if data_block.get("is_free_part"):
            return None
        fmt = "flac" if ".flac" in url.lower() else "mp3"
        br = _safe_int(data_block.get("bitrate"))
        br = br * 1000 if 0 < br <= 4000 else (br or (999_000 if fmt == "flac" else 320_000))
        return ExternalCandidate(
            title=candidate.title,
            artists=candidate.artists,
            album=candidate.album,
            duration_ms=_parse_duration_ms(data_block.get("timelength")) or candidate.duration_ms,
            source_provider=self.name,
            provider_track_id=candidate.provider_track_id,
            url=url,
            headers={"Referer": "https://www.kugou.com/", "User-Agent": _EXTERNAL_USER_AGENT},
            format=fmt,
            bitrate=br,
            size=_safe_int(data_block.get("filesize")),
            match_score=candidate.match_score,
            strict_match=candidate.strict_match,
            needs_proxy=True,
            level="lossless" if fmt == "flac" else ("exhigh" if br >= 300_000 else "standard"),
            cover_url=candidate.cover_url,
            extra=candidate.extra,
        )


class MiguProvider:
    """Migu Music (咪咕音乐) search & ZQ/SQ/HQ/PQ lossless resolver (strictly scoped credentials)."""

    name = "migu"
    search_url = "https://pd.musicapp.migu.cn/MIGUM3.0/v1.0/content/search_all.do"
    listen_url = "https://app.pd.nf.migu.cn/MIGUM3.0/v1.0/content/sub/listenSong.do"

    def _build_secret_headers(self, secret: dict[str, Any] | None) -> dict[str, str]:
        if not isinstance(secret, dict):
            return {}
        headers: dict[str, str] = {}
        cookie = str(secret.get("cookie") or "").strip()
        token = str(secret.get("token") or "").strip()
        if cookie:
            headers["Cookie"] = cookie
        if token:
            headers["Authorization"] = f"Bearer {token}" if not token.lower().startswith("bearer ") else token
        return headers

    def search_page(
        self,
        keyword: str,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        limit: int = 30,
        offset: int = 0,
        timeout: float = 5.0,
    ) -> tuple[list[ExternalCandidate], bool]:
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return [], False
        per_page = max(1, min(100, int(limit or 30)))
        off = max(0, min(1_000, int(offset or 0)))
        page = (off // per_page) + 1
        api_base = _validated_channel_api_base(secret)
        secret_headers = self._build_secret_headers(secret)

        if api_base:
            resp = _scoped_channel_request(
                http_client,
                "GET",
                f"{api_base}/search",
                channel_id=self.name,
                params={"keyword": cleaned, "pageSize": per_page, "pageNo": page},
                secret_headers=secret_headers,
                validated_api_base=api_base,
                timeout=timeout,
            )
        else:
            resp = _scoped_channel_request(
                http_client,
                "GET",
                self.search_url,
                channel_id=self.name,
                params={"text": cleaned, "pageNo": page, "pageSize": per_page, "searchSwitch": '{"song":1}'},
                headers={"Referer": "https://music.migu.cn/"},
                secret_headers=secret_headers,
                timeout=timeout,
            )
        _check_search_response(resp)
        if resp.status_code != 200:
            return [], False
        try:
            payload = resp.json()
        except ValueError:
            return [], False
        if not isinstance(payload, dict):
            return [], False
        raw_items: Any = None
        total = 0
        if isinstance(payload.get("songResultData"), dict):
            raw_items = payload["songResultData"].get("result")
            total = _safe_int(payload["songResultData"].get("totalCount"))
        elif isinstance(payload.get("data"), dict):
            raw_items = payload["data"].get("list") or payload["data"].get("songs")
            total = _safe_int(payload["data"].get("total") or payload["data"].get("totalCount"))
        elif isinstance(payload.get("songs"), list):
            raw_items = payload["songs"]
        if not isinstance(raw_items, list):
            return [], False

        candidates: list[ExternalCandidate] = []
        for item in raw_items[:per_page]:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("copyrightId") or item.get("cid") or item.get("id") or "").strip()
            content_id = str(item.get("contentId") or item.get("id") or cid).strip()
            title = html.unescape(str(item.get("name") or item.get("title") or "")).strip()
            if not cid or not title:
                continue
            singers = item.get("singers") or item.get("artists") or []
            artists: list[str] = []
            if isinstance(singers, list):
                for s in singers:
                    if isinstance(s, dict) and s.get("name"):
                        artists.append(str(s["name"]).strip())
                    elif isinstance(s, str) and s.strip():
                        artists.append(s.strip())
            albums = item.get("albums")
            album = ""
            if isinstance(albums, list) and albums and isinstance(albums[0], dict):
                album = str(albums[0].get("name") or "").strip()
            elif isinstance(item.get("album"), dict):
                album = str(item["album"].get("name") or "").strip()
            elif isinstance(item.get("album"), str):
                album = item["album"].strip()
            duration_ms = _parse_duration_ms(item.get("duration") or item.get("length") or 0)
            img_items = item.get("imgItems")
            cover_url = ""
            if isinstance(img_items, list) and img_items and isinstance(img_items[0], dict):
                cover_url = str(img_items[0].get("img") or "")
            candidates.append(
                ExternalCandidate(
                    title=title,
                    artists=artists,
                    album=album,
                    duration_ms=duration_ms,
                    source_provider=self.name,
                    provider_track_id=cid,
                    match_score=1.0,
                    strict_match=False,
                    needs_proxy=True,
                    cover_url=cover_url,
                    extra={
                        "content_id": content_id,
                        "from_custom_endpoint": bool(api_base),
                    },
                )
            )
        has_more = (off + len(raw_items) < total) if total > 0 else (len(raw_items) >= per_page)
        return candidates, has_more

    def search(
        self,
        query: TrackQuery,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
        duration_tolerance_ms: int = 5_000,
    ) -> list[ExternalCandidate]:
        raw, _ = self.search_page(query.search_keyword, http_client, secret=secret, limit=8, offset=0, timeout=timeout)
        scored: list[ExternalCandidate] = []
        for cand in raw:
            strict, score = evaluate_match(query, cand.title, cand.artists, cand.duration_ms, duration_tolerance_ms=duration_tolerance_ms)
            if score < 0.25:
                continue
            scored.append(
                ExternalCandidate(
                    title=cand.title,
                    artists=cand.artists,
                    album=cand.album,
                    duration_ms=cand.duration_ms,
                    source_provider=cand.source_provider,
                    provider_track_id=cand.provider_track_id,
                    match_score=score,
                    strict_match=strict,
                    needs_proxy=cand.needs_proxy,
                    cover_url=cand.cover_url,
                    extra=cand.extra,
                )
            )
        return scored

    def resolve_url(
        self,
        candidate: ExternalCandidate,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> ExternalCandidate | None:
        api_base = _validated_channel_api_base(secret)
        allow_loopback = api_base
        secret_headers = self._build_secret_headers(secret)
        cid = candidate.provider_track_id
        content_id = str(candidate.extra.get("content_id") or cid)

        tones = (
            ("ZQ", "flac", 1_920_000, 96_000, "hires"),
            ("SQ", "flac", 999_000, 44_100, "lossless"),
            ("HQ", "mp3", 320_000, 44_100, "exhigh"),
            ("PQ", "mp3", 128_000, 44_100, "standard"),
        )
        for tone, default_fmt, default_br, default_sr, lvl in tones:
            if api_base:
                resp = _scoped_channel_request(
                    http_client,
                    "GET",
                    f"{api_base}/song/url",
                    channel_id=self.name,
                    params={"cid": cid, "id": content_id, "br": tone},
                    secret_headers=secret_headers,
                    validated_api_base=api_base,
                    timeout=timeout,
                )
            else:
                resp = _scoped_channel_request(
                    http_client,
                    "GET",
                    self.listen_url,
                    channel_id=self.name,
                    params={
                        "toneFlag": tone,
                        "netType": "00",
                        "ua": "Android_migu",
                        "version": "5.1",
                        "copyrightId": cid,
                        "contentId": content_id,
                        "resourceType": "2",
                        "channel": "0",
                    },
                    headers={"Referer": "https://music.migu.cn/"},
                    secret_headers=secret_headers,
                    timeout=timeout,
                )
            if resp is None or resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            data_block = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            url = str(data_block.get("url") or payload.get("url") or "").strip()
            if not is_safe_external_url(url, allow_loopback=allow_loopback):
                continue
            fmt = str(data_block.get("format") or default_fmt).strip().lower()
            if ".flac" in url.lower():
                fmt = "flac"
            elif ".mp3" in url.lower():
                fmt = "mp3"
            br = _safe_int(data_block.get("bitrate")) or default_br
            if 0 < br <= 4000:
                br *= 1000
            return ExternalCandidate(
                title=candidate.title,
                artists=candidate.artists,
                album=candidate.album,
                duration_ms=candidate.duration_ms,
                source_provider=self.name,
                provider_track_id=cid,
                url=url,
                headers={"Referer": "https://music.migu.cn/", "User-Agent": _EXTERNAL_USER_AGENT},
                format=fmt,
                bitrate=br,
                sample_rate=_safe_int(data_block.get("sample_rate")) or (default_sr if fmt == "flac" else 44_100),
                size=_safe_int(data_block.get("size")),
                match_score=candidate.match_score,
                strict_match=candidate.strict_match,
                needs_proxy=True,
                level=lvl if fmt == "flac" else ("exhigh" if br >= 300_000 else "standard"),
                cover_url=candidate.cover_url,
                extra={**candidate.extra, "tone": tone, "from_custom_endpoint": bool(api_base)},
            )
        return None


class QQMusicProvider:
    """QQ Music (QQ音乐) search & FLAC/320k/128k resolver (strictly scoped credentials)."""

    name = "qq"
    musicu_url = "https://u.y.qq.com/cgi-bin/musicu.fcg"

    def _build_secret_headers(self, secret: dict[str, Any] | None) -> tuple[dict[str, str], str]:
        if not isinstance(secret, dict):
            return {}, "0"
        cookie = str(secret.get("cookie") or "").strip()
        uin = str(secret.get("uin") or "0").strip() or "0"
        headers: dict[str, str] = {}
        if cookie:
            headers["Cookie"] = cookie
        return headers, uin

    def search_page(
        self,
        keyword: str,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        limit: int = 30,
        offset: int = 0,
        timeout: float = 5.0,
    ) -> tuple[list[ExternalCandidate], bool]:
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return [], False
        per_page = max(1, min(100, int(limit or 30)))
        off = max(0, min(1_000, int(offset or 0)))
        page = (off // per_page) + 1
        api_base = _validated_channel_api_base(secret)
        secret_headers, _uin = self._build_secret_headers(secret)

        if api_base:
            resp = _scoped_channel_request(
                http_client,
                "GET",
                f"{api_base}/search",
                channel_id=self.name,
                params={"key": cleaned, "pageSize": per_page, "pageNo": page},
                secret_headers=secret_headers,
                validated_api_base=api_base,
                timeout=timeout,
            )
        else:
            body = {
                "comm": {"ct": "19", "cv": "1859", "uin": "0"},
                "req": {
                    "method": "DoSearchForQQMusicDesktop",
                    "module": "music.search.SearchCgiService",
                    "param": {
                        "grp": 1,
                        "num_per_page": per_page,
                        "page_num": page,
                        "query": cleaned,
                        "search_type": 0,
                    },
                },
            }
            resp = _scoped_channel_request(
                http_client,
                "POST",
                self.musicu_url,
                channel_id=self.name,
                json_body=body,
                headers={"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"},
                secret_headers=secret_headers,
                timeout=timeout,
            )
        _check_search_response(resp)
        if resp.status_code != 200:
            return [], False
        try:
            payload = resp.json()
        except ValueError:
            return [], False
        if not isinstance(payload, dict):
            return [], False

        raw_items: Any = None
        total = 0
        if isinstance(payload.get("req"), dict):
            song_block = payload["req"].get("data", {}).get("body", {}).get("song", {})
            raw_items = song_block.get("list")
            total = _safe_int(song_block.get("totalnum") or payload["req"].get("data", {}).get("meta", {}).get("sum"))
        elif isinstance(payload.get("data"), dict):
            raw_items = payload["data"].get("list") or payload["data"].get("song", {}).get("list")
            total = _safe_int(payload["data"].get("total") or payload["data"].get("song", {}).get("totalnum"))
        elif isinstance(payload.get("songs"), list):
            raw_items = payload["songs"]
        if not isinstance(raw_items, list):
            return [], False

        candidates: list[ExternalCandidate] = []
        for item in raw_items[:per_page]:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("mid") or item.get("songmid") or item.get("id") or "").strip()
            title = html.unescape(str(item.get("name") or item.get("songname") or item.get("title") or "")).strip()
            if not mid or not title:
                continue
            singers = item.get("singer") or item.get("artists") or []
            artists: list[str] = []
            if isinstance(singers, list):
                for s in singers:
                    if isinstance(s, dict) and s.get("name"):
                        artists.append(str(s["name"]).strip())
                    elif isinstance(s, str) and s.strip():
                        artists.append(s.strip())
            album_obj = item.get("album")
            album = str(album_obj.get("name") or "") if isinstance(album_obj, dict) else str(item.get("albumname") or "")
            duration_ms = _parse_duration_ms(item.get("interval") or item.get("duration") or 0)
            album_mid = str(album_obj.get("mid") or "") if isinstance(album_obj, dict) else str(item.get("albummid") or "")
            cover_url = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else ""
            candidates.append(
                ExternalCandidate(
                    title=title,
                    artists=artists,
                    album=album.strip(),
                    duration_ms=duration_ms,
                    source_provider=self.name,
                    provider_track_id=mid,
                    match_score=1.0,
                    strict_match=False,
                    needs_proxy=True,
                    cover_url=cover_url,
                    extra={"from_custom_endpoint": bool(api_base)},
                )
            )
        has_more = (off + len(raw_items) < total) if total > 0 else (len(raw_items) >= per_page)
        return candidates, has_more

    def search(
        self,
        query: TrackQuery,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
        duration_tolerance_ms: int = 5_000,
    ) -> list[ExternalCandidate]:
        raw, _ = self.search_page(query.search_keyword, http_client, secret=secret, limit=8, offset=0, timeout=timeout)
        scored: list[ExternalCandidate] = []
        for cand in raw:
            strict, score = evaluate_match(query, cand.title, cand.artists, cand.duration_ms, duration_tolerance_ms=duration_tolerance_ms)
            if score < 0.25:
                continue
            scored.append(
                ExternalCandidate(
                    title=cand.title,
                    artists=cand.artists,
                    album=cand.album,
                    duration_ms=cand.duration_ms,
                    source_provider=cand.source_provider,
                    provider_track_id=cand.provider_track_id,
                    match_score=score,
                    strict_match=strict,
                    needs_proxy=cand.needs_proxy,
                    cover_url=cand.cover_url,
                    extra=cand.extra,
                )
            )
        return scored

    def resolve_url(
        self,
        candidate: ExternalCandidate,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> ExternalCandidate | None:
        api_base = _validated_channel_api_base(secret)
        allow_loopback = api_base
        secret_headers, uin = self._build_secret_headers(secret)
        mid = candidate.provider_track_id

        if api_base:
            for qtype, default_fmt, default_br, lvl in (
                ("flac", "flac", 999_000, "lossless"),
                ("320", "mp3", 320_000, "exhigh"),
                ("128", "mp3", 128_000, "standard"),
            ):
                resp = _scoped_channel_request(
                    http_client,
                    "GET",
                    f"{api_base}/song/url",
                    channel_id=self.name,
                    params={"id": mid, "type": qtype},
                    secret_headers=secret_headers,
                    validated_api_base=api_base,
                    timeout=timeout,
                )
                if resp is None or resp.status_code != 200:
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                data_val = payload.get("data")
                url = str(data_val if isinstance(data_val, str) else (data_val.get("url") if isinstance(data_val, dict) else payload.get("url") or "")).strip()
                if not is_safe_external_url(url, allow_loopback=allow_loopback):
                    continue
                fmt = "flac" if ".flac" in url.lower() else default_fmt
                return ExternalCandidate(
                    title=candidate.title,
                    artists=candidate.artists,
                    album=candidate.album,
                    duration_ms=candidate.duration_ms,
                    source_provider=self.name,
                    provider_track_id=mid,
                    url=url,
                    headers={"Referer": "https://y.qq.com/", "User-Agent": _EXTERNAL_USER_AGENT},
                    format=fmt,
                    bitrate=default_br,
                    match_score=candidate.match_score,
                    strict_match=candidate.strict_match,
                    needs_proxy=True,
                    level=lvl if fmt == "flac" else ("exhigh" if default_br >= 300_000 else "standard"),
                    cover_url=candidate.cover_url,
                    extra={**candidate.extra, "from_custom_endpoint": True},
                )
            return None

        # Direct QQ vkey lookup (strictly scoped to *.qq.com).
        file_specs = (
            (f"F000{mid}.flac", "flac", 999_000, "lossless"),
            (f"M800{mid}.mp3", "mp3", 320_000, "exhigh"),
            (f"M500{mid}.mp3", "mp3", 128_000, "standard"),
        )
        for filename, default_fmt, default_br, lvl in file_specs:
            if default_fmt == "flac" and not secret_headers:
                continue
            body = {
                "req_0": {
                    "module": "vkey.GetVkeyServer",
                    "method": "CgiGetVkey",
                    "param": {
                        "filename": [filename],
                        "guid": "10000",
                        "songmid": [mid],
                        "songtype": [0],
                        "uin": uin,
                        "loginflag": 1 if secret_headers else 0,
                        "platform": "20",
                    },
                }
            }
            resp = _scoped_channel_request(
                http_client,
                "POST",
                self.musicu_url,
                channel_id=self.name,
                json_body=body,
                headers={"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"},
                secret_headers=secret_headers,
                timeout=timeout,
            )
            if resp is None or resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            req_data = payload.get("req_0", {}).get("data", {}) if isinstance(payload, dict) else {}
            midurlinfo = req_data.get("midurlinfo") if isinstance(req_data, dict) else None
            sip = req_data.get("sip") if isinstance(req_data, dict) else None
            if not isinstance(midurlinfo, list) or not midurlinfo or not isinstance(midurlinfo[0], dict):
                continue
            purl = str(midurlinfo[0].get("purl") or "").strip()
            if not purl:
                continue
            host_prefix = str(sip[0]) if isinstance(sip, list) and sip else "https://ws.stream.qqmusic.qq.com/"
            url = urllib.parse.urljoin(host_prefix, purl)
            if not is_safe_external_url(url, allow_loopback=False):
                continue
            return ExternalCandidate(
                title=candidate.title,
                artists=candidate.artists,
                album=candidate.album,
                duration_ms=candidate.duration_ms,
                source_provider=self.name,
                provider_track_id=mid,
                url=url,
                headers={"Referer": "https://y.qq.com/", "User-Agent": _EXTERNAL_USER_AGENT},
                format=default_fmt,
                bitrate=default_br,
                match_score=candidate.match_score,
                strict_match=candidate.strict_match,
                needs_proxy=True,
                level=lvl,
                cover_url=candidate.cover_url,
                extra=candidate.extra,
            )
        return None


class GDStudioProvider:
    """Multi-source public music resolver via music-api.gdstudio.xyz (STRICTLY ZERO CREDENTIALS)."""

    name = "gdstudio"
    api_url = "https://music-api.gdstudio.xyz/api.php"

    def __init__(self, sub_sources: tuple[str, ...] = ("kuwo", "kugou", "migu", "joox")):
        self.sub_sources = sub_sources

    def select_sub_sources(self, enabled_providers: list[str] | None) -> list[str]:
        if not enabled_providers:
            return list(self.sub_sources)
        lowered = [str(item).strip().lower() for item in enabled_providers]
        explicit = [src for src in lowered if src in GDSTUDIO_SUB_SOURCES]
        if explicit:
            return explicit
        if "gdstudio" in lowered:
            return list(self.sub_sources)
        return []

    def search_page(
        self,
        keyword: str,
        http_client: httpx.Client,
        *,
        limit: int = 30,
        offset: int = 0,
        timeout: float = 5.0,
        enabled_providers: list[str] | None = None,
    ) -> tuple[list[ExternalCandidate], bool]:
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return [], False
        sub_sources = self.select_sub_sources(enabled_providers)[:3]
        if not sub_sources:
            return [], False
        per_page = max(1, min(100, int(limit or 30)))
        sub_count = max(1, min(30, per_page))
        off = max(0, min(1_000, int(offset or 0)))
        page = (off // per_page) + 1
        headers = sanitize_outbound_headers()
        candidates: list[ExternalCandidate] = []
        any_full_page = False

        for sub_source in sub_sources:
            try:
                response = _scoped_channel_request(
                    http_client,
                    "GET",
                    self.api_url,
                    channel_id="gdstudio",
                    params={
                        "types": "search",
                        "source": sub_source,
                        "name": cleaned,
                        "count": sub_count,
                        "pages": page,
                    },
                    headers=headers,
                    secret_headers=None,
                    timeout=timeout,
                )
                _check_search_response(response)
                if response.status_code != 200:
                    continue
                payload = response.json()
            except ValueError:
                continue
            if not isinstance(payload, list):
                continue
            if len(payload) >= sub_count:
                any_full_page = True
            for item in payload[:sub_count]:
                if not isinstance(item, dict):
                    continue
                track_id = str(item.get("id") or "").strip()
                title = str(item.get("name") or "").strip()
                if not track_id or not title:
                    continue
                raw_artists = item.get("artist")
                if isinstance(raw_artists, list):
                    artists = [str(a).strip() for a in raw_artists if str(a).strip()]
                elif isinstance(raw_artists, str) and raw_artists.strip():
                    artists = [raw_artists.strip()]
                else:
                    artists = []
                duration_ms = _parse_duration_ms(item.get("duration") or item.get("dt") or 0)
                candidates.append(
                    ExternalCandidate(
                        title=title,
                        artists=artists,
                        album=str(item.get("album") or "").strip(),
                        duration_ms=duration_ms,
                        source_provider=f"gdstudio:{sub_source}",
                        provider_track_id=track_id,
                        match_score=1.0,
                        strict_match=False,
                        extra={"sub_source": sub_source},
                    )
                )
        return candidates, any_full_page

    def search(
        self,
        query: TrackQuery,
        http_client: httpx.Client,
        *,
        timeout: float = 5.0,
        enabled_providers: list[str] | None = None,
        duration_tolerance_ms: int = 5_000,
    ) -> list[ExternalCandidate]:
        raw, _ = self.search_page(
            query.search_keyword,
            http_client,
            limit=18,
            offset=0,
            timeout=timeout,
            enabled_providers=enabled_providers,
        )
        scored: list[ExternalCandidate] = []
        for cand in raw:
            strict, score = evaluate_match(
                query,
                cand.title,
                cand.artists,
                cand.duration_ms,
                duration_tolerance_ms=duration_tolerance_ms,
            )
            if score < 0.25:
                continue
            scored.append(
                ExternalCandidate(
                    title=cand.title,
                    artists=cand.artists,
                    album=cand.album,
                    duration_ms=cand.duration_ms,
                    source_provider=cand.source_provider,
                    provider_track_id=cand.provider_track_id,
                    match_score=score,
                    strict_match=strict,
                    extra=cand.extra,
                )
            )
        return scored

    def resolve_url(
        self,
        candidate: ExternalCandidate,
        http_client: httpx.Client,
        *,
        timeout: float = 5.0,
    ) -> ExternalCandidate | None:
        sub_source = str(
            candidate.extra.get("sub_source")
            or (candidate.source_provider.split(":", 1)[1] if ":" in candidate.source_provider else "")
            or "kuwo"
        )
        headers = sanitize_outbound_headers()
        for br in (999, 320):
            try:
                response = _scoped_channel_request(
                    http_client,
                    "GET",
                    self.api_url,
                    channel_id="gdstudio",
                    params={
                        "types": "url",
                        "source": sub_source,
                        "id": candidate.provider_track_id,
                        "br": br,
                    },
                    headers=headers,
                    secret_headers=None,
                    timeout=timeout,
                )
                if response is None or response.status_code != 200:
                    continue
                payload = response.json()
            except (httpx.HTTPError, ValueError, TimeoutError, OSError):
                continue
            if not isinstance(payload, dict):
                continue
            url = str(payload.get("url") or "").strip()
            if not is_safe_external_url(url, allow_loopback=False):
                continue
            raw_br = _safe_int(payload.get("br"))
            bitrate = raw_br * 1000 if 0 < raw_br <= 4000 else raw_br
            size = _safe_int(payload.get("size"))
            fmt = "flac" if br == 999 or raw_br >= 740 or ".flac" in url.lower() else "mp3"
            return ExternalCandidate(
                title=candidate.title,
                artists=candidate.artists,
                album=candidate.album,
                duration_ms=candidate.duration_ms,
                source_provider=candidate.source_provider,
                provider_track_id=candidate.provider_track_id,
                url=url,
                headers={},
                format=fmt,
                bitrate=bitrate,
                sample_rate=candidate.sample_rate,
                size=size,
                match_score=candidate.match_score,
                strict_match=candidate.strict_match,
                needs_proxy=not url.startswith("https://"),
                level="lossless" if fmt == "flac" else ("exhigh" if bitrate >= 300_000 else "standard"),
                extra=candidate.extra,
            )
        return None


class KuwoProvider:
    """Direct public Kuwo search + mobi URL lookup (optional strictly-scoped credentials)."""

    name = "kuwo"
    search_url = "https://search.kuwo.cn/r.s"
    convert_url = "https://antiserver.kuwo.cn/anti.s"

    def search_page(
        self,
        keyword: str,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        limit: int = 30,
        offset: int = 0,
        timeout: float = 5.0,
    ) -> tuple[list[ExternalCandidate], bool]:
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return [], False
        per_page = max(1, min(100, int(limit or 30)))
        off = max(0, min(1_000, int(offset or 0)))
        page_index = off // per_page  # Kuwo pn is 0-based
        api_base = _validated_channel_api_base(secret)
        secret_headers = {"Cookie": str(secret["cookie"]).strip()} if isinstance(secret, dict) and secret.get("cookie") else {}
        response = _scoped_channel_request(
            http_client,
            "GET",
            f"{api_base}/search" if api_base else self.search_url,
            channel_id=self.name,
            params={
                "all": cleaned,
                "ft": "music",
                "rformat": "json",
                "encoding": "utf8",
                "rn": per_page,
                "pn": page_index,
                "vipver": 1,
            },
            headers={"Referer": "https://www.kuwo.cn/"},
            secret_headers=secret_headers,
            validated_api_base=api_base,
            timeout=timeout,
        )
        _check_search_response(response)
        if response.status_code != 200:
            return [], False
        try:
            payload = _parse_relaxed_json(response.text)
        except ValueError:
            return [], False
        items = payload.get("abslist") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return [], False
        candidates: list[ExternalCandidate] = []
        for item in items[:per_page]:
            if not isinstance(item, dict):
                continue
            raw_rid = str(item.get("MUSICRID") or "").strip()
            rid = raw_rid.removeprefix("MUSIC_")
            title = html.unescape(str(item.get("SONGNAME") or item.get("NAME") or "")).strip()
            artist_raw = html.unescape(str(item.get("ARTIST") or "")).strip()
            if not rid or not title:
                continue
            artists = [part.strip() for part in re.split(r"[&/,、]", artist_raw) if part.strip()]
            duration_ms = _safe_int(item.get("DURATION")) * 1000
            candidates.append(
                ExternalCandidate(
                    title=title,
                    artists=artists,
                    album=html.unescape(str(item.get("ALBUM") or "")).strip(),
                    duration_ms=duration_ms,
                    source_provider=self.name,
                    provider_track_id=rid,
                    match_score=1.0,
                    strict_match=False,
                    extra={"from_custom_endpoint": bool(api_base)},
                )
            )
        total = _safe_int(payload.get("TOTAL") if isinstance(payload, dict) else 0)
        has_more = (off + len(items) < total) if total > 0 else (len(items) >= per_page)
        return candidates, has_more

    def search(
        self,
        query: TrackQuery,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
        duration_tolerance_ms: int = 5_000,
    ) -> list[ExternalCandidate]:
        raw, _ = self.search_page(query.search_keyword, http_client, secret=secret, limit=8, offset=0, timeout=timeout)
        scored: list[ExternalCandidate] = []
        for cand in raw:
            strict, score = evaluate_match(
                query,
                cand.title,
                cand.artists,
                cand.duration_ms,
                duration_tolerance_ms=duration_tolerance_ms,
            )
            if score < 0.25:
                continue
            scored.append(
                ExternalCandidate(
                    title=cand.title,
                    artists=cand.artists,
                    album=cand.album,
                    duration_ms=cand.duration_ms,
                    source_provider=cand.source_provider,
                    provider_track_id=cand.provider_track_id,
                    match_score=score,
                    strict_match=strict,
                    extra=cand.extra,
                )
            )
        return scored

    def resolve_url(
        self,
        candidate: ExternalCandidate,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> ExternalCandidate | None:
        api_base = _validated_channel_api_base(secret)
        allow_loopback = api_base
        secret_headers = {"Cookie": str(secret["cookie"]).strip()} if isinstance(secret, dict) and secret.get("cookie") else {}
        for fmt in ("flac", "mp3"):
            response = _scoped_channel_request(
                http_client,
                "GET",
                f"{api_base}/song/url" if api_base else self.convert_url,
                channel_id=self.name,
                params={
                    "type": "convert_url3",
                    "rid": candidate.provider_track_id,
                    "format": fmt,
                },
                headers={"Referer": "https://www.kuwo.cn/"},
                secret_headers=secret_headers,
                validated_api_base=api_base,
                timeout=timeout,
            )
            if response is None or response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            url = str(payload.get("url") or "").strip()
            if not is_safe_external_url(url, allow_loopback=allow_loopback):
                continue
            actual_fmt = "flac" if ".flac" in url.lower() else ("mp3" if ".mp3" in url.lower() else fmt)
            return ExternalCandidate(
                title=candidate.title,
                artists=candidate.artists,
                album=candidate.album,
                duration_ms=candidate.duration_ms,
                source_provider=self.name,
                provider_track_id=candidate.provider_track_id,
                url=url,
                headers={"Referer": "https://www.kuwo.cn/", "User-Agent": _EXTERNAL_USER_AGENT},
                format=actual_fmt,
                bitrate=999_000 if actual_fmt == "flac" else 320_000,
                match_score=candidate.match_score,
                strict_match=candidate.strict_match,
                needs_proxy=True,
                level="lossless" if actual_fmt == "flac" else "exhigh",
                extra=candidate.extra,
            )
        return None


class BilibiliProvider:
    """Public/SESSDATA-upgraded Bilibili audio resolver (SESSDATA strictly scoped to *.bilibili.com)."""

    name = "bilibili"
    search_url = "https://api.bilibili.com/x/web-interface/search/type"
    pagelist_url = "https://api.bilibili.com/x/player/pagelist"
    playurl_url = "https://api.bilibili.com/x/player/playurl"

    def _build_secret_headers(self, secret: dict[str, Any] | None) -> dict[str, str]:
        if not isinstance(secret, dict):
            return {}
        sessdata = str(secret.get("sessdata") or "").strip()
        cookie = str(secret.get("cookie") or "").strip()
        if cookie:
            if sessdata and "SESSDATA=" not in cookie:
                return {"Cookie": f"SESSDATA={sessdata}; {cookie}"}
            return {"Cookie": cookie}
        if sessdata:
            return {"Cookie": f"SESSDATA={sessdata}"}
        return {}

    def search_page(
        self,
        keyword: str,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        limit: int = 30,
        offset: int = 0,
        timeout: float = 5.0,
    ) -> tuple[list[ExternalCandidate], bool]:
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return [], False
        per_page = max(1, min(50, int(limit or 30)))
        off = max(0, min(1_000, int(offset or 0)))
        page = (off // per_page) + 1
        secret_headers = self._build_secret_headers(secret)
        response = _scoped_channel_request(
            http_client,
            "GET",
            self.search_url,
            channel_id=self.name,
            params={"search_type": "video", "keyword": cleaned, "page": page, "page_size": per_page},
            headers={"Referer": "https://www.bilibili.com/"},
            secret_headers=secret_headers,
            timeout=timeout,
        )
        _check_search_response(response)
        if response.status_code != 200:
            return [], False
        try:
            payload = response.json()
        except ValueError:
            return [], False
        data_obj = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
        results = data_obj.get("result")
        if not isinstance(results, list):
            return [], False
        candidates: list[ExternalCandidate] = []
        for item in results[:per_page]:
            if not isinstance(item, dict):
                continue
            bvid = str(item.get("bvid") or "").strip()
            raw_title = _HTML_TAG_RE.sub("", html.unescape(str(item.get("title") or ""))).strip()
            author = html.unescape(str(item.get("author") or "")).strip()
            if not bvid or not raw_title:
                continue
            duration_ms = _parse_colon_duration_ms(str(item.get("duration") or ""))
            pic = str(item.get("pic") or "").strip()
            if pic.startswith("//"):
                pic = "https:" + pic
            candidates.append(
                ExternalCandidate(
                    title=raw_title,
                    artists=[author] if author else [],
                    album="Bilibili",
                    duration_ms=duration_ms,
                    source_provider=self.name,
                    provider_track_id=bvid,
                    match_score=1.0,
                    strict_match=False,
                    needs_proxy=True,
                    cover_url=pic,
                )
            )
        total = _safe_int(data_obj.get("numResults"))
        has_more = (off + len(results) < total) if total > 0 else (len(results) >= per_page)
        return candidates, has_more

    def search(
        self,
        query: TrackQuery,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
        duration_tolerance_ms: int = 5_000,
    ) -> list[ExternalCandidate]:
        raw, _ = self.search_page(query.search_keyword, http_client, secret=secret, limit=8, offset=0, timeout=timeout)
        scored: list[ExternalCandidate] = []
        for cand in raw:
            strict, score = evaluate_match(
                query,
                cand.title,
                cand.artists,
                cand.duration_ms,
                duration_tolerance_ms=duration_tolerance_ms,
                allow_title_embedded_artist=True,
            )
            if score < 0.25:
                continue
            scored.append(
                ExternalCandidate(
                    title=cand.title,
                    artists=cand.artists,
                    album=cand.album,
                    duration_ms=cand.duration_ms,
                    source_provider=cand.source_provider,
                    provider_track_id=cand.provider_track_id,
                    match_score=score,
                    strict_match=strict,
                    needs_proxy=cand.needs_proxy,
                    cover_url=cand.cover_url,
                )
            )
        return scored

    def resolve_url(
        self,
        candidate: ExternalCandidate,
        http_client: httpx.Client,
        *,
        secret: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> ExternalCandidate | None:
        secret_headers = self._build_secret_headers(secret)
        page_resp = _scoped_channel_request(
            http_client,
            "GET",
            self.pagelist_url,
            channel_id=self.name,
            params={"bvid": candidate.provider_track_id},
            headers={"Referer": "https://www.bilibili.com/"},
            secret_headers=secret_headers,
            timeout=timeout,
        )
        if page_resp is None or page_resp.status_code != 200:
            return None
        try:
            page_payload = page_resp.json()
        except ValueError:
            return None
        pages = page_payload.get("data") if isinstance(page_payload, dict) else None
        if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
            return None
        cid = pages[0].get("cid")
        if not cid:
            return None

        # fnval=4048 requests DASH + Hi-Res FLAC (30251) + Dolby (30250) when SESSDATA is present.
        play_resp = _scoped_channel_request(
            http_client,
            "GET",
            self.playurl_url,
            channel_id=self.name,
            params={"bvid": candidate.provider_track_id, "cid": cid, "fnval": 4048 if secret_headers else 16},
            headers={"Referer": "https://www.bilibili.com/"},
            secret_headers=secret_headers,
            timeout=timeout,
        )
        if play_resp is None or play_resp.status_code != 200:
            return None
        try:
            play_payload = play_resp.json()
        except ValueError:
            return None
        dash = (
            play_payload.get("data", {}).get("dash")
            if isinstance(play_payload, dict) and isinstance(play_payload.get("data"), dict)
            else None
        )
        if not isinstance(dash, dict):
            return None
        stream_item: dict[str, Any] | None = None
        fmt = "m4a"
        lvl = "standard"
        flac_block = dash.get("flac")
        if isinstance(flac_block, dict) and isinstance(flac_block.get("audio"), dict):
            stream_item = flac_block["audio"]
            fmt = "flac"
            lvl = "hires" if _safe_int(stream_item.get("id")) == 30251 else "lossless"
        else:
            audio_list = dash.get("audio")
            if isinstance(audio_list, list):
                valid_streams = [
                    entry
                    for entry in audio_list
                    if isinstance(entry, dict) and (entry.get("baseUrl") or entry.get("base_url"))
                ]
                if valid_streams:
                    # Prefer 30251 Hi-Res FLAC if listed in audio array, else highest bandwidth.
                    hires_entry = next((e for e in valid_streams if _safe_int(e.get("id")) == 30251), None)
                    if hires_entry is not None:
                        stream_item = hires_entry
                        fmt = "flac"
                        lvl = "hires"
                    else:
                        stream_item = max(valid_streams, key=lambda entry: _safe_int(entry.get("bandwidth")))
                        lvl = "exhigh" if _safe_int(stream_item.get("bandwidth")) >= 190_000 else "standard"
        if not stream_item:
            return None
        url = str(stream_item.get("baseUrl") or stream_item.get("base_url") or "").strip()
        if not is_safe_external_url(url, allow_loopback=False):
            return None
        # NEVER attach SESSDATA/Cookie to CDN audio stream headers!
        return ExternalCandidate(
            title=candidate.title,
            artists=candidate.artists,
            album=candidate.album,
            duration_ms=candidate.duration_ms,
            source_provider=self.name,
            provider_track_id=candidate.provider_track_id,
            url=url,
            headers={"Referer": "https://www.bilibili.com/", "User-Agent": _EXTERNAL_USER_AGENT},
            format=fmt,
            bitrate=_safe_int(stream_item.get("bandwidth")),
            match_score=candidate.match_score,
            strict_match=candidate.strict_match,
            needs_proxy=True,
            level=lvl,
            cover_url=candidate.cover_url,
        )


class CustomEndpointProvider:
    """Unified HTTP Resolver Contract v2 (/manifest, /search, /resolve, /lyric) with v1 fallback (ZERO NetEase credentials)."""

    name = "custom_endpoint"

    @staticmethod
    def _auth_headers(api_key: str = "") -> dict[str, str]:
        cleaned = str(api_key or "").strip()
        if not cleaned:
            return {}
        return {"X-API-Key": cleaned, "X-Resolver-Token": cleaned}

    def _request_v2_or_v1(
        self,
        http_client: httpx.Client,
        validated_base: str,
        subpath: str,
        *,
        json_body: dict[str, Any],
        get_params: dict[str, Any],
        secret_headers: dict[str, str],
        timeout: float,
    ) -> httpx.Response | None:
        base = validated_base.rstrip("/")
        # If the user already configured a path ending with /search or /resolve, call it directly; otherwise try v2 subpath first.
        if base.endswith(subpath):
            urls_to_try = [base]
        else:
            urls_to_try = [f"{base}{subpath}", validated_base]
        last_resp: httpx.Response | None = None
        for idx, target_url in enumerate(urls_to_try):
            resp = _scoped_channel_request(
                http_client,
                "POST",
                target_url,
                channel_id="custom",
                json_body=json_body,
                headers={"Accept": "application/json"},
                secret_headers=secret_headers,
                validated_api_base=validated_base,
                timeout=timeout,
            )
            if resp is not None and resp.status_code == 405:
                resp = _scoped_channel_request(
                    http_client,
                    "GET",
                    target_url,
                    channel_id="custom",
                    params=get_params,
                    headers={"Accept": "application/json"},
                    secret_headers=secret_headers,
                    validated_api_base=validated_base,
                    timeout=timeout,
                )
            last_resp = resp
            if resp is not None and resp.status_code == 200:
                return resp
            # On 404 from v2 subpath, fall back cleanly to v1 root endpoint.
            if idx == 0 and len(urls_to_try) > 1 and resp is not None and resp.status_code in {404, 405, 501}:
                continue
            break
        return last_resp

    @staticmethod
    def _sanitize_provider(raw: Any, default: str) -> str:
        fallback = str(default or "custom").strip().lower() or "custom"
        cleaned = str(raw or "").strip().lower()
        if not cleaned or not re.fullmatch(r"[a-z0-9_.-]{1,48}(?::[a-z0-9_.-]{1,48})?", cleaned):
            return fallback
        if ":" in cleaned:
            head, tail = cleaned.split(":", 1)
            if head in {"local", "netease", ".", ".."} or tail in {".", ".."}:
                return fallback
        elif cleaned in {"local", ".", ".."}:
            return fallback
        return cleaned

    def search_page(
        self,
        endpoint_url: str,
        keyword: str,
        http_client: httpx.Client,
        *,
        api_key: str = "",
        limit: int = 30,
        offset: int = 0,
        timeout: float = 5.0,
        resolver_id: str = "",
    ) -> tuple[list[ExternalCandidate], bool]:
        cleaned = str(keyword or "").strip()
        if not cleaned:
            return [], False
        try:
            validated = validate_custom_external_endpoint(endpoint_url)
        except ValueError:
            return [], False
        if not validated:
            return [], False
        per_page = max(1, min(100, int(limit or 30)))
        off = max(0, min(1_000, int(offset or 0)))
        secret_headers = self._auth_headers(api_key)
        body = {
            "query": cleaned,
            "title": cleaned,
            "artists": [],
            "album": "",
            "duration_ms": 0,
            "limit": per_page,
            "offset": off,
        }
        response = self._request_v2_or_v1(
            http_client,
            validated,
            "/search",
            json_body=body,
            get_params={"q": cleaned, "title": cleaned, "limit": per_page, "offset": off},
            secret_headers=secret_headers,
            timeout=timeout,
        )
        _check_search_response(response)
        if response.status_code != 200:
            return [], False
        try:
            payload = response.json()
        except ValueError:
            return [], False

        raw_items: list[dict[str, Any]] = []
        explicit_has_more: bool | None = None
        if isinstance(payload, dict):
            if isinstance(payload.get("has_more"), bool):
                explicit_has_more = payload["has_more"]
            if isinstance(payload.get("candidates"), list):
                raw_items = [item for item in payload["candidates"] if isinstance(item, dict)]
            elif isinstance(payload.get("tracks"), list):
                raw_items = [item for item in payload["tracks"] if isinstance(item, dict)]
            elif payload.get("url"):
                raw_items = [payload]
        elif isinstance(payload, list):
            raw_items = [item for item in payload if isinstance(item, dict)]

        allow_loopback = validated
        results: list[ExternalCandidate] = []
        default_prov = resolver_id or self.name
        for index, item in enumerate(raw_items[:per_page]):
            url = str(item.get("url") or "").strip()
            if url and not is_safe_external_url(url, allow_loopback=allow_loopback):
                continue
            cand_title = str(item.get("title") or item.get("name") or "").strip()
            if not cand_title:
                continue
            raw_artists = item.get("artists")
            if isinstance(raw_artists, list):
                cand_artists = [str(a).strip() for a in raw_artists if str(a).strip()]
            elif item.get("artist"):
                cand_artists = [str(item["artist"]).strip()]
            else:
                cand_artists = []
            cand_duration = _parse_duration_ms(item.get("duration_ms") or item.get("duration") or 0)
            item_headers = sanitize_outbound_headers(item.get("headers") if isinstance(item.get("headers"), dict) else None)
            extra_headers = {k: v for k, v in item_headers.items() if k.lower() != "user-agent"}
            results.append(
                ExternalCandidate(
                    title=cand_title,
                    artists=cand_artists,
                    album=str(item.get("album") or "").strip(),
                    duration_ms=cand_duration,
                    source_provider=self._sanitize_provider(item.get("provider"), default_prov),
                    provider_track_id=str(item.get("id") or f"custom-{off + index}"),
                    url=url,
                    headers=extra_headers,
                    format=str(item.get("format") or "").strip().lower(),
                    bitrate=_safe_int(item.get("bitrate")),
                    sample_rate=_safe_int(item.get("sample_rate")),
                    size=_safe_int(item.get("size")),
                    match_score=1.0,
                    strict_match=False,
                    needs_proxy=bool(extra_headers) or (bool(url) and not url.startswith("https://")),
                    cover_url=str(item.get("cover_url") or "").strip(),
                    extra={
                        "from_custom_endpoint": True,
                        "resolver_url": validated,
                        "resolver_id": resolver_id,
                        "isrc": str(item.get("isrc") or ""),
                        "mbid": str(item.get("mbid") or ""),
                    },
                )
            )
        has_more = explicit_has_more if explicit_has_more is not None else (len(raw_items) >= per_page)
        return results, has_more

    def search_and_resolve(
        self,
        endpoint_url: str,
        query: TrackQuery,
        http_client: httpx.Client,
        *,
        api_key: str = "",
        timeout: float = 5.0,
        duration_tolerance_ms: int = 5_000,
        resolver_id: str = "",
        track_id: str = "",
    ) -> list[ExternalCandidate]:
        try:
            validated = validate_custom_external_endpoint(endpoint_url)
        except ValueError:
            return []
        if not validated:
            return []
        secret_headers = self._auth_headers(api_key)
        cleaned_track_id = str(track_id or "").strip()
        body: dict[str, Any] = {
            "title": query.title,
            "artists": list(query.artists),
            "album": query.album,
            "duration_ms": query.duration_ms,
        }
        get_params: dict[str, Any] = {
            "title": query.title,
            "artist": query.primary_artist,
            "album": query.album,
            "duration_ms": query.duration_ms,
        }
        if cleaned_track_id:
            body["id"] = cleaned_track_id
            get_params["id"] = cleaned_track_id
        response = self._request_v2_or_v1(
            http_client,
            validated,
            "/resolve",
            json_body=body,
            get_params=get_params,
            secret_headers=secret_headers,
            timeout=timeout,
        )
        if response is None or response.status_code != 200:
            return []
        try:
            payload = response.json()
        except ValueError:
            return []

        raw_items: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            if isinstance(payload.get("candidates"), list):
                raw_items = [item for item in payload["candidates"] if isinstance(item, dict)]
            elif isinstance(payload.get("tracks"), list):
                raw_items = [item for item in payload["tracks"] if isinstance(item, dict)]
            elif payload.get("url"):
                raw_items = [payload]
        elif isinstance(payload, list):
            raw_items = [item for item in payload if isinstance(item, dict)]

        allow_loopback = validated
        results: list[ExternalCandidate] = []
        default_prov = resolver_id or self.name
        for index, item in enumerate(raw_items[:8]):
            url = str(item.get("url") or "").strip()
            if not is_safe_external_url(url, allow_loopback=allow_loopback):
                continue
            cand_title = str(item.get("title") or query.title).strip()
            raw_artists = item.get("artists")
            if isinstance(raw_artists, list):
                cand_artists = [str(a).strip() for a in raw_artists if str(a).strip()]
            elif item.get("artist"):
                cand_artists = [str(item["artist"]).strip()]
            else:
                cand_artists = list(query.artists)
            cand_duration = _parse_duration_ms(item.get("duration_ms") or item.get("duration") or query.duration_ms)
            strict, score = evaluate_match(
                query,
                cand_title,
                cand_artists,
                cand_duration,
                duration_tolerance_ms=duration_tolerance_ms,
            )
            item_headers = sanitize_outbound_headers(item.get("headers") if isinstance(item.get("headers"), dict) else None)
            extra_headers = {k: v for k, v in item_headers.items() if k.lower() != "user-agent"}
            results.append(
                ExternalCandidate(
                    title=cand_title,
                    artists=cand_artists,
                    album=str(item.get("album") or query.album).strip(),
                    duration_ms=cand_duration,
                    source_provider=self._sanitize_provider(item.get("provider"), default_prov),
                    provider_track_id=str(item.get("id") or cleaned_track_id or f"custom-{index}"),
                    url=url,
                    headers=extra_headers,
                    format=str(item.get("format") or "").strip().lower(),
                    bitrate=_safe_int(item.get("bitrate")),
                    sample_rate=_safe_int(item.get("sample_rate")),
                    size=_safe_int(item.get("size")),
                    match_score=score,
                    strict_match=strict,
                    needs_proxy=bool(extra_headers) or not url.startswith("https://"),
                    cover_url=str(item.get("cover_url") or "").strip(),
                    extra={
                        "from_custom_endpoint": True,
                        "resolver_url": validated,
                        "resolver_id": resolver_id,
                        "replaygain_track_gain_db": item.get("replaygain_track_gain_db"),
                        "replaygain_track_peak": item.get("replaygain_track_peak"),
                    },
                )
            )
        return results


class ExternalAudioResolver:
    """Multi-channel coordinator for local audio files & strictly isolated third-party providers."""

    def __init__(
        self,
        *,
        db: Any = None,
        credential_vault: Any = None,
        timeout: float = 5.5,
        http_client: httpx.Client | None = None,
        probe_fn: Callable[..., StreamProbeResult] | None = None,
    ):
        self.timeout = min(6.0, max(1.0, float(timeout)))
        self.credential_vault = credential_vault
        self.local_scanner = LocalLibraryScanner(db=db)
        self.kugou_lite = KugouLiteProvider()
        self.migu = MiguProvider()
        self.qq = QQMusicProvider()
        self.gdstudio = GDStudioProvider()
        self.kuwo = KuwoProvider()
        self.bilibili = BilibiliProvider()
        self.custom = CustomEndpointProvider()
        self.opensubsonic = OpenSubsonicProvider()
        self.webdav = WebDAVProvider()
        self.health = ChannelHealthTracker()
        self.resolver_registry: Any = None
        self.sidecar_manager: Any = None
        self._client_lock = threading.Lock()
        self._http_client = http_client
        self._owns_client = http_client is None
        self._probe_fn = probe_fn or probe_audio_url

    def _get_http_client(self) -> httpx.Client:
        with self._client_lock:
            if self._http_client is None:
                self._http_client = httpx.Client(
                    timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 3.5)),
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8, keepalive_expiry=20.0),
                    follow_redirects=False,
                    trust_env=False,
                    cookies=NoOpCookies(),
                    http2=False,
                )
                self._owns_client = True
            return self._http_client

    def close(self) -> None:
        with self._client_lock:
            if self._http_client is not None and self._owns_client:
                try:
                    self._http_client.close()
                except Exception:
                    pass
                self._http_client = None

    def _channel_secret(self, channel_id: str) -> dict[str, Any] | None:
        """Fetch ONLY the secret dict belonging strictly to ``channel_id``."""
        if channel_id == "gdstudio" or self.credential_vault is None:
            return None
        getter = getattr(self.credential_vault, "get_channel_secret", None)
        if not callable(getter):
            return None
        try:
            secret = getter(channel_id)
            return dict(secret) if isinstance(secret, dict) else None
        except Exception:
            return None

    def _is_channel_enabled(self, channel_id: str) -> bool:
        if self.credential_vault is None:
            return True
        checker = getattr(self.credential_vault, "is_channel_enabled", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(channel_id))
        except Exception:
            return True

    def _effective_custom_api(self, custom_api: str = "") -> tuple[str, str]:
        cleaned = (custom_api or "").strip()
        custom_secret = self._channel_secret("custom") or {}
        vault_api = str(custom_secret.get("api_base") or "").strip()
        api_key = str(custom_secret.get("api_key") or "").strip()
        return (cleaned or vault_api), api_key

    def scan_local_dirs(self, dirs: list[str | Path]) -> dict[str, Any]:
        return self.local_scanner.scan(dirs)

    def list_local_tracks(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.local_scanner.records()]

    def match_local(self, song: Song, duration_tolerance_ms: int = 5_000) -> LocalTrackMatch | None:
        return self.local_scanner.match(TrackQuery.from_song(song), duration_tolerance_ms=duration_tolerance_ms)

    def _search_single_provider(
        self,
        channel: str,
        query: TrackQuery,
        client: httpx.Client,
        *,
        enabled_providers: list[str],
        custom_api: str = "",
        custom_api_key: str = "",
        duration_tolerance_ms: int = 5_000,
    ) -> list[ExternalCandidate]:
        if channel == "kugou_lite":
            return self.kugou_lite.search(
                query,
                client,
                secret=self._channel_secret("kugou_lite"),
                timeout=self.timeout,
                duration_tolerance_ms=duration_tolerance_ms,
            )
        if channel == "migu":
            return self.migu.search(
                query,
                client,
                secret=self._channel_secret("migu"),
                timeout=self.timeout,
                duration_tolerance_ms=duration_tolerance_ms,
            )
        if channel == "qq":
            return self.qq.search(
                query,
                client,
                secret=self._channel_secret("qq"),
                timeout=self.timeout,
                duration_tolerance_ms=duration_tolerance_ms,
            )
        if channel == "kuwo":
            return self.kuwo.search(
                query,
                client,
                secret=self._channel_secret("kuwo"),
                timeout=self.timeout,
                duration_tolerance_ms=duration_tolerance_ms,
            )
        if channel == "bilibili":
            return self.bilibili.search(
                query,
                client,
                secret=self._channel_secret("bilibili"),
                timeout=self.timeout,
                duration_tolerance_ms=duration_tolerance_ms,
            )
        if channel == "gdstudio":
            return self.gdstudio.search(
                query,
                client,
                timeout=self.timeout,
                enabled_providers=enabled_providers,
                duration_tolerance_ms=duration_tolerance_ms,
            )
        if channel == "custom" and custom_api:
            return self.custom.search_and_resolve(
                custom_api,
                query,
                client,
                api_key=custom_api_key,
                timeout=self.timeout,
                duration_tolerance_ms=duration_tolerance_ms,
            )
        if channel == "opensubsonic":
            secret = self._channel_secret("opensubsonic") or {}
            if not secret.get("api_base"):
                return []
            try:
                tracks, _ = self.opensubsonic.search_tracks(query.search_keyword, secret, client, limit=10, timeout=self.timeout)
            except Exception:
                return []
            out: list[ExternalCandidate] = []
            for t in tracks:
                strict, sc = evaluate_match(query, t["title"], t["artists"], t["duration_ms"], duration_tolerance_ms=duration_tolerance_ms)
                if sc < 0.25:
                    continue
                out.append(
                    ExternalCandidate(
                        title=t["title"],
                        artists=list(t["artists"]),
                        album=t["album"],
                        duration_ms=t["duration_ms"],
                        source_provider="opensubsonic",
                        provider_track_id=t["external_id"],
                        format=str(t.get("format") or ""),
                        bitrate=_safe_int(t.get("bitrate")),
                        sample_rate=_safe_int(t.get("sample_rate")),
                        size=_safe_int(t.get("size")),
                        match_score=sc,
                        strict_match=strict,
                        needs_proxy=True,
                        cover_url=str(t.get("cover_url") or ""),
                        extra={
                            "replaygain_track_gain_db": t.get("replaygain_track_gain_db"),
                            "replaygain_track_peak": t.get("replaygain_track_peak"),
                        },
                    )
                )
            return out
        if channel == "webdav":
            tracks, _ = self.webdav.search_tracks(query.search_keyword, limit=10)
            out = []
            for t in tracks:
                strict, sc = evaluate_match(query, t["title"], t["artists"], t["duration_ms"], duration_tolerance_ms=duration_tolerance_ms)
                if sc < 0.25:
                    continue
                out.append(
                    ExternalCandidate(
                        title=t["title"],
                        artists=list(t["artists"]),
                        album=t["album"],
                        duration_ms=t["duration_ms"],
                        source_provider="webdav",
                        provider_track_id=t["external_id"],
                        format=str(t.get("format") or ""),
                        size=_safe_int(t.get("size")),
                        match_score=sc,
                        strict_match=strict,
                        needs_proxy=True,
                        extra={"webdav_url": t.get("webdav_url", "")},
                    )
                )
            return out
        return []

    def _active_v2_resolvers(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if self.resolver_registry is not None and hasattr(self.resolver_registry, "list_enabled_secrets"):
            try:
                entries.extend(self.resolver_registry.list_enabled_secrets())
            except Exception:
                pass
        if self.sidecar_manager is not None and hasattr(self.sidecar_manager, "active_resolver_config"):
            try:
                sc = self.sidecar_manager.active_resolver_config()
                if isinstance(sc, dict) and sc.get("url"):
                    entries.append(sc)
            except Exception:
                pass
        return entries

    def search_candidates(
        self,
        song: Song,
        *,
        enabled_providers: list[str] | None = None,
        custom_api: str = "",
        duration_tolerance_ms: int = 5_000,
        limit: int = 12,
    ) -> list[ExternalCandidate]:
        query = TrackQuery.from_song(song)
        if not query.title:
            return []
        providers = (
            [str(p).strip().lower() for p in enabled_providers if str(p).strip()]
            if enabled_providers is not None
            else list(DEFAULT_EXTERNAL_PROVIDERS)
        )
        eff_custom_api, custom_key = self._effective_custom_api(custom_api)
        client = self._get_http_client()

        active_channels: list[str] = []
        if eff_custom_api and self._is_channel_enabled("custom") and self.health.allow_request("custom"):
            active_channels.append("custom")
        for ch in ("opensubsonic", "webdav", "kugou_lite", "migu", "qq", "kuwo", "bilibili"):
            if ch in providers and self._is_channel_enabled(ch) and self.health.allow_request(ch):
                active_channels.append(ch)
        if any(p in {"gdstudio", *GDSTUDIO_SUB_SOURCES} for p in providers) and self._is_channel_enabled("gdstudio") and self.health.allow_request("gdstudio"):
            active_channels.append("gdstudio")

        def _run_guarded(ch: str) -> list[ExternalCandidate]:
            started = time.monotonic()
            try:
                res = self._search_single_provider(
                    ch,
                    query,
                    client,
                    enabled_providers=providers,
                    custom_api=eff_custom_api,
                    custom_api_key=custom_key,
                    duration_tolerance_ms=duration_tolerance_ms,
                )
                self.health.record_success(ch, (time.monotonic() - started) * 1000)
                return res
            except Exception as exc:
                self.health.record_failure(ch, str(exc), latency_ms=(time.monotonic() - started) * 1000)
                return []

        candidates: list[ExternalCandidate] = []
        if len(active_channels) <= 1:
            for ch in active_channels:
                candidates.extend(_run_guarded(ch))
        else:
            futures = [_SEARCH_EXECUTOR.submit(_run_guarded, ch) for ch in active_channels]
            done, _not_done = wait(futures, timeout=min(5.5, self.timeout + 1.0))
            for fut in done:
                try:
                    candidates.extend(fut.result())
                except Exception:
                    continue

        # Also query registered HTTP Resolver v2 endpoints and running Sidecar bridge.
        for res_cfg in self._active_v2_resolvers():
            rid = str(res_cfg.get("id") or "resolver")
            if not self.health.allow_request(f"resolver:{rid}"):
                continue
            started = time.monotonic()
            try:
                cands = self.custom.search_and_resolve(
                    str(res_cfg["url"]),
                    query,
                    client,
                    api_key=str(res_cfg.get("api_key") or ""),
                    timeout=self.timeout,
                    duration_tolerance_ms=duration_tolerance_ms,
                    resolver_id=rid,
                )
                self.health.record_success(f"resolver:{rid}", (time.monotonic() - started) * 1000)
                candidates.extend(cands)
            except Exception as exc:
                self.health.record_failure(f"resolver:{rid}", str(exc), latency_ms=(time.monotonic() - started) * 1000)

        candidates.sort(
            key=lambda c: (
                c.strict_match,
                score_audio_quality(c.format, c.level, c.bitrate, c.sample_rate, c.bit_depth),
                c.match_score,
            ),
            reverse=True,
        )
        return candidates[:limit]

    def resolve_candidate(
        self,
        candidate: ExternalCandidate,
        *,
        verify_stream: bool = True,
        allow_loopback: str | bool = False,
    ) -> ExternalCandidate | None:
        client = self._get_http_client()
        resolved: ExternalCandidate | None = candidate
        provider_base = candidate.source_provider.split(":", 1)[0]
        channel_secret = self._channel_secret(provider_base)
        channel_api_base = _validated_channel_api_base(channel_secret)
        resolver_hint_url = str(candidate.extra.get("resolver_url") or "").strip()
        effective_allow_loopback = allow_loopback or resolver_hint_url or channel_api_base
        self_hosted_origin = ""

        if candidate.source_provider == "opensubsonic":
            if not channel_secret or not channel_secret.get("api_base"):
                return None
            try:
                stream_url, stream_headers = self.opensubsonic.build_stream_url(candidate.provider_track_id, channel_secret)
            except Exception:
                return None
            from .audio_service import self_hosted_cache_scope
            self_hosted_origin = str(channel_secret.get("api_base") or "")
            resolved = ExternalCandidate(
                title=candidate.title,
                artists=candidate.artists,
                album=candidate.album,
                duration_ms=candidate.duration_ms,
                source_provider="opensubsonic",
                provider_track_id=candidate.provider_track_id,
                url=stream_url,
                headers=stream_headers,
                format=candidate.format or "unknown",
                bitrate=candidate.bitrate,
                sample_rate=candidate.sample_rate,
                bit_depth=candidate.bit_depth,
                size=candidate.size,
                match_score=candidate.match_score,
                strict_match=candidate.strict_match,
                needs_proxy=True,
                level=candidate.level or ("lossless" if candidate.format in {"flac", "wav"} else "exhigh"),
                cover_url=candidate.cover_url,
                extra={**candidate.extra, "allow_self_hosted_origin": self_hosted_origin, "cache_scope": self_hosted_cache_scope(channel_secret)},
            )
            return resolved

        if candidate.source_provider == "webdav":
            if not channel_secret or not channel_secret.get("api_base"):
                return None
            fallback = {"webdav_url": candidate.extra.get("webdav_url")} if candidate.extra.get("webdav_url") else None
            try:
                stream_url, stream_headers = self.webdav.build_stream_request(
                    candidate.provider_track_id, channel_secret, fallback_track=fallback
                )
            except Exception:
                return None
            from .audio_service import self_hosted_cache_scope
            self_hosted_origin = str(channel_secret.get("api_base") or "")
            resolved = ExternalCandidate(
                title=candidate.title,
                artists=candidate.artists,
                album=candidate.album,
                duration_ms=candidate.duration_ms,
                source_provider="webdav",
                provider_track_id=candidate.provider_track_id,
                url=stream_url,
                headers=stream_headers,
                format=candidate.format or "mp3",
                bitrate=candidate.bitrate,
                sample_rate=candidate.sample_rate,
                bit_depth=candidate.bit_depth,
                size=candidate.size,
                match_score=candidate.match_score,
                strict_match=candidate.strict_match,
                needs_proxy=True,
                level=candidate.level or ("lossless" if candidate.format in {"flac", "wav"} else "local"),
                cover_url=candidate.cover_url,
                extra={**candidate.extra, "allow_self_hosted_origin": self_hosted_origin, "cache_scope": self_hosted_cache_scope(channel_secret)},
            )
            return resolved

        if not candidate.url:
            if candidate.source_provider == "kugou_lite":
                resolved = self.kugou_lite.resolve_url(candidate, client, secret=channel_secret, timeout=self.timeout)
            elif candidate.source_provider == "migu":
                resolved = self.migu.resolve_url(candidate, client, secret=channel_secret, timeout=self.timeout)
            elif candidate.source_provider == "qq":
                resolved = self.qq.resolve_url(candidate, client, secret=channel_secret, timeout=self.timeout)
            elif candidate.source_provider.startswith("gdstudio"):
                resolved = self.gdstudio.resolve_url(candidate, client, timeout=self.timeout)
            elif candidate.source_provider == "kuwo":
                resolved = self.kuwo.resolve_url(candidate, client, secret=channel_secret, timeout=self.timeout)
            elif candidate.source_provider == "bilibili":
                resolved = self.bilibili.resolve_url(candidate, client, secret=channel_secret, timeout=self.timeout)
            else:
                resolved = None
        if resolved is None or not resolved.url:
            return None

        clean_headers = {
            k: v for k, v in sanitize_outbound_headers(resolved.headers).items() if k.lower() != "cookie"
        }
        if not verify_stream:
            return ExternalCandidate(
                title=resolved.title,
                artists=resolved.artists,
                album=resolved.album,
                duration_ms=resolved.duration_ms,
                source_provider=resolved.source_provider,
                provider_track_id=resolved.provider_track_id,
                url=resolved.url,
                headers=clean_headers,
                format=resolved.format or "mp3",
                bitrate=resolved.bitrate,
                sample_rate=resolved.sample_rate,
                bit_depth=resolved.bit_depth,
                size=resolved.size,
                match_score=resolved.match_score,
                strict_match=resolved.strict_match,
                needs_proxy=resolved.needs_proxy,
                level=resolved.level,
                cover_url=resolved.cover_url,
                extra=resolved.extra,
            )

        probe = self._probe_fn(
            resolved.url,
            client,
            headers=clean_headers,
            timeout=min(4.0, self.timeout),
            allow_loopback=effective_allow_loopback,
        )
        if not probe.ok:
            return None
        return ExternalCandidate(
            title=resolved.title,
            artists=resolved.artists,
            album=resolved.album,
            duration_ms=resolved.duration_ms,
            source_provider=resolved.source_provider,
            provider_track_id=resolved.provider_track_id,
            url=probe.final_url or resolved.url,
            headers=probe.final_headers,
            format=probe.format or resolved.format or "mp3",
            bitrate=resolved.bitrate,
            sample_rate=resolved.sample_rate,
            bit_depth=resolved.bit_depth,
            size=probe.size or resolved.size,
            match_score=resolved.match_score,
            strict_match=resolved.strict_match,
            needs_proxy=resolved.needs_proxy or not (probe.final_url or resolved.url).startswith("https://"),
            level=resolved.level,
            cover_url=resolved.cover_url,
            extra={**resolved.extra, "content_type": probe.content_type},
        )

    def resolve(
        self,
        song: Song,
        *,
        enabled_providers: list[str] | None = None,
        custom_api: str = "",
        duration_tolerance_ms: int = 5_000,
        verify_stream: bool = True,
        best_quality_race: bool = False,
        blocked_candidates: set[str] | None = None,
    ) -> ExternalCandidate | None:
        """Strictly resolve a verified third-party audio stream for auto-play."""
        deadline = time.monotonic() + 10.0
        candidates = self.search_candidates(
            song,
            enabled_providers=enabled_providers,
            custom_api=custom_api,
            duration_tolerance_ms=duration_tolerance_ms,
            limit=12,
        )
        strict_candidates = [item for item in candidates if item.strict_match]
        if blocked_candidates:
            strict_candidates = [
                item
                for item in strict_candidates
                if item.source_provider not in blocked_candidates
                and f"{item.source_provider}:{item.provider_track_id}" not in blocked_candidates
            ]
        if not strict_candidates:
            return None

        eff_custom_api, _ = self._effective_custom_api(custom_api)
        is_loopback_custom = is_loopback_endpoint(eff_custom_api)

        def _try_resolve(cand: ExternalCandidate) -> ExternalCandidate | None:
            from_custom = cand.source_provider == "custom_endpoint" or bool(cand.extra.get("from_custom_endpoint"))
            resolver_url = str(cand.extra.get("resolver_url") or "").strip()
            allow_lb: str | bool = resolver_url if resolver_url else (eff_custom_api if is_loopback_custom and from_custom else False)
            return self.resolve_candidate(
                cand,
                verify_stream=verify_stream,
                allow_loopback=allow_lb,
            )

        if best_quality_race and len(strict_candidates) > 1:
            # Resolve top distinct providers in parallel and select highest verified quality!
            picked: list[ExternalCandidate] = []
            seen_prov: set[str] = set()
            for cand in strict_candidates:
                if cand.source_provider not in seen_prov:
                    seen_prov.add(cand.source_provider)
                    picked.append(cand)
                if len(picked) >= 4:
                    break
            verified: list[ExternalCandidate] = []
            with ThreadPoolExecutor(max_workers=len(picked)) as executor:
                futures = [executor.submit(_try_resolve, cand) for cand in picked]
                try:
                    for fut in as_completed(futures, timeout=min(5.0, max(1.0, deadline - time.monotonic()))):
                        try:
                            res = fut.result()
                            if res is not None:
                                verified.append(res)
                        except Exception:
                            continue
                except TimeoutError:
                    pass
            if verified:
                verified.sort(
                    key=lambda c: (
                        score_audio_quality(c.format, c.level, c.bitrate, c.sample_rate, c.bit_depth),
                        c.match_score,
                    ),
                    reverse=True,
                )
                return verified[0]
            return None

        for candidate in strict_candidates[:5]:
            if time.monotonic() > deadline:
                break
            resolved = _try_resolve(candidate)
            if resolved is not None:
                return resolved
        return None

    def resolve_direct_channel(
        self,
        provider: str,
        external_id: str,
        *,
        title: str = "",
        artists: list[str] | None = None,
        duration_ms: int = 0,
        stream_url_hint: str = "",
        custom_api: str = "",
        verify_stream: bool = True,
    ) -> ExternalCandidate | None:
        """Directly resolve a cross-channel search hit for playback."""
        prov = str(provider or "").strip().lower()
        ext_id = str(external_id or "").strip()
        if not prov or (not ext_id and not stream_url_hint):
            return None

        eff_custom_api, _ = self._effective_custom_api(custom_api)
        base_prov = prov.split(":", 1)[0]
        sub_prov = prov.split(":", 1)[1] if ":" in prov else ""
        channel_secret = self._channel_secret(base_prov)
        channel_api_base = _validated_channel_api_base(channel_secret)
        builtin_non_custom = {"kugou_lite", "migu", "qq", "kuwo", "bilibili", "gdstudio", "opensubsonic", "webdav"}
        v2_match = None
        if prov in {"resolver", "resolver_v2"} and ":" in ext_id:
            maybe_sub, maybe_ext = ext_id.split(":", 1)
            maybe_sub_l = maybe_sub.strip().lower()
            maybe_ext = maybe_ext.strip()
            if maybe_sub_l and maybe_ext:
                v2_match = next(
                    (r for r in self._active_v2_resolvers() if str(r.get("id") or "").lower() == maybe_sub_l),
                    None,
                )
                if v2_match is not None:
                    prov = f"{prov}:{maybe_sub_l}"
                    ext_id = maybe_ext
        if v2_match is None and base_prov not in builtin_non_custom:
            if base_prov in {"resolver", "resolver_v2"} and sub_prov:
                v2_match_ids = {prov, sub_prov}
            else:
                v2_match_ids = {prov}
            v2_match = next(
                (r for r in self._active_v2_resolvers() if str(r.get("id") or "").lower() in v2_match_ids),
                None,
            )
        if v2_match is not None:
            resolver_url = str(v2_match.get("url") or "").strip()
            resolver_key = str(v2_match.get("api_key") or "").strip()
            if stream_url_hint and is_safe_external_url(stream_url_hint.strip(), allow_loopback=resolver_url):
                seed = ExternalCandidate(
                    title=title or ext_id,
                    artists=list(artists or []),
                    album="",
                    duration_ms=max(0, int(duration_ms or 0)),
                    source_provider=prov,
                    provider_track_id=ext_id,
                    url=stream_url_hint.strip(),
                    match_score=1.0,
                    strict_match=True,
                    needs_proxy=True,
                    extra={"from_custom_endpoint": True, "resolver_url": resolver_url, "resolver_id": str(v2_match.get("id") or "")},
                )
                return self.resolve_candidate(seed, verify_stream=verify_stream, allow_loopback=resolver_url)
            q_obj = TrackQuery(title=title, artists=tuple(artists or []), duration_ms=max(0, int(duration_ms or 0)))
            cands = self.custom.search_and_resolve(
                resolver_url,
                q_obj,
                self._get_http_client(),
                api_key=resolver_key,
                timeout=self.timeout,
                resolver_id=prov or str(v2_match.get("id") or ""),
                track_id=ext_id,
            )
            if ext_id and len(cands) > 1:
                cands = sorted(cands, key=lambda c: c.provider_track_id != ext_id)
            for cand in cands:
                resolved = self.resolve_candidate(cand, verify_stream=verify_stream, allow_loopback=resolver_url)
                if resolved is not None:
                    return resolved
            return None

        allow_loopback = (eff_custom_api if base_prov in {"custom", "custom_endpoint"} else channel_api_base)

        extra: dict[str, Any] = {}
        if base_prov == "gdstudio":
            if sub_prov:
                extra["sub_source"] = sub_prov
            elif ":" in ext_id:
                maybe_sub, maybe_ext = ext_id.split(":", 1)
                maybe_sub_l = maybe_sub.strip().lower()
                maybe_ext = maybe_ext.strip()
                if maybe_sub_l in GDSTUDIO_SUB_SOURCES and maybe_ext:
                    sub_prov = maybe_sub_l
                    prov = f"gdstudio:{sub_prov}"
                    ext_id = maybe_ext
                    extra["sub_source"] = sub_prov
        if prov in {"custom", "custom_endpoint"} or bool(channel_api_base):
            extra["from_custom_endpoint"] = True

        seed_candidate = ExternalCandidate(
            title=title or ext_id,
            artists=list(artists or []),
            album="",
            duration_ms=max(0, int(duration_ms or 0)),
            source_provider=prov,
            provider_track_id=ext_id,
            url=stream_url_hint.strip() if stream_url_hint and is_safe_external_url(stream_url_hint.strip(), allow_loopback=allow_loopback) else "",
            match_score=1.0,
            strict_match=True,
            needs_proxy=True,
            extra=extra,
        )
        return self.resolve_candidate(
            seed_candidate,
            verify_stream=verify_stream,
            allow_loopback=allow_loopback,
        )

    def _search_channel_page(
        self,
        channel: str,
        keyword: str,
        client: httpx.Client,
        *,
        limit: int = 30,
        offset: int = 0,
        custom_api: str = "",
        custom_api_key: str = "",
    ) -> tuple[list[ExternalCandidate], bool]:
        """Dedicated paginated search mode for external channels (no strict TrackQuery dropping)."""
        if channel == "kugou_lite":
            return self.kugou_lite.search_page(
                keyword,
                client,
                secret=self._channel_secret("kugou_lite"),
                limit=limit,
                offset=offset,
                timeout=self.timeout,
            )
        if channel == "migu":
            return self.migu.search_page(
                keyword,
                client,
                secret=self._channel_secret("migu"),
                limit=limit,
                offset=offset,
                timeout=self.timeout,
            )
        if channel == "qq":
            return self.qq.search_page(
                keyword,
                client,
                secret=self._channel_secret("qq"),
                limit=limit,
                offset=offset,
                timeout=self.timeout,
            )
        if channel == "kuwo":
            return self.kuwo.search_page(
                keyword,
                client,
                secret=self._channel_secret("kuwo"),
                limit=limit,
                offset=offset,
                timeout=self.timeout,
            )
        if channel == "bilibili":
            return self.bilibili.search_page(
                keyword,
                client,
                secret=self._channel_secret("bilibili"),
                limit=limit,
                offset=offset,
                timeout=self.timeout,
            )
        if channel == "gdstudio":
            return self.gdstudio.search_page(
                keyword,
                client,
                limit=limit,
                offset=offset,
                timeout=self.timeout,
                enabled_providers=["gdstudio", "kuwo", "kugou"],
            )
        if channel == "custom" and custom_api:
            return self.custom.search_page(
                custom_api,
                keyword,
                client,
                api_key=custom_api_key,
                limit=limit,
                offset=offset,
                timeout=self.timeout,
            )
        if channel == "opensubsonic":
            secret = self._channel_secret("opensubsonic") or {}
            if not secret.get("api_base"):
                return [], False
            tracks, more = self.opensubsonic.search_tracks(keyword, secret, client, limit=limit, offset=offset, timeout=self.timeout)
            return [
                ExternalCandidate(
                    title=t["title"],
                    artists=list(t["artists"]),
                    album=t["album"],
                    duration_ms=t["duration_ms"],
                    source_provider="opensubsonic",
                    provider_track_id=t["external_id"],
                    format=str(t.get("format") or ""),
                    bitrate=_safe_int(t.get("bitrate")),
                    sample_rate=_safe_int(t.get("sample_rate")),
                    cover_url=str(t.get("cover_url") or ""),
                    needs_proxy=True,
                )
                for t in tracks
            ], more
        if channel == "webdav":
            tracks, more = self.webdav.search_tracks(keyword, limit=limit, offset=offset)
            return [
                ExternalCandidate(
                    title=t["title"],
                    artists=list(t["artists"]),
                    album=t["album"],
                    duration_ms=t["duration_ms"],
                    source_provider="webdav",
                    provider_track_id=t["external_id"],
                    format=str(t.get("format") or ""),
                    size=_safe_int(t.get("size")),
                    needs_proxy=True,
                )
                for t in tracks
            ], more
        return [], False

    def search_multi_channel(
        self,
        query: str,
        *,
        channels: list[str] | None = None,
        limit: int = 30,
        offset: int = 0,
        cursors: dict | None = None,
        netease_client: Any = None,
        custom_api: str = "",
    ) -> dict[str, Any]:
        """Unthrottled concurrent paginated multi-channel search with ±3s dedup & relevance interleaving."""
        cleaned_query = str(query or "").strip()
        cap = max(1, min(100, _safe_int(limit) or 30))
        off = max(0, min(1_000, _safe_int(offset)))
        v2_resolvers = self._active_v2_resolvers()
        v2_by_task = {f"resolver:{str(r.get('id') or 'resolver').lower()}": r for r in v2_resolvers if r.get("url")}
        if channels:
            requested = [str(ch).strip().lower() for ch in channels if str(ch).strip()]
        else:
            default_list = ["netease", *DEFAULT_EXTERNAL_PROVIDERS]
            for proto in ("opensubsonic", "webdav"):
                if self._is_channel_enabled(proto):
                    secret = self._channel_secret(proto) or {}
                    if secret.get("api_base"):
                        default_list.append(proto)
            default_list.extend(v2_by_task.keys())
            requested = default_list
        requested = list(dict.fromkeys(requested))
        if not cleaned_query:
            return {
                "query": "",
                "offset": off,
                "limit": cap,
                "has_more": False,
                "channels_queried": [],
                "tracks": [],
                "channel_errors": {},
            }

        eff_custom_api, custom_key = self._effective_custom_api(custom_api)
        http_client = self._get_http_client()

        tasks: list[str] = []
        for ch in requested:
            if ch == "netease":
                if netease_client is not None:
                    tasks.append("netease")
            elif ch in v2_by_task:
                tasks.append(ch)
            elif ch.startswith("resolver_v2:") and f"resolver:{ch.split(':', 1)[1]}" in v2_by_task:
                tasks.append(f"resolver:{ch.split(':', 1)[1]}")
            elif f"resolver:{ch}" in v2_by_task:
                tasks.append(f"resolver:{ch}")
            elif ch in ALL_EXTERNAL_CHANNELS:
                if ch == "custom" and not eff_custom_api:
                    continue
                if self._is_channel_enabled(ch):
                    tasks.append(ch)
        tasks = list(dict.fromkeys(tasks))

        per_channel_limit = cap if len(tasks) <= 1 else min(cap, 50)
        sizes = {ch: min(per_channel_limit, 50 if ch == "bilibili" else 100) for ch in tasks}
        positions = {ch: (cursors.get(ch, [off, 0]) if cursors is not None else [off, 0]) for ch in tasks}
        tasks = [ch for ch in tasks if positions[ch] is not None]
        channel_results: dict[str, list[dict[str, Any]]] = {}
        channel_has_more: dict[str, bool] = {}
        channel_errors: dict[str, str] = {}

        def _run_channel(ch: str) -> tuple[str, list[dict[str, Any]], bool]:
            off, _ = positions[ch]
            cap = sizes[ch]
            if ch != "netease" and not self.health.allow_request(ch):
                raise RuntimeError("渠道熔断冷却中")
            started = time.monotonic()
            if ch == "netease":
                page_fn = getattr(netease_client, "search_songs_page", None)
                if callable(page_fn):
                    songs, ch_more = page_fn(cleaned_query, limit=cap, offset=off)
                else:
                    try:
                        songs = netease_client.search_songs(cleaned_query, limit=cap, offset=off)
                    except TypeError:
                        songs = netease_client.search_songs(cleaned_query, limit=cap)
                    ch_more = len(songs) >= cap
                rows: list[dict[str, Any]] = []
                for song in songs:
                    d = song.to_dict()
                    d["provider"] = "netease"
                    d["external_id"] = str(song.id)
                    rows.append(d)
                return ch, rows, bool(ch_more)

            try:
                if ch in v2_by_task:
                    rcfg = v2_by_task[ch]
                    cands, ch_more = self.custom.search_page(
                        str(rcfg["url"]),
                        cleaned_query,
                        http_client,
                        api_key=str(rcfg.get("api_key") or ""),
                        limit=cap,
                        offset=off,
                        timeout=self.timeout,
                        resolver_id=str(rcfg.get("id") or ch),
                    )
                else:
                    cands, ch_more = self._search_channel_page(
                        ch,
                        cleaned_query,
                        http_client,
                        limit=cap,
                        offset=off,
                        custom_api=eff_custom_api,
                        custom_api_key=custom_key,
                    )
                self.health.record_success(ch, (time.monotonic() - started) * 1000)
            except Exception as exc:
                self.health.record_failure(ch, str(exc), latency_ms=(time.monotonic() - started) * 1000)
                raise
            rows = []
            for cand in cands:
                rows.append(
                    {
                        "id": 0,
                        "track_key": f"{cand.source_provider}:{cand.provider_track_id}",
                        "title": cand.title,
                        "artists": list(cand.artists),
                        "album": cand.album,
                        "duration": cand.duration_ms,
                        "duration_ms": cand.duration_ms,
                        "cover_url": cand.cover_url,
                        "availability": "available",
                        "provider": cand.source_provider,
                        "external_id": cand.provider_track_id,
                        "stream_url_hint": cand.url,
                        "format": cand.format,
                        "bitrate": cand.bitrate,
                        "isrc": str(cand.extra.get("isrc") or ""),
                        "mbid": str(cand.extra.get("mbid") or ""),
                    }
                )
            return ch, rows, bool(ch_more)

        if len(tasks) == 1:
            ch = tasks[0]
            try:
                _name, rows, ch_more = _run_channel(ch)
                channel_results[ch] = rows
                channel_has_more[ch] = ch_more
            except Exception as exc:
                channel_errors[ch] = str(exc)
        elif tasks:
            future_map = {_SEARCH_EXECUTOR.submit(_run_channel, ch): ch for ch in tasks}
            done, not_done = wait(future_map.keys(), timeout=5.0)
            for fut in done:
                ch = future_map[fut]
                try:
                    _name, rows, ch_more = fut.result()
                    channel_results[ch] = rows
                    channel_has_more[ch] = ch_more
                except Exception as exc:
                    channel_errors[ch] = str(exc)
            for fut in not_done:
                ch = future_map[fut]
                fut.cancel()
                channel_errors[ch] = "timeout"

        # Keep the native page start and the number of normalized rows consumed.
        # A partially displayed provider page is resumed before fetching its next page.
        next_cursors = dict(cursors or {})
        available = {ch: rows[positions[ch][1]:] for ch, rows in channel_results.items()}
        chosen = {ch: [] for ch in tasks}
        budget = cap
        for rank in range(max(map(len, available.values()), default=0)):
            for ch in tasks:
                rows = available.get(ch, [])
                if budget and rank < len(rows):
                    chosen[ch].append(rows[rank])
                    budget -= 1
            if not budget:
                break
        for ch in tasks:
            start, skipped = positions[ch]
            if ch in channel_errors:
                next_cursors[ch] = [start, skipped]
            elif len(chosen[ch]) < len(available[ch]):
                next_cursors[ch] = [start, skipped + len(chosen[ch])]
            elif channel_has_more.get(ch) and start + sizes[ch] <= 1000:
                next_cursors[ch] = [start + sizes[ch], 0]
            else:
                next_cursors[ch] = None
        channel_results = chosen

        # Deduplicate ONLY on normalized title + primary artist + duration within ±3s,
        # tracking per-channel relevance rank for fair cross-channel interleaving.
        buckets_by_ta: dict[tuple[str, str], list[dict[str, Any]]] = {}
        all_merged: list[dict[str, Any]] = []
        channel_order = {ch: idx for idx, ch in enumerate(tasks)}

        for ch in tasks:
            ch_idx = channel_order[ch]
            for rank_idx, row in enumerate(channel_results.get(ch, [])):
                nt = normalize_text(str(row.get("title") or ""))
                if not nt:
                    continue
                artists = row.get("artists") if isinstance(row.get("artists"), list) else []
                na = normalize_text(str(artists[0])) if artists else ""
                dur_ms = _safe_int(row.get("duration"))
                hit_info = {
                    "provider": str(row.get("provider") or ch),
                    "external_id": str(row.get("external_id") or ""),
                    "stream_url_hint": str(row.get("stream_url_hint") or ""),
                    "duration": dur_ms,
                    "duration_ms": dur_ms,
                }
                ta_key = (nt, na)
                existing_list = buckets_by_ta.setdefault(ta_key, [])
                matched_meta: dict[str, Any] | None = None
                for meta in existing_list:
                    existing_dur = meta["duration"]
                    if existing_dur <= 0 or dur_ms <= 0 or abs(existing_dur - dur_ms) <= _MULTI_SEARCH_DEDUP_TOLERANCE_MS:
                        matched_meta = meta
                        break

                if matched_meta is None:
                    entry = dict(row)
                    entry["available_channels"] = [ch]
                    entry["channel_hits"] = [hit_info]
                    meta = {
                        "entry": entry,
                        "duration": dur_ms,
                        "duration_ms": dur_ms,
                        "best_rank": rank_idx,
                        "netease_rank": rank_idx if ch == "netease" else 10_000,
                        "first_ch_idx": ch_idx,
                    }
                    existing_list.append(meta)
                    all_merged.append(meta)
                else:
                    entry = matched_meta["entry"]
                    if ch not in entry["available_channels"]:
                        entry["available_channels"].append(ch)
                    entry["channel_hits"].append(hit_info)
                    if rank_idx < matched_meta["best_rank"]:
                        matched_meta["best_rank"] = rank_idx
                    if ch == "netease":
                        if rank_idx < matched_meta["netease_rank"]:
                            matched_meta["netease_rank"] = rank_idx
                        # Promote NetEase primary track ID & metadata when merged onto an external hit.
                        if not entry.get("id") and row.get("id"):
                            entry["id"] = row["id"]
                            entry["provider"] = "netease"
                            entry["external_id"] = str(row["id"])
                            entry["availability"] = row.get("availability", entry.get("availability", "available"))
                            for field_name in ("fee", "privilege_st", "max_br_level", "is_cloud"):
                                if field_name in row:
                                    entry[field_name] = row[field_name]
                    if not entry.get("cover_url") and row.get("cover_url"):
                        entry["cover_url"] = row["cover_url"]
                    if not entry.get("album") and row.get("album"):
                        entry["album"] = row["album"]
                    if matched_meta["duration"] == 0 and dur_ms > 0:
                        matched_meta["duration"] = dur_ms
                        entry["duration"] = dur_ms
                    if _safe_int(row.get("bitrate")) > _safe_int(entry.get("bitrate")):
                        entry["bitrate"] = row["bitrate"]
                        if row.get("format"):
                            entry["format"] = row["format"]

        # Interleave ranking by channel relevance rank (using NetEase rank & multi-channel hit count as tie-breakers).
        all_merged.sort(
            key=lambda m: (
                m["best_rank"],
                0 if "netease" in m["entry"]["available_channels"] else 1,
                -len(m["entry"]["available_channels"]),
                m["netease_rank"],
                m["first_ch_idx"],
            )
        )
        has_more = any(next_cursors.get(ch) is not None for ch in tasks)
        tracks = [m["entry"] for m in all_merged[:cap]]
        return {
            "query": cleaned_query,
            "offset": off,
            "limit": cap,
            "has_more": has_more,
            "next_cursors": next_cursors,
            "channels_queried": tasks,
            "tracks": tracks,
            "channel_errors": channel_errors,
        }

    def test_channel(self, channel_id: str, *, custom_api: str = "") -> dict[str, Any]:
        """Check whether the channel returns search results."""
        ch = str(channel_id or "").strip().lower()
        started = time.monotonic()
        if ch not in ALL_EXTERNAL_CHANNELS:
            return {
                "channel": ch,
                "ok": False,
                "latency_ms": 0,
                "status": "unsupported",
                "capability": "unconfigured",
                "message": f"不支持的渠道：{ch}",
                "max_quality_hint": "",
            }

        eff_custom_api, custom_key = self._effective_custom_api(custom_api)
        secret = self._channel_secret(ch)
        capability = channel_capability(ch, secret, custom_api=eff_custom_api if ch == "custom" else "")
        if ch == "opensubsonic":
            if not (secret or {}).get("api_base"):
                return {
                    "channel": ch,
                    "ok": False,
                    "latency_ms": 0,
                    "status": "unconfigured",
                    "capability": capability,
                    "message": "尚未配置 OpenSubsonic / Navidrome 服务器地址",
                    "max_quality_hint": "",
                }
            try:
                res = self.opensubsonic.ping(secret or {}, self._get_http_client(), timeout=self.timeout)
                self.health.record_success(ch, res.get("latency_ms", 0))
                return res
            except Exception as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                self.health.record_failure(ch, str(exc), latency_ms=latency_ms)
                return {
                    "channel": ch,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "status": "error",
                    "capability": capability,
                    "message": str(exc),
                    "max_quality_hint": "",
                }

        if ch == "webdav":
            if not (secret or {}).get("api_base"):
                return {
                    "channel": ch,
                    "ok": False,
                    "latency_ms": 0,
                    "status": "unconfigured",
                    "capability": capability,
                    "message": "尚未配置 WebDAV 服务器地址",
                    "max_quality_hint": "",
                }
            try:
                res = self.webdav.ping(secret or {}, self._get_http_client(), timeout=self.timeout)
                self.health.record_success(ch, res.get("latency_ms", 0))
                return res
            except Exception as exc:
                latency_ms = int((time.monotonic() - started) * 1000)
                self.health.record_failure(ch, str(exc), latency_ms=latency_ms)
                return {
                    "channel": ch,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "status": "error",
                    "capability": capability,
                    "message": str(exc),
                    "max_quality_hint": "",
                }

        if ch == "custom":
            if not eff_custom_api:
                return {
                    "channel": ch,
                    "ok": False,
                    "latency_ms": 0,
                    "status": "unconfigured",
                    "capability": capability,
                    "message": "尚未配置自定义外部音源接口地址",
                    "max_quality_hint": "",
                }
            manifest = fetch_resolver_manifest(eff_custom_api, self._get_http_client(), api_key=custom_key, timeout=self.timeout)
            if manifest is not None:
                latency_ms = int((time.monotonic() - started) * 1000)
                self.health.record_success(ch, latency_ms)
                return {
                    "channel": ch,
                    "ok": True,
                    "latency_ms": latency_ms,
                    "status": "connected",
                    "capability": capability,
                    "protocol_version": 2,
                    "manifest": manifest,
                    "message": f"HTTP Resolver v2 已连接：{manifest.get('name') or manifest.get('id') or 'custom'}",
                    "max_quality_hint": "flac",
                }

        client = self._get_http_client()
        self.health.reset(ch)
        probe_query = TrackQuery(title="晴天", artists=("周杰伦",), duration_ms=269_000)
        try:
            cands = self._search_single_provider(
                ch,
                probe_query,
                client,
                enabled_providers=[ch] if ch != "gdstudio" else ["gdstudio", "kuwo"],
                custom_api=eff_custom_api,
                custom_api_key=custom_key,
                duration_tolerance_ms=10_000,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            if not cands:
                self.health.record_failure(ch, "empty", latency_ms=latency_ms)
                return {
                    "channel": ch,
                    "ok": False,
                    "latency_ms": latency_ms,
                    "status": "empty",
                    "capability": capability,
                    "message": "渠道已响应但未返回测试曲目，请检查网络或配置",
                    "max_quality_hint": "",
                }
            self.health.record_success(ch, latency_ms)
            return {
                "channel": ch,
                "ok": True,
                "latency_ms": latency_ms,
                "status": "connected",
                "capability": capability,
                "message": f"搜索可用（命中 {len(cands)} 条结果）",
                "max_quality_hint": "",
            }
        except (httpx.TimeoutException, TimeoutError):
            latency_ms = int((time.monotonic() - started) * 1000)
            self.health.record_failure(ch, "timeout", latency_ms=latency_ms)
            return {
                "channel": ch,
                "ok": False,
                "latency_ms": latency_ms,
                "status": "timeout",
                "capability": capability,
                "message": "渠道连通性测试超时，请稍后重试",
                "max_quality_hint": "",
            }
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            self.health.record_failure(ch, str(exc), latency_ms=latency_ms)
            return {
                "channel": ch,
                "ok": False,
                "latency_ms": latency_ms,
                "status": "error",
                "capability": capability,
                "message": "渠道连通性测试失败，请检查网络或服务地址",
                "max_quality_hint": "",
            }


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _parse_duration_ms(value: Any) -> int:
    num = _safe_int(value)
    if num <= 0:
        return 0
    return num * 1000 if num < 3600 else num


def _parse_colon_duration_ms(raw: str) -> int:
    parts = [part.strip() for part in (raw or "").split(":") if part.strip().isdigit()]
    if len(parts) == 2:
        return (int(parts[0]) * 60 + int(parts[1])) * 1000
    if len(parts) == 3:
        return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
    return 0


def _parse_relaxed_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = text.replace("'", '"')
        return json.loads(cleaned)


def mime_type_for_format(fmt: str, path: Path | None = None) -> str:
    cleaned = (fmt or "").strip().lower().lstrip(".")
    if cleaned in _FORMAT_TO_MIME:
        return _FORMAT_TO_MIME[cleaned]
    if path is not None:
        guessed = mimetypes.guess_type(path.name)[0]
        if guessed:
            return guessed
    return "audio/mpeg"
