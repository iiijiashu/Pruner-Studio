from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any

from Crypto.Cipher import AES

EAPI_KEY = b"e82ckenh8dichen8"
EAPI_SEPARATOR = "-36cd479b6b5-"

WEAPI_PRESET_KEY = b"0CoJUm6Qyw8W8jud"
WEAPI_IV = b"0102030405060708"
WEAPI_BASE62 = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
WEAPI_RSA_MODULUS = int(
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b424d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
    16,
)
WEAPI_RSA_EXPONENT = 0x10001

DEFAULT_PC_OS = "pc"
DEFAULT_PC_APPVER = "3.1.3.203419"
DEFAULT_PC_CHANNEL = "netease"
DEFAULT_PC_OSVER = "Microsoft-Windows-10-Professional-build-22631-64bit"

QUALITY_LEVELS = (
    "jymaster",
    "sky",
    "jyeffect",
    "hires",
    "lossless",
    "exhigh",
    "higher",
    "standard",
)
LOSSLESS_LEVELS = frozenset({"jymaster", "sky", "jyeffect", "hires", "lossless"})

_PROCESS_DEVICE_ID = secrets.token_hex(16)
_DEVICE_ID_LOCK = threading.Lock()
_PERSISTENT_DEVICE_ID: str | None = None
_DEVICE_ID_RE = re.compile(r"^[0-9a-fA-F]{16,64}$")


def set_device_id(device_id: str | None) -> str:
    """Configure a persistent stable deviceId so NetEase requests do not rotate IDs per launch."""
    global _PERSISTENT_DEVICE_ID
    cleaned = str(device_id or "").strip()
    with _DEVICE_ID_LOCK:
        if cleaned and _DEVICE_ID_RE.match(cleaned):
            _PERSISTENT_DEVICE_ID = cleaned
        return _PERSISTENT_DEVICE_ID or _PROCESS_DEVICE_ID


def get_device_id() -> str:
    with _DEVICE_ID_LOCK:
        return _PERSISTENT_DEVICE_ID or _PROCESS_DEVICE_ID


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data or len(data) % block_size != 0:
        raise ValueError("invalid padded ciphertext length")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > block_size:
        raise ValueError("invalid PKCS#7 padding byte")
    if data[-pad_len:] != bytes([pad_len] * pad_len):
        raise ValueError("corrupted PKCS#7 padding")
    return data[:-pad_len]


def _parse_cookie_pairs(cookie: str | None) -> dict[str, str]:
    if not cookie:
        return {}
    pairs: dict[str, str] = {}
    for segment in cookie.split(";"):
        if "=" not in segment:
            continue
        name, value = segment.split("=", 1)
        key = name.strip()
        if key:
            pairs[key] = value.strip()
    return pairs


def normalize_quality_level(level: str | None, default: str = "jymaster") -> str:
    candidate = str(level or "").strip().lower()
    if candidate in QUALITY_LEVELS:
        return candidate
    return default if default in QUALITY_LEVELS else "standard"


def encode_type_for_level(level: str) -> str:
    normalized = normalize_quality_level(level, default="standard")
    if normalized in LOSSLESS_LEVELS:
        return "flac"
    return "aac"


def build_eapi_header(
    cookie: str | None = None,
    *,
    device_id: str | None = None,
    request_id: str | None = None,
    build_ver: str | None = None,
) -> dict[str, str]:
    pairs = _parse_cookie_pairs(cookie)
    now_ms = int(time.time() * 1000)
    header: dict[str, str] = {
        "osver": DEFAULT_PC_OSVER,
        "deviceId": device_id or pairs.get("deviceId") or get_device_id(),
        "os": DEFAULT_PC_OS,
        "appver": DEFAULT_PC_APPVER,
        "versioncode": "140",
        "mobilename": "",
        "buildver": build_ver or str(now_ms // 1000),
        "resolution": "1920x1080",
        "__csrf": pairs.get("__csrf", ""),
        "channel": DEFAULT_PC_CHANNEL,
        "requestId": request_id or f"{now_ms}_{secrets.randbelow(10_000):04d}",
    }
    music_u = pairs.get("MUSIC_U")
    if music_u:
        header["MUSIC_U"] = music_u
    music_a = pairs.get("MUSIC_A")
    if music_a:
        header["MUSIC_A"] = music_a
    return header


def build_eapi_cookie(cookie: str | None, header: dict[str, str] | None = None) -> str:
    effective_header = header or build_eapi_header(cookie)
    base_pairs = _parse_cookie_pairs(cookie)
    merged: dict[str, str] = {
        "os": effective_header.get("os", DEFAULT_PC_OS),
        "appver": effective_header.get("appver", DEFAULT_PC_APPVER),
        "channel": effective_header.get("channel", DEFAULT_PC_CHANNEL),
        "osver": effective_header.get("osver", DEFAULT_PC_OSVER),
        "deviceId": effective_header.get("deviceId", get_device_id()),
    }
    merged.update(base_pairs)
    return "; ".join(f"{key}={value}" for key, value in merged.items() if value != "")


def build_player_url_v1_payload(
    song_ids: list[int],
    level: str = "jymaster",
    *,
    cookie: str | None = None,
    header: dict[str, str] | None = None,
) -> dict[str, Any]:
    normalized_level = normalize_quality_level(level, default="jymaster")
    header_dict = header or build_eapi_header(cookie)
    payload: dict[str, Any] = {
        "ids": json.dumps([int(song_id) for song_id in song_ids]),
        "level": normalized_level,
        "encodeType": encode_type_for_level(normalized_level),
        "header": json.dumps(header_dict, separators=(",", ":"), ensure_ascii=False),
    }
    if normalized_level == "sky":
        payload["immerseType"] = "c51"
    return payload


def eapi_encrypt(url: str, payload: dict[str, Any]) -> dict[str, str]:
    """Sign and encrypt a NetEase EAPI request using AES-128-ECB."""
    if not url.startswith("/"):
        url = f"/{url}"
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(f"nobody{url}use{text}md5forencrypt".encode("utf-8")).hexdigest()
    plain = f"{url}{EAPI_SEPARATOR}{text}{EAPI_SEPARATOR}{digest}".encode("utf-8")
    cipher = AES.new(EAPI_KEY, AES.MODE_ECB)
    encrypted = cipher.encrypt(pkcs7_pad(plain, 16))
    return {"params": encrypted.hex().upper()}


def eapi_decrypt(data: str | bytes) -> bytes:
    """Decrypt a hex-encoded or raw-bytes NetEase EAPI AES-128-ECB payload."""
    if isinstance(data, str):
        raw = bytes.fromhex(data.strip())
    else:
        raw = data
    cipher = AES.new(EAPI_KEY, AES.MODE_ECB)
    return pkcs7_unpad(cipher.decrypt(raw), 16)


def eapi_decrypt_response(raw: bytes) -> dict[str, Any]:
    """Decode an EAPI HTTP response that may be plain JSON or EAPI-encrypted bytes."""
    if not raw:
        raise ValueError("empty EAPI response")
    try:
        parsed = json.loads(raw.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    try:
        decrypted = eapi_decrypt(raw)
        parsed = json.loads(decrypted.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    stripped = raw.strip()
    if stripped and all(ch in b"0123456789abcdefABCDEF" for ch in stripped):
        decrypted = eapi_decrypt(stripped.decode("ascii"))
        parsed = json.loads(decrypted.decode("utf-8"))
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("unable to decode EAPI response payload")


def _aes_cbc_encrypt_b64(plaintext: bytes, key: bytes, iv: bytes) -> str:
    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(pkcs7_pad(plaintext, 16))
    return base64.b64encode(encrypted).decode("ascii")


def weapi_encrypt(payload: dict[str, Any], *, secret_key: str | None = None) -> dict[str, str]:
    """Encrypt a NetEase WEAPI request using dual AES-128-CBC + RSA-1024."""
    if secret_key is None:
        key_str = "".join(secrets.choice(WEAPI_BASE62) for _ in range(16))
    else:
        if len(secret_key) != 16:
            raise ValueError("WEAPI secret_key must be exactly 16 characters")
        key_str = secret_key
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    first_pass = _aes_cbc_encrypt_b64(text.encode("utf-8"), WEAPI_PRESET_KEY, WEAPI_IV)
    params = _aes_cbc_encrypt_b64(first_pass.encode("utf-8"), key_str.encode("utf-8"), WEAPI_IV)
    reversed_key = key_str[::-1].encode("utf-8")
    rsa_int = pow(int.from_bytes(reversed_key, "big"), WEAPI_RSA_EXPONENT, WEAPI_RSA_MODULUS)
    enc_sec_key = f"{rsa_int:0256x}"
    return {"params": params, "encSecKey": enc_sec_key}
