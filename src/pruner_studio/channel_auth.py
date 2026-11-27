from __future__ import annotations

import base64
import io
import re
import urllib.parse
from http.cookies import SimpleCookie
from typing import Any, Iterable

import httpx

from .errors import AppError, UpstreamError
from .storage import CHANNEL_SECRET_KEYS

_AUTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_BILIBILI_QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_BILIBILI_QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
_BILIBILI_ALLOWED_HOSTS = frozenset({"passport.bilibili.com"})
_QR_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{4,256}$")
_COOKIE_ATTR_NAMES = frozenset(
    {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite", "priority", "partitioned"}
)
_EMPTY_COOKIE_VALUES = frozenset({"", "0", "null", "none", "undefined", "deleted"})

CHANNEL_AUTH_CONFIG: dict[str, dict[str, Any]] = {
    "qq": {
        "login_url": "https://y.qq.com/",
        "title": "QQ 音乐官方登录 — Pruner Studio",
        "allowed_domains": ("y.qq.com", "qq.com"),
        "cookie_query_urls": ("https://y.qq.com/", "https://u.y.qq.com/"),
        "required_any": ("qm_keyst", "qqmusic_key"),
        "keep_keys": (
            "qm_keyst",
            "qqmusic_key",
            "uin",
            "wxuin",
            "p_uin",
            "euin",
            "qqmusic_uin",
            "psrf_qqaccess_token",
            "psrf_qqopenid",
            "psrf_qqunionid",
            "tmeLoginType",
            "login_type",
            "wxopenid",
            "wxrefresh_token",
            "wxunionid",
        ),
    },
    "kugou_lite": {
        "login_url": "https://www.kugou.com/",
        "title": "酷狗音乐官方登录 — Pruner Studio",
        "allowed_domains": ("kugou.com",),
        "cookie_query_urls": ("https://www.kugou.com/",),
        "required_any": ("t", "token"),
        "keep_keys": ("t", "token", "KugooID", "userid", "kg_dfid", "dfid", "kg_mid", "KuGoo"),
    },
    "migu": {
        "login_url": "https://music.migu.cn/",
        "title": "咪咕音乐官方登录 — Pruner Studio",
        "allowed_domains": ("migu.cn",),
        "cookie_query_urls": ("https://music.migu.cn/",),
        "required_any": ("migu_music_status", "SESSION", "aversionid", "user_id", "music_token"),
        "keep_keys": (
            "migu_music_status",
            "SESSION",
            "aversionid",
            "user_id",
            "music_token",
            "migu_cookie_id",
            "migu_music_uid",
            "migu_music_avatar",
            "migu_music_nickname",
        ),
    },
    "kuwo": {
        "login_url": "https://www.kuwo.cn/",
        "title": "酷我音乐官方登录 — Pruner Studio",
        "allowed_domains": ("kuwo.cn",),
        "cookie_query_urls": ("https://www.kuwo.cn/",),
        "required_any": ("userid", "websid"),
        "keep_keys": ("userid", "websid", "kw_token", "uname3", "t3", "sid"),
    },
    "bilibili": {
        "login_url": "https://passport.bilibili.com/login",
        "title": "哔哩哔哩官方登录 — Pruner Studio",
        "allowed_domains": ("bilibili.com",),
        "cookie_query_urls": ("https://www.bilibili.com/", "https://passport.bilibili.com/"),
        "required_any": ("SESSDATA",),
        "keep_keys": ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3"),
    },
}

CHANNEL_AUTH_ALLOWLIST = frozenset(CHANNEL_AUTH_CONFIG.keys())


def _safe_cookie_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or "\r" in cleaned or "\n" in cleaned or "\x00" in cleaned:
        return ""
    return cleaned


def channel_capability(
    channel_id: str,
    secret: dict[str, Any] | None = None,
    *,
    custom_api: str = "",
) -> str:
    ch = str(channel_id or "").strip().lower()
    sec = secret if isinstance(secret, dict) else {}
    has_cred = any(bool(str(sec.get(k) or "").strip()) for k in CHANNEL_SECRET_KEYS)
    api_base = str(sec.get("api_base") or "").strip()
    if ch == "custom":
        eff_api = str(custom_api or "").strip() or api_base
        return "custom_endpoint" if eff_api else "unconfigured"
    if ch in {"opensubsonic", "webdav"}:
        return "custom_endpoint" if api_base else "unconfigured"
    if ch == "gdstudio":
        return "public_search"
    if ch in CHANNEL_AUTH_ALLOWLIST:
        if api_base:
            return "custom_endpoint"
        if has_cred:
            return "authenticated_search"
        return "public_search"
    return "unconfigured"


def domain_matches_allowlist(domain: str, allowed_domains: Iterable[str]) -> bool:
    cleaned = str(domain or "").strip().lower().lstrip(".")
    if not cleaned or "/" in cleaned or ":" in cleaned or " " in cleaned:
        return False
    for suffix in allowed_domains:
        norm = str(suffix or "").strip().lower().lstrip(".")
        if not norm:
            continue
        if cleaned == norm or cleaned.endswith("." + norm):
            return True
    return False


def extract_channel_auth_cookies(
    channel_id: str,
    cookie_entries: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Strictly filter WebView2 cookies to allowlisted domains and build a vault payload.

    Returns ``None`` if no authenticated session cookie for ``channel_id`` is present yet.
    """
    ch = str(channel_id or "").strip().lower()
    cfg = CHANNEL_AUTH_CONFIG.get(ch)
    if cfg is None:
        raise AppError("CHANNEL_AUTH_UNSUPPORTED", f"渠道 {ch or 'unknown'} 不支持官方弹窗登录", 400)

    allowed_domains: tuple[str, ...] = cfg["allowed_domains"]
    required_any: tuple[str, ...] = cfg["required_any"]
    keep_keys = set(cfg["keep_keys"])

    filtered: dict[str, str] = {}
    for item in cookie_entries:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        if not domain_matches_allowlist(domain, allowed_domains):
            continue
        name = _safe_cookie_text(item.get("name"))
        value = _safe_cookie_text(item.get("value"))
        if not name or not value or ";" in name or "=" in name:
            continue
        if name.lower() in _COOKIE_ATTR_NAMES:
            continue
        if name in keep_keys or name in required_any:
            filtered[name] = value

    def _has_real_value(key: str) -> bool:
        val = filtered.get(key, "")
        return bool(val) and val.lower() not in _EMPTY_COOKIE_VALUES

    if not any(_has_real_value(req) for req in required_any):
        return None

    # Special guard for migu_music_status: "true" alone without session id is insufficient unless paired or non-false.
    if ch == "migu" and set(k for k in required_any if _has_real_value(k)) == {"migu_music_status"}:
        if filtered.get("migu_music_status", "").lower() in {"false", "0"}:
            return None

    ordered_pairs: list[str] = []
    for key in cfg["keep_keys"]:
        if key in filtered:
            ordered_pairs.append(f"{key}={filtered[key]}")
    cookie_header = "; ".join(ordered_pairs)
    if len(cookie_header) > 8000:
        cookie_header = cookie_header[:8000]

    payload: dict[str, Any] = {
        "enabled": True,
        "cookie": cookie_header,
    }
    if ch == "qq":
        raw_uin = filtered.get("uin") or filtered.get("qqmusic_uin") or filtered.get("wxuin") or filtered.get("p_uin") or ""
        cleaned_uin = raw_uin.lstrip("oO")
        if cleaned_uin and cleaned_uin.lower() not in _EMPTY_COOKIE_VALUES:
            payload["uin"] = cleaned_uin[:128]
    elif ch == "kugou_lite":
        tok = filtered.get("t") or filtered.get("token") or ""
        uid = filtered.get("userid") or filtered.get("KugooID") or ""
        dfid = filtered.get("kg_dfid") or filtered.get("dfid") or ""
        if tok:
            payload["token"] = tok[:1024]
        if uid and uid.lower() not in _EMPTY_COOKIE_VALUES:
            payload["userid"] = uid[:128]
        if dfid and dfid.lower() not in _EMPTY_COOKIE_VALUES:
            payload["dfid"] = dfid[:256]
    elif ch == "bilibili":
        sessdata = filtered.get("SESSDATA") or ""
        if sessdata:
            payload["sessdata"] = sessdata[:2048]
    return payload


def _extract_bilibili_cookies(set_cookie_headers: Iterable[str], cross_url: str = "") -> dict[str, str]:
    extracted: dict[str, str] = {}
    want = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")
    for raw_header in set_cookie_headers:
        text = str(raw_header or "").strip()
        if not text:
            continue
        jar = SimpleCookie()
        try:
            jar.load(text)
        except Exception:
            jar = SimpleCookie()
        if jar:
            for key in want:
                if key in jar:
                    val = _safe_cookie_text(jar[key].value)
                    if val:
                        extracted[key] = val
        else:
            first_pair = text.split(";", 1)[0].strip()
            if "=" in first_pair:
                k, v = first_pair.split("=", 1)
                k = k.strip()
                if k in want:
                    val = _safe_cookie_text(v)
                    if val:
                        extracted[k] = val

    if cross_url:
        try:
            parsed = urllib.parse.urlsplit(str(cross_url))
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
            for key in want:
                if key not in extracted and key in qs and qs[key]:
                    val = _safe_cookie_text(qs[key][0])
                    if val:
                        extracted[key] = val
        except ValueError:
            pass
    return extracted


def render_qr_data_url(url: str) -> str:
    import qrcode

    image = qrcode.make(url)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class BilibiliQrService:
    """Isolated Bilibili Passport Web QR login client (STRICTLY ZERO NetEase cookies)."""

    def __init__(self, http_client: httpx.Client | None = None):
        self._injected_client = http_client

    def _get(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _BILIBILI_ALLOWED_HOSTS:
            raise AppError("BILIBILI_QR_URL_REJECTED", "非法哔哩哔哩登录接口地址", 400)
        headers = {
            "User-Agent": _AUTH_USER_AGENT,
            "Accept": "application/json",
            "Referer": "https://passport.bilibili.com/login",
            "Origin": "https://passport.bilibili.com",
        }
        try:
            if self._injected_client is not None:
                return self._injected_client.get(url, params=params, headers=headers)
            with httpx.Client(
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(8.0, read=10.0),
            ) as client:
                return client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamError("BILIBILI_QR_TIMEOUT", "连接哔哩哔哩扫码服务超时，请稍后重试", 504) from exc
        except httpx.HTTPError as exc:
            raise UpstreamError("BILIBILI_QR_NETWORK_ERROR", "连接哔哩哔哩扫码服务失败，请检查网络", 502) from exc

    def create_qr(self) -> dict[str, str]:
        resp = self._get(_BILIBILI_QR_GENERATE_URL)
        if resp.status_code != 200:
            raise UpstreamError("BILIBILI_QR_CREATE_FAILED", f"哔哩哔哩二维码生成失败 (HTTP {resp.status_code})", 502)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise UpstreamError("BILIBILI_QR_INVALID", "哔哩哔哩二维码接口返回了无效 JSON", 502) from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            msg = str(payload.get("message") or "哔哩哔哩二维码生成失败") if isinstance(payload, dict) else "哔哩哔哩二维码生成失败"
            raise UpstreamError("BILIBILI_QR_CREATE_FAILED", msg, 502)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        qrcode_key = _safe_cookie_text(data.get("qrcode_key"))
        url = _safe_cookie_text(data.get("url"))
        if not qrcode_key or not url.startswith("https://"):
            raise UpstreamError("BILIBILI_QR_INVALID", "哔哩哔哩未返回有效的二维码链接", 502)
        qr_image = render_qr_data_url(url)
        return {
            "qrcode_key": qrcode_key,
            "url": url,
            "qr_image": qr_image,
        }

    def poll_qr(self, qrcode_key: str) -> dict[str, Any]:
        cleaned_key = _safe_cookie_text(qrcode_key)
        if not cleaned_key or not _QR_KEY_RE.fullmatch(cleaned_key):
            raise AppError("BILIBILI_QR_KEY_INVALID", "二维码标识无效", 400)
        resp = self._get(_BILIBILI_QR_POLL_URL, params={"qrcode_key": cleaned_key})
        if resp.status_code != 200:
            raise UpstreamError("BILIBILI_QR_POLL_FAILED", f"哔哩哔哩扫码状态查询失败 (HTTP {resp.status_code})", 502)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise UpstreamError("BILIBILI_QR_INVALID", "哔哩哔哩扫码状态返回了无效 JSON", 502) from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            msg = str(payload.get("message") or "哔哩哔哩扫码状态查询失败") if isinstance(payload, dict) else "哔哩哔哩扫码状态查询失败"
            raise UpstreamError("BILIBILI_QR_POLL_FAILED", msg, 502)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        raw_code = data.get("code")
        try:
            status_code = int(raw_code)
        except (TypeError, ValueError) as exc:
            raise UpstreamError("BILIBILI_QR_INVALID", "哔哩哔哩扫码状态码无效", 502) from exc

        if status_code == 86101:
            return {"state": "waiting", "code": status_code, "message": "等待使用哔哩哔哩 App 扫码"}
        if status_code == 86090:
            return {"state": "scanned", "code": status_code, "message": "已扫码，请在哔哩哔哩 App 中确认登录"}
        if status_code == 86038:
            return {"state": "expired", "code": status_code, "message": "二维码已失效，请刷新后重试"}
        if status_code == 0:
            set_cookies = resp.headers.get_list("set-cookie") if hasattr(resp.headers, "get_list") else []
            cross_url = str(data.get("url") or "")
            cookies = _extract_bilibili_cookies(set_cookies, cross_url)
            sessdata = cookies.get("SESSDATA") or ""
            if not sessdata:
                raise UpstreamError("BILIBILI_QR_SESSION_MISSING", "扫码已确认但未获取到 SESSDATA，请重试", 502)
            cookie_parts = [f"SESSDATA={sessdata}"]
            for extra_key in ("bili_jct", "DedeUserID", "DedeUserID__ckMd5"):
                if cookies.get(extra_key):
                    cookie_parts.append(f"{extra_key}={cookies[extra_key]}")
            return {
                "state": "confirmed",
                "code": 0,
                "message": "哔哩哔哩扫码登录成功，凭证已加密保存",
                "secrets": {
                    "sessdata": sessdata,
                    "cookie": "; ".join(cookie_parts),
                },
            }
        return {
            "state": "waiting",
            "code": status_code,
            "message": str(data.get("message") or "等待扫码确认"),
        }
