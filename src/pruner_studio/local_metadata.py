"""Rich local audio metadata, ReplayGain/R128, ISRC/MBID, and embedded cover art extraction.

Uses `mutagen` when installed, and includes a comprehensive pure-Python parser fallback
supporting FLAC, MP3 (ID3v2/v1 + GBK fallback + MPEG/Xing/VBRI duration), M4A/MP4,
OGG Vorbis, Opus, WAV, and AAC (ADTS).
"""
from __future__ import annotations

import base64
import hashlib
import math
import os
from pathlib import Path
import re
import struct
import tempfile
import threading
from typing import Any

from .track_identity import normalize_isrc, normalize_mbid

SUPPORTED_AUDIO_EXTENSIONS = frozenset({".flac", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".opus"})

_COVER_MAX_BYTES = 4 * 1024 * 1024
_COVER_CACHE_MAX_FILES = 1500
_COVER_ID_RE = re.compile(r"^[0-9a-f]{24}$")

_RG_DB_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)")

# MPEG audio tables for accurate MP3 frame/Xing/VBRI duration parsing
_MPEG1_L3_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MPEG2_L3_BITRATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_MPEG1_RATES = (44100, 48000, 32000, 0)
_MPEG2_RATES = (22050, 24000, 16000, 0)
_MPEG25_RATES = (11025, 12000, 8000, 0)
_ADTS_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)


def parse_replaygain_db(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        val = float(value)
        return round(val, 3) if math.isfinite(val) and -40.0 <= val <= 40.0 else None
    match = _RG_DB_RE.search(str(value).strip())
    if not match:
        return None
    try:
        val = float(match.group(1))
    except ValueError:
        return None
    return round(val, 3) if math.isfinite(val) and -40.0 <= val <= 40.0 else None


def parse_replaygain_peak(value: Any) -> float | None:
    if value is None:
        return None
    try:
        val = float(str(value).strip())
    except ValueError:
        return None
    if not math.isfinite(val) or val <= 0.0 or val > 4.0:
        return None
    return round(val, 6)


def compute_loudness_compensation(
    track_gain_db: float | None = None,
    track_peak: float | None = None,
    *,
    album_gain_db: float | None = None,
    album_peak: float | None = None,
    target_lufs: float = -14.0,
) -> dict[str, Any]:
    """Compute EBU R128 / ReplayGain linear gain compensation with anti-clipping peak cap."""
    raw_gain = track_gain_db if track_gain_db is not None else album_gain_db
    raw_peak = track_peak if track_peak is not None else album_peak
    # Standard ReplayGain 2.0 reference is -18.0 LUFS; shift smoothly toward target_lufs (default -14.0 LUFS).
    target = max(-24.0, min(-9.0, float(target_lufs)))
    if raw_gain is None:
        return {
            "replaygain_track_gain_db": None,
            "replaygain_track_peak": raw_peak,
            "replaygain_album_gain_db": album_gain_db,
            "replaygain_album_peak": album_peak,
            "target_lufs": target,
            "computed_gain_db": 0.0,
            "computed_gain_linear": 1.0,
            "source": "none",
        }
    adjusted_db = float(raw_gain) + (target - (-18.0))
    adjusted_db = max(-18.0, min(8.0, adjusted_db))
    linear = 10.0 ** (adjusted_db / 20.0)
    if raw_peak is not None and raw_peak > 0.0 and linear * raw_peak > 0.99:
        linear = 0.99 / raw_peak
        adjusted_db = 20.0 * math.log10(max(linear, 1e-4))
    linear = max(0.15, min(2.5, linear))
    return {
        "replaygain_track_gain_db": track_gain_db,
        "replaygain_track_peak": raw_peak,
        "replaygain_album_gain_db": album_gain_db,
        "replaygain_album_peak": album_peak,
        "target_lufs": target,
        "computed_gain_db": round(adjusted_db, 2),
        "computed_gain_linear": round(linear, 4),
        "source": "replaygain_tag" if track_gain_db is not None else "replaygain_album_tag",
    }


def detect_image_mime(data: bytes) -> tuple[str, str] | None:
    """Validate image magic bytes and return (mime_type, file_ext) or None."""
    if not data or len(data) < 12 or len(data) > _COVER_MAX_BYTES:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    return None


class LocalCoverCache:
    """Content-addressed bounded disk cache for extracted embedded local audio covers."""

    def __init__(self, data_dir: Path | None):
        self.cache_dir = (Path(data_dir) / "cover_cache") if data_dir is not None else None
        self._lock = threading.RLock()

    def put(self, image_bytes: bytes) -> str:
        if self.cache_dir is None:
            return ""
        detected = detect_image_mime(image_bytes)
        if detected is None:
            return ""
        _, ext = detected
        cover_id = hashlib.sha256(image_bytes).hexdigest()[:24]
        with self._lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            target = self.cache_dir / f"{cover_id}.{ext}"
            if target.is_file():
                return cover_id
            fd, tmp = tempfile.mkstemp(prefix=".cover-", suffix=f".{ext}", dir=self.cache_dir)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(image_bytes)
                    handle.flush()
                os.replace(tmp, target)
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                return ""
            self._evict_if_needed_locked()
        return cover_id

    def get(self, cover_id: str) -> tuple[bytes, str] | None:
        if self.cache_dir is None or not isinstance(cover_id, str) or not _COVER_ID_RE.match(cover_id):
            return None
        with self._lock:
            for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png"), ("webp", "image/webp"), ("gif", "image/gif")):
                candidate = self.cache_dir / f"{cover_id}.{ext}"
                if candidate.is_file():
                    try:
                        data = candidate.read_bytes()
                        if len(data) <= _COVER_MAX_BYTES:
                            return data, mime
                    except OSError:
                        return None
        return None

    def _evict_if_needed_locked(self) -> None:
        if self.cache_dir is None or not self.cache_dir.is_dir():
            return
        try:
            files = [p for p in self.cache_dir.iterdir() if p.is_file() and not p.name.startswith(".")]
            if len(files) <= _COVER_CACHE_MAX_FILES:
                return
            files.sort(key=lambda p: p.stat().st_mtime)
            for victim in files[: len(files) - _COVER_CACHE_MAX_FILES]:
                try:
                    victim.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass


def _decode_legacy_bytes(raw: bytes) -> str:
    """Decode Latin-1/GBK/UTF-8 text cleanly for legacy MP3/WAV tags with Chinese GBK fallback."""
    cleaned = raw.split(b"\x00", 1)[0]
    if not cleaned:
        return ""
    if all(b < 0x80 for b in cleaned):
        return cleaned.decode("ascii", errors="ignore").strip()
    try:
        return cleaned.decode("utf-8").strip()
    except UnicodeDecodeError:
        pass
    try:
        return cleaned.decode("gb18030").strip()
    except UnicodeDecodeError:
        return cleaned.decode("latin-1", errors="ignore").strip()


def _decode_id3_text_bytes(raw: bytes) -> str:
    if not raw:
        return ""
    encoding = raw[0]
    body = raw[1:]
    try:
        if encoding == 0:
            return _decode_legacy_bytes(body)
        if encoding == 1:
            return body.decode("utf-16", errors="ignore").split("\x00", 1)[0].strip()
        if encoding == 2:
            return body.decode("utf-16-be", errors="ignore").split("\x00", 1)[0].strip()
        if encoding == 3:
            return body.decode("utf-8", errors="ignore").split("\x00", 1)[0].strip()
    except Exception:
        return ""
    return ""


def _decode_id3_txxx(frame_data: bytes) -> tuple[str, str]:
    if len(frame_data) < 2:
        return "", ""
    enc = frame_data[0]
    body = frame_data[1:]
    try:
        if enc in (1, 2):
            codec = "utf-16" if enc == 1 else "utf-16-be"
            text = body.decode(codec, errors="ignore")
            parts = text.split("\x00", 1)
            return parts[0].strip(), (parts[1].rstrip("\x00").strip() if len(parts) > 1 else "")
        parts = body.split(b"\x00", 1)
        if enc == 3:
            desc = parts[0].decode("utf-8", errors="ignore").strip()
            val = parts[1].decode("utf-8", errors="ignore").rstrip("\x00").strip() if len(parts) > 1 else ""
        else:
            desc = _decode_legacy_bytes(parts[0])
            val = _decode_legacy_bytes(parts[1]) if len(parts) > 1 else ""
        return desc, val
    except Exception:
        return "", ""


def _extract_id3_apic(frame_data: bytes) -> bytes | None:
    if len(frame_data) < 16:
        return None
    enc = frame_data[0]
    pos = 1
    mime_end = frame_data.find(b"\x00", pos)
    if mime_end == -1:
        return None
    pos = mime_end + 1
    if pos >= len(frame_data):
        return None
    # Picture type byte
    pos += 1
    if enc in (1, 2):
        # UTF-16 null terminator (2 zero bytes on 2-byte boundary)
        while pos + 1 < len(frame_data):
            if frame_data[pos] == 0 and frame_data[pos + 1] == 0:
                pos += 2
                break
            pos += 2
    else:
        desc_end = frame_data.find(b"\x00", pos)
        if desc_end == -1:
            return None
        pos = desc_end + 1
    img = frame_data[pos:]
    return img if detect_image_mime(img) is not None else None


def _apply_common_tag(meta: dict[str, Any], key: str, value: str) -> None:
    ku = key.strip().upper().replace(" ", "_").replace("-", "_")
    val = value.strip()
    if not val:
        return
    if ku == "TITLE" and not meta.get("title"):
        meta["title"] = val
    elif ku in {"ARTIST", "ALBUMARTIST", "ALBUM_ARTIST"} and not meta.get("artist"):
        meta["artist"] = val
    elif ku == "ALBUM" and not meta.get("album"):
        meta["album"] = val
    elif ku == "ISRC" and not meta.get("isrc"):
        norm = normalize_isrc(val)
        if norm:
            meta["isrc"] = norm
    elif ku in {"MUSICBRAINZ_TRACKID", "MUSICBRAINZ_TRACK_ID", "MUSICBRAINZ_RECORDINGID", "MBID"} and not meta.get("mbid"):
        norm = normalize_mbid(val)
        if norm:
            meta["mbid"] = norm
    elif ku == "REPLAYGAIN_TRACK_GAIN" and meta.get("replaygain_track_gain_db") is None:
        meta["replaygain_track_gain_db"] = parse_replaygain_db(val)
    elif ku == "REPLAYGAIN_TRACK_PEAK" and meta.get("replaygain_track_peak") is None:
        meta["replaygain_track_peak"] = parse_replaygain_peak(val)
    elif ku == "REPLAYGAIN_ALBUM_GAIN" and meta.get("replaygain_album_gain_db") is None:
        meta["replaygain_album_gain_db"] = parse_replaygain_db(val)
    elif ku == "REPLAYGAIN_ALBUM_PEAK" and meta.get("replaygain_album_peak") is None:
        meta["replaygain_album_peak"] = parse_replaygain_peak(val)
    elif ku == "R128_TRACK_GAIN" and meta.get("replaygain_track_gain_db") is None:
        try:
            q78 = int(val)
            # RFC 7845 R128 reference is -23 LUFS; ReplayGain 2.0 reference is -18 LUFS (+5 dB offset).
            meta["replaygain_track_gain_db"] = round(q78 / 256.0 + 5.0, 3)
        except ValueError:
            pass
    elif ku == "R128_ALBUM_GAIN" and meta.get("replaygain_album_gain_db") is None:
        try:
            q78 = int(val)
            meta["replaygain_album_gain_db"] = round(q78 / 256.0 + 5.0, 3)
        except ValueError:
            pass


def _parse_flac_picture_block(block: bytes) -> bytes | None:
    if len(block) < 32:
        return None
    pos = 4  # skip picture type
    mime_len = struct.unpack_from(">I", block, pos)[0]
    pos += 4 + mime_len
    if pos + 4 > len(block):
        return None
    desc_len = struct.unpack_from(">I", block, pos)[0]
    pos += 4 + desc_len + 16  # skip width, height, depth, colors
    if pos + 4 > len(block):
        return None
    data_len = struct.unpack_from(">I", block, pos)[0]
    pos += 4
    if data_len <= 0 or pos + data_len > len(block):
        return None
    img = block[pos : pos + data_len]
    return img if detect_image_mime(img) is not None else None


def _parse_vorbis_comment_blob(block: bytes, meta: dict[str, Any]) -> None:
    if len(block) < 8:
        return
    offset = 0
    vendor_len = struct.unpack_from("<I", block, offset)[0]
    offset += 4 + vendor_len
    if offset + 4 > len(block):
        return
    comment_count = struct.unpack_from("<I", block, offset)[0]
    offset += 4
    for _ in range(min(comment_count, 256)):
        if offset + 4 > len(block):
            break
        entry_len = struct.unpack_from("<I", block, offset)[0]
        offset += 4
        if entry_len <= 0 or offset + entry_len > len(block):
            break
        raw_entry = block[offset : offset + entry_len].decode("utf-8", errors="ignore")
        offset += entry_len
        if "=" not in raw_entry:
            continue
        k, v = raw_entry.split("=", 1)
        ku = k.strip().upper()
        if ku == "METADATA_BLOCK_PICTURE" and "cover_bytes" not in meta:
            try:
                pic_block = base64.b64decode(v.strip())
                img = _parse_flac_picture_block(pic_block)
                if img is not None:
                    meta["cover_bytes"] = img
            except Exception:
                pass
        else:
            _apply_common_tag(meta, k, v)


def _parse_flac_pure(path: Path, file_size: int) -> dict[str, Any]:
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
            if length > _COVER_MAX_BYTES + 65536:
                handle.seek(length, os.SEEK_CUR)
                if is_last:
                    break
                continue
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
            elif block_type == 4:
                _parse_vorbis_comment_blob(block, meta)
            elif block_type == 6 and "cover_bytes" not in meta:
                img = _parse_flac_picture_block(block)
                if img is not None:
                    meta["cover_bytes"] = img
            if is_last:
                break
    return meta


def _parse_mp3_frames_info(handle: Any, audio_start: int, file_size: int, meta: dict[str, Any]) -> None:
    handle.seek(audio_start)
    buf = handle.read(65536)
    if len(buf) < 4:
        return
    # Locate first valid MPEG audio frame sync (11 set bits)
    sync_pos = -1
    for i in range(len(buf) - 4):
        b0, b1, b2, b3 = buf[i], buf[i + 1], buf[i + 2], buf[i + 3]
        if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
            continue
        version_bits = (b1 >> 3) & 0x03
        layer_bits = (b1 >> 1) & 0x03
        br_idx = (b2 >> 4) & 0x0F
        sr_idx = (b2 >> 2) & 0x03
        if version_bits == 1 or layer_bits == 0 or br_idx in (0, 15) or sr_idx == 3:
            continue
        sync_pos = i
        break
    if sync_pos < 0:
        return

    b1, b2, b3 = buf[sync_pos + 1], buf[sync_pos + 2], buf[sync_pos + 3]
    version_bits = (b1 >> 3) & 0x03  # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    layer_bits = (b1 >> 1) & 0x03  # 1=Layer III
    br_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    channel_mode = (b3 >> 6) & 0x03  # 3=mono

    if version_bits == 3:
        sample_rate = _MPEG1_RATES[sr_idx]
        bitrate_kbps = _MPEG1_L3_BITRATES[br_idx] if layer_bits == 1 else 0
        samples_per_frame = 1152 if layer_bits in (1, 2) else 384
        xing_offset = 17 if channel_mode == 3 else 32
    else:
        sample_rate = _MPEG2_RATES[sr_idx] if version_bits == 2 else _MPEG25_RATES[sr_idx]
        bitrate_kbps = _MPEG2_L3_BITRATES[br_idx] if layer_bits == 1 else 0
        samples_per_frame = 576 if layer_bits == 1 else (1152 if layer_bits == 2 else 384)
        xing_offset = 9 if channel_mode == 3 else 17

    if sample_rate > 0:
        meta["sample_rate"] = sample_rate

    audio_bytes = max(0, file_size - audio_start - sync_pos)
    # Check Xing / Info VBR header
    x_pos = sync_pos + 4 + xing_offset
    if x_pos + 12 <= len(buf) and buf[x_pos : x_pos + 4] in (b"Xing", b"Info"):
        flags = struct.unpack_from(">I", buf, x_pos + 4)[0]
        if (flags & 0x01) and x_pos + 12 <= len(buf):
            total_frames = struct.unpack_from(">I", buf, x_pos + 8)[0]
            if total_frames > 0 and sample_rate > 0:
                duration_ms = int((total_frames * samples_per_frame * 1000) / sample_rate)
                meta["duration_ms"] = duration_ms
                if duration_ms > 0:
                    meta["bitrate"] = int((audio_bytes * 8 * 1000) / duration_ms)
                return

    # Check VBRI header (always 32 bytes after MPEG header)
    vbri_pos = sync_pos + 4 + 32
    if vbri_pos + 18 <= len(buf) and buf[vbri_pos : vbri_pos + 4] == b"VBRI":
        total_frames = struct.unpack_from(">I", buf, vbri_pos + 14)[0]
        if total_frames > 0 and sample_rate > 0:
            duration_ms = int((total_frames * samples_per_frame * 1000) / sample_rate)
            meta["duration_ms"] = duration_ms
            if duration_ms > 0:
                meta["bitrate"] = int((audio_bytes * 8 * 1000) / duration_ms)
            return

    # CBR estimate from first frame bitrate
    if bitrate_kbps > 0:
        meta["bitrate"] = bitrate_kbps * 1000
        if not meta.get("duration_ms") and audio_bytes > 0:
            meta["duration_ms"] = int((audio_bytes * 8 * 1000) / (bitrate_kbps * 1000))


def _extract_id3v22_pic(frame_data: bytes) -> bytes | None:
    """Extract embedded image bytes from an ID3v2.2 PIC frame."""
    if len(frame_data) < 10:
        return None
    enc = frame_data[0]
    pos = 1 + 3 + 1  # encoding + 3-byte image format + picture type
    if enc in (1, 2):
        while pos + 1 < len(frame_data):
            if frame_data[pos] == 0 and frame_data[pos + 1] == 0:
                pos += 2
                break
            pos += 2
    else:
        desc_end = frame_data.find(b"\x00", pos)
        if desc_end == -1:
            return None
        pos = desc_end + 1
    img = frame_data[pos:]
    return img if detect_image_mime(img) is not None else None


def _deunsync(data: bytes) -> bytes:
    return data.replace(b"\xff\x00", b"\xff")


def _parse_id3v2_blob(raw_tag: bytes, version: int, meta: dict[str, Any], *, tag_flags: int = 0) -> None:
    # Tag-level unsynchronization (ID3v2.2/2.3/2.4 flag bit 0x80).
    if tag_flags & 0x80:
        raw_tag = _deunsync(raw_tag)
    pos = 0
    # Skip ID3v2.3 / ID3v2.4 extended header (flag bit 0x40).
    if (tag_flags & 0x40) and version in (3, 4) and len(raw_tag) >= 4:
        if version == 4:
            ext_size = (
                ((raw_tag[0] & 0x7F) << 21)
                | ((raw_tag[1] & 0x7F) << 14)
                | ((raw_tag[2] & 0x7F) << 7)
                | (raw_tag[3] & 0x7F)
            )
            pos = ext_size if 6 <= ext_size <= len(raw_tag) else 0
        else:
            ext_size = struct.unpack_from(">I", raw_tag, 0)[0]
            total_ext = 4 + ext_size
            pos = total_ext if 4 < total_ext <= len(raw_tag) else 0

    if version == 2:
        while pos + 6 <= len(raw_tag):
            fid = raw_tag[pos : pos + 3].decode("latin-1", errors="ignore")
            if not fid.strip("\x00"):
                break
            fsize = int.from_bytes(raw_tag[pos + 3 : pos + 6], "big")
            pos += 6
            if fsize <= 0 or pos + fsize > len(raw_tag):
                break
            fdata = raw_tag[pos : pos + fsize]
            pos += fsize
            if fid == "TT2" and not meta.get("title"):
                meta["title"] = _decode_id3_text_bytes(fdata)
            elif fid == "TP1" and not meta.get("artist"):
                meta["artist"] = _decode_id3_text_bytes(fdata)
            elif fid == "TAL" and not meta.get("album"):
                meta["album"] = _decode_id3_text_bytes(fdata)
            elif fid == "PIC" and "cover_bytes" not in meta:
                img = _extract_id3v22_pic(fdata)
                if img is not None:
                    meta["cover_bytes"] = img
        return

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
        frame_flags = struct.unpack_from(">H", raw_tag, pos + 8)[0]
        pos += 10
        if frame_size <= 0 or pos + frame_size > len(raw_tag):
            break
        frame_data = raw_tag[pos : pos + frame_size]
        pos += frame_size
        if version == 4:
            if (frame_flags & 0x0002) and not (tag_flags & 0x80):
                frame_data = _deunsync(frame_data)
            if (frame_flags & 0x0001) and len(frame_data) >= 4:
                frame_data = frame_data[4:]
        if frame_id == "TIT2" and not meta.get("title"):
            meta["title"] = _decode_id3_text_bytes(frame_data)
        elif frame_id == "TPE1" and not meta.get("artist"):
            meta["artist"] = _decode_id3_text_bytes(frame_data)
        elif frame_id == "TALB" and not meta.get("album"):
            meta["album"] = _decode_id3_text_bytes(frame_data)
        elif frame_id == "TLEN" and not meta.get("duration_ms"):
            text_val = _decode_id3_text_bytes(frame_data)
            if text_val.isdigit():
                meta["duration_ms"] = int(text_val)
        elif frame_id == "TSRC" and not meta.get("isrc"):
            norm = normalize_isrc(_decode_id3_text_bytes(frame_data))
            if norm:
                meta["isrc"] = norm
        elif frame_id == "TXXX":
            desc, val = _decode_id3_txxx(frame_data)
            if desc and val:
                _apply_common_tag(meta, desc, val)
        elif frame_id == "UFID" and not meta.get("mbid"):
            parts = frame_data.split(b"\x00", 1)
            if len(parts) == 2 and b"musicbrainz" in parts[0].lower():
                norm = normalize_mbid(parts[1].decode("ascii", errors="ignore"))
                if norm:
                    meta["mbid"] = norm
        elif frame_id == "APIC" and "cover_bytes" not in meta:
            img = _extract_id3_apic(frame_data)
            if img is not None:
                meta["cover_bytes"] = img


def _parse_mp3_pure(path: Path, file_size: int) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    audio_start = 0
    with path.open("rb") as handle:
        header = handle.read(10)
        if len(header) == 10 and header[:3] == b"ID3" and header[3] in (2, 3, 4):
            version = header[3]
            tag_flags = header[5]
            tag_size = (
                ((header[6] & 0x7F) << 21)
                | ((header[7] & 0x7F) << 14)
                | ((header[8] & 0x7F) << 7)
                | (header[9] & 0x7F)
            )
            audio_start = 10 + tag_size + (10 if (version == 4 and (tag_flags & 0x10)) else 0)
            raw_tag = handle.read(min(tag_size, _COVER_MAX_BYTES + 262_144))
            _parse_id3v2_blob(raw_tag, version, meta, tag_flags=tag_flags)

        # ID3v1 fallback at tail of file
        if file_size >= 128 and (not meta.get("title") or not meta.get("artist") or not meta.get("album")):
            handle.seek(file_size - 128)
            tail = handle.read(128)
            if len(tail) == 128 and tail[:3] == b"TAG":
                if not meta.get("title"):
                    meta["title"] = _decode_legacy_bytes(tail[3:33])
                if not meta.get("artist"):
                    meta["artist"] = _decode_legacy_bytes(tail[33:63])
                if not meta.get("album"):
                    meta["album"] = _decode_legacy_bytes(tail[63:93])

        _parse_mp3_frames_info(handle, audio_start, file_size, meta)
    return meta


def _parse_wav_pure(path: Path) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    with path.open("rb") as handle:
        riff = handle.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            return meta
        byte_rate = 0
        data_size = 0
        for _ in range(48):
            chunk_hdr = handle.read(8)
            if len(chunk_hdr) < 8:
                break
            chunk_id = chunk_hdr[:4]
            chunk_size = struct.unpack("<I", chunk_hdr[4:8])[0]
            chunk_start = handle.tell()
            if chunk_id == b"fmt " and chunk_size >= 16:
                fmt = handle.read(16)
                if len(fmt) == 16:
                    _, _channels, sample_rate, byte_rate = struct.unpack("<HHII", fmt[:12])
                    meta["sample_rate"] = sample_rate
                    meta["bitrate"] = byte_rate * 8
            elif chunk_id == b"data":
                data_size = chunk_size
            elif chunk_id == b"LIST" and 4 < chunk_size <= 65536:
                blob = handle.read(chunk_size)
                if blob[:4] == b"INFO":
                    pos = 4
                    while pos + 8 <= len(blob):
                        sub_id = blob[pos : pos + 4]
                        sub_len = struct.unpack_from("<I", blob, pos + 4)[0]
                        pos += 8
                        if pos + sub_len > len(blob):
                            break
                        val = _decode_legacy_bytes(blob[pos : pos + sub_len])
                        pos += sub_len + (sub_len % 2)
                        if sub_id == b"INAM" and not meta.get("title"):
                            meta["title"] = val
                        elif sub_id == b"IART" and not meta.get("artist"):
                            meta["artist"] = val
                        elif sub_id == b"IPRD" and not meta.get("album"):
                            meta["album"] = val
            elif chunk_id in (b"id3 ", b"ID3 ") and 10 < chunk_size <= _COVER_MAX_BYTES + 262_144:
                blob = handle.read(chunk_size)
                if blob[:3] == b"ID3" and blob[3] in (2, 3, 4):
                    _parse_id3v2_blob(blob[10:], blob[3], meta, tag_flags=blob[5])
            handle.seek(chunk_start + chunk_size + (chunk_size % 2), os.SEEK_SET)
        if byte_rate > 0 and data_size > 0:
            meta["duration_ms"] = int((data_size * 1000) / byte_rate)
    return meta


def _iter_mp4_boxes(buf: bytes, start: int = 0, end: int | None = None):
    limit = len(buf) if end is None else min(end, len(buf))
    pos = start
    while pos + 8 <= limit:
        size = struct.unpack_from(">I", buf, pos)[0]
        box_type = buf[pos + 4 : pos + 8]
        header_len = 8
        if size == 1:
            if pos + 16 > limit:
                break
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header_len = 16
        elif size == 0:
            size = limit - pos
        if size < header_len or pos + size > limit:
            break
        yield box_type, pos + header_len, pos + size
        pos += size


def _parse_mp4_ilst(buf: bytes, start: int, end: int, meta: dict[str, Any]) -> None:
    for box_type, bstart, bend in _iter_mp4_boxes(buf, start, end):
        if box_type == b"----":
            mean_name = ""
            data_val = ""
            for sub_t, sstart, send in _iter_mp4_boxes(buf, bstart, bend):
                if sub_t == b"name" and send - sstart > 4:
                    mean_name = buf[sstart + 4 : send].decode("utf-8", errors="ignore").strip()
                elif sub_t == b"data" and send - sstart > 8:
                    data_val = buf[sstart + 8 : send].decode("utf-8", errors="ignore").strip()
            if mean_name and data_val:
                _apply_common_tag(meta, mean_name, data_val)
            continue
        for sub_t, sstart, send in _iter_mp4_boxes(buf, bstart, bend):
            if sub_t != b"data" or send - sstart <= 8:
                continue
            payload = buf[sstart + 8 : send]
            if box_type == b"\xa9nam" and not meta.get("title"):
                meta["title"] = payload.decode("utf-8", errors="ignore").strip()
            elif box_type in (b"\xa9ART", b"aART") and not meta.get("artist"):
                meta["artist"] = payload.decode("utf-8", errors="ignore").strip()
            elif box_type == b"\xa9alb" and not meta.get("album"):
                meta["album"] = payload.decode("utf-8", errors="ignore").strip()
            elif box_type == b"covr" and "cover_bytes" not in meta:
                if detect_image_mime(payload) is not None:
                    meta["cover_bytes"] = payload


def _parse_m4a_pure(path: Path, file_size: int) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    with path.open("rb") as handle:
        # Walk top-level boxes to locate `moov` cleanly without reading huge `mdat`
        pos = 0
        moov_blob: bytes | None = None
        while pos + 8 <= file_size:
            handle.seek(pos)
            hdr = handle.read(16)
            if len(hdr) < 8:
                break
            size = struct.unpack_from(">I", hdr, 0)[0]
            box_type = hdr[4:8]
            hdr_len = 8
            if size == 1 and len(hdr) >= 16:
                size = struct.unpack_from(">Q", hdr, 8)[0]
                hdr_len = 16
            elif size == 0:
                size = file_size - pos
            if size < hdr_len:
                break
            if box_type == b"moov" and size - hdr_len <= _COVER_MAX_BYTES + 2_097_152:
                handle.seek(pos + hdr_len)
                moov_blob = handle.read(size - hdr_len)
                break
            pos += size
    if not moov_blob:
        return meta

    def walk(start: int, end: int) -> None:
        for btype, bstart, bend in _iter_mp4_boxes(moov_blob, start, end):
            if btype == b"mvhd" and bend - bstart >= 20:
                version = moov_blob[bstart]
                if version == 0 and bend - bstart >= 20:
                    timescale, duration = struct.unpack_from(">II", moov_blob, bstart + 12)
                elif version == 1 and bend - bstart >= 32:
                    timescale = struct.unpack_from(">I", moov_blob, bstart + 20)[0]
                    duration = struct.unpack_from(">Q", moov_blob, bstart + 24)[0]
                else:
                    timescale, duration = 0, 0
                if timescale > 0 and duration > 0:
                    dur_ms = int((duration * 1000) / timescale)
                    meta["duration_ms"] = dur_ms
                    if dur_ms > 0:
                        meta["bitrate"] = int((file_size * 8 * 1000) / dur_ms)
            elif btype == b"mdhd" and bend - bstart >= 20 and not meta.get("sample_rate"):
                version = moov_blob[bstart]
                if version == 0:
                    ts = struct.unpack_from(">I", moov_blob, bstart + 12)[0]
                elif version == 1 and bend - bstart >= 24:
                    ts = struct.unpack_from(">I", moov_blob, bstart + 20)[0]
                else:
                    ts = 0
                if 8000 <= ts <= 384000:
                    meta["sample_rate"] = ts
            elif btype == b"meta" and bend - bstart > 4:
                walk(bstart + 4, bend)
            elif btype == b"ilst":
                _parse_mp4_ilst(moov_blob, bstart, bend, meta)
            elif btype in (b"trak", b"mdia", b"udta"):
                walk(bstart, bend)

    walk(0, len(moov_blob))
    return meta


def _read_ogg_packets(data: bytes, max_packets: int = 4) -> list[bytes]:
    packets: list[bytes] = []
    current = bytearray()
    pos = 0
    while pos + 27 <= len(data) and len(packets) < max_packets:
        if data[pos : pos + 4] != b"OggS":
            nxt = data.find(b"OggS", pos + 1)
            if nxt == -1:
                break
            pos = nxt
            continue
        seg_count = data[pos + 26]
        if pos + 27 + seg_count > len(data):
            break
        seg_table = data[pos + 27 : pos + 27 + seg_count]
        body_pos = pos + 27 + seg_count
        body_len = sum(seg_table)
        if body_pos + body_len > len(data):
            break
        cursor = body_pos
        for seg_len in seg_table:
            current.extend(data[cursor : cursor + seg_len])
            cursor += seg_len
            if seg_len < 255:
                packets.append(bytes(current))
                current.clear()
                if len(packets) >= max_packets:
                    break
        pos = body_pos + body_len
    return packets


def _last_ogg_granule(handle: Any, file_size: int) -> int:
    seek_pos = max(0, file_size - 65536)
    handle.seek(seek_pos)
    tail = handle.read()
    last_granule = 0
    idx = 0
    while True:
        found = tail.find(b"OggS", idx)
        if found == -1 or found + 14 > len(tail):
            break
        granule = struct.unpack_from("<q", tail, found + 6)[0]
        if granule > last_granule:
            last_granule = granule
        idx = found + 4
    return last_granule


def _parse_ogg_or_opus_pure(path: Path, file_size: int) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    with path.open("rb") as handle:
        head = handle.read(min(file_size, 512 * 1024))
        packets = _read_ogg_packets(head, max_packets=3)
        if not packets:
            return meta
        p0 = packets[0]
        is_opus = p0.startswith(b"OpusHead")
        sample_rate = 0
        pre_skip = 0
        if is_opus and len(p0) >= 18:
            sample_rate = 48000
            pre_skip = struct.unpack_from("<H", p0, 10)[0]
            meta["sample_rate"] = 48000
            # Per RFC 7845, OpusHead.output_gain is already applied by the decoder; read R128_TRACK_GAIN / REPLAYGAIN from OpusTags.
            if len(packets) >= 2 and packets[1].startswith(b"OpusTags"):
                _parse_vorbis_comment_blob(packets[1][8:], meta)
        elif p0.startswith(b"\x01vorbis") and len(p0) >= 28:
            sample_rate = struct.unpack_from("<I", p0, 12)[0]
            bitrate_nom = struct.unpack_from("<i", p0, 20)[0]
            if sample_rate > 0:
                meta["sample_rate"] = sample_rate
            if bitrate_nom > 0:
                meta["bitrate"] = bitrate_nom
            if len(packets) >= 2 and packets[1].startswith(b"\x03vorbis"):
                _parse_vorbis_comment_blob(packets[1][7:], meta)

        granule = _last_ogg_granule(handle, file_size)
        if granule > 0 and sample_rate > 0:
            effective = max(0, granule - pre_skip)
            duration_ms = int((effective * 1000) / sample_rate)
            if duration_ms > 0:
                meta["duration_ms"] = duration_ms
                if not meta.get("bitrate"):
                    meta["bitrate"] = int((file_size * 8 * 1000) / duration_ms)
    return meta


def _parse_aac_adts_pure(path: Path, file_size: int) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    audio_start = 0
    with path.open("rb") as handle:
        head = handle.read(10)
        if len(head) == 10 and head[:3] == b"ID3" and head[3] in (2, 3, 4):
            tag_flags = head[5]
            tag_size = (
                ((head[6] & 0x7F) << 21)
                | ((head[7] & 0x7F) << 14)
                | ((head[8] & 0x7F) << 7)
                | (head[9] & 0x7F)
            )
            audio_start = 10 + tag_size + (10 if (head[3] == 4 and (tag_flags & 0x10)) else 0)
            raw_tag = handle.read(min(tag_size, _COVER_MAX_BYTES + 262_144))
            _parse_id3v2_blob(raw_tag, head[3], meta, tag_flags=tag_flags)
        handle.seek(audio_start)
        buf = handle.read(32768)
    pos = 0
    frames = 0
    frame_bytes = 0
    sample_rate = 0
    while pos + 7 <= len(buf) and frames < 64:
        if buf[pos] != 0xFF or (buf[pos + 1] & 0xF0) != 0xF0:
            pos += 1
            continue
        sr_idx = (buf[pos + 2] >> 2) & 0x0F
        if sr_idx >= len(_ADTS_RATES):
            break
        flen = ((buf[pos + 3] & 0x03) << 11) | (buf[pos + 4] << 3) | ((buf[pos + 5] >> 5) & 0x07)
        if flen < 7 or pos + flen > len(buf):
            break
        sample_rate = _ADTS_RATES[sr_idx]
        frames += 1
        frame_bytes += flen
        pos += flen
    if sample_rate > 0 and frames > 0:
        meta["sample_rate"] = sample_rate
        avg_frame = frame_bytes / frames
        bitrate = int((avg_frame * 8 * sample_rate) / 1024)
        meta["bitrate"] = bitrate
        audio_bytes = max(0, file_size - audio_start)
        if bitrate > 0 and not meta.get("duration_ms"):
            meta["duration_ms"] = int((audio_bytes * 8 * 1000) / bitrate)
    return meta


def _try_parse_with_mutagen(path: Path) -> dict[str, Any] | None:
    try:
        import mutagen  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        audio = mutagen.File(str(path), easy=False)
        if audio is None:
            return None
        meta: dict[str, Any] = {}
        info = getattr(audio, "info", None)
        if info is not None:
            length = getattr(info, "length", 0) or 0
            if length > 0:
                meta["duration_ms"] = int(round(float(length) * 1000))
            br = getattr(info, "bitrate", 0) or 0
            if br > 0:
                meta["bitrate"] = int(br)
            sr = getattr(info, "sample_rate", 0) or 0
            if sr > 0:
                meta["sample_rate"] = int(sr)
        tags = getattr(audio, "tags", None)
        if tags is not None:
            for k in list(tags.keys()):
                val = tags[k]
                k_str = str(k)
                if k_str.startswith("APIC") and hasattr(val, "data") and "cover_bytes" not in meta:
                    if detect_image_mime(bytes(val.data)) is not None:
                        meta["cover_bytes"] = bytes(val.data)
                    continue
                if k_str == "covr" and isinstance(val, list) and val and "cover_bytes" not in meta:
                    raw = bytes(val[0])
                    if detect_image_mime(raw) is not None:
                        meta["cover_bytes"] = raw
                    continue
                if k_str.startswith("UFID:") and "musicbrainz" in k_str.lower() and hasattr(val, "data") and not meta.get("mbid"):
                    _apply_common_tag(meta, "MBID", bytes(val.data).decode("ascii", errors="ignore"))
                    continue
                if k_str.upper() == "METADATA_BLOCK_PICTURE" and "cover_bytes" not in meta:
                    raw_items = val if isinstance(val, list) else [val]
                    for b64_item in raw_items:
                        try:
                            img = _parse_flac_picture_block(base64.b64decode(str(b64_item).strip()))
                            if img is not None:
                                meta["cover_bytes"] = img
                                break
                        except Exception:
                            continue
                    continue
                if k_str.startswith("TXXX:"):
                    _apply_common_tag(meta, k_str[5:], str(val))
                    continue
                if k_str in ("TIT2", "\xa9nam"):
                    _apply_common_tag(meta, "TITLE", str(val[0] if isinstance(val, list) else val))
                elif k_str in ("TPE1", "\xa9ART", "aART"):
                    _apply_common_tag(meta, "ARTIST", str(val[0] if isinstance(val, list) else val))
                elif k_str in ("TALB", "\xa9alb"):
                    _apply_common_tag(meta, "ALBUM", str(val[0] if isinstance(val, list) else val))
                elif k_str == "TSRC":
                    _apply_common_tag(meta, "ISRC", str(val[0] if isinstance(val, list) else val))
                elif k_str.startswith("----:"):
                    name = k_str.split(":")[-1]
                    raw_v = val[0] if isinstance(val, list) else val
                    text_v = raw_v.decode("utf-8", errors="ignore") if isinstance(raw_v, (bytes, bytearray)) else str(raw_v)
                    _apply_common_tag(meta, name, text_v)
                else:
                    text_v = str(val[0]) if isinstance(val, list) and val else str(val)
                    _apply_common_tag(meta, k_str, text_v)
        pictures = getattr(audio, "pictures", None)
        if pictures and "cover_bytes" not in meta:
            for pic in pictures:
                raw = bytes(getattr(pic, "data", b""))
                if detect_image_mime(raw) is not None:
                    meta["cover_bytes"] = raw
                    break
        return meta
    except Exception:
        return None


def parse_audio_metadata(path: Path, file_size: int, *, cover_cache: LocalCoverCache | None = None) -> dict[str, Any]:
    """Parse rich metadata, ReplayGain tags, ISRC/MBID, and embedded cover from a local audio file."""
    fmt = path.suffix.lower().lstrip(".")
    meta = _try_parse_with_mutagen(path) or {}
    # Supplement/fallback with pure-Python parsers so every format works identically without mutagen.
    if not meta or not meta.get("title") or not meta.get("duration_ms") or "cover_bytes" not in meta:
        try:
            pure: dict[str, Any] = {}
            if fmt == "flac":
                pure = _parse_flac_pure(path, file_size)
            elif fmt == "mp3":
                pure = _parse_mp3_pure(path, file_size)
            elif fmt == "wav":
                pure = _parse_wav_pure(path)
            elif fmt == "m4a":
                pure = _parse_m4a_pure(path, file_size)
            elif fmt in ("ogg", "opus"):
                pure = _parse_ogg_or_opus_pure(path, file_size)
            elif fmt == "aac":
                pure = _parse_aac_adts_pure(path, file_size)
            for k, v in pure.items():
                if v is None or v == "":
                    continue
                if k in {"duration_ms", "bitrate", "sample_rate"} and v == 0:
                    continue
                existing = meta.get(k)
                if existing is None or existing == "" or (k in {"duration_ms", "bitrate", "sample_rate"} and existing == 0):
                    meta[k] = v
        except OSError:
            pass

    cover_bytes = meta.pop("cover_bytes", None)
    cover_id = ""
    if isinstance(cover_bytes, (bytes, bytearray)) and cover_cache is not None:
        cover_id = cover_cache.put(bytes(cover_bytes))
    meta["cover_id"] = cover_id
    return meta
