from __future__ import annotations

import base64
import ctypes
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import AppError
from .models import AccountSummary

_LOG = logging.getLogger("pruner_studio.storage")
_PLAINTEXT_WARNING_EMITTED = False
_KEYRING_SERVICE_NAME = "Pruner-Studio"

if sys.platform == "win32":
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]


_CRYPTPROTECT_UI_FORBIDDEN = 0x01


def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _load_keyring_module() -> Any | None:
    if sys.platform == "win32":
        return None
    try:
        import keyring  # type: ignore[import-not-found]

        backend = keyring.get_keyring()
        backend_name = type(backend).__name__.lower()
        # Reject fail/null/plaintext backends that do not provide real OS keychain protection.
        if any(bad in backend_name for bad in ("fail", "null", "chainer", "plaintext")):
            return None
        return keyring
    except Exception:
        return None


def _keyring_available() -> bool:
    return _load_keyring_module() is not None


def _keyring_set(entry_key: str, secret_value: str) -> bool:
    kr = _load_keyring_module()
    if kr is None:
        return False
    try:
        kr.set_password(_KEYRING_SERVICE_NAME, entry_key, secret_value)
        return True
    except Exception:
        return False


def _keyring_get(entry_key: str) -> str | None:
    kr = _load_keyring_module()
    if kr is None:
        return None
    try:
        val = kr.get_password(_KEYRING_SERVICE_NAME, entry_key)
        return val if isinstance(val, str) and val else None
    except Exception:
        return None


def _keyring_delete(entry_key: str) -> None:
    kr = _load_keyring_module()
    if kr is None:
        return
    try:
        kr.delete_password(_KEYRING_SERVICE_NAME, entry_key)
    except Exception:
        pass


def credential_protection_mode() -> str:
    """Report the active credential-at-rest protection mechanism for diagnostics/UI."""
    if _dpapi_available():
        return "dpapi"
    if _keyring_available():
        return "keyring"
    return "plaintext_file_0600"


def _warn_plaintext_fallback(context: str) -> None:
    global _PLAINTEXT_WARNING_EMITTED
    if not _PLAINTEXT_WARNING_EMITTED:
        _PLAINTEXT_WARNING_EMITTED = True
        _LOG.warning(
            "OS keyring unavailable on %s while storing %s; falling back to mode 0600 private file. "
            "Install `keyring` with a system keychain backend for encrypted at-rest credentials.",
            sys.platform,
            context,
        )


def _dpapi_protect(plaintext: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(plaintext, len(plaintext))
    in_blob = _DataBlob(len(plaintext), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Pruner Studio Session",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    buffer = ctypes.create_string_buffer(ciphertext, len(ciphertext))
    in_blob = _DataBlob(len(ciphertext), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


class JsonStore:
    """Small JSON store with serialized, atomic writes."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, name: str) -> Path:
        if not name or Path(name).name != name or not name.endswith(".json"):
            raise ValueError("invalid store name")
        return self.data_dir / name

    def read(self, name: str, default: Any) -> Any:
        target = self.path(name)
        with self._lock:
            try:
                with target.open("r", encoding="utf-8") as handle:
                    return json.load(handle)
            except FileNotFoundError:
                return default
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AppError(
                    "LOCAL_DATA_CORRUPT",
                    f"本地数据文件 {name} 已损坏；原文件已保留，请先恢复或移走后再继续",
                    500,
                ) from exc
            except OSError as exc:
                raise AppError(
                    "LOCAL_DATA_UNREADABLE",
                    f"无法读取本地数据文件 {name}；未对其进行覆盖",
                    500,
                ) from exc

    def write(self, name: str, value: Any, *, private: bool = False) -> None:
        target = self.path(name)
        payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        with self._lock:
            fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            try:
                if private and os.name != "nt":
                    try:
                        os.chmod(temporary, 0o600)
                    except OSError:
                        pass
                with os.fdopen(fd, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def delete(self, name: str) -> None:
        target = self.path(name)
        with self._lock:
            try:
                target.unlink()
            except FileNotFoundError:
                pass

    def quarantine(self, name: str) -> Path | None:
        target = self.path(name)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantined = target.with_name(f"{target.stem}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}.json")
        with self._lock:
            try:
                os.replace(target, quarantined)
            except FileNotFoundError:
                return None
        return quarantined


class _LegacySession:
    FILE_NAME = "session.json"

    def __init__(self, store: JsonStore):
        self.store = store

    def load_cookie(self) -> str | None:
        missing = object()
        try:
            value = self.store.read(self.FILE_NAME, missing)
        except AppError as exc:
            if exc.code != "LOCAL_DATA_CORRUPT":
                raise
            self.store.quarantine(self.FILE_NAME)
            return None
        if value is missing:
            return None
        if not isinstance(value, dict):
            self.store.quarantine(self.FILE_NAME)
            return None

        if value.get("version") == 2 and value.get("encoding") == "dpapi":
            encoded = value.get("data")
            if not isinstance(encoded, str) or not encoded or not _dpapi_available():
                self.store.quarantine(self.FILE_NAME)
                return None
            try:
                raw = base64.b64decode(encoded.encode("ascii"), validate=True)
                cookie = _dpapi_unprotect(raw).decode("utf-8")
            except (ValueError, OSError, UnicodeDecodeError):
                self.store.quarantine(self.FILE_NAME)
                return None
            if not cookie or "\r" in cookie or "\n" in cookie:
                self.store.quarantine(self.FILE_NAME)
                return None
            return cookie

        if value.get("version") == 2 and value.get("encoding") == "keyring":
            ref = value.get("ref")
            if not isinstance(ref, str) or not ref:
                self.store.quarantine(self.FILE_NAME)
                return None
            cookie = _keyring_get(ref)
            if not cookie or "\r" in cookie or "\n" in cookie:
                return None
            return cookie

        if "cookie" in value:
            cookie = value.get("cookie")
            if not isinstance(cookie, str) or not cookie or "\r" in cookie or "\n" in cookie:
                self.store.quarantine(self.FILE_NAME)
                return None
            if _dpapi_available() or _keyring_available():
                try:
                    self.save_cookie(cookie)
                except OSError:
                    pass
            return cookie

        self.store.quarantine(self.FILE_NAME)
        return None

    def save_cookie(self, cookie: str) -> None:
        if not isinstance(cookie, str) or not cookie or "\r" in cookie or "\n" in cookie:
            raise ValueError("invalid session cookie")
        if _dpapi_available():
            encrypted = _dpapi_protect(cookie.encode("utf-8"))
            encoded = base64.b64encode(encrypted).decode("ascii")
            self.store.write(
                self.FILE_NAME,
                {"version": 2, "encoding": "dpapi", "data": encoded},
                private=True,
            )
            return
        ref = f"legacy-session:{self.store.data_dir.resolve()}"
        if _keyring_set(ref, cookie):
            self.store.write(
                self.FILE_NAME,
                {"version": 2, "encoding": "keyring", "ref": ref},
                private=True,
            )
            return
        _warn_plaintext_fallback("legacy session")
        self.store.write(self.FILE_NAME, {"cookie": cookie}, private=True)

    def clear(self) -> None:
        _keyring_delete(f"legacy-session:{self.store.data_dir.resolve()}")
        self.store.delete(self.FILE_NAME)


class SessionStore:
    """The v3 vault owns named sessions. The legacy file mirrors the active cookie."""
    FILE_NAME = "sessions.json"

    def __init__(self, store: JsonStore):
        self.store = store
        self.legacy = _LegacySession(store)
        self.lock = store._lock

    def _read(self):
        try:
            vault = self.store.read(self.FILE_NAME, None)
            if vault is not None:
                if isinstance(vault, dict) and vault.get("version") == 3 and isinstance(vault.get("accounts"), dict):
                    aid = vault.get("active_account_id")
                    if "active_account_id" in vault and (aid is None or isinstance(aid, str) and aid in vault["accounts"]):
                        return vault
                raise AppError("LOCAL_DATA_CORRUPT", "账号存储格式无效")
        except AppError as exc:
            if exc.code != "LOCAL_DATA_CORRUPT":
                raise
            self.store.quarantine(self.FILE_NAME)
            self.legacy.clear()
        return {"version": 3, "active_account_id": None, "accounts": {}}

    def _encode(self, cookie: str, account_id: str = ""):
        if not isinstance(cookie, str) or not cookie or any(c in cookie for c in "\r\n"):
            raise ValueError("invalid session cookie")
        if _dpapi_available():
            return {"encoding": "dpapi", "data": base64.b64encode(_dpapi_protect(cookie.encode())).decode()}
        ref = f"session:{self.store.data_dir.resolve()}:{account_id or 'default'}"
        if _keyring_set(ref, cookie):
            return {"encoding": "keyring", "ref": ref}
        _warn_plaintext_fallback("account session")
        return {"encoding": "plain", "data": cookie}

    def save_account_session(self, account: AccountSummary, cookie: str):
        aid = str(account.user_id)
        with self.lock:
            vault = self._read()
            vault["accounts"][aid] = {**self._encode(cookie, aid), "display_name": account.display_name,
                                      "avatar_url": account.avatar_url, "vip_type": account.vip_type,
                                      "updated_at": datetime.now(timezone.utc).isoformat()}
            vault["active_account_id"] = aid
            self.legacy.save_cookie(cookie)
            self.store.write(self.FILE_NAME, vault, private=True)

    @property
    def active_account_id(self):
        with self.lock:
            return self._read()["active_account_id"]

    def list_saved_accounts(self) -> list[dict]:
        with self.lock:
            return [{"account_id": aid, **{k: entry.get(k) for k in
                    ("display_name", "avatar_url", "vip_type", "updated_at")}}
                    for aid, entry in self._read()["accounts"].items() if isinstance(entry, dict)]

    def load_account_cookie(self, account_id: str) -> str | None:
        with self.lock:
            entry = self._read()["accounts"].get(str(account_id))
            if not isinstance(entry, dict):
                return None
            try:
                if entry["encoding"] == "dpapi" and _dpapi_available():
                    value = _dpapi_unprotect(base64.b64decode(entry["data"], validate=True)).decode()
                elif entry["encoding"] == "keyring" and isinstance(entry.get("ref"), str):
                    value = _keyring_get(entry["ref"])
                    if value and any(key in entry for key in ("data", "cookie")):
                        vault = self._read()
                        stored = vault["accounts"][str(account_id)]
                        stored.pop("data", None)
                        stored.pop("cookie", None)
                        self.store.write(self.FILE_NAME, vault, private=True)
                elif entry["encoding"] == "plain":
                    value = entry["data"]
                    if _dpapi_available() or _keyring_available():
                        encoded = self._encode(value, str(account_id))
                        if encoded["encoding"] != "plain":
                            vault = self._read()
                            metadata = {k: entry[k] for k in ("display_name", "avatar_url", "vip_type", "updated_at") if k in entry}
                            vault["accounts"][str(account_id)] = {**metadata, **encoded}
                            self.legacy.load_cookie()  # migrate its independent plaintext copy as well
                            self.store.write(self.FILE_NAME, vault, private=True)
                else:
                    return None
                return value if isinstance(value, str) and value and not any(c in value for c in "\r\n") else None
            except (KeyError, ValueError, OSError, TypeError):
                return None

    def set_active_account(self, account_id: str | None) -> str | None:
        with self.lock:
            cookie = self.load_account_cookie(account_id) if account_id is not None else None
            vault = self._read()
            if account_id is not None and not cookie:
                return None
            if cookie:
                self.legacy.save_cookie(cookie)
            else:
                self.legacy.clear()
            vault["active_account_id"] = account_id
            self.store.write(self.FILE_NAME, vault)
            return cookie

    def remove_account_session(self, account_id: str) -> bool:
        with self.lock:
            vault = self._read()
            removed = vault["accounts"].pop(str(account_id), None)
            if removed is None:
                return False
            if isinstance(removed, dict) and removed.get("encoding") == "keyring" and isinstance(removed.get("ref"), str):
                _keyring_delete(removed["ref"])
            if vault["active_account_id"] == str(account_id):
                vault["active_account_id"] = None
                self.legacy.clear()
            self.store.write(self.FILE_NAME, vault, private=True)
            return True

    def load_cookie(self):
        with self.lock:
            aid = self.active_account_id
            return self.load_account_cookie(aid) if aid else self.legacy.load_cookie()

    def save_cookie(self, cookie: str):
        with self.lock:
            vault = self._read()
            self.legacy.save_cookie(cookie)
            # An identity-free cookie is promoted when /user/account verifies its UID.
            vault["active_account_id"] = None
            self.store.write(self.FILE_NAME, vault)

    def clear(self):
        with self.lock:
            if self.active_account_id:
                self.remove_account_session(self.active_account_id)
            else:
                self.legacy.clear()


class DeviceIdentityStore:
    """Persists a stable per-installation deviceId so requests do not rotate IDs per launch."""

    FILE_NAME = "device_identity.json"

    def __init__(self, store: JsonStore):
        self.store = store
        self._lock = threading.RLock()

    def get_or_create_device_id(self) -> str:
        with self._lock:
            try:
                raw = self.store.read(self.FILE_NAME, None)
            except AppError:
                raw = None
            if isinstance(raw, dict):
                existing = str(raw.get("device_id") or "").strip()
                if re.fullmatch(r"[0-9a-fA-F]{16,64}", existing):
                    return existing
            device_id = secrets.token_hex(16)
            self.store.write(
                self.FILE_NAME,
                {
                    "version": 1,
                    "device_id": device_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                private=True,
            )
            return device_id


DEFAULT_APPEARANCE: dict[str, Any] = {
    "theme": "dark",
    "accent_mode": "preset",
    "accent": "#62a368",
    "density": "comfortable",
    "font_scale": 1.0,
}

DEFAULT_DESKTOP_LYRICS_STYLE: dict[str, Any] = {
    "font_size": 28,
    "played_color": "#62a368",
    "unplayed_color": "#f5f5f7",
    "stroke": False,
    "bg_opacity": 0.0,
    "preset": "cinema_white",
    "surface_style": "borderless",
    "shadow_strength": 1.0,
    "font_weight": 700,
    "align": "center",
    "mode": "double",
    "show_translation": True,
    "wheel_volume": False,
}

DEFAULT_USER_SETTINGS: dict[str, Any] = {
    "quality_preference": "jymaster",
    "quality_arbitration": "best_quality_race",
    "enable_external_sources": True,
    "external_providers": ["kugou_lite", "migu", "qq", "kuwo", "bilibili", "gdstudio"],
    "custom_external_api": "",
    "local_music_dirs": [],
    "visual_mode": "soft_dynamic",
    "minimize_to_tray": True,
    "loudness_normalization": True,
    "crossfade_ms": 150,
    "audio_cache_limit_mb": 512,
    "lyrics_blur_depth": False,
    "audio_reactive_glow": True,
    "enable_amll_ttml_fallback": True,
    "sync_fm_trash_to_netease": False,
    "global_hotkeys_enabled": True,
    "smart_radio_explore_ratio": 0.4,
    "appearance": deepcopy(DEFAULT_APPEARANCE),
    "desktop_lyrics_style": deepcopy(DEFAULT_DESKTOP_LYRICS_STYLE),
    "desktop_lyrics_bounds": None,
    "window_bounds": None,
    "gpu_performance_mode": "balanced",
    "game_mode": False,
    "stage_theme": "fluid",
    "stage_lyric_size": 24,
    "stage_lyric_align": "left",
    "visualizer_mode": "bars",
    "enable_3d_effects": True,
    "eq_enabled": False,
    "eq_preset": "flat",
    "eq_bands": [0.0] * 10,
    "playback_rate": 1.0,
    "audio_output_device_id": "",
}

VALID_QUALITIES = frozenset({"jymaster", "hires", "lossless", "exhigh", "standard", "sky"})
VALID_QUALITY_ARBITRATIONS = frozenset({"best_quality_race", "netease_first"})
VALID_EXTERNAL_PROVIDERS = frozenset(
    {
        "kugou_lite",
        "migu",
        "qq",
        "kuwo",
        "kugou",
        "bilibili",
        "gdstudio",
        "joox",
        "custom",
        "custom_endpoint",
        "opensubsonic",
        "webdav",
    }
)
VALID_VISUAL_MODES = frozenset({"soft_dynamic", "quiet_eco", "quiet", "quiet_power_save", "power_save", "static"})
VALID_APPEARANCE_THEMES = frozenset({"dark", "midnight", "light", "celadon", "parchment", "auto", "nordic", "espresso", "emerald", "dynamic"})
VALID_ACCENT_MODES = frozenset({"preset", "custom", "cover"})
VALID_APPEARANCE_DENSITIES = frozenset({"comfortable", "compact"})
VALID_LYRICS_ALIGNS = frozenset({"left", "center", "right"})
VALID_LYRICS_MODES = frozenset({"single", "double"})
VALID_LYRICS_SURFACE_STYLES = frozenset({"borderless", "pill", "acrylic"})
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_NESTED_SETTINGS_KEYS = ("appearance", "desktop_lyrics_style")

SUPPORTED_CHANNELS = (
    "kugou_lite",
    "migu",
    "qq",
    "kuwo",
    "bilibili",
    "gdstudio",
    "custom",
    "opensubsonic",
    "webdav",
)
SELF_HOSTED_PROTOCOL_CHANNELS = frozenset({"opensubsonic", "webdav"})
CHANNEL_SECRET_KEYS = ("cookie", "token", "userid", "dfid", "sessdata", "uin", "api_key", "password")
CHANNEL_PUBLIC_EXTRA_KEYS = ("username", "music_path", "sync_path")
CHANNEL_DEFAULTS: dict[str, dict[str, Any]] = {
    "kugou_lite": {"enabled": True, "display_name": "酷狗概念版", "api_base": ""},
    "migu": {"enabled": True, "display_name": "咪咕音乐", "api_base": ""},
    "qq": {"enabled": True, "display_name": "QQ音乐", "api_base": ""},
    "kuwo": {"enabled": True, "display_name": "酷我音乐", "api_base": ""},
    "bilibili": {"enabled": True, "display_name": "B站音频", "api_base": ""},
    "gdstudio": {"enabled": True, "display_name": "GDStudio", "api_base": ""},
    "custom": {"enabled": False, "display_name": "HTTP Resolver v2 / 自定义接口", "api_base": ""},
    "opensubsonic": {"enabled": False, "display_name": "OpenSubsonic / Navidrome", "api_base": ""},
    "webdav": {"enabled": False, "display_name": "WebDAV 音乐库与同步", "api_base": ""},
}


def validate_self_hosted_api(url: str) -> str:
    """Validate self-hosted OpenSubsonic/WebDAV server URL (permits user LAN/loopback, blocks cloud metadata)."""
    cleaned = str(url or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > 512:
        raise ValueError("自建服务器地址过长")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("自建服务器地址必须是有效的 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("请通过独立的用户名和密码字段填写凭据，不要内嵌在 URL 中")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("自建服务器端口无效") from exc
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("自建服务器缺少有效主机名")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        is_local_lan = host in {"localhost"} or host.endswith(".local") or host.endswith(".lan")
        if parsed.scheme == "http" and not is_local_lan:
            raise ValueError("公网自建服务器必须使用 HTTPS（本机或局域网地址可使用 HTTP）")
    else:
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("自建服务器不允许指向链路本地或保留地址")
        if parsed.scheme == "http" and not (ip.is_private or ip.is_loopback):
            raise ValueError("公网 IP 自建服务器必须使用 HTTPS")
    return cleaned.rstrip("/")


def validate_custom_external_api(url: str) -> str:
    """Validate user-configured custom external resolver URL against SSRF and plaintext leakage."""
    cleaned = str(url or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > 512:
        raise ValueError("自定义外部音源接口地址过长")
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("自定义外部音源接口必须是有效的 HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("自定义外部音源接口不允许包含用户名或密码")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("自定义外部音源接口端口无效") from exc
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("自定义外部音源接口缺少有效主机名")
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme == "http" and not is_loopback:
        raise ValueError("非本机自定义外部音源接口必须使用 HTTPS")
    if not is_loopback:
        if host.endswith(".local") or host.endswith(".internal"):
            raise ValueError("自定义外部音源接口不允许指向内网域名")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
                or ip.is_unspecified
            ):
                raise ValueError("自定义外部音源接口不允许指向内网或保留 IP 地址")
    return cleaned


class SettingsStore:
    FILE_NAME = "settings.json"

    def __init__(self, store: JsonStore):
        self.hotkey_status = lambda: {"hotkeys_supported": False, "hotkey_registration": {}}
        self.store = store
        self._lock = threading.RLock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            raw = self.store.read(self.FILE_NAME, None)
            if raw is None:
                return {**deepcopy(DEFAULT_USER_SETTINGS), **self.hotkey_status()}
            if not isinstance(raw, dict):
                raise AppError("LOCAL_DATA_INVALID", "本地设置文件结构无效；原文件已保留，未执行覆盖", 500)
            merged = deepcopy(DEFAULT_USER_SETTINGS)
            for key in DEFAULT_USER_SETTINGS:
                if key not in raw:
                    continue
                if key in _NESTED_SETTINGS_KEYS and isinstance(raw[key], dict):
                    merged[key] = {**merged[key], **raw[key]}
                else:
                    merged[key] = raw[key]
            return {**self._normalize(merged, strict=False), **self.hotkey_status()}

    def save(self, updates: Any) -> dict[str, Any]:
        if not isinstance(updates, dict):
            raise AppError("SETTINGS_INVALID", "设置内容必须是 JSON 对象")
        with self._lock:
            current = self.load()
            merged = deepcopy(current)
            for key in DEFAULT_USER_SETTINGS:
                if key not in updates:
                    continue
                incoming = updates[key]
                if key in _NESTED_SETTINGS_KEYS:
                    if isinstance(incoming, dict) and isinstance(merged.get(key), dict):
                        merged[key] = {**merged[key], **incoming}
                    else:
                        merged[key] = incoming
                elif key == "desktop_lyrics_bounds":
                    if incoming is None:
                        merged[key] = None
                    elif isinstance(incoming, dict) and isinstance(merged.get(key), dict):
                        merged[key] = {**merged[key], **incoming}
                    else:
                        merged[key] = incoming
                else:
                    merged[key] = incoming
            validated = self._normalize(merged, strict=True)
            self.store.write(self.FILE_NAME, validated)
            return validated

    @staticmethod
    def _normalize(data: dict[str, Any], *, strict: bool) -> dict[str, Any]:
        quality = str(data.get("quality_preference") or DEFAULT_USER_SETTINGS["quality_preference"]).strip().lower()
        if quality not in VALID_QUALITIES:
            if strict:
                raise AppError("SETTINGS_INVALID", "音质偏好设置无效")
            quality = DEFAULT_USER_SETTINGS["quality_preference"]

        enable_external = data.get("enable_external_sources")
        if strict and not isinstance(enable_external, bool):
            raise AppError("SETTINGS_INVALID", "外部音源开关必须是布尔值")
        enable_external_val = bool(enable_external) if isinstance(enable_external, bool) else True

        raw_providers = data.get("external_providers")
        if not isinstance(raw_providers, list):
            if strict:
                raise AppError("SETTINGS_INVALID", "外部音源列表必须是数组")
            providers = list(DEFAULT_USER_SETTINGS["external_providers"])
        else:
            providers = []
            for item in raw_providers:
                name = str(item or "").strip().lower()
                if name not in VALID_EXTERNAL_PROVIDERS:
                    if strict:
                        raise AppError("SETTINGS_INVALID", f"不支持的外部音源：{name}")
                    continue
                if name not in providers:
                    providers.append(name)

        raw_custom = str(data.get("custom_external_api") or "").strip()
        if raw_custom:
            try:
                raw_custom = validate_custom_external_api(raw_custom)
            except ValueError as exc:
                if strict:
                    raise AppError("SETTINGS_INVALID", str(exc)) from exc
                raw_custom = ""

        raw_dirs = data.get("local_music_dirs")
        if not isinstance(raw_dirs, list):
            if strict:
                raise AppError("SETTINGS_INVALID", "本地曲库目录必须是数组")
            local_dirs: list[str] = []
        else:
            if strict and len(raw_dirs) > 20:
                raise AppError("SETTINGS_INVALID", "本地曲库目录最多配置 20 个")
            local_dirs = []
            for item in raw_dirs:
                if not isinstance(item, str):
                    if strict:
                        raise AppError("SETTINGS_INVALID", "本地曲库目录必须是路径字符串")
                    continue
                cleaned = item.strip()
                if cleaned and cleaned not in local_dirs:
                    local_dirs.append(cleaned)

        visual_mode = str(data.get("visual_mode") or DEFAULT_USER_SETTINGS["visual_mode"]).strip().lower()
        if visual_mode not in VALID_VISUAL_MODES:
            if strict:
                raise AppError("SETTINGS_INVALID", "视觉动效模式无效")
            visual_mode = DEFAULT_USER_SETTINGS["visual_mode"]

        minimize = data.get("minimize_to_tray")
        if strict and not isinstance(minimize, bool):
            raise AppError("SETTINGS_INVALID", "最小化到托盘选项必须是布尔值")
        minimize_val = bool(minimize) if isinstance(minimize, bool) else True

        arbitration = str(
            data.get("quality_arbitration") or DEFAULT_USER_SETTINGS["quality_arbitration"]
        ).strip().lower()
        if arbitration not in VALID_QUALITY_ARBITRATIONS:
            if strict:
                raise AppError("SETTINGS_INVALID", "跨渠道音质仲裁策略无效")
            arbitration = DEFAULT_USER_SETTINGS["quality_arbitration"]

        def _bool_field(field_name: str, label: str, default_val: bool) -> bool:
            raw_v = data.get(field_name, default_val)
            if strict and not isinstance(raw_v, bool):
                raise AppError("SETTINGS_INVALID", f"{label}必须是布尔值")
            return bool(raw_v) if isinstance(raw_v, bool) else default_val

        loudness_val = _bool_field("loudness_normalization", "响度均衡", True)
        blur_depth_val = _bool_field("lyrics_blur_depth", "歌词景深模糊", False)
        reactive_glow_val = _bool_field("audio_reactive_glow", "低频律动光晕", True)
        amll_ttml_val = _bool_field("enable_amll_ttml_fallback", "AMLL 逐字歌词补全", True)
        hotkeys_val = _bool_field("global_hotkeys_enabled", "全局快捷键", True)

        def _int_range(field_name: str, label: str, default_val: int, lo: int, hi: int) -> int:
            raw_v = data.get(field_name, default_val)
            if isinstance(raw_v, bool):
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须是整数")
                return default_val
            try:
                parsed_v = int(raw_v)
            except (TypeError, ValueError) as exc:
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须是整数") from exc
                return default_val
            if parsed_v < lo or parsed_v > hi:
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须在 {lo}–{hi} 之间")
                return max(lo, min(hi, parsed_v))
            return parsed_v

        crossfade_val = _int_range("crossfade_ms", "淡入淡出时长", 150, 0, 500)
        cache_limit_val = _int_range("audio_cache_limit_mb", "音频磁盘缓存上限", 512, 0, 4096)

        raw_explore = data.get("smart_radio_explore_ratio", 0.4)
        if isinstance(raw_explore, bool):
            if strict:
                raise AppError("SETTINGS_INVALID", "心动电台探索比例必须是数值")
            explore_val = 0.4
        else:
            try:
                explore_val = float(raw_explore)
            except (TypeError, ValueError) as exc:
                if strict:
                    raise AppError("SETTINGS_INVALID", "心动电台探索比例必须是数值") from exc
                explore_val = 0.4
            if not math.isfinite(explore_val) or explore_val < 0.0 or explore_val > 1.0:
                if strict:
                    raise AppError("SETTINGS_INVALID", "心动电台探索比例必须在 0.0–1.0 之间")
                explore_val = 0.4 if not math.isfinite(explore_val) else max(0.0, min(1.0, explore_val))
            explore_val = round(explore_val, 3)

        def _hex_color(raw_val: Any, label: str, default_val: str) -> str:
            if not isinstance(raw_val, str):
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须是 #rrggbb 十六进制颜色")
                return default_val
            cleaned = raw_val.strip()
            if not _HEX_COLOR_RE.fullmatch(cleaned):
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须是有效的 #rrggbb 颜色值")
                return default_val
            return cleaned.lower()

        def _enum_str(raw_val: Any, valid: frozenset[str], label: str, default_val: str) -> str:
            if not isinstance(raw_val, str):
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}无效")
                return default_val
            cleaned = raw_val.strip().lower()
            if cleaned not in valid:
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}无效")
                return default_val
            return cleaned

        def _nested_bool(src: dict[str, Any], key: str, label: str, default_val: bool) -> bool:
            raw_v = src.get(key, default_val)
            if strict and not isinstance(raw_v, bool):
                raise AppError("SETTINGS_INVALID", f"{label}必须是布尔值")
            return bool(raw_v) if isinstance(raw_v, bool) else default_val

        def _nested_int_range(src: dict[str, Any], key: str, label: str, default_val: int, lo: int, hi: int) -> int:
            raw_v = src.get(key, default_val)
            if isinstance(raw_v, bool) or (isinstance(raw_v, float) and not raw_v.is_integer()):
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须是整数")
                return default_val
            try:
                parsed_v = int(raw_v)
            except (TypeError, ValueError) as exc:
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须是整数") from exc
                return default_val
            if parsed_v < lo or parsed_v > hi:
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须在 {lo}–{hi} 之间")
                return max(lo, min(hi, parsed_v))
            return parsed_v

        def _nested_float_range(
            src: dict[str, Any], key: str, label: str, default_val: float, lo: float, hi: float
        ) -> float:
            raw_v = src.get(key, default_val)
            if isinstance(raw_v, bool):
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须是数值")
                return default_val
            try:
                parsed_v = float(raw_v)
            except (TypeError, ValueError) as exc:
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须是数值") from exc
                return default_val
            if not math.isfinite(parsed_v) or parsed_v < lo or parsed_v > hi:
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{label}必须在 {lo}–{hi} 之间")
                return default_val if not math.isfinite(parsed_v) else round(max(lo, min(hi, parsed_v)), 3)
            return round(parsed_v, 3)

        # --- Appearance ---
        raw_appearance = data.get("appearance")
        if not isinstance(raw_appearance, dict):
            if strict:
                raise AppError("SETTINGS_INVALID", "外观设置必须是 JSON 对象")
            appearance_val = deepcopy(DEFAULT_APPEARANCE)
        else:
            appearance_val = {
                "theme": _enum_str(
                    raw_appearance.get("theme", DEFAULT_APPEARANCE["theme"]),
                    VALID_APPEARANCE_THEMES,
                    "主题模式",
                    DEFAULT_APPEARANCE["theme"],
                ),
                "accent_mode": _enum_str(
                    raw_appearance.get("accent_mode", DEFAULT_APPEARANCE["accent_mode"]),
                    VALID_ACCENT_MODES,
                    "主题色模式",
                    DEFAULT_APPEARANCE["accent_mode"],
                ),
                "accent": _hex_color(
                    raw_appearance.get("accent", DEFAULT_APPEARANCE["accent"]),
                    "主题强调色",
                    DEFAULT_APPEARANCE["accent"],
                ),
                "density": _enum_str(
                    raw_appearance.get("density", DEFAULT_APPEARANCE["density"]),
                    VALID_APPEARANCE_DENSITIES,
                    "界面密度",
                    DEFAULT_APPEARANCE["density"],
                ),
                "font_scale": _nested_float_range(
                    raw_appearance,
                    "font_scale",
                    "字体缩放比例",
                    DEFAULT_APPEARANCE["font_scale"],
                    0.9,
                    1.25,
                ),
            }

        # --- Desktop Lyrics Style ---
        raw_lyrics_style = data.get("desktop_lyrics_style")
        if not isinstance(raw_lyrics_style, dict):
            if strict:
                raise AppError("SETTINGS_INVALID", "桌面歌词样式必须是 JSON 对象")
            lyrics_style_val = deepcopy(DEFAULT_DESKTOP_LYRICS_STYLE)
        else:
            presets = {
                "cinema_white", "emerald_flow", "vinyl_gold", "light_desktop_ink",
                "sakura_mist", "game_contrast", "apple_glass", "neon_duo", "custom",
            }
            lyrics_style_val = {
                "preset": _enum_str(raw_lyrics_style.get("preset"), presets, "歌词预设", "cinema_white"),
                "surface_style": _enum_str(
                    raw_lyrics_style.get("surface_style", DEFAULT_DESKTOP_LYRICS_STYLE["surface_style"]),
                    VALID_LYRICS_SURFACE_STYLES,
                    "桌面歌词外观形态",
                    DEFAULT_DESKTOP_LYRICS_STYLE["surface_style"],
                ),
                "font_weight": _nested_int_range(raw_lyrics_style, "font_weight", "字重", 700, 400, 800),
                "shadow_strength": _nested_float_range(raw_lyrics_style, "shadow_strength", "阴影", 1.0, 0.0, 1.5),
                "font_size": _nested_int_range(
                    raw_lyrics_style,
                    "font_size",
                    "桌面歌词字号",
                    DEFAULT_DESKTOP_LYRICS_STYLE["font_size"],
                    18,
                    48,
                ),
                "played_color": _hex_color(
                    raw_lyrics_style.get("played_color", DEFAULT_DESKTOP_LYRICS_STYLE["played_color"]),
                    "已唱歌词颜色",
                    DEFAULT_DESKTOP_LYRICS_STYLE["played_color"],
                ),
                "unplayed_color": _hex_color(
                    raw_lyrics_style.get("unplayed_color", DEFAULT_DESKTOP_LYRICS_STYLE["unplayed_color"]),
                    "未唱歌词颜色",
                    DEFAULT_DESKTOP_LYRICS_STYLE["unplayed_color"],
                ),
                "stroke": _nested_bool(
                    raw_lyrics_style,
                    "stroke",
                    "桌面歌词描边",
                    DEFAULT_DESKTOP_LYRICS_STYLE["stroke"],
                ),
                "bg_opacity": _nested_float_range(
                    raw_lyrics_style,
                    "bg_opacity",
                    "桌面歌词背景透明度",
                    DEFAULT_DESKTOP_LYRICS_STYLE["bg_opacity"],
                    0.0,
                    0.8,
                ),
                "align": _enum_str(
                    raw_lyrics_style.get("align", DEFAULT_DESKTOP_LYRICS_STYLE["align"]),
                    VALID_LYRICS_ALIGNS,
                    "桌面歌词对齐方式",
                    DEFAULT_DESKTOP_LYRICS_STYLE["align"],
                ),
                "mode": _enum_str(
                    raw_lyrics_style.get("mode", DEFAULT_DESKTOP_LYRICS_STYLE["mode"]),
                    VALID_LYRICS_MODES,
                    "桌面歌词显示模式",
                    DEFAULT_DESKTOP_LYRICS_STYLE["mode"],
                ),
                "show_translation": _nested_bool(
                    raw_lyrics_style,
                    "show_translation",
                    "桌面歌词翻译显示",
                    DEFAULT_DESKTOP_LYRICS_STYLE["show_translation"],
                ),
                "wheel_volume": _nested_bool(
                    raw_lyrics_style,
                    "wheel_volume",
                    "桌面歌词滚轮调音",
                    DEFAULT_DESKTOP_LYRICS_STYLE["wheel_volume"],
                ),
            }

        # --- Desktop Lyrics Bounds ---
        raw_bounds = data.get("desktop_lyrics_bounds")
        if raw_bounds is None:
            bounds_val: dict[str, int] | None = None
        elif not isinstance(raw_bounds, dict):
            if strict:
                raise AppError("SETTINGS_INVALID", "桌面歌词窗口位置必须是 JSON 对象或 null")
            bounds_val = None
        else:
            required_keys = ("x", "y", "width", "height")
            if any(k not in raw_bounds for k in required_keys):
                if strict:
                    raise AppError("SETTINGS_INVALID", "桌面歌词窗口位置缺少必要字段 (x, y, width, height)")
                bounds_val = None
            else:
                bounds_val = {
                    "x": _nested_int_range(raw_bounds, "x", "窗口横坐标 x", 0, -16384, 16384),
                    "y": _nested_int_range(raw_bounds, "y", "窗口纵坐标 y", 0, -16384, 16384),
                    "width": _nested_int_range(raw_bounds, "width", "桌面歌词窗口宽度", 820, 360, 1600),
                    "height": _nested_int_range(raw_bounds, "height", "桌面歌词窗口高度", 120, 64, 360),
                }

        extra = {}
        for key, choices in (("gpu_performance_mode", {"eco", "balanced", "high"}),
                             ("stage_theme", {"fluid", "vinyl", "poster", "visualizer"}),
                             ("visualizer_mode", {"bars", "ring", "wave3d"})):
            extra[key] = _enum_str(data.get(key), choices, key, DEFAULT_USER_SETTINGS[key])
        extra["stage_lyric_align"] = _enum_str(
            data.get("stage_lyric_align", DEFAULT_USER_SETTINGS["stage_lyric_align"]),
            frozenset({"left", "center"}),
            "舞台歌词对齐",
            DEFAULT_USER_SETTINGS["stage_lyric_align"],
        )
        extra["stage_lyric_size"] = _nested_int_range(
            data,
            "stage_lyric_size",
            "舞台歌词字号",
            DEFAULT_USER_SETTINGS["stage_lyric_size"],
            18,
            40,
        )
        for key in ("enable_3d_effects", "eq_enabled", "game_mode"):
            extra[key] = _nested_bool(data, key, key, DEFAULT_USER_SETTINGS[key])
        for key in ("audio_output_device_id", "eq_preset"):
            value = data.get(key)
            if not isinstance(value, str) or len(value) > 2048:
                if strict:
                    raise AppError("SETTINGS_INVALID", f"{key} 无效")
                value = DEFAULT_USER_SETTINGS[key]
            extra[key] = value

        def clamped(v, default, lo, hi):
            if type(v) not in (int, float) or not math.isfinite(v):
                if strict:
                    raise AppError("SETTINGS_INVALID", "音频参数必须是有限数值")
                return default
            return max(lo, min(hi, float(v)))
        bands = data.get("eq_bands")
        if not isinstance(bands, list) or len(bands) != 10:
            if strict:
                raise AppError("SETTINGS_INVALID", "均衡器需要十个频段")
            bands = DEFAULT_USER_SETTINGS["eq_bands"]
        extra["eq_bands"] = [clamped(v, 0, -12, 12) for v in bands]
        extra["playback_rate"] = clamped(data.get("playback_rate"), 1, 0.5, 2)
        bounds = data.get("window_bounds")
        extra["window_bounds"] = None
        if bounds is not None:
            if (isinstance(bounds, dict) and all(type(bounds.get(k)) is int for k in ("x","y","width","height"))
                    and all(0 < bounds[k] <= 65535 for k in ("width","height"))
                    and type(bounds.get("maximized")) is bool):
                extra["window_bounds"] = {k: bounds[k] for k in ("x","y","width","height","maximized")}
            elif strict:
                raise AppError("SETTINGS_INVALID", "主窗口位置无效")

        return {
            **extra,
            "quality_preference": quality,
            "quality_arbitration": arbitration,
            "enable_external_sources": enable_external_val,
            "external_providers": providers,
            "custom_external_api": raw_custom,
            "local_music_dirs": local_dirs,
            "visual_mode": visual_mode,
            "minimize_to_tray": minimize_val,
            "loudness_normalization": loudness_val,
            "crossfade_ms": crossfade_val,
            "audio_cache_limit_mb": cache_limit_val,
            "lyrics_blur_depth": blur_depth_val,
            "audio_reactive_glow": reactive_glow_val,
            "enable_amll_ttml_fallback": amll_ttml_val,
            "global_hotkeys_enabled": hotkeys_val,
            "sync_fm_trash_to_netease": _bool_field("sync_fm_trash_to_netease", "同步私人漫游垃圾桶", False),
            "smart_radio_explore_ratio": explore_val,
            "appearance": appearance_val,
            "desktop_lyrics_style": lyrics_style_val,
            "desktop_lyrics_bounds": bounds_val,
        }


class ChannelCredentialVault:
    """Per-channel DPAPI-encrypted credential & endpoint store with zero secret leakage."""

    FILE_NAME = "channel_credentials.json"

    def __init__(self, store: JsonStore):
        self.store = store
        self._lock = threading.RLock()

    @staticmethod
    def _validate_channel_id(channel_id: str) -> str:
        cid = str(channel_id or "").strip().lower()
        if cid not in CHANNEL_DEFAULTS:
            raise AppError("CHANNEL_UNKNOWN", f"不支持的音源渠道：{cid}", 404)
        return cid

    @staticmethod
    def _default_entry(channel_id: str) -> dict[str, Any]:
        defaults = CHANNEL_DEFAULTS[channel_id]
        entry: dict[str, Any] = {
            "channel": channel_id,
            "enabled": bool(defaults["enabled"]),
            "api_base": str(defaults["api_base"]),
            "display_name": str(defaults["display_name"]),
            "vip_label": "",
            "updated_at": "",
        }
        for key in CHANNEL_SECRET_KEYS:
            entry[key] = ""
        for key in CHANNEL_PUBLIC_EXTRA_KEYS:
            entry[key] = ""
        return entry

    def _load_all_locked(self) -> dict[str, dict[str, Any]]:
        missing = object()
        raw = self.store.read(self.FILE_NAME, missing)
        if raw is missing:
            return {cid: self._default_entry(cid) for cid in SUPPORTED_CHANNELS}
        try:
            if raw.get("encoding") == "dpapi":
                data = _dpapi_unprotect(base64.b64decode(raw["data"], validate=True))
                channels_obj = json.loads(data)["channels"]
            elif raw.get("encoding") == "keyring":
                channels_obj = json.loads(_keyring_get(raw["ref"]))["channels"]
            elif raw.get("version") == 1 and isinstance(raw.get("channels"), dict):
                channels_obj = raw["channels"]
                if _dpapi_available() or _keyring_available():
                    self._save_all_locked(self._merge_loaded(channels_obj))
            else:
                raise ValueError("unsupported credential vault")
            if not isinstance(channels_obj, dict):
                raise ValueError("invalid channels")
        except Exception as exc:
            raise AppError("CHANNEL_VAULT_UNAVAILABLE", "渠道凭据暂时无法读取，原配置已保留", 503) from exc
        return self._merge_loaded(channels_obj)

    def _merge_loaded(self, raw_channels: dict[str, Any]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for cid in SUPPORTED_CHANNELS:
            entry = self._default_entry(cid)
            stored = raw_channels.get(cid)
            if isinstance(stored, dict):
                if isinstance(stored.get("enabled"), bool):
                    entry["enabled"] = stored["enabled"]
                entry["verified"] = stored.get("verified") is True
                raw_api = str(stored.get("api_base") or "").strip()
                if raw_api:
                    try:
                        validator = (
                            validate_self_hosted_api
                            if cid in SELF_HOSTED_PROTOCOL_CHANNELS
                            else validate_custom_external_api
                        )
                        entry["api_base"] = validator(raw_api)
                    except ValueError:
                        entry["api_base"] = ""
                if isinstance(stored.get("display_name"), str) and stored["display_name"].strip():
                    entry["display_name"] = stored["display_name"].strip()[:80]
                if isinstance(stored.get("vip_label"), str):
                    entry["vip_label"] = stored["vip_label"].strip()[:60]
                if isinstance(stored.get("updated_at"), str):
                    entry["updated_at"] = stored["updated_at"]
                for key in CHANNEL_PUBLIC_EXTRA_KEYS:
                    val = stored.get(key)
                    if isinstance(val, str) and "\r" not in val and "\n" not in val and "\x00" not in val:
                        entry[key] = val.strip()[:256]
                for key in CHANNEL_SECRET_KEYS:
                    val = stored.get(key)
                    if isinstance(val, str) and "\r" not in val and "\n" not in val and "\x00" not in val:
                        entry[key] = val[:8192]
            result[cid] = entry
        return result

    def _save_all_locked(self, channels: dict[str, dict[str, Any]]) -> None:
        doc = {"version": 1, "channels": channels}
        raw_text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
        if _dpapi_available():
            encrypted = _dpapi_protect(raw_text.encode("utf-8"))
            encoded = base64.b64encode(encrypted).decode("ascii")
            self.store.write(
                self.FILE_NAME,
                {"version": 2, "encoding": "dpapi", "data": encoded},
                private=True,
            )
            return
        ref = f"channels:{self.store.data_dir.resolve()}"
        if _keyring_set(ref, raw_text):
            self.store.write(
                self.FILE_NAME,
                {"version": 2, "encoding": "keyring", "ref": ref},
                private=True,
            )
            return
        if self.store.read(self.FILE_NAME, {}).get("encoding") == "keyring":
            raise AppError("CHANNEL_VAULT_UNAVAILABLE", "渠道凭据暂时无法写入，原配置已保留", 503)
        _warn_plaintext_fallback("channel credentials")
        self.store.write(self.FILE_NAME, doc, private=True)

    @staticmethod
    def _to_public(entry: dict[str, Any]) -> dict[str, Any]:
        has_cred = any(bool(entry.get(key)) for key in CHANNEL_SECRET_KEYS)
        api_base = str(entry.get("api_base") or "")
        configured = bool(has_cred or api_base)
        connected = bool(entry.get("verified"))
        result = {
            "channel": str(entry["channel"]),
            "enabled": bool(entry.get("enabled", False)),
            "configured": configured,
            "verified": connected,
            "connection_state": "reachable" if connected else ("configured" if configured else "unconfigured"),
            "connected": connected,
            "has_credential": has_cred,
            "api_base": api_base,
            "display_name": str(entry.get("display_name") or ""),
            "vip_label": str(entry.get("vip_label") or ""),
            "updated_at": str(entry.get("updated_at") or ""),
            "protection_mode": credential_protection_mode(),
        }
        for key in CHANNEL_PUBLIC_EXTRA_KEYS:
            if entry.get(key):
                result[key] = str(entry[key])
        return result

    def public_summary(self) -> list[dict[str, Any]]:
        """Return safe non-secret channel metadata for UI & API responses."""
        with self._lock:
            channels = self._load_all_locked()
            return [self._to_public(channels[cid]) for cid in SUPPORTED_CHANNELS]

    def record_probe(self, cid, expected_revision, reachable):
        with self._lock:
            channels = self._load_all_locked()
            if channels[cid]["updated_at"] == expected_revision:
                channels[cid]["verified"] = reachable
                self._save_all_locked(channels)

    def record_probes(self, outcomes: dict[str, tuple[str, bool]]) -> list[dict[str, Any]]:
        """Atomically record multiple channel probe outcomes in a single DPAPI write and return public summary."""
        with self._lock:
            channels = self._load_all_locked()
            changed = False
            for cid, (expected_revision, reachable) in outcomes.items():
                if cid in channels and channels[cid]["updated_at"] == expected_revision:
                    channels[cid]["verified"] = bool(reachable)
                    changed = True
            if changed:
                self._save_all_locked(channels)
            return [self._to_public(channels[cid]) for cid in SUPPORTED_CHANNELS]

    def get_channel_public(self, channel_id: str) -> dict[str, Any]:
        cid = self._validate_channel_id(channel_id)
        with self._lock:
            channels = self._load_all_locked()
            return self._to_public(channels[cid])

    def get_channel_secret(self, channel_id: str) -> dict[str, Any]:
        """Return decrypted credentials strictly scoped to channel_id ONLY."""
        cid = self._validate_channel_id(channel_id)
        with self._lock:
            channels = self._load_all_locked()
            return deepcopy(channels[cid])

    def is_channel_enabled(self, channel_id: str) -> bool:
        cid = self._validate_channel_id(channel_id)
        with self._lock:
            channels = self._load_all_locked()
            return bool(channels[cid].get("enabled", False))

    def update_channel(self, channel_id: str, payload: Any) -> dict[str, Any]:
        cid = self._validate_channel_id(channel_id)
        if not isinstance(payload, dict):
            raise AppError("CHANNEL_CONFIG_INVALID", "渠道配置必须是 JSON 对象")
        with self._lock:
            channels = self._load_all_locked()
            entry = deepcopy(channels[cid])

            if "enabled" in payload:
                if not isinstance(payload["enabled"], bool):
                    raise AppError("CHANNEL_CONFIG_INVALID", "enabled 必须是布尔值")
                entry["enabled"] = payload["enabled"]

            if "api_base" in payload:
                raw_api = str(payload.get("api_base") or "").strip()
                if raw_api:
                    try:
                        validator = (
                            validate_self_hosted_api
                            if cid in SELF_HOSTED_PROTOCOL_CHANNELS
                            else validate_custom_external_api
                        )
                        entry["api_base"] = validator(raw_api)
                    except ValueError as exc:
                        raise AppError("CHANNEL_CONFIG_INVALID", str(exc)) from exc
                else:
                    entry["api_base"] = ""

            for key in CHANNEL_PUBLIC_EXTRA_KEYS:
                if key in payload and payload[key] is not None:
                    raw_val = str(payload[key]).strip()
                    if "\r" in raw_val or "\n" in raw_val or "\x00" in raw_val or len(raw_val) > 256:
                        raise AppError("CHANNEL_CONFIG_INVALID", f"字段 {key} 格式无效")
                    entry[key] = raw_val

            if "display_name" in payload and payload["display_name"] is not None:
                name = str(payload["display_name"]).strip()
                if not name or len(name) > 80:
                    raise AppError("CHANNEL_CONFIG_INVALID", "渠道显示名称无效")
                entry["display_name"] = name

            if "vip_label" in payload and payload["vip_label"] is not None:
                entry["vip_label"] = str(payload["vip_label"]).strip()[:60]

            if payload.get("clear_credentials") is True:
                for key in CHANNEL_SECRET_KEYS:
                    entry[key] = ""
                if "vip_label" not in payload:
                    entry["vip_label"] = ""

            nested_creds = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
            for key in CHANNEL_SECRET_KEYS:
                if key in payload:
                    raw_secret = payload[key]
                elif key in nested_creds:
                    raw_secret = nested_creds[key]
                else:
                    continue
                if raw_secret is None:
                    continue
                if not isinstance(raw_secret, str):
                    raise AppError("CHANNEL_CONFIG_INVALID", f"凭据字段 {key} 必须是字符串")
                cleaned = raw_secret.strip()
                if "\r" in cleaned or "\n" in cleaned or "\x00" in cleaned or len(cleaned) > 8192:
                    raise AppError("CHANNEL_CONFIG_INVALID", f"凭据字段 {key} 格式无效")
                # Empty string only clears when clear_credentials=True; ignore empty form placeholders otherwise
                if cleaned or payload.get("clear_credentials") is True:
                    entry[key] = cleaned

            entry["verified"] = False
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            channels[cid] = entry
            self._save_all_locked(channels)
            return self._to_public(entry)

    def clear_channel(self, channel_id: str) -> dict[str, Any]:
        return self.update_channel(channel_id, {"clear_credentials": True})


_AUDIO_CACHE_MIME_BY_EXT: dict[str, str] = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "mp4": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "webm": "audio/webm",
}


def sniff_audio_header_mime(header: bytes) -> str | None:
    """Detect canonical audio MIME type from the leading bytes of an audio file."""
    if not header:
        return None
    if header.startswith(b"fLaC"):
        return "audio/flac"
    if header.startswith(b"RIFF") and (len(header) < 12 or header[8:12] == b"WAVE"):
        return "audio/wav"
    if header.startswith(b"OggS"):
        return "audio/ogg"
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return "audio/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "audio/webm"
    if header.startswith(b"ID3") or (len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
        return "audio/mpeg"
    return None


def canonical_audio_mime(
    content_type: str,
    *,
    format_name: str = "",
    file_path: Path | None = None,
) -> str:
    """Reconcile stored/upstream MIME against file magic bytes and format extension."""
    cleaned = (content_type or "").split(";")[0].strip().lower()
    if file_path is not None:
        try:
            with file_path.open("rb") as handle:
                sniffed = sniff_audio_header_mime(handle.read(32))
            if sniffed:
                return sniffed
        except OSError:
            pass
    ext = (format_name or (file_path.suffix.lstrip(".") if file_path is not None else "")).strip().lower()
    ext_mime = _AUDIO_CACHE_MIME_BY_EXT.get(ext)
    if ext_mime and (
        cleaned in {"", "application/octet-stream", "binary/octet-stream"}
        or (cleaned == "audio/mpeg" and ext_mime != "audio/mpeg")
    ):
        return ext_mime
    if cleaned and cleaned not in {"application/octet-stream", "binary/octet-stream"}:
        return cleaned
    return ext_mime or "audio/mpeg"


class AudioDiskCache:
    """Bounded LRU on-disk audio cache with atomic temp-write + verification."""

    DIR_NAME = "audio_cache"
    INDEX_FILE = "index.json"

    def __init__(self, data_dir: Path, limit_mb_provider: Any = 512):
        self.cache_dir = data_dir / self.DIR_NAME
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._limit_mb_provider = limit_mb_provider
        self._lock = threading.RLock()

    def limit_mb(self) -> int:
        raw = self._limit_mb_provider() if callable(self._limit_mb_provider) else self._limit_mb_provider
        try:
            return max(0, min(4096, int(raw)))
        except (TypeError, ValueError):
            return 512

    def limit_bytes(self) -> int:
        return self.limit_mb() * 1024 * 1024

    @staticmethod
    def _key_hash(cache_key: str) -> str:
        import hashlib

        return hashlib.sha256(str(cache_key).encode("utf-8")).hexdigest()

    def _index_path(self) -> Path:
        return self.cache_dir / self.INDEX_FILE

    def _load_index_locked(self) -> dict[str, dict[str, Any]]:
        target = self._index_path()
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError, OSError):
            return {}
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return {}
        cleaned: dict[str, dict[str, Any]] = {}
        for k, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            fname = str(entry.get("filename") or "")
            if not fname or Path(fname).name != fname:
                continue
            fpath = self.cache_dir / fname
            if not fpath.is_file():
                continue
            cleaned[str(k)] = entry
        return cleaned

    def _save_index_locked(self, entries: dict[str, dict[str, Any]]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix=".index.", suffix=".tmp", dir=self.cache_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._index_path())
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _evict_locked(self, entries: dict[str, dict[str, Any]], max_bytes: int) -> None:
        total = sum(int(e.get("size_bytes") or 0) for e in entries.values())
        if total <= max_bytes:
            return
        ordered = sorted(entries.items(), key=lambda pair: float(pair[1].get("last_accessed_at") or 0.0))
        for key_hash, entry in ordered:
            if total <= max_bytes:
                break
            fname = str(entry.get("filename") or "")
            if fname:
                try:
                    (self.cache_dir / fname).unlink()
                except OSError:
                    pass
            total -= int(entry.get("size_bytes") or 0)
            entries.pop(key_hash, None)

    def get(self, cache_key: str) -> tuple[Path, str, int] | None:
        if self.limit_bytes() <= 0 or not cache_key:
            return None
        key_hash = self._key_hash(cache_key)
        with self._lock:
            entries = self._load_index_locked()
            entry = entries.get(key_hash)
            if entry is None:
                return None
            fpath = self.cache_dir / str(entry["filename"])
            try:
                actual_size = fpath.stat().st_size
            except OSError:
                entries.pop(key_hash, None)
                self._save_index_locked(entries)
                return None
            expected = int(entry.get("size_bytes") or 0)
            if actual_size <= 0 or (expected > 0 and actual_size != expected):
                try:
                    fpath.unlink()
                except OSError:
                    pass
                entries.pop(key_hash, None)
                self._save_index_locked(entries)
                return None
            entry["last_accessed_at"] = datetime.now(timezone.utc).timestamp()
            resolved_ct = canonical_audio_mime(
                str(entry.get("content_type") or ""),
                format_name=str(entry.get("format") or ""),
                file_path=fpath,
            )
            if resolved_ct != entry.get("content_type"):
                entry["content_type"] = resolved_ct
            self._save_index_locked(entries)
            return fpath, resolved_ct, actual_size

    def create_temp_path(self) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".stream.", suffix=".part", dir=self.cache_dir)
        os.close(fd)
        return Path(temp_name)

    def commit_temp_file(
        self,
        cache_key: str,
        temp_path: Path,
        expected_size: int,
        content_type: str,
        *,
        format_name: str = "",
    ) -> bool:
        max_bytes = self.limit_bytes()
        try:
            if max_bytes <= 0 or not cache_key or not temp_path.is_file():
                return False
            actual_size = temp_path.stat().st_size
            if actual_size <= 0 or actual_size > max_bytes:
                return False
            if expected_size > 0 and actual_size != expected_size:
                return False
            key_hash = self._key_hash(cache_key)
            ext = "".join(ch for ch in (format_name or "bin").lower() if ch.isalnum())[:8] or "bin"
            filename = f"{key_hash}.{ext}"
            target_path = self.cache_dir / filename
            now_ts = datetime.now(timezone.utc).timestamp()
            with self._lock:
                entries = self._load_index_locked()
                # Remove old file if extension changed
                old = entries.get(key_hash)
                if old and old.get("filename") != filename:
                    try:
                        (self.cache_dir / str(old["filename"])).unlink()
                    except OSError:
                        pass
                os.replace(temp_path, target_path)
                resolved_ct = canonical_audio_mime(
                    str(content_type or ""),
                    format_name=ext,
                    file_path=target_path,
                )
                entries[key_hash] = {
                    "key": str(cache_key)[:256],
                    "filename": filename,
                    "content_type": resolved_ct,
                    "format": ext,
                    "size_bytes": actual_size,
                    "created_at": now_ts,
                    "last_accessed_at": now_ts,
                }
                self._evict_locked(entries, max_bytes)
                self._save_index_locked(entries)
                return key_hash in entries
        except OSError:
            return False
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    def put_bytes(
        self,
        cache_key: str,
        data: bytes,
        content_type: str,
        *,
        format_name: str = "",
    ) -> bool:
        if self.limit_bytes() <= 0 or not data or len(data) > self.limit_bytes():
            return False
        temp_path = self.create_temp_path()
        try:
            with temp_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            try:
                temp_path.unlink()
            except OSError:
                pass
            return False
        return self.commit_temp_file(cache_key, temp_path, len(data), content_type, format_name=format_name)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            entries = self._load_index_locked()
            lim_mb = self.limit_mb()
            # Prune on stats if user lowered limit_mb
            if lim_mb == 0 and entries:
                self._evict_locked(entries, 0)
                self._save_index_locked(entries)
            elif lim_mb > 0:
                before = len(entries)
                self._evict_locked(entries, lim_mb * 1024 * 1024)
                if len(entries) != before:
                    self._save_index_locked(entries)
            total_bytes = sum(int(e.get("size_bytes") or 0) for e in entries.values())
            return {
                "entry_count": len(entries),
                "total_bytes": total_bytes,
                "total_mb": round(total_bytes / (1024 * 1024), 2),
                "limit_mb": lim_mb,
                "enabled": lim_mb > 0,
                "cache_dir": str(self.cache_dir),
            }

    def clear(self) -> dict[str, Any]:
        with self._lock:
            entries = self._load_index_locked()
            cleared = 0
            freed = 0
            for entry in entries.values():
                fname = str(entry.get("filename") or "")
                if fname:
                    fpath = self.cache_dir / fname
                    try:
                        size = fpath.stat().st_size
                        fpath.unlink()
                        cleared += 1
                        freed += size
                    except OSError:
                        pass
            # Also clean any stray .part files
            for stray in self.cache_dir.glob("*.part"):
                try:
                    stray.unlink()
                except OSError:
                    pass
            self._save_index_locked({})
            return {"cleared_entries": cleared, "freed_bytes": freed}
