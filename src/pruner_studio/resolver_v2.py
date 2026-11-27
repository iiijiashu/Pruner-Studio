"""Unified HTTP Resolver Contract v2, Subscription Registry, and Per-Channel Health / Circuit Breaker."""
from __future__ import annotations

import base64
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .errors import AppError
from .netease_client import NoOpCookies, isolate_cookie_jar
from .storage import (
    JsonStore,
    _dpapi_available,
    _dpapi_protect,
    _dpapi_unprotect,
    _keyring_available,
    _keyring_get,
    _keyring_set,
    _warn_plaintext_fallback,
    validate_custom_external_api,
)

RESOLVER_PROTOCOL_VERSION = 2
_MAX_RESOLVERS = 32
_MAX_SUBSCRIPTION_BYTES = 256 * 1024
_RESOLVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")


class ChannelHealthTracker:
    """Per-channel health, latency EMA, and automatic circuit breaker."""

    def __init__(self, *, failure_threshold: int = 3, base_cooldown_sec: float = 30.0, max_cooldown_sec: float = 180.0):
        self.failure_threshold = max(1, int(failure_threshold))
        self.base_cooldown_sec = max(1.0, float(base_cooldown_sec))
        self.max_cooldown_sec = max(self.base_cooldown_sec, float(max_cooldown_sec))
        self._lock = threading.RLock()
        self._channels: dict[str, dict[str, Any]] = {}

    def _entry_locked(self, channel_id: str) -> dict[str, Any]:
        cid = str(channel_id or "").strip().lower()
        if cid not in self._channels:
            self._channels[cid] = {
                "channel": cid,
                "state": "closed",
                "consecutive_failures": 0,
                "total_requests": 0,
                "total_failures": 0,
                "latency_ema_ms": 0.0,
                "last_error": "",
                "last_success_at": "",
                "last_failure_at": "",
                "open_until_mono": 0.0,
                "cooldown_sec": 0.0,
                "half_open_in_flight": False,
            }
        return self._channels[cid]

    def allow_request(self, channel_id: str, *, now_mono: float | None = None) -> bool:
        now = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            entry = self._entry_locked(channel_id)
            if entry["state"] == "open":
                if now < entry["open_until_mono"]:
                    return False
                entry["state"] = "half_open"
                entry["half_open_in_flight"] = True
                return True
            if entry["state"] == "half_open":
                if entry.get("half_open_in_flight"):
                    return False
                entry["half_open_in_flight"] = True
            return True

    def record_success(self, channel_id: str, latency_ms: float = 0.0) -> dict[str, Any]:
        with self._lock:
            entry = self._entry_locked(channel_id)
            entry["total_requests"] += 1
            entry["consecutive_failures"] = 0
            entry["state"] = "closed"
            entry["open_until_mono"] = 0.0
            entry["cooldown_sec"] = 0.0
            entry["half_open_in_flight"] = False
            entry["last_error"] = ""
            entry["last_success_at"] = datetime.now(timezone.utc).isoformat()
            lat = max(0.0, float(latency_ms or 0.0))
            if lat > 0:
                prev = entry["latency_ema_ms"]
                entry["latency_ema_ms"] = round(lat if prev <= 0 else (0.7 * prev + 0.3 * lat), 1)
            return self._public_view(entry)

    def record_failure(self, channel_id: str, error: str = "", *, latency_ms: float = 0.0, now_mono: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            entry = self._entry_locked(channel_id)
            entry["total_requests"] += 1
            entry["total_failures"] += 1
            entry["consecutive_failures"] += 1
            entry["half_open_in_flight"] = False
            entry["last_error"] = str(error or "request_failed")[:200]
            entry["last_failure_at"] = datetime.now(timezone.utc).isoformat()
            lat = max(0.0, float(latency_ms or 0.0))
            if lat > 0:
                prev = entry["latency_ema_ms"]
                entry["latency_ema_ms"] = round(lat if prev <= 0 else (0.7 * prev + 0.3 * lat), 1)
            if entry["consecutive_failures"] >= self.failure_threshold:
                exponent = min(4, entry["consecutive_failures"] - self.failure_threshold)
                cooldown = min(self.max_cooldown_sec, self.base_cooldown_sec * (2**exponent))
                entry["state"] = "open"
                entry["cooldown_sec"] = cooldown
                entry["open_until_mono"] = now + cooldown
            return self._public_view(entry, now_mono=now)

    def reset(self, channel_id: str) -> None:
        with self._lock:
            entry = self._entry_locked(channel_id)
            entry["state"] = "closed"
            entry["consecutive_failures"] = 0
            entry["open_until_mono"] = 0.0
            entry["cooldown_sec"] = 0.0
            entry["half_open_in_flight"] = False
            entry["last_error"] = ""

    @staticmethod
    def _public_view(entry: dict[str, Any], *, now_mono: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now_mono is None else now_mono
        remaining = max(0.0, entry["open_until_mono"] - now) if entry["state"] == "open" else 0.0
        state = "half_open" if (entry["state"] == "open" and remaining <= 0.0) else entry["state"]
        return {
            "channel": entry["channel"],
            "circuit_state": state,
            "consecutive_failures": entry["consecutive_failures"],
            "total_requests": entry["total_requests"],
            "total_failures": entry["total_failures"],
            "latency_ema_ms": entry["latency_ema_ms"],
            "last_error": entry["last_error"],
            "last_success_at": entry["last_success_at"],
            "last_failure_at": entry["last_failure_at"],
            "retry_after_sec": round(remaining, 1),
        }

    def snapshot(self, channel_id: str | None = None, *, now_mono: float | None = None) -> dict[str, Any]:
        now = time.monotonic() if now_mono is None else now_mono
        with self._lock:
            if channel_id is not None:
                return self._public_view(self._entry_locked(channel_id), now_mono=now)
            return {cid: self._public_view(entry, now_mono=now) for cid, entry in self._channels.items()}


def normalize_resolver_entry(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AppError("RESOLVER_INVALID", "Resolver 配置必须是 JSON 对象", 400)
    raw_url = str(raw.get("url") or raw.get("api_base") or "").strip()
    try:
        validated_url = validate_custom_external_api(raw_url).rstrip("/")
    except ValueError as exc:
        raise AppError("RESOLVER_URL_INVALID", str(exc), 400) from exc
    if not validated_url:
        raise AppError("RESOLVER_URL_REQUIRED", "缺少 Resolver 接口地址", 400)

    raw_id = str(raw.get("id") or "").strip().lower()
    if not raw_id:
        raw_id = "res-" + hashlib.sha256(validated_url.encode("utf-8")).hexdigest()[:10]
    if not _RESOLVER_ID_RE.match(raw_id):
        raise AppError("RESOLVER_ID_INVALID", "Resolver ID 仅允许小写字母、数字和 -_.", 400)

    name = str(raw.get("name") or raw_id).strip()[:80]
    api_key = str(raw.get("api_key") or raw.get("token") or "").strip()
    if "\r" in api_key or "\n" in api_key or "\x00" in api_key or len(api_key) > 4096:
        raise AppError("RESOLVER_TOKEN_INVALID", "Resolver 凭据格式无效", 400)
    enabled = bool(raw.get("enabled", True))
    priority = max(0, min(100, int(raw.get("priority") or 50)))
    subscription_url = str(raw.get("subscription_url") or "").strip()
    if subscription_url:
        try:
            subscription_url = validate_custom_external_api(subscription_url)
        except ValueError as exc:
            raise AppError("RESOLVER_SUBSCRIPTION_INVALID", str(exc), 400) from exc

    raw_caps = raw.get("capabilities")
    caps = [str(c).strip().lower() for c in raw_caps if str(c).strip().lower() in {"search", "resolve", "lyric"}] if isinstance(raw_caps, list) else ["search", "resolve"]
    if not caps:
        caps = ["search", "resolve"]

    return {
        "id": raw_id,
        "name": name,
        "url": validated_url,
        "enabled": enabled,
        "priority": priority,
        "protocol_version": int(raw.get("protocol_version") or RESOLVER_PROTOCOL_VERSION),
        "capabilities": caps,
        "api_key": api_key,
        "subscription_url": subscription_url,
        "is_sidecar": bool(raw.get("is_sidecar", False)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class ResolverRegistry:
    """Persisted registry of HTTP Resolver v2 endpoints and subscription URLs (zero secret leakage)."""

    FILE_NAME = "resolvers_v2.json"

    def __init__(self, store: JsonStore):
        self.store = store
        self._lock = threading.RLock()

    def _load_locked(self) -> dict[str, Any]:
        default = {"version": 2, "resolvers": {}, "subscriptions": []}
        try:
            raw = self.store.read(self.FILE_NAME, default)
        except AppError:
            raise
        try:
            if raw.get("encoding") == "dpapi":
                raw = json.loads(_dpapi_unprotect(base64.b64decode(raw["data"], validate=True)))
            elif raw.get("encoding") == "keyring":
                raw = json.loads(_keyring_get(raw["ref"]))
            if not isinstance(raw["resolvers"], dict):
                raise ValueError("invalid registry")
        except Exception as exc:
            raise AppError("RESOLVER_VAULT_UNAVAILABLE", "解析器凭据暂时无法读取，原配置已保留", 503) from exc
        return {
            "version": 2,
            "resolvers": dict(raw.get("resolvers") or {}),
            "subscriptions": list(raw.get("subscriptions") or []),
        }

    def _save_locked(self, doc: dict[str, Any]) -> None:
        raw_text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        if _dpapi_available():
            encrypted = _dpapi_protect(raw_text.encode("utf-8"))
            self.store.write(
                self.FILE_NAME,
                {"version": 2, "encoding": "dpapi", "data": base64.b64encode(encrypted).decode("ascii")},
                private=True,
            )
            return
        ref = f"resolvers_v2:{self.store.data_dir.resolve()}"
        if _keyring_set(ref, raw_text):
            self.store.write(
                self.FILE_NAME,
                {"version": 2, "encoding": "keyring", "ref": ref},
                private=True,
            )
            return
        if self.store.read(self.FILE_NAME, {}).get("encoding") == "keyring":
            raise AppError("RESOLVER_VAULT_UNAVAILABLE", "解析器凭据暂时无法写入，原配置已保留", 503)
        if any(bool(v.get("api_key")) for v in (doc.get("resolvers") or {}).values() if isinstance(v, dict)):
            _warn_plaintext_fallback("resolver_v2 credentials")
        self.store.write(self.FILE_NAME, doc, private=True)

    @staticmethod
    def _to_public(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": entry["id"],
            "name": entry.get("name", entry["id"]),
            "url": entry.get("url", ""),
            "enabled": bool(entry.get("enabled", True)),
            "priority": int(entry.get("priority", 50)),
            "protocol_version": int(entry.get("protocol_version", RESOLVER_PROTOCOL_VERSION)),
            "capabilities": list(entry.get("capabilities") or ["search", "resolve"]),
            "has_token": bool(entry.get("api_key")),
            "subscription_url": entry.get("subscription_url", ""),
            "is_sidecar": bool(entry.get("is_sidecar", False)),
            "updated_at": entry.get("updated_at", ""),
        }

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            doc = self._load_locked()
            items = [self._to_public(v) for v in doc["resolvers"].values() if isinstance(v, dict)]
            items.sort(key=lambda r: (-r["priority"], r["id"]))
            return items

    def list_enabled_secrets(self) -> list[dict[str, Any]]:
        with self._lock:
            doc = self._load_locked()
            items = [deepcopy(v) for v in doc["resolvers"].values() if isinstance(v, dict) and v.get("enabled")]
            items.sort(key=lambda r: (-int(r.get("priority", 50)), str(r.get("id", ""))))
            return items

    def list_subscriptions(self) -> list[str]:
        with self._lock:
            return list(self._load_locked()["subscriptions"])

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        entry = normalize_resolver_entry(payload)
        with self._lock:
            doc = self._load_locked()
            existing = doc["resolvers"].get(entry["id"])
            if (not entry["api_key"] and isinstance(existing, dict) and payload.get("clear_token") is not True
                    and _origin(existing["url"]) == _origin(entry["url"])):
                entry["api_key"] = str(existing.get("api_key") or "")
            if entry["id"] not in doc["resolvers"] and len(doc["resolvers"]) >= _MAX_RESOLVERS:
                raise AppError("RESOLVER_LIMIT_REACHED", f"最多支持 {_MAX_RESOLVERS} 个外部 Resolver", 400)
            doc["resolvers"][entry["id"]] = entry
            self._save_locked(doc)
            return self._to_public(entry)

    def remove(self, resolver_id: str) -> bool:
        rid = str(resolver_id or "").strip().lower()
        with self._lock:
            doc = self._load_locked()
            if doc["resolvers"].pop(rid, None) is None:
                return False
            self._save_locked(doc)
            return True

    def import_subscription_payload(self, subscription_url: str, payload: Any) -> dict[str, Any]:
        try:
            validated_sub = validate_custom_external_api(subscription_url)
        except ValueError as exc:
            raise AppError("RESOLVER_SUBSCRIPTION_INVALID", str(exc), 400) from exc
        if not validated_sub:
            raise AppError("RESOLVER_SUBSCRIPTION_INVALID", "缺少订阅地址", 400)
        if not isinstance(payload, dict):
            raise AppError("SUBSCRIPTION_INVALID", "订阅源格式必须是 JSON 对象", 400)
        raw_items = payload.get("resolvers")
        if not isinstance(raw_items, list) or not raw_items:
            raise AppError("SUBSCRIPTION_EMPTY", "订阅源中未找到有效的 resolvers 列表", 400)

        imported: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        with self._lock:
            doc = self._load_locked()
            for item in raw_items[:_MAX_RESOLVERS]:
                if not isinstance(item, dict):
                    continue
                try:
                    # Subscriptions must NEVER inject untrusted api_key/token or arbitrary local sidecar flags.
                    sanitized_item = {k: v for k, v in item.items() if k not in {"api_key", "token", "is_sidecar"}}
                    normalized = normalize_resolver_entry({**sanitized_item, "subscription_url": validated_sub})
                    normalized["is_sidecar"] = False
                    existing = doc["resolvers"].get(normalized["id"])
                    if isinstance(existing, dict):
                        if existing.get("subscription_url") != validated_sub:
                            skipped.append({"id": normalized["id"], "reason": "independent_entry"})
                            continue
                        if _origin(existing["url"]) == _origin(normalized["url"]):
                            normalized["api_key"] = str(existing.get("api_key") or "")
                    else:
                        normalized["api_key"] = ""
                    if normalized["id"] not in doc["resolvers"] and len(doc["resolvers"]) >= _MAX_RESOLVERS:
                        skipped.append({"id": normalized["id"], "reason": "limit_reached"})
                        continue
                    doc["resolvers"][normalized["id"]] = normalized
                    imported.append(self._to_public(normalized))
                except AppError as exc:
                    skipped.append({"id": str(item.get("id") or item.get("url") or "?"), "reason": exc.message})
            if validated_sub not in doc["subscriptions"]:
                doc["subscriptions"].append(validated_sub)
            self._save_locked(doc)
        return {"subscription_url": validated_sub, "imported": imported, "skipped": skipped}


def fetch_subscription_json(subscription_url: str, http_client: httpx.Client | None = None, *, timeout: float = 8.0) -> dict[str, Any]:
    """Fetch a Resolver v2 subscription JSON document with strict SSRF, DNS-pinning & streaming size bounds."""
    from .external_sources import _pinned_request, sanitize_outbound_headers, validated_addresses

    try:
        validated = validate_custom_external_api(subscription_url)
    except ValueError as exc:
        raise AppError("RESOLVER_SUBSCRIPTION_INVALID", str(exc), 400) from exc
    if not validated:
        raise AppError("RESOLVER_SUBSCRIPTION_INVALID", "缺少订阅地址", 400)

    addresses = validated_addresses(validated, loopback_bases=(validated,))
    if not addresses:
        raise AppError("RESOLVER_SUBSCRIPTION_REJECTED", "订阅地址不被允许", 403)

    headers = sanitize_outbound_headers({"Accept": "application/json"})
    owns = http_client is None
    client = http_client or httpx.Client(follow_redirects=False, trust_env=False, cookies=NoOpCookies(), timeout=timeout)
    try:
        req = _pinned_request("GET", validated, addresses, headers, timeout)
        with closing(isolate_cookie_jar(client).send(req, auth=None, follow_redirects=False, stream=True)) as resp:
            if resp.status_code != 200:
                raise AppError("SUBSCRIPTION_FETCH_FAILED", f"订阅地址返回 HTTP {resp.status_code}", 502)
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_SUBSCRIPTION_BYTES:
                    raise AppError("SUBSCRIPTION_TOO_LARGE", "订阅文件内容过大", 413)
                chunks.append(chunk)
            raw_bytes = b"".join(chunks)
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict):
            raise AppError("SUBSCRIPTION_INVALID", "订阅内容必须是 JSON 对象", 400)
        return data
    except AppError:
        raise
    except (httpx.HTTPError, ValueError, OSError) as exc:
        raise AppError("SUBSCRIPTION_FETCH_FAILED", f"获取订阅失败：{exc}", 502) from exc
    finally:
        if owns:
            client.close()


def fetch_resolver_manifest(
    base_url: str,
    http_client: httpx.Client,
    *,
    api_key: str = "",
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Probe GET {base_url}/manifest for HTTP Resolver Contract v2 capabilities."""
    from .external_sources import _scoped_channel_request

    try:
        validated = validate_custom_external_api(base_url).rstrip("/")
    except ValueError:
        return None
    if not validated:
        return None
    secret_headers: dict[str, str] = {}
    if api_key and api_key.strip():
        secret_headers["X-API-Key"] = api_key.strip()
        secret_headers["X-Resolver-Token"] = api_key.strip()
    manifest_url = f"{validated}/manifest"
    resp = _scoped_channel_request(
        http_client,
        "GET",
        manifest_url,
        channel_id="custom",
        headers={"Accept": "application/json"},
        secret_headers=secret_headers,
        validated_api_base=validated,
        timeout=timeout,
    )
    if resp is None or resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    ver = payload.get("protocol_version")
    if ver not in (2, "2"):
        return None
    caps = payload.get("capabilities")
    if not isinstance(caps, list):
        caps = ["search", "resolve"]
    return {
        "protocol_version": 2,
        "id": str(payload.get("id") or "").strip(),
        "name": str(payload.get("name") or "").strip(),
        "version": str(payload.get("version") or "").strip(),
        "capabilities": [str(c).strip().lower() for c in caps if str(c).strip()],
        "sources": list(payload.get("sources") or []) if isinstance(payload.get("sources"), list) else [],
        "rate_limit": payload.get("rate_limit") if isinstance(payload.get("rate_limit"), dict) else {},
    }


def fetch_resolver_lyric(
    base_url: str,
    http_client: httpx.Client,
    *,
    track_id: str = "",
    title: str = "",
    artist: str = "",
    api_key: str = "",
    timeout: float = 5.0,
) -> dict[str, Any] | None:
    """Query POST/GET {base_url}/lyric on an HTTP Resolver v2 endpoint."""
    from .external_sources import _scoped_channel_request

    try:
        validated = validate_custom_external_api(base_url).rstrip("/")
    except ValueError:
        return None
    if not validated:
        return None
    secret_headers: dict[str, str] = {}
    if api_key and api_key.strip():
        secret_headers["X-API-Key"] = api_key.strip()
        secret_headers["X-Resolver-Token"] = api_key.strip()
    lyric_url = f"{validated}/lyric"
    body = {"id": track_id, "title": title, "artist": artist}
    resp = _scoped_channel_request(
        http_client,
        "POST",
        lyric_url,
        channel_id="custom",
        json_body=body,
        headers={"Accept": "application/json"},
        secret_headers=secret_headers,
        validated_api_base=validated,
        timeout=timeout,
    )
    if resp is not None and resp.status_code == 405:
        resp = _scoped_channel_request(
            http_client,
            "GET",
            lyric_url,
            channel_id="custom",
            params=body,
            headers={"Accept": "application/json"},
            secret_headers=secret_headers,
            validated_api_base=validated,
            timeout=timeout,
        )
    if resp is None or resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    lrc = str(payload.get("lrc") or payload.get("lyric") or "").strip()
    tlyric = str(payload.get("tlyric") or "").strip()
    romalrc = str(payload.get("romalrc") or "").strip()
    yrc = str(payload.get("yrc") or "").strip()
    if not any((lrc, tlyric, romalrc, yrc)):
        return None
    return {"lrc": lrc, "tlyric": tlyric, "romalrc": romalrc, "yrc": yrc, "source": f"resolver_v2:{urlsplit(validated).netloc}"}
