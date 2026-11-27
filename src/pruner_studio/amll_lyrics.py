from __future__ import annotations

from collections import OrderedDict
import re
import threading
import time
from typing import Any
import xml.etree.ElementTree as ET

import httpx

AMLL_TTML_URL_TEMPLATE = "https://raw.githubusercontent.com/amll-dev/amll-ttml-db/main/ncm-lyrics/{song_id}.ttml"
_AMLL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_MAX_TTML_BYTES = 1_048_576
_CACHE_MAX_ENTRIES = 100
_CACHE_TTL_SEC = 1_800.0
_MISS_TTL_SEC = 600.0

_UNSAFE_XML_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)\b", re.IGNORECASE)
_CLOCK_RE = re.compile(
    r"^\s*(?:(?P<h>\d+):)?(?:(?P<m>\d+):)?(?P<s>\d+(?:\.\d+)?)\s*$"
)
_UNIT_TIME_RE = re.compile(r"^\s*(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h)\s*$", re.IGNORECASE)

_cache_lock = threading.RLock()
_ttml_cache: OrderedDict[int, tuple[float, dict[str, Any] | None]] = OrderedDict()


def clear_amll_cache() -> None:
    with _cache_lock:
        _ttml_cache.clear()


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _get_role(elem: ET.Element) -> str:
    for key, value in elem.attrib.items():
        if key == "role" or key.endswith("}role"):
            return str(value or "").strip().lower()
    return ""


def parse_ttml_time_ms(raw: str | None) -> int | None:
    """Parse TTML timestamp formats (HH:MM:SS.mmm, MM:SS.mmm, 12.34s, 500ms) into integer ms."""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    unit_match = _UNIT_TIME_RE.match(text)
    if unit_match:
        val = float(unit_match.group("val"))
        unit = unit_match.group("unit").lower()
        if unit == "ms":
            return max(0, int(round(val)))
        if unit == "s":
            return max(0, int(round(val * 1000.0)))
        if unit == "m":
            return max(0, int(round(val * 60_000.0)))
        if unit == "h":
            return max(0, int(round(val * 3_600_000.0)))
    parts = text.split(":")
    try:
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return max(0, int(round((hours * 3600.0 + minutes * 60.0 + seconds) * 1000.0)))
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return max(0, int(round((minutes * 60.0 + seconds) * 1000.0)))
        if len(parts) == 1:
            seconds = float(parts[0])
            return max(0, int(round(seconds * 1000.0)))
    except ValueError:
        return None
    return None


def _format_lrc_timestamp(ms: int) -> str:
    clamped = max(0, int(ms))
    minutes = clamped // 60_000
    seconds = (clamped % 60_000) / 1000.0
    return f"[{minutes:02d}:{seconds:05.2f}]"


def _flatten_text(elem: ET.Element) -> str:
    return "".join(elem.itertext()).strip()


def _collect_word_spans(parent: ET.Element) -> tuple[list[tuple[int, int, str]], str, str]:
    """Extract timed word spans plus optional x-translation / x-roman metadata from a <p>."""
    words: list[tuple[int, int, str]] = []
    translations: list[str] = []
    romans: list[str] = []

    for child in parent:
        if _strip_ns(child.tag) != "span":
            continue
        role = _get_role(child)
        if role == "x-translation":
            txt = _flatten_text(child)
            if txt:
                translations.append(txt)
            continue
        if role == "x-roman":
            txt = _flatten_text(child)
            if txt:
                romans.append(txt)
            continue
        if role == "x-bg":
            # Background vocal sub-spans: capture translation/roman if nested, skip overwriting main vocal.
            for sub in child:
                if _strip_ns(sub.tag) != "span":
                    continue
                sub_role = _get_role(sub)
                if sub_role == "x-translation":
                    txt = _flatten_text(sub)
                    if txt:
                        translations.append(txt)
                elif sub_role == "x-roman":
                    txt = _flatten_text(sub)
                    if txt:
                        romans.append(txt)
            continue

        begin_ms = parse_ttml_time_ms(child.attrib.get("begin"))
        end_ms = parse_ttml_time_ms(child.attrib.get("end"))
        text = "".join(child.itertext())
        tail = child.tail or ""
        # Preserve single separating spaces between word spans (e.g. English lyrics).
        if tail and tail.strip() == "":
            if " " in tail or "\t" in tail:
                text = text + " "
        if begin_ms is not None and text:
            dur = max(1, (end_ms - begin_ms)) if (end_ms is not None and end_ms >= begin_ms) else 250
            words.append((begin_ms, dur, text))

    return words, " ".join(translations).strip(), " ".join(romans).strip()


def parse_ttml_to_yrc(ttml_text: str) -> dict[str, Any] | None:
    """Safely parse AMLL TTML XML (DTD/XXE rejected) into YRC, LRC, tlyrics, and romalrc."""
    if not ttml_text or not isinstance(ttml_text, str):
        return None
    if len(ttml_text.encode("utf-8", errors="ignore")) > _MAX_TTML_BYTES:
        return None
    if _UNSAFE_XML_RE.search(ttml_text):
        raise ValueError("TTML payload contains disallowed DTD/ENTITY declarations")

    try:
        root = ET.fromstring(ttml_text)
    except ET.ParseError:
        return None

    yrc_lines: list[str] = []
    lrc_lines: list[str] = []
    tlyric_lines: list[str] = []
    roman_lines: list[str] = []
    has_word_timing = False

    for elem in root.iter():
        if _strip_ns(elem.tag) != "p":
            continue
        p_begin = parse_ttml_time_ms(elem.attrib.get("begin"))
        p_end = parse_ttml_time_ms(elem.attrib.get("end"))
        words, trans_text, roman_text = _collect_word_spans(elem)

        if words:
            has_word_timing = True
            line_start = p_begin if p_begin is not None else words[0][0]
            last_word_end = words[-1][0] + words[-1][1]
            line_end = p_end if (p_end is not None and p_end >= line_start) else last_word_end
            line_dur = max(1, line_end - line_start)
            word_tokens = "".join(f"({w_start},{w_dur},0){w_text}" for w_start, w_dur, w_text in words)
            yrc_lines.append(f"[{line_start},{line_dur}]{word_tokens}")
            plain_line = "".join(w_text for _s, _d, w_text in words).strip()
        else:
            # Line-level <p> fallback without word spans.
            direct_parts: list[str] = []
            if elem.text:
                direct_parts.append(elem.text)
            for child in elem:
                if _strip_ns(child.tag) == "span" and _get_role(child) in {"x-translation", "x-roman", "x-bg"}:
                    continue
                direct_parts.append("".join(child.itertext()))
                if child.tail:
                    direct_parts.append(child.tail)
            plain_line = "".join(direct_parts).strip()
            if p_begin is None or not plain_line:
                continue
            line_start = p_begin
            line_dur = max(1, (p_end - p_begin)) if (p_end is not None and p_end > p_begin) else 3_000
            yrc_lines.append(f"[{line_start},{line_dur}]({line_start},{line_dur},0){plain_line}")

        ts = _format_lrc_timestamp(line_start)
        if plain_line:
            lrc_lines.append(f"{ts}{plain_line}")
        if trans_text:
            tlyric_lines.append(f"{ts}{trans_text}")
        if roman_text:
            roman_lines.append(f"{ts}{roman_text}")

    if not yrc_lines:
        return None
    return {
        "yrc": "\n".join(yrc_lines) if has_word_timing or yrc_lines else "",
        "lrc": "\n".join(lrc_lines),
        "tlyrics": "\n".join(tlyric_lines),
        "romalrc": "\n".join(roman_lines),
    }


def fetch_amll_ttml_as_yrc(
    song_id: int,
    *,
    timeout: float = 3.0,
    http_client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """Fetch word-timed TTML lyrics from amll-ttml-db with strict zero-Cookie isolation & LRU cache."""
    try:
        sid = int(song_id)
    except (TypeError, ValueError):
        return None
    if sid <= 0:
        return None

    now = time.monotonic()
    with _cache_lock:
        cached = _ttml_cache.get(sid)
        if cached is not None:
            expires_at, value = cached
            if now < expires_at:
                _ttml_cache.move_to_end(sid)
                return dict(value) if value is not None else None
            _ttml_cache.pop(sid, None)

    url = AMLL_TTML_URL_TEMPLATE.format(song_id=sid)
    headers = {"User-Agent": _AMLL_USER_AGENT, "Accept": "application/xml, text/xml, text/plain"}
    effective_timeout = min(3.0, max(0.5, float(timeout)))

    owns_client = http_client is None
    if http_client is not None:
        client = http_client
    else:
        try:
            client = httpx.Client(
                timeout=httpx.Timeout(effective_timeout, connect=min(effective_timeout, 2.0)),
                follow_redirects=False,
                cookies=httpx.Cookies(),
                http2=False,
            )
        except httpx.InvalidURL:
            client = httpx.Client(
                timeout=httpx.Timeout(effective_timeout, connect=min(effective_timeout, 2.0)),
                follow_redirects=False,
                trust_env=False,
                cookies=httpx.Cookies(),
                http2=False,
            )
    try:
        with client.stream("GET", url, headers=headers, timeout=effective_timeout) as response:
            if response.status_code != 200:
                _put_cache(sid, None, _MISS_TTL_SEC)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > _MAX_TTML_BYTES:
                    _put_cache(sid, None, _MISS_TTL_SEC)
                    return None
                chunks.append(chunk)
            raw = b"".join(chunks)
    except (httpx.HTTPError, TimeoutError, OSError):
        return None
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:
                pass

    try:
        ttml_text = raw.decode("utf-8")
        parsed = parse_ttml_to_yrc(ttml_text)
    except (UnicodeDecodeError, ValueError):
        _put_cache(sid, None, _MISS_TTL_SEC)
        return None

    if parsed is None:
        _put_cache(sid, None, _MISS_TTL_SEC)
        return None

    result = {**parsed, "ttml_source": "amll-ttml-db"}
    _put_cache(sid, result, _CACHE_TTL_SEC)
    return dict(result)


def _put_cache(song_id: int, value: dict[str, Any] | None, ttl_sec: float) -> None:
    now = time.monotonic()
    with _cache_lock:
        _ttml_cache[song_id] = (now + ttl_sec, dict(value) if value is not None else None)
        _ttml_cache.move_to_end(song_id)
        while len(_ttml_cache) > _CACHE_MAX_ENTRIES:
            _ttml_cache.popitem(last=False)
