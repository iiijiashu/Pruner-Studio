"""Comprehensive regression & integration tests for Milestone 0 and Milestone 1."""
from __future__ import annotations

import asyncio
import base64
import gzip
import json
from pathlib import Path
import ssl
import struct
import subprocess
import sys
import time
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from pruner_studio import amll_lyrics, lan_sync
from pruner_studio.app import (
    _feedback_identity,
    _stream_remote_audio,
    build_application,
    create_asgi_app,
    validate_cover_url,
)
from pruner_studio.config import Settings
from pruner_studio.db import CatalogDatabase
from pruner_studio.errors import AppError
from pruner_studio.external_sources import (
    CustomEndpointProvider,
    ExternalCandidate,
    LocalLibraryScanner,
    TrackQuery,
    _scoped_channel_request,
)
from pruner_studio.lan_tls import (
    certificate_fingerprint,
    certificate_spki_fingerprint,
    certificate_spki_pin,
    load_identity,
    spki_fingerprint_from_der,
    spki_pin_from_der,
)
from pruner_studio.local_metadata import LocalCoverCache, parse_audio_metadata
from pruner_studio.models import AccountSummary, Song
from pruner_studio.netease_client import NoOpCookies, UrllibTransport
from pruner_studio.netease_crypto import get_device_id
from pruner_studio.open_protocols import (
    WebDAVEncryptedSync,
    WebDAVProvider,
    _self_hosted_request,
    decrypt_sync_envelope,
    encrypt_sync_envelope,
)
from pruner_studio.playlist_migration import parse_playlist_import
from pruner_studio.resolver_v2 import ChannelHealthTracker, ResolverRegistry, fetch_subscription_json
from pruner_studio.shuffle_engine import balanced_shuffle
from pruner_studio.sidecar_bridge import SidecarProcessManager, build_sanitized_sidecar_env
from pruner_studio.storage import ChannelCredentialVault, JsonStore, SessionStore
from test_backend import FakeTransport


def _make_app(tmp_path: Path, transport=None):
    settings = Settings(data_dir=tmp_path, web_dir=Path("web"), api_base="http://fake.invalid", port=0)
    return build_application(settings, transport or FakeTransport())


def _sample_png_bytes() -> bytes:
    # Minimal valid 1x1 PNG header + body (>= 12 bytes)
    return (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _sample_jpeg_bytes() -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _build_flac_file(path: Path, *, title: str, artist: str, album: str, isrc: str, rg_gain: str, rg_peak: str, cover: bytes) -> None:
    # STREAMINFO: 44100 Hz, 2ch, 16-bit, 441000 samples (10.0s)
    sr = 44100
    total_samples = 441000
    packed = (sr << 44) | (1 << 41) | (15 << 36) | total_samples
    streaminfo = b"\x00" * 10 + packed.to_bytes(8, "big") + (b"\x00" * 16)

    comments = [
        f"TITLE={title}",
        f"ARTIST={artist}",
        f"ALBUM={album}",
        f"ISRC={isrc}",
        "MUSICBRAINZ_TRACKID=123e4567-e89b-12d3-a456-426614174000",
        f"REPLAYGAIN_TRACK_GAIN={rg_gain}",
        f"REPLAYGAIN_TRACK_PEAK={rg_peak}",
    ]
    vc = bytearray(struct.pack("<I", 6) + b"pruner" + struct.pack("<I", len(comments)))
    for c in comments:
        raw = c.encode("utf-8")
        vc.extend(struct.pack("<I", len(raw)) + raw)

    mime = b"image/png"
    pic = (
        struct.pack(">II", 3, len(mime))
        + mime
        + struct.pack(">I", 0)
        + struct.pack(">IIII", 1, 1, 24, 0)
        + struct.pack(">I", len(cover))
        + cover
    )

    def block(btype: int, payload: bytes, last: bool = False) -> bytes:
        hdr = ((0x80 if last else 0x00) | btype).to_bytes(1, "big") + len(payload).to_bytes(3, "big")
        return hdr + payload

    path.write_bytes(b"fLaC" + block(0, streaminfo) + block(4, bytes(vc)) + block(6, pic, last=True) + b"\x00" * 256)


def _build_mp3_gbk_xing_file(path: Path, *, title_gbk: str, artist_gbk: str, cover_jpeg: bytes) -> None:
    def id3_frame(fid: str, data: bytes) -> bytes:
        return fid.encode("latin-1") + struct.pack(">I", len(data)) + b"\x00\x00" + data

    frames = b"".join(
        [
            id3_frame("TIT2", b"\x00" + title_gbk.encode("gb18030")),
            id3_frame("TPE1", b"\x00" + artist_gbk.encode("gb18030")),
            id3_frame("TXXX", b"\x03REPLAYGAIN_TRACK_GAIN\x00-5.40 dB"),
            id3_frame("TXXX", b"\x03REPLAYGAIN_TRACK_PEAK\x000.950000"),
            id3_frame("APIC", b"\x00image/jpeg\x00\x03\x00" + cover_jpeg),
        ]
    )
    size = len(frames)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
    id3 = b"ID3\x03\x00\x00" + syncsafe + frames

    # Valid MPEG1 Layer3 stereo 128kbps 44100Hz frame + Xing header with 1000 frames (~26122ms)
    mpeg_hdr = bytes([0xFF, 0xFB, 0x90, 0x00])
    xing = b"\x00" * 32 + b"Xing" + struct.pack(">II", 0x01, 1000) + (b"\x00" * 400)
    path.write_bytes(id3 + mpeg_hdr + xing)


def _build_m4a_file(path: Path, *, title: str, artist: str, cover_png: bytes) -> None:
    def box(btype: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + btype + payload

    def data_atom(dtype: int, payload: bytes) -> bytes:
        return box(b"data", struct.pack(">II", dtype, 0) + payload)

    mvhd = box(b"mvhd", b"\x00" * 12 + struct.pack(">II", 1000, 125_000) + b"\x00" * 80)
    mdhd = box(b"mdhd", b"\x00" * 12 + struct.pack(">II", 44100, 5_512_500) + b"\x00" * 4)
    ilst = box(
        b"ilst",
        box(b"\xa9nam", data_atom(1, title.encode("utf-8")))
        + box(b"\xa9ART", data_atom(1, artist.encode("utf-8")))
        + box(b"covr", data_atom(14, cover_png)),
    )
    meta = box(b"meta", b"\x00\x00\x00\x00" + ilst)
    moov = box(b"moov", mvhd + box(b"trak", box(b"mdia", mdhd)) + box(b"udta", meta))
    ftyp = box(b"ftyp", b"M4A \x00\x00\x00\x00M4A isom")
    path.write_bytes(ftyp + moov)


def _build_opus_file(path: Path, *, title: str, artist: str) -> None:
    def ogg_page(payload: bytes, granule: int = 0) -> bytes:
        assert len(payload) < 255
        return (
            b"OggS\x00\x00"
            + struct.pack("<qIIIB", granule, 1, 0, 0, 1)
            + bytes([len(payload)])
            + payload
        )

    # OpusHead: 48000 Hz, pre_skip=312, output_gain=-512 (-2 dB Q7.8 -> RG +3 dB)
    opus_head = b"OpusHead" + struct.pack("<BBHIhB", 1, 2, 312, 48000, -512, 0)
    comments = [f"TITLE={title}", f"ARTIST={artist}", "R128_TRACK_GAIN=-1024"]
    tags = bytearray(b"OpusTags" + struct.pack("<I", 6) + b"pruner" + struct.pack("<I", len(comments)))
    for c in comments:
        b = c.encode("utf-8")
        tags.extend(struct.pack("<I", len(b)) + b)
    # Final page with granule = 48000 * 15 + 312 -> exact 15_000 ms
    path.write_bytes(ogg_page(opus_head, 0) + ogg_page(bytes(tags), 0) + ogg_page(b"\x01\x02\x03", 48000 * 15 + 312))


def _build_aac_adts_file(path: Path) -> None:
    # 10 valid ADTS frames at 44100 Hz (sr_idx=4), frame_length=64 bytes
    flen = 64
    hdr = bytes(
        [
            0xFF,
            0xF1,
            (1 << 6) | (4 << 2) | 0,
            (2 << 6) | ((flen >> 11) & 0x03),
            (flen >> 3) & 0xFF,
            ((flen & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )
    frame = hdr + (b"\x00" * (flen - 7))
    path.write_bytes(frame * 20)


# ---------------------------------------------------------------------------
# Milestone 0 Tests
# ---------------------------------------------------------------------------


def test_feedback_identity_bug_fix_and_canonical_keys():
    # Regression: previously key[7:] turned "netease:12345" into ":12345" and always raised 400!
    assert _feedback_identity(None, "netease:12345") == (12345, "")
    assert _feedback_identity(12345, "netease:12345") == (12345, "")
    assert _feedback_identity(None, "local:/music/a.flac") == (0, "local:/music/a.flac")
    assert _feedback_identity(0, "opensubsonic:song-42") == (0, "opensubsonic:song-42")
    assert _feedback_identity(None, "webdav:abcdef123456") == (0, "webdav:abcdef123456")
    with pytest.raises(Exception):
        _feedback_identity(999, "netease:12345")
    with pytest.raises(Exception):
        _feedback_identity(None, "netease:")


def test_anti_ban_ua_stable_device_id_and_cookie_jar_isolation(tmp_path):
    # 1. Persistent stable deviceId across restarts
    _make_app(tmp_path)
    dev1 = get_device_id()
    assert len(dev1) == 32
    _make_app(tmp_path)
    dev2 = get_device_id()
    assert dev1 == dev2
    assert (tmp_path / "device_identity.json").is_file()

    # 2. NetEase transport sends realistic browser UA (NO Pruner-Studio/) AND never leaks Set-Cookie across requests
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"code": 200}, headers={"Set-Cookie": "LEAKED_SESSION=secret_from_req1; Path=/"})

    shared_client = httpx.Client(transport=httpx.MockTransport(handler))
    transport = UrllibTransport("https://music.163.com", http_client=shared_client)
    transport.request("/api/test", cookie="MUSIC_U=account_a")
    transport.request("/api/test", cookie="MUSIC_U=account_b")
    assert len(seen_headers) == 2
    assert "Pruner-Studio/" not in seen_headers[0]["user-agent"]
    assert "Chrome/" in seen_headers[0]["user-agent"]
    assert "LEAKED_SESSION" not in seen_headers[1].get("cookie", "")
    assert "account_a" not in seen_headers[1].get("cookie", "")
    assert "account_b" in seen_headers[1].get("cookie", "")
    shared_client.close()


def test_local_process_auth_token_protection(tmp_path):
    app = _make_app(tmp_path)
    asgi = create_asgi_app(app, 8765, require_local_token=True)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        # Health remains accessible without token
        assert client.get("/api/health").status_code == 200
        # Protected endpoints reject missing or wrong token
        r_missing = client.get("/api/status")
        assert r_missing.status_code == 401
        assert r_missing.json()["error"]["code"] == "LOCAL_AUTH_REQUIRED"
        assert client.get("/api/settings", headers={"X-Pruner-Token": "wrong"}).status_code == 401

        home = client.get("/")
        assert home.status_code == 200
        assert app.local_api_token not in home.text
        assert app.browser_api_token not in home.text
        assert "set-cookie" not in home.headers
        assert client.get("/api/status").status_code == 401

        launch_url = app.launcher_url(str(client.base_url))
        bootstrap = client.get(launch_url)
        assert bootstrap.status_code == 200
        assert app.local_api_token not in bootstrap.text
        assert f'name="pruner-browser-session" content="{app.browser_api_token}"' in bootstrap.text
        assert 'src="/bootstrap.js"' in bootstrap.text
        assert "set-cookie" not in bootstrap.headers
        assert client.get(launch_url).status_code == 403
        assert client.get("/bootstrap.js").status_code == 200
        assert client.get("/session-worker.js").status_code == 200
        assert client.get("/api/status", headers={"X-Pruner-Token": app.browser_api_token}).status_code == 200

    # Header-only token also succeeds without cookies
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as fresh_client:
        assert fresh_client.get("/api/status", headers={"X-Pruner-Token": app.local_api_token}).status_code == 200


def test_self_hosted_requests_pin_each_origin_and_decode_gzip_once(monkeypatch):
    addresses = {"music.example.test": "192.168.10.5", "media.example.test": "8.8.4.4"}
    def lookup(host, port, **_kwargs):
        return [(2, 1, 6, "", (addresses[host], port))]

    monkeypatch.setattr("pruner_studio.open_protocols.socket.getaddrinfo", lookup)
    monkeypatch.setattr("pruner_studio.external_sources._resolve_addresses", lookup)
    calls = []
    payload = b'{"result":"ok"}'
    def handler(req):
        authority = req.headers["host"]
        assert req.url.host == addresses[authority]
        assert req.extensions["sni_hostname"] == authority
        assert "cookie" not in req.headers
        calls.append(authority)
        if authority == "music.example.test":
            assert req.headers["authorization"] == "Basic test"
            return httpx.Response(302, headers={"Location": "https://media.example.test/data"})
        assert "authorization" not in req.headers
        zipped = gzip.compress(payload)
        return httpx.Response(200, content=zipped, headers={
            "Content-Encoding": "gzip", "Content-Length": str(len(zipped)), "Set-Cookie": "a=synthetic",
        })

    base = "https://music.example.test/"
    with httpx.Client(transport=httpx.MockTransport(handler), cookies={"unrelated": "synthetic"}) as client:
        resp = _self_hosted_request(client, "GET", base, base_url=base, headers={"Authorization": "Basic test"})
        assert resp.content == payload and resp.json() == {"result": "ok"}
        assert "content-encoding" not in resp.headers
        assert int(resp.headers["content-length"]) == len(payload)
        assert not client.cookies
        assert calls == ["music.example.test", "media.example.test"]
        addresses["media.example.test"] = "192.168.10.6"
        with pytest.raises(AppError) as redirected:
            _self_hosted_request(client, "GET", base, base_url=base, headers={"Authorization": "Basic test"})
        assert redirected.value.code == "PROTOCOL_ADDRESS_REJECTED"
        addresses["music.example.test"] = "fe80::2"
        with pytest.raises(AppError) as local:
            _self_hosted_request(client, "GET", base, base_url=base)
        assert local.value.code == "PROTOCOL_ADDRESS_REJECTED"
        assert calls == ["music.example.test", "media.example.test", "music.example.test"]


def test_self_hosted_audio_revalidates_dns_before_following_a_redirect(tmp_path, monkeypatch):
    app = _make_app(tmp_path)
    target = app.audio.register_stream_token(
        kind="remote", url="https://music.example.test:4533/song",
        allow_self_hosted_origin="https://music.example.test:4533",
        headers={"Authorization": "Basic synthetic"}, provider="opensubsonic",
        session_generation=app.session_generation,
    )
    addresses = {"music.example.test": "10.1.1.1", "media.example.test": "10.1.1.2"}
    def lookup(host, port, **_options):
        return [(2, 1, 6, "", (addresses[host], port))]
    def upstream(req):
        if req.headers["host"] == "music.example.test:4533":
            assert req.url.host == "10.1.1.1"
            assert req.extensions["sni_hostname"] == "music.example.test"
            assert req.headers["authorization"] == "Basic synthetic"
            return httpx.Response(302, headers={"Location": "https://media.example.test/song"})
        assert req.url.host == "8.8.4.4"
        assert req.headers["host"] == "media.example.test"
        assert req.extensions["sni_hostname"] == "media.example.test"
        assert "authorization" not in req.headers
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"})

    original_client = httpx.AsyncClient
    monkeypatch.setattr("pruner_studio.open_protocols.socket.getaddrinfo", lookup)
    monkeypatch.setattr("pruner_studio.external_sources._resolve_addresses", lookup)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **options: original_client(
        transport=httpx.MockTransport(upstream), **options,
    ))
    with TestClient(create_asgi_app(app, 8765), base_url="http://127.0.0.1:8765") as client:
        assert client.head(f"/api/stream/{target.token}").status_code == 403
        addresses["media.example.test"] = "8.8.4.4"
        assert client.head(f"/api/stream/{target.token}").status_code == 200


def test_cached_remote_stream_admission_and_mid_transfer_revocation(tmp_path):
    app = _make_app(tmp_path)
    manager = app.lan_sync
    manager.pair = {"code": "000000", "expires_at": time.time() + 30}
    device = manager.claim_pairing("000000", "Test Phone", "android")["device_id"]
    target = app.audio.register_stream_token(kind="remote", url="https://8.8.8.8/song", cache_key="cached")
    assert app.audio_cache.put_bytes("cached", b"fLaC" + b"x" * 1_000_000, "audio/flac")
    sent = []

    async def exercise():
        options = {"head_only": False, "register_stream": lambda cancel: manager.register_stream(device, cancel)}
        response = await _stream_remote_audio(target, app, None, **options)
        assert response.headers["x-audio-cache"] == "HIT"
        scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        async def receive():
            raise AssertionError("Unexpected read")
        async def send(message):
            if message["type"] == "http.response.body":
                sent.append(message["body"])
                manager.revoke(device)
                await asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await response(scope, receive, send)
        denied = await _stream_remote_audio(target, app, None, **options)
        messages = []
        async def record(message):
            messages.append(message)
        await denied(scope, receive, record)
        assert messages[0]["status"] == 401

    asyncio.run(exercise())
    assert len(sent) == 1 and 0 < len(sent[0]) < 1_000_000
    assert not manager.active_streams


def test_local_restore_paginates_and_composite_migration_finds_hybrid_tracks(tmp_path):
    app = _make_app(tmp_path)
    db = app.db
    paths = [tmp_path / f"{i:05}.flac" for i in range(5002)]
    db.replace_local_files([{"path": str(path), "mtime": 42} for path in paths])
    restored = {record.path: record for record in LocalLibraryScanner(db).records()}
    assert len(restored) == len(paths) and restored[paths[-1]].mtime == 42

    track = {"provider": "resolver_v2:hub", "external_id": "leaf", "title": "Hybrid Track", "artists": ["Band"]}
    key = "resolver_v2:hub:leaf"
    playlist = db.create_hybrid_playlist("local", "Migration")
    db.add_tracks_to_hybrid_playlist("local", playlist["id"], [track])
    db.set_canonical_favorite("local", key, track=track)
    db.record_play("local", track, completed=True)
    with db.write_batch() as conn:
        for table in ("canonical_tracks", "canonical_favorites", "hybrid_playlist_tracks", "play_history"):
            conn.execute(f"UPDATE {table} SET track_key=? WHERE track_key=?", ("resolver_v2.hub:leaf", key))
        conn.execute("DELETE FROM meta WHERE key='composite_keys_v1'")
    db = app.db = CatalogDatabase(tmp_path)
    assert db.is_canonical_favorite("local", key)
    assert db.get_hybrid_playlist("local", playlist["id"])["songs"][0]["track_key"] == key
    assert db.list_play_history("local")[0]["provider"] == "resolver_v2:hub"

    db.upsert_canonical_track("another", {"provider": "webdav", "external_id": "private", "title": "Private Song"})
    db.upsert_canonical_track("local", {
        "provider": "opensubsonic", "external_id": "metadata", "title": "Catalog Track", "artists": ["Band"],
    })
    # A membership may predate the canonical rows. Its exact identifier remains useful.
    with db.write_batch() as conn:
        conn.execute("DELETE FROM canonical_tracks WHERE account_id='local' AND track_key=?", (key,))
    content = json.dumps({"tracks": [
        {"title": "Unrelated title", "track_key": "resolver_v2.hub:leaf"},
        {"title": "Private Song", "track_key": "webdav:private"},
        {"title": "Catalog Track", "artists": ["Band"]},
    ]})
    preview = app.api("POST", "/api/playlists/migrate/preview", {"format": "json", "content": content})
    assert preview.status == 200, preview.payload
    items = preview.payload["data"]["items"]
    assert items[0]["reason"] == "track_key" and items[0]["matched_track"]["track_key"] == key
    assert items[1]["confidence"] == "unmatched"
    assert items[2]["matched_track"]["track_key"] == "opensubsonic:metadata"


def test_spki_pinning_survives_ip_change_cert_rotation(tmp_path):
    cert1, key1, fp1, _ = load_identity(tmp_path, ["127.0.0.1"])
    spki1 = certificate_spki_fingerprint(Path(cert1))
    pin1 = certificate_spki_pin(Path(cert1))
    assert len(spki1) == 64 and pin1.startswith("sha256/")

    # Rotate SAN IPs -> cert_fingerprint changes, but SPKI pin stays 100% stable!
    cert2, key2, fp2, _ = load_identity(tmp_path, ["127.0.0.2"])
    spki2 = certificate_spki_fingerprint(Path(cert2))
    pin2 = certificate_spki_pin(Path(cert2))
    assert fp2 != fp1
    assert spki2 == spki1
    assert pin2 == pin1

    # Verify SPKI matches DER extraction directly
    der = ssl.PEM_cert_to_DER_cert(Path(cert2).read_text("ascii"))
    assert spki_fingerprint_from_der(der) == spki1
    assert spki_pin_from_der(der) == pin1

    # Verify live LAN manager exposes SPKI in status, QR URI, pair/claim, and info?v=2
    app = _make_app(tmp_path)
    try:
        port = app.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
        pair = app.lan_sync.start_pairing()
        q = parse_qs(urlsplit(pair["qr_uri"]).query)
        assert q["spki"] == [spki1]
        assert app.lan_sync.status()["spki_fingerprint"] == spki1
        assert app.lan_sync.status()["spki_pin"] == pin1
        ctx = ssl.create_default_context(cafile=str(tmp_path / "lan_tls_cert.pem"))
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=ctx, trust_env=False) as client:
            info_v2 = client.get("/api/lan/v1/info?v=2").json()
            assert info_v2["spki_fingerprint"] == spki1
            assert info_v2["spki_pin"] == pin1
            claimed = client.post(
                "/api/lan/v1/pair/claim",
                json={"code": pair["code"], "device_name": "Pixel", "platform": "android"},
            ).json()
            assert claimed["spki_fingerprint"] == spki1
            assert claimed["spki_pin"] == pin1
    finally:
        app.lan_sync.stop()


def test_non_windows_keyring_storage_and_fallback(tmp_path):
    store = JsonStore(tmp_path)
    fake_ring: dict[str, str] = {}

    with (
        patch("pruner_studio.storage._dpapi_available", return_value=False),
        patch("pruner_studio.storage._keyring_available", return_value=True),
        patch("pruner_studio.storage._keyring_set", side_effect=lambda k, v: (fake_ring.__setitem__(k, v) or True)),
        patch("pruner_studio.storage._keyring_get", side_effect=lambda k: fake_ring.get(k)),
        patch("pruner_studio.storage._keyring_delete", side_effect=lambda k: fake_ring.pop(k, None)),
    ):
        sess = SessionStore(store)
        sess.save_account_session(AccountSummary(42, "LinuxUser"), "MUSIC_U=top_secret_cookie")
        disk_text = (tmp_path / "sessions.json").read_text("utf-8")
        assert "top_secret_cookie" not in disk_text
        assert '"encoding": "keyring"' in disk_text
        assert sess.load_account_cookie("42") == "MUSIC_U=top_secret_cookie"

        vault = ChannelCredentialVault(store)
        vault.update_channel("opensubsonic", {"api_base": "http://192.168.1.9:4533", "username": "alice", "password": "pw_secret"})
        vault_text = (tmp_path / "channel_credentials.json").read_text("utf-8")
        assert "pw_secret" not in vault_text
        assert '"encoding": "keyring"' in vault_text
        assert vault.get_channel_secret("opensubsonic")["password"] == "pw_secret"


def test_plaintext_session_migration_scrubs_both_files_and_locked_vaults_refuse_writes(tmp_path, monkeypatch):
    store = JsonStore(tmp_path)
    ring = {}
    def save(key, value):
        ring[key] = value
        return True

    for module in ("storage", "resolver_v2"):
        monkeypatch.setattr(f"pruner_studio.{module}._dpapi_available", lambda: False)
        monkeypatch.setattr(f"pruner_studio.{module}._keyring_get", ring.get)
        monkeypatch.setattr(f"pruner_studio.{module}._keyring_set", save)
    monkeypatch.setattr("pruner_studio.storage._keyring_available", lambda: True)
    cookie = "MUSIC_U=synthetic-cookie"
    store.write("session.json", {"cookie": cookie})
    store.write("sessions.json", {"version": 3, "active_account_id": "42", "accounts": {
        "42": {"display_name": "Test", "encoding": "plain", "data": cookie, "cookie": cookie},
    }})
    session = SessionStore(store)
    assert session.load_account_cookie("42") == cookie
    entry = store.read("sessions.json", {})["accounts"]["42"]
    assert entry["encoding"] == "keyring" and entry["display_name"] == "Test"
    assert set(entry) == {"encoding", "ref", "display_name"}
    assert cookie not in store.path("session.json").read_text("utf-8")
    assert session.legacy.load_cookie() == cookie

    channels = ChannelCredentialVault(store)
    channels.update_channel("webdav", {"api_base": "http://127.0.0.1:9001", "password": "synthetic"})
    registry = ResolverRegistry(store)
    resolver = {"id": "test", "url": "http://127.0.0.1:9002", "api_key": "synthetic"}
    registry.upsert(resolver)
    expected = {name: store.path(name).read_bytes() for name in (channels.FILE_NAME, registry.FILE_NAME)}
    for operation, code in (
        (lambda: channels.update_channel("webdav", {"clear_credentials": True}), "CHANNEL_VAULT_UNAVAILABLE"),
        (lambda: registry.remove(resolver["id"]), "RESOLVER_VAULT_UNAVAILABLE"),
    ):
        for module in ("storage", "resolver_v2"):
            monkeypatch.setattr(f"pruner_studio.{module}._keyring_set", lambda *_: False)
        with pytest.raises(AppError) as unwritable:
            operation()
        assert unwritable.value.code == code
        with monkeypatch.context() as locked:
            for module in ("storage", "resolver_v2"):
                locked.setattr(f"pruner_studio.{module}._keyring_get", lambda *_: None)
            with pytest.raises(AppError) as unreadable:
                operation()
            assert unreadable.value.code == code
    assert {name: store.path(name).read_bytes() for name in expected} == expected


def test_resolver_credentials_remain_bound_to_id_and_effective_origin(tmp_path):
    registry = ResolverRegistry(JsonStore(tmp_path))
    manual = {"id": "manual", "url": "https://music.example.org", "api_key": "synthetic"}
    registry.upsert(manual)
    def stored():
        return {entry["id"]: entry for entry in registry.list_enabled_secrets()}
    subscription = "https://sub.example.org/list.json"
    result = registry.import_subscription_payload(subscription, {"resolvers": [
        {"id": "manual", "url": "https://other.example.org"},
        {"id": "managed", "url": "https://music.example.org"},
    ]})
    assert result["skipped"] == [{"id": "manual", "reason": "independent_entry"}]
    assert stored()["manual"]["url"] == manual["url"]
    registry.upsert({"id": "manual", "url": "https://MUSIC.example.org:443/other"})
    assert stored()["manual"]["api_key"] == "synthetic"
    registry.upsert({"id": "manual", "url": "https://music.example.org:8443"})
    assert stored()["manual"]["api_key"] == ""
    registry.upsert({"id": "managed", "url": "https://music.example.org", "api_key": "fresh",
                     "subscription_url": subscription})
    registry.import_subscription_payload(subscription, {"resolvers": [
        {"id": "managed", "url": "https://music.example.org:443/path"},
    ]})
    assert stored()["managed"]["api_key"] == "fresh"
    registry.import_subscription_payload(subscription, {"resolvers": [
        {"id": "managed", "url": "https://elsewhere.example.org", "api_key": "ignored"},
    ]})
    assert stored()["managed"]["api_key"] == ""


def test_self_hosted_audio_cache_isolates_server_principal_and_unknown_format(tmp_path, monkeypatch):
    app = _make_app(tmp_path)
    resolver = app.audio.external_resolver
    monkeypatch.setattr(resolver.opensubsonic, "get_song", lambda *_args, **_kwargs: None)
    keys = {"opensubsonic": [], "webdav": []}
    for base, name, password in (
        ("http://127.0.0.1:4533", "alice", "pw-one"),
        ("http://127.0.0.1:4534", "alice", "pw-one"),
        ("http://127.0.0.1:4534", "bob", "pw-one"),
        ("http://127.0.0.1:4534", "bob", "pw-two"),
    ):
        settings = {"api_base": base, "username": name, "password": password, "enabled": True}
        assert app.api("PUT", "/api/channels/opensubsonic", settings).status == 200
        result = app.audio.resolve_external_track(provider="opensubsonic", external_id="same-track")
        assert (result["format"], result["level"]) == ("unknown", "standard")
        target = app.audio.resolve_stream_token(result["stream_token"])
        assert password not in target.cache_key
        assert app.audio_cache.get(target.cache_key) is None
        assert app.audio_cache.put_bytes(target.cache_key, b"fLaCtest", "audio/flac")
        keys["opensubsonic"].append(target.cache_key)

        assert app.api("PUT", "/api/channels/webdav", settings).status == 200
        resolver.webdav.seed_tracks([{
            "external_id": "same-track", "provider": "webdav",
            "title": "Sample", "format": "flac", "webdav_url": base + "/song.flac",
        }])
        result = app.audio.resolve_external_track(provider="webdav", external_id="same-track")
        target = app.audio.resolve_stream_token(result["stream_token"])
        assert password not in target.cache_key
        assert app.audio_cache.get(target.cache_key) is None
        assert app.audio_cache.put_bytes(target.cache_key, b"fLaCtest", "audio/flac")
        keys["webdav"].append(target.cache_key)
    assert all(len(set(provider_keys)) == 4 for provider_keys in keys.values())


def test_builtin_providers_preserved_including_gdstudio(tmp_path):
    app = _make_app(tmp_path)
    resolver = app.audio.external_resolver
    for attr in ("kugou_lite", "migu", "qq", "kuwo", "bilibili", "gdstudio", "custom", "opensubsonic", "webdav"):
        assert hasattr(resolver, attr), f"Missing provider attribute: {attr}"


# ---------------------------------------------------------------------------
# Milestone 1 Tests
# ---------------------------------------------------------------------------


def test_canonical_favorites_hybrid_playlists_routes_blocklist_and_shuffle(tmp_path):
    app = _make_app(tmp_path)
    # 1. Favorite non-NetEase tracks
    r_fav = app.api(
        "POST",
        "/api/favorites",
        {
            "track_key": "opensubsonic:nav-101",
            "liked": True,
            "track": {
                "provider": "opensubsonic",
                "external_id": "nav-101",
                "title": "Navidrome Track",
                "artists": ["Indie Band"],
                "album": "Self Hosted",
                "duration_ms": 210000,
                "isrc": "USRC17607839",
            },
        },
    )
    assert r_fav.status == 200 and r_fav.payload["data"]["liked"] is True
    favs = app.api("GET", "/api/favorites").payload["data"]["tracks"]
    assert len(favs) == 1 and favs[0]["track_key"] == "opensubsonic:nav-101"

    # 2. Hybrid playlist CRUD with mixed NetEase + local + OpenSubsonic tracks
    created = app.api(
        "POST",
        "/api/hybrid-playlists",
        {
            "name": "跨平台精选",
            "tracks": [
                {"id": 1001, "provider": "netease", "title": "网易曲", "artists": ["歌手A"], "duration_ms": 200000},
                {"provider": "opensubsonic", "external_id": "nav-101", "title": "Navidrome Track", "artists": ["Indie Band"], "duration_ms": 210000},
                {"provider": "webdav", "external_id": "dav-abc", "title": "WebDAV FLAC", "artists": ["歌手C"], "duration_ms": 180000},
            ],
        },
    )
    assert created.status == 201
    hpid = created.payload["data"]["id"]
    assert hpid.startswith("hybrid:")
    assert created.payload["data"]["track_count"] == 3

    # Hybrid playlist appears in catalog view cleanly
    cat_view = app.api("GET", f"/api/catalog?playlist_id={hpid}").payload["data"]
    assert len(cat_view["tracks"]) == 3
    assert any(p["id"] == hpid and p["kind"] == "hybrid" for p in cat_view["playlists"])

    # Remove single track by canonical track_key
    rem = app.api("DELETE", f"/api/hybrid-playlists/{hpid}/tracks", {"track_keys": ["webdav:dav-abc"]})
    assert rem.status == 200 and rem.payload["data"]["removed"] == 1

    # 3. Route cache & candidate blocklist
    saved_route = app.api(
        "PUT",
        "/api/tracks/route",
        {"track_key": "netease:1001", "provider": "kugou_lite", "external_id": "kg99", "format": "flac", "bitrate": 980000},
    )
    assert saved_route.status == 200
    assert app.api("GET", "/api/tracks/route?track_key=netease:1001").payload["data"]["route"]["provider"] == "kugou_lite"

    # Blocking the cached candidate automatically evicts its route!
    blk = app.api(
        "POST",
        "/api/tracks/blocklist",
        {"track_key": "netease:1001", "candidate_key": "kugou_lite:kg99", "reason": "wrong live version"},
    )
    assert blk.status == 201
    assert app.api("GET", "/api/tracks/route?track_key=netease:1001").payload["data"]["route"] is None
    assert len(app.api("GET", "/api/tracks/blocklist?track_key=netease:1001").payload["data"]["items"]) == 1

    # 4. BalancedShuffle respects canonical track_key play history
    tracks = [
        {"id": 0, "track_key": "opensubsonic:a", "title": "A", "artists": ["Artist1"]},
        {"id": 0, "track_key": "opensubsonic:b", "title": "B", "artists": ["Artist2"]},
        {"id": 0, "track_key": "webdav:c", "title": "C", "artists": ["Artist3"]},
    ]
    shuffled = balanced_shuffle(
        tracks,
        play_history={"opensubsonic:a": {"last_played_at": "2026-11-26T00:00:00+00:00"}},
        seed=7,
    )
    assert {t["track_key"] for t in shuffled} == {"opensubsonic:a", "opensubsonic:b", "webdav:c"}


def test_local_library_multiformat_covers_replaygain_incremental_and_fault_tolerance(tmp_path):
    app = _make_app(tmp_path)
    music_dir = tmp_path / "music"
    music_dir.mkdir()

    png_bytes = _sample_png_bytes()
    jpeg_bytes = _sample_jpeg_bytes()

    flac_path = music_dir / "track1.flac"
    mp3_path = music_dir / "track2.mp3"
    m4a_path = music_dir / "track3.m4a"
    opus_path = music_dir / "track4.opus"
    aac_path = music_dir / "track5.aac"
    corrupt_path = music_dir / "corrupt.flac"

    _build_flac_file(flac_path, title="晴天 FLAC", artist="周杰伦", album="叶惠美", isrc="TWK970300403", rg_gain="-6.5 dB", rg_peak="0.98", cover=png_bytes)
    _build_mp3_gbk_xing_file(mp3_path, title_gbk="中文老歌", artist_gbk="测试歌手", cover_jpeg=jpeg_bytes)
    _build_m4a_file(m4a_path, title="Apple Lossless", artist="M4A Singer", cover_png=png_bytes)
    _build_opus_file(opus_path, title="Opus Track", artist="Opus Artist")
    _build_aac_adts_file(aac_path)
    corrupt_path.write_bytes(b"NOT_A_FLAC_FILE")

    res1 = app.api("POST", "/api/local-library/scan", {"dirs": [str(music_dir)]})
    assert res1.status == 200
    data1 = res1.payload["data"]
    assert data1["status"] == "ok"
    assert data1["scanned_count"] == 6  # corrupt file still safely degrades via filename fallback without aborting dir!

    by_name = {Path(f["path"]).name: f for f in app.db.list_local_files(limit=50)}
    assert by_name["track1.flac"]["title"] == "晴天 FLAC"
    assert by_name["track1.flac"]["isrc"] == "TWK970300403"
    assert by_name["track1.flac"]["replaygain_track_gain_db"] == pytest.approx(-6.5, abs=0.01)
    assert by_name["track1.flac"]["cover_id"]
    assert by_name["track2.mp3"]["title"] == "中文老歌"
    assert by_name["track2.mp3"]["artist"] == "测试歌手"
    assert by_name["track2.mp3"]["duration_ms"] > 20_000
    assert by_name["track2.mp3"]["cover_id"]
    assert by_name["track3.m4a"]["title"] == "Apple Lossless"
    assert by_name["track3.m4a"]["duration_ms"] == 125_000
    assert by_name["track4.opus"]["title"] == "Opus Track"
    assert by_name["track4.opus"]["duration_ms"] == 15_000
    assert by_name["track5.aac"]["sample_rate"] == 44100

    # Incremental second scan skips unchanged files
    res2 = app.api("POST", "/api/local-library/scan", {"dirs": [str(music_dir)]}).payload["data"]
    assert res2["reused_unchanged"] == 6

    # Local cover HTTP endpoint returns exact embedded cover bytes and blocks traversal
    asgi = create_asgi_app(app, 8765)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
        cid = by_name["track1.flac"]["cover_id"]
        cov = client.get(f"/api/local-cover/{cid}")
        assert cov.status_code == 200
        assert cov.content == png_bytes
        assert cov.headers["content-type"].startswith("image/png")
        assert client.get("/api/local-cover/..%2Fsecret").status_code == 404

    # Local stream endpoint exposes ReplayGain loudness compensation metadata
    stream_resp = app.api("POST", "/api/local-library/stream", {"path": str(flac_path)})
    assert stream_resp.status == 200
    loud = stream_resp.payload["data"]["loudness"]
    assert loud["source"] == "replaygain_tag"
    assert loud["replaygain_track_gain_db"] == pytest.approx(-6.5, abs=0.01)
    assert 0.15 <= loud["computed_gain_linear"] <= 2.5


def test_opensubsonic_provider_end_to_end(tmp_path):
    app = _make_app(tmp_path)
    # Reject cloud metadata SSRF
    bad = app.api("PUT", "/api/channels/opensubsonic", {"api_base": "http://169.254.169.254:4533"})
    assert bad.status == 400
    # Allow self-hosted LAN Navidrome server
    ok = app.api(
        "PUT",
        "/api/channels/opensubsonic",
        {"enabled": True, "api_base": "http://192.168.1.50:4533", "username": "alice", "password": "secret"},
    )
    assert ok.status == 200

    png = _sample_png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.url.params.get("u") == "alice"
        assert request.url.params.get("t") and request.url.params.get("s")
        if path.endswith("/ping.view"):
            return httpx.Response(200, json={"subsonic-response": {"status": "ok", "type": "navidrome", "serverVersion": "0.53.0", "openSubsonic": True}})
        if path.endswith("/search3.view"):
            return httpx.Response(
                200,
                json={
                    "subsonic-response": {
                        "status": "ok",
                        "searchResult3": {
                            "song": [
                                {
                                    "id": "os-77",
                                    "title": "Navidrome Song",
                                    "artist": "Self Hosted",
                                    "album": "Home Lab",
                                    "duration": 240,
                                    "bitRate": 960,
                                    "samplingRate": 44100,
                                    "suffix": "flac",
                                    "coverArt": "al-77",
                                    "replayGain": {"trackGain": -4.2, "trackPeak": 0.96},
                                }
                            ]
                        },
                    }
                },
            )
        if path.endswith("/getSong.view"):
            return httpx.Response(
                200,
                json={
                    "subsonic-response": {
                        "status": "ok",
                        "song": {
                            "id": "os-77",
                            "title": "Navidrome Song",
                            "artist": "Self Hosted",
                            "duration": 240,
                            "bitRate": 960,
                            "suffix": "flac",
                            "coverArt": "al-77",
                            "replayGain": {"trackGain": -4.2, "trackPeak": 0.96},
                        },
                    }
                },
            )
        if path.endswith("/getPlaylists.view"):
            return httpx.Response(200, json={"subsonic-response": {"status": "ok", "playlists": {"playlist": [{"id": "pl-1", "name": "FLAC Mix", "songCount": 1}]}}})
        if path.endswith("/getCoverArt.view"):
            return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})
        if path.endswith("/getLyricsBySongId.view"):
            return httpx.Response(
                200,
                json={
                    "subsonic-response": {
                        "status": "ok",
                        "lyricsList": {"structuredLyrics": [{"line": [{"start": 1000, "value": "Hello OpenSubsonic"}]}]},
                    }
                },
            )
        return httpx.Response(404)

    mock_client = httpx.Client(transport=httpx.MockTransport(handler))
    app.audio.external_resolver._http_client = mock_client
    try:
        ping = app.api("POST", "/api/channels/opensubsonic/test", {}).payload["data"]
        assert ping["ok"] is True and ping["server_type"] == "navidrome"

        sr = app.api("GET", "/api/opensubsonic/search?q=Navidrome").payload["data"]
        assert len(sr["tracks"]) == 1 and sr["tracks"][0]["track_key"] == "opensubsonic:os-77"

        pls = app.api("GET", "/api/opensubsonic/playlists").payload["data"]["playlists"]
        assert pls[0]["id"] == "opensubsonic:pl-1"

        audio = app.api("GET", "/api/audio?provider=opensubsonic&external_id=os-77").payload["data"]
        assert audio["source"] == "opensubsonic"
        assert audio["stream_url"].startswith("/api/stream/")
        assert audio["loudness"]["source"] == "replaygain_tag"

        ly = app.api("GET", "/api/lyrics?provider=opensubsonic&external_id=os-77").payload["data"]
        assert "Hello OpenSubsonic" in ly["lyrics"]

        asgi = create_asgi_app(app, 8765)
        with TestClient(asgi, base_url="http://127.0.0.1:8765") as client:
            cov = client.get("/api/opensubsonic/cover/al-77")
            assert cov.status_code == 200 and cov.content == png
    finally:
        mock_client.close()


def test_webdav_library_and_encrypted_e2ee_sync(tmp_path):
    # Test crypto envelope tampering detection directly
    env = encrypt_sync_envelope({"secret_song": "晴天"}, "my-strong-passphrase", iterations=10_000)
    assert "晴天" not in json.dumps(env)
    assert decrypt_sync_envelope(env, "my-strong-passphrase") == {"secret_song": "晴天"}
    with pytest.raises(Exception):
        decrypt_sync_envelope(env, "wrong-passphrase")
    tampered = {**env, "ciphertext": base64.b64encode(b"corrupted").decode("ascii")}
    with pytest.raises(Exception):
        decrypt_sync_envelope(tampered, "my-strong-passphrase")

    # End-to-end WebDAV PROPFIND + Encrypted Push/Pull between 2 isolated desktop instances!
    remote_files: dict[str, bytes] = {}
    remote_etags: dict[str, str] = {}

    propfind_xml = """<?xml version="1.0" encoding="utf-8"?>
    <d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/dav/Music/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
      <d:response><d:href>/dav/Music/Artist%20A%20-%20Song%20One.flac</d:href><d:propstat><d:prop><d:resourcetype/><d:getcontentlength>12345</d:getcontentlength></d:prop></d:propstat></d:response>
    </d:multistatus>""".encode("utf-8")

    def dav_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization", "").startswith("Basic ")
        p = request.url.path
        if request.method == "PROPFIND":
            return httpx.Response(207, content=propfind_xml, headers={"Content-Type": "application/xml"})
        if request.method == "MKCOL":
            return httpx.Response(201)
        if request.method == "GET":
            if p not in remote_files:
                return httpx.Response(404)
            return httpx.Response(200, content=remote_files[p], headers={"ETag": remote_etags.get(p, '"1"')})
        if request.method == "PUT":
            expected_etag = request.headers.get("if-match")
            if expected_etag and remote_etags.get(p) != expected_etag:
                return httpx.Response(412)
            remote_files[p] = request.content
            remote_etags[p] = f'"{len(remote_files[p])}-{time.monotonic_ns()}"'
            return httpx.Response(201)
        return httpx.Response(405)

    app_a = _make_app(tmp_path / "device_a")
    app_b = _make_app(tmp_path / "device_b")
    app_a.webdav_sync = WebDAVEncryptedSync(iterations=10_000)
    app_b.webdav_sync = WebDAVEncryptedSync(iterations=10_000)

    cfg = {"enabled": True, "api_base": "http://192.168.1.20:5244/dav", "username": "u", "password": "p", "music_path": "/Music", "sync_path": "/pruner-sync"}
    app_a.api("PUT", "/api/channels/webdav", cfg)
    app_b.api("PUT", "/api/channels/webdav", cfg)

    dav_client = httpx.Client(transport=httpx.MockTransport(dav_handler))
    app_a.audio.external_resolver._http_client = dav_client
    app_b.audio.external_resolver._http_client = dav_client
    try:
        scanned = app_a.api("POST", "/api/webdav/scan", {}).payload["data"]
        assert scanned["scanned_count"] == 1
        assert scanned["tracks"][0]["title"] == "Song One"

        # Populate favorites & hybrid playlist on Device A and push encrypted
        app_a.api("POST", "/api/favorites", {"track_key": "webdav:xyz", "liked": True, "track": {"provider": "webdav", "external_id": "xyz", "title": "Secret E2EE Song", "artists": ["Vault"]}})
        app_a.api("POST", "/api/hybrid-playlists", {"name": "加密同步歌单", "tracks": [{"provider": "webdav", "external_id": "xyz", "title": "Secret E2EE Song", "artists": ["Vault"]}]})
        push_res = app_a.api("POST", "/api/webdav/sync/push", {"passphrase": "vault-password-123"})
        assert push_res.status == 200 and push_res.payload["data"]["seq"] == 1

        # Verify server NEVER saw plaintext title or playlist name!
        all_remote_blob = b"".join(remote_files.values()).decode("utf-8", errors="ignore")
        assert "Secret E2EE Song" not in all_remote_blob
        assert "加密同步歌单" not in all_remote_blob

        # Pull & decrypt on Device B
        pull_res = app_b.api("POST", "/api/webdav/sync/pull", {"passphrase": "vault-password-123"})
        assert pull_res.status == 200 and pull_res.payload["data"]["applied_events"] >= 1
        b_favs = app_b.api("GET", "/api/favorites").payload["data"]["tracks"]
        assert any(t["track_key"] == "webdav:xyz" for t in b_favs)
        b_pls = app_b.api("GET", "/api/hybrid-playlists").payload["data"]["playlists"]
        assert any(p["name"] == "加密同步歌单" for p in b_pls)
    finally:
        dav_client.close()


@pytest.fixture
def sync_repository():
    objects = {}
    revisions = {}
    put_hooks = []
    writes = []
    passphrase = "synthetic-sync-pass"
    server = {"api_base": "http://127.0.0.1:8877/dav"}
    sync = WebDAVEncryptedSync(iterations=10_000)

    def dav(req):
        path = req.url.path
        if req.method == "MKCOL":
            return httpx.Response(201)
        if req.method == "GET":
            if path not in objects:
                return httpx.Response(404)
            return httpx.Response(200, content=objects[path], headers={"ETag": revisions[path]})
        assert req.method == "PUT"
        writes.append((path, req.headers.get("if-match"), req.headers.get("if-none-match")))
        if put_hooks and path.endswith("/manifest.enc.json"):
            put_hooks.pop()()
        if path in objects:
            if req.headers.get("if-match") != revisions[path] or req.headers.get("if-none-match"):
                return httpx.Response(412)
        else:
            assert req.headers.get("if-none-match") == "*"
        objects[path] = req.content
        revisions[path] = f'"{len(writes)}"'
        return httpx.Response(201)

    with httpx.Client(transport=httpx.MockTransport(dav)) as client:
        def push(db, aid="local"):
            return sync.push_incremental(server, passphrase, account_id=aid, db=db,
                                         rules_service=None, device_id=db.data_dir.name, http_client=client)
        def pull(db, aid="local", since=0):
            return sync.pull_incremental(server, passphrase, account_id=aid, db=db,
                                         rules_service=None, since_seq=since, http_client=client)
        def decode(path):
            return decrypt_sync_envelope(json.loads(objects[path]), passphrase)
        def encode(path, payload):
            envelope = encrypt_sync_envelope(payload, passphrase, iterations=10_000)
            objects[path] = json.dumps(envelope, separators=(",", ":")).encode()
        yield push, pull, objects, writes, put_hooks, decode, encode


def test_webdav_snapshot_compaction_merges_counts_and_retains_deletions(tmp_path, sync_repository):
    push, pull, objects, _, _, decode, _ = sync_repository
    a, b, restored = [CatalogDatabase(tmp_path / name) for name in ("a", "b", "restored")]
    song = {"provider": "opensubsonic", "external_id": "one", "title": "Song"}
    track_id = "opensubsonic:one"
    other = {"provider": "webdav", "external_id": "two", "title": "Other"}
    for ms in (30_000, 40_000, 50_000):
        a.record_play("local", song, listened_ms=ms, completed=ms == 50_000)
    a.set_canonical_favorite("local", track_id, track=song)
    surviving = a.create_hybrid_playlist("local", "Retained")["id"]
    a.add_tracks_to_hybrid_playlist("local", surviving, [song, other])
    removed = a.create_hybrid_playlist("local", "Removed")["id"]
    a.add_tracks_to_hybrid_playlist("local", removed, [song])
    assert push(a)["seq"] == 1
    assert pull(b)["seq"] == 1
    b.record_play("local", song, listened_ms=60_000, completed=True)
    last_played = CatalogDatabase(b.data_dir).list_play_history("local")[0]["last_played_at"]
    b.set_canonical_favorite("local", track_id, liked=False)
    b.remove_tracks_from_hybrid_playlist("local", surviving, [track_id])
    assert b.delete_hybrid_playlist("local", removed)
    assert push(b)["seq"] == 2

    # Device A remains stale while compacting, so its copy still contains the removed entries.
    for expected in range(3, 12):
        pushed = push(a)
        assert pushed["seq"] == expected
    assert pushed["snapshot_seq"] == 11
    manifest_path = next(path for path in objects if path.endswith("/manifest.enc.json"))
    manifest = decode(manifest_path)
    assert manifest["events"] == []
    snapshot = decode(manifest_path.rsplit("/", 1)[0] + "/" + manifest["snapshot"])
    assert len(snapshot["deletions"]) == 3
    for db in (a, b, restored):
        for _attempt in range(2):
            assert pull(db)["seq"] == 11
            row = db.list_play_history("local")[0]
            assert (row["play_count"], row["completed_count"], row["total_listen_ms"], row["last_played_at"]) == (
                4, 2, 180_000, last_played,
            )
            assert not db.is_canonical_favorite("local", track_id)
            assert db.get_hybrid_playlist("local", removed) is None
            assert [t["track_key"] for t in db.get_hybrid_playlist("local", surviving)["songs"]] == ["webdav:two"]


def test_webdav_account_objects_and_gaps_fail_before_applying(tmp_path, sync_repository):
    push, pull, objects, _, _, decode, encode = sync_repository
    a, b = [CatalogDatabase(tmp_path / name) for name in ("source", "receiver")]
    a.record_play("alpha", {"id": 1, "title": "First"}, completed=True)
    assert push(a, "alpha")["seq"] == 1
    a.record_play("alpha", {"id": 2, "title": "Second"}, completed=True)
    assert push(a, "alpha")["seq"] == 2
    assert pull(b, "beta")["seq"] == 0
    assert push(b, "beta")["seq"] == 1
    manifests = {decode(path)["account_id"]: path for path in objects if path.endswith("/manifest.enc.json")}
    alpha, beta = manifests["alpha"], manifests["beta"]
    assert alpha != beta
    manifest = decode(alpha)
    root = alpha.rsplit("/", 1)[0]
    snapshot = root + "/" + manifest["snapshot"]
    event = root + "/" + manifest["events"][0]["file"]
    saved_event = objects.pop(event)
    with pytest.raises(AppError) as missing:
        pull(b, "alpha")
    assert (missing.value.code, missing.value.status) == ("SYNC_OBJECT_MISSING", 503)
    assert b.list_play_history("alpha") == []
    objects[event] = saved_event

    for file, bad in ((alpha, {**manifest, "events": []}),
                      (event, {**decode(event), "account_id": "beta"}),
                      (snapshot, {**decode(snapshot), "account_id": "beta"})):
        saved = objects[file]
        encode(file, bad)
        with pytest.raises(AppError) as invalid:
            pull(b, "alpha")
        assert invalid.value.code in {"SYNC_SEQUENCE_GAP", "SYNC_ACCOUNT_MISMATCH"}
        assert b.list_play_history("alpha") == []
        objects[file] = saved

    objects[beta] = objects[alpha]
    for operation in (push, pull):
        with pytest.raises(AppError) as wrong_account:
            operation(b, "beta")
        assert wrong_account.value.code == "SYNC_ACCOUNT_MISMATCH"
    assert pull(b, "alpha")["seq"] == 2
    assert len(b.list_play_history("alpha")) == 2


def test_webdav_competing_writers_cannot_replace_winning_snapshot(tmp_path, sync_repository):
    push, pull, objects, writes, hooks, decode, _ = sync_repository
    a, b, restored = [CatalogDatabase(tmp_path / name) for name in ("a", "b", "restored")]
    for _seq in range(10):
        push(a)
    a.record_play("local", {"id": 1, "title": "Losing play"}, completed=True)
    b.record_play("local", {"id": 2, "title": "Winning play"}, completed=True)
    hooks.append(lambda: push(b))
    with pytest.raises(AppError) as conflict:
        push(a)
    assert conflict.value.code == "WEBDAV_SYNC_CONFLICT"
    snapshots = [path for path in objects if "/snapshot-11-" in path]
    assert len(snapshots) == 2
    assert all(creation == "*" for path, _, creation in writes if path in snapshots)
    manifest_path = next(path for path in objects if path.endswith("/manifest.enc.json"))
    manifests = [write for write in writes if write[0] == manifest_path]
    assert manifests[0][1:] == (None, "*")
    assert manifests[-1][1] == manifests[-2][1]
    assert decode(manifest_path)["snapshot_seq"] == 11
    assert pull(restored)["seq"] == 11
    assert [row["song_id"] for row in restored.list_play_history("local")] == [2]


def test_webdav_push_and_pull_share_encoded_size_limit(tmp_path, sync_repository, monkeypatch):
    push, pull, objects, _, _, _, _ = sync_repository
    monkeypatch.setattr("pruner_studio.open_protocols._SYNC_MAX_BYTES", 2400)
    source = CatalogDatabase(tmp_path / "source")
    restored = CatalogDatabase(tmp_path / "restored")
    source.record_play("local", {"id": 1, "title": "Accepted"}, completed=True)
    assert push(source)["seq"] == pull(restored)["seq"] == 1
    assert all(len(raw) <= 2400 for raw in objects.values())
    for index in range(2, 6):
        source.record_play("local", {"id": index, "title": "wide" * 80}, completed=True)
    before = dict(objects)
    with pytest.raises(AppError) as oversized:
        push(source)
    assert (oversized.value.code, oversized.value.status) == ("SYNC_PAYLOAD_TOO_LARGE", 413)
    assert objects == before


def test_http_resolver_v2_subscriptions_and_circuit_breaker(tmp_path):
    # 1. Circuit breaker transitions closed -> open -> half_open -> closed
    tracker = ChannelHealthTracker(failure_threshold=2, base_cooldown_sec=30.0)
    assert tracker.allow_request("qq", now_mono=100.0) is True
    tracker.record_failure("qq", "timeout", now_mono=100.0)
    assert tracker.allow_request("qq", now_mono=101.0) is True
    tracker.record_failure("qq", "500", now_mono=101.0)
    assert tracker.allow_request("qq", now_mono=110.0) is False
    assert tracker.snapshot("qq", now_mono=110.0)["circuit_state"] == "open"
    assert tracker.snapshot("qq", now_mono=135.0)["circuit_state"] == "half_open"
    assert tracker.allow_request("qq", now_mono=135.0) is True  # cooldown elapsed -> half_open
    tracker.record_success("qq", latency_ms=42.0)
    assert tracker.snapshot("qq")["circuit_state"] == "closed"

    # 2. HTTP Resolver v2 endpoints (/manifest, /search, /resolve, /lyric) + subscription import
    def resolver_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-resolver-token") == "tok-v2"
        if request.url.path.endswith("/manifest"):
            return httpx.Response(200, json={"protocol_version": 2, "id": "res-local", "name": "Local V2", "capabilities": ["search", "resolve", "lyric"]})
        if request.url.path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "has_more": False,
                    "tracks": [
                        {"id": "v2-1", "title": "晴天", "artists": ["周杰伦"], "duration_ms": 269000, "url": "http://127.0.0.1:9911/audio/1.flac", "format": "flac", "bitrate": 980000}
                    ],
                },
            )
        if request.url.path.endswith("/resolve"):
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"id": "v2-1", "title": "晴天", "artists": ["周杰伦"], "duration_ms": 269000, "url": "http://127.0.0.1:9911/audio/1.flac", "format": "flac", "bitrate": 980000}
                    ]
                },
            )
        if request.url.path.endswith("/lyric"):
            return httpx.Response(200, json={"lrc": "[00:01.00]故事的小黄花"})
        return httpx.Response(404)

    app = _make_app(tmp_path)
    mock_client = httpx.Client(transport=httpx.MockTransport(resolver_handler))
    app.audio.external_resolver._http_client = mock_client
    try:
        probe = app.api("POST", "/api/resolvers/probe", {"url": "http://127.0.0.1:9911", "api_key": "tok-v2"})
        assert probe.status == 200 and probe.payload["data"]["manifest"]["protocol_version"] == 2

        sub = app.api(
            "POST",
            "/api/resolvers/subscribe",
            {
                "url": "http://127.0.0.1:9911/sub.json",
                "manifest_json": {"resolvers": [{"id": "res-local", "name": "Local V2", "url": "http://127.0.0.1:9911"}]},
            },
        )
        assert sub.status == 200 and len(sub.payload["data"]["imported"]) == 1

        prov = CustomEndpointProvider()
        tracks, more = prov.search_page("http://127.0.0.1:9911", "晴天", mock_client, api_key="tok-v2")
        assert len(tracks) == 1 and tracks[0].format == "flac" and more is False
        resolved = prov.search_and_resolve("http://127.0.0.1:9911", TrackQuery("晴天", ("周杰伦",), duration_ms=269000), mock_client, api_key="tok-v2")
        assert len(resolved) == 1 and resolved[0].strict_match is True
    finally:
        mock_client.close()


def test_frozen_sidecar_dispatch_arguments_and_pipe_authentication(monkeypatch):
    real_popen = subprocess.Popen
    launched = []

    def invoke(cmd, **kwargs):
        launched.append(cmd)
        assert cmd[:2] == [sys.executable, "--sidecar"]
        assert kwargs["stdin"] == kwargs["stdout"] == subprocess.PIPE
        assert "PRUNER_STUDIO_TEST_SENTINEL" not in kwargs["env"]
        # Use the module entry point to exercise the frozen command contract in the source suite.
        return real_popen([cmd[0], "-m", "pruner_studio", *cmd[1:]], **kwargs)

    monkeypatch.setenv("PRUNER_STUDIO_TEST_SENTINEL", "synthetic")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(subprocess, "Popen", invoke)
    bridge = SidecarProcessManager()
    try:
        status = bridge.start()
        assert launched and status["running"]
        assert status["manifest"]["protocol_version"] == 2
        with httpx.Client(trust_env=False, timeout=2) as client:
            assert client.get(f"http://127.0.0.1:{status['port']}/manifest").status_code == 401
    finally:
        assert not bridge.stop()["running"]


def test_isolated_sidecar_bridge_live_process(tmp_path, monkeypatch):
    # Verify env sanitization strips host secrets
    monkeypatch.setenv("PRUNER_STUDIO_SECRET_TEST", "must_never_leak")
    sanitized = build_sanitized_sidecar_env()
    assert "PRUNER_STUDIO_SECRET_TEST" not in sanitized

    adapter_file = tmp_path / "demo_adapter.py"
    adapter_file.write_text(
        """
import os
class Adapter:
    def manifest(self):
        return {
            "protocol_version": 2,
            "id": "demo-sidecar",
            "name": "Demo Isolated Sidecar",
            "capabilities": ["search", "resolve", "lyric"],
            "leaked_env": os.environ.get("PRUNER_STUDIO_SECRET_TEST", ""),
        }
    def search(self, query, *, limit=30, offset=0):
        return {"tracks": [{"id": "sc-1", "title": query, "artists": ["Sidecar"], "duration_ms": 180000}], "has_more": False}
    def resolve(self, payload):
        return {"candidates": [{"id": "sc-1", "title": payload.get("title", ""), "url": "https://cdn.example.com/a.flac", "format": "flac"}]}
    def lyric(self, payload):
        return {"lrc": "[00:00.00]Sidecar Lyric"}
""",
        encoding="utf-8",
    )

    mgr = SidecarProcessManager()
    try:
        st = mgr.start(adapter_path=str(adapter_file))
        assert st["running"] is True and st["port"] > 0
        assert st["manifest"]["id"] == "demo-sidecar"
        assert st["manifest"]["leaked_env"] == ""

        cfg = mgr.active_resolver_config()
        assert cfg is not None and cfg["api_key"]
        with httpx.Client(trust_env=False, timeout=5.0) as client:
            # Unauthorized without token
            assert client.get(f"{cfg['url']}/manifest").status_code == 401
            # Authorized with token
            s_resp = client.post(f"{cfg['url']}/search", json={"query": "晴天"}, headers={"X-Resolver-Token": cfg["api_key"]})
            assert s_resp.status_code == 200 and s_resp.json()["tracks"][0]["id"] == "sc-1"
            l_resp = client.post(f"{cfg['url']}/lyric", json={"id": "sc-1"}, headers={"X-Resolver-Token": cfg["api_key"]})
            assert l_resp.status_code == 200 and "Sidecar Lyric" in l_resp.json()["lrc"]
    finally:
        stopped = mgr.stop()
        assert stopped["running"] is False


def test_playlist_export_and_migration_assistant(tmp_path):
    app = _make_app(tmp_path)
    music_dir = tmp_path / "lib"
    music_dir.mkdir()
    flac = music_dir / "hero.flac"
    _build_flac_file(flac, title="晴天", artist="周杰伦", album="叶惠美", isrc="TWK970300403", rg_gain="-5.0 dB", rg_peak="0.9", cover=_sample_png_bytes())
    app.api("POST", "/api/local-library/scan", {"dirs": [str(music_dir)]})

    created = app.api(
        "POST",
        "/api/hybrid-playlists",
        {"name": "导出源歌单", "tracks": [{"provider": "local", "local_path": str(flac), "title": "晴天", "artists": ["周杰伦"], "isrc": "TWK970300403", "duration_ms": 10000}]},
    ).payload["data"]
    hpid = created["id"]

    # Export JSON, M3U, TXT
    exp_json = app.api("GET", f"/api/playlists/{hpid}/export?format=json").payload["data"]
    assert exp_json["format"] == "json" and "TWK970300403" in exp_json["content"]
    exp_m3u = app.api("GET", f"/api/playlists/{hpid}/export?format=m3u").payload["data"]
    assert exp_m3u["format"] == "m3u8" and "#EXTM3U" in exp_m3u["content"]
    exp_txt = app.api("GET", f"/api/playlists/{hpid}/export?format=txt").payload["data"]
    assert "晴天 - 周杰伦" in exp_txt["content"]

    # Migration preview (text with matched + unmatched items)
    preview = app.api(
        "POST",
        "/api/playlists/migrate/preview",
        {"content": "1. 晴天 - 周杰伦\n2. 找不到的神秘歌曲 - 未知歌手\n", "format": "txt", "allow_online": False},
    ).payload["data"]
    assert preview["summary"]["total"] == 2
    assert preview["summary"]["high"] + preview["summary"]["exact"] >= 1
    assert preview["summary"]["unmatched"] == 1

    # Commit matched item into new hybrid playlist
    matched_tracks = [item["matched_track"] for item in preview["items"] if item["matched_track"]]
    committed = app.api("POST", "/api/playlists/migrate/commit", {"playlist_name": "迁移结果", "tracks": matched_tracks})
    assert committed.status == 201
    assert committed.payload["data"]["playlist"]["track_count"] == 1


def test_lan_multisource_proxy_endpoints(tmp_path):
    app = _make_app(tmp_path)
    music_dir = tmp_path / "lan_music"
    music_dir.mkdir()
    flac = music_dir / "lan_song.flac"
    png = _sample_png_bytes()
    _build_flac_file(flac, title="LAN Track", artist="LAN Artist", album="LAN Album", isrc="USRC17607839", rg_gain="-4.0 dB", rg_peak="0.9", cover=png)
    app.api("POST", "/api/local-library/scan", {"dirs": [str(music_dir)]})
    rec = app.db.list_local_files(limit=10)[0]
    fid = lan_sync.file_id(rec["path"])

    try:
        port = app.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
        ctx = ssl.create_default_context(cafile=str(tmp_path / "lan_tls_cert.pem"))
        pair = app.lan_sync.start_pairing()
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=ctx, trust_env=False) as client:
            dev = client.post("/api/lan/v1/pair/claim", json={"code": pair["code"], "device_name": "Phone", "platform": "android"}).json()
            hdr = {"Authorization": f"Bearer {dev['token']}"}

            # Unauthorized denied
            assert client.get("/api/lan/v1/search/multi?q=test").status_code == 401
            assert client.get(f"/api/lan/v1/cover?local_cover_id={rec['cover_id']}").status_code == 401

            # Authenticated cover proxy returns embedded local cover
            cov = client.get(f"/api/lan/v1/cover?local_cover_id={rec['cover_id']}", headers=hdr)
            assert cov.status_code == 200 and cov.content == png

            # Authenticated audio/resolve for local track
            res = client.post(
                "/api/lan/v1/audio/resolve",
                headers=hdr,
                json={"track": {"provider": "local", "file_id": fid, "track_key": f"local:{fid}", "title": "LAN Track"}},
            )
            assert res.status_code == 200
            assert res.json()["stream_url"] == f"/api/lan/v1/local-library/stream/{fid}"

            # Authenticated multi-search proxy works without leaking local paths
            with patch.object(
                app.audio.external_resolver,
                "search_multi_channel",
                return_value={
                    "query": "LAN",
                    "offset": 0,
                    "limit": 10,
                    "has_more": False,
                    "channels_queried": ["opensubsonic"],
                    "tracks": [{"id": 0, "provider": "opensubsonic", "external_id": "os-9", "track_key": "opensubsonic:os-9", "title": "LAN Hit", "artists": ["A"]}],
                    "channel_errors": {},
                },
            ):
                s = client.get("/api/lan/v1/search/multi?q=LAN&limit=10", headers=hdr)
                assert s.status_code == 200
                assert s.json()["tracks"][0]["track_key"] == "opensubsonic:os-9"

            # Revoke blocks subsequent proxy access
            app.lan_sync.revoke(dev["device_id"])
            assert client.get(f"/api/lan/v1/cover?local_cover_id={rec['cover_id']}", headers=hdr).status_code == 401
    finally:
        app.lan_sync.stop()


def test_audio_blocklist_skips_blocked_candidate_and_webdav_rejects_xxe(tmp_path):
    # 1. WebDAV XML parser must reject DTD / XXE payloads (both at start and padded >4KB)
    for prefix in (b"", b" " * 5000):
        xxe_xml = (
            b'<?xml version="1.0"?>'
            + prefix
            + b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/music/a.flac</d:href></d:response></d:multistatus>'
        )
        dav_client = httpx.Client(
            transport=httpx.MockTransport(lambda req, body=xxe_xml: httpx.Response(207, content=body, headers={"content-type": "application/xml"}))
        )
        try:
            dav = WebDAVProvider()
            with pytest.raises(AppError) as exc_info:
                dav.scan_library({"api_base": "http://127.0.0.1:8080/dav", "music_path": "/music"}, dav_client)
            assert exc_info.value.code == "WEBDAV_XML_INVALID"
        finally:
            dav_client.close()

    # 2. AudioService.resolve() skips blocked candidate and falls through to next unblocked candidate
    app = _make_app(tmp_path)
    song = Song(id=88801, title="晴天", artists=["周杰伦"], album="叶惠美", duration=269000)
    cand_bad = ExternalCandidate(
        source_provider="qq",
        provider_track_id="bad-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        duration_ms=269000,
        url="https://example.invalid/bad.flac",
        format="flac",
        bitrate=900_000,
        level="lossless",
        strict_match=True,
        match_score=0.99,
    )
    cand_good = ExternalCandidate(
        source_provider="kugou_lite",
        provider_track_id="good-2",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        duration_ms=269000,
        url="https://example.invalid/good.mp3",
        format="mp3",
        bitrate=320_000,
        level="exhigh",
        strict_match=True,
        match_score=0.95,
    )
    app.db.block_track_candidate("local", "netease:88801", "qq:bad-1", reason="wrong live version")
    with (
        patch.object(app.audio, "_resolve_netease_track", return_value=None),
        patch.object(app.audio, "_try_netease_alternative", return_value=None),
        patch.object(app.audio.external_resolver, "search_candidates", return_value=[cand_bad, cand_good]),
        patch.object(app.audio.external_resolver, "resolve_candidate", side_effect=lambda c, **kw: c),
    ):
        resolved = app.audio.resolve(88801, allow_external=True, song_hint=song, account_id="local")
    assert resolved["source_provider"] == "kugou_lite"
    assert resolved["candidate_key"] == "kugou_lite:good-2"
    route = app.db.get_track_source_route("local", "netease:88801")
    assert route is not None and route["provider"] == "kugou_lite"


def test_review_fixes_security_sync_metadata_and_pipeline(tmp_path):
    # 1. No self-identifying UA in amll_lyrics and NoOpCookies blocks all jar writes
    assert "Pruner-Studio/" not in amll_lyrics._AMLL_USER_AGENT
    jar = NoOpCookies()
    jar.set("MUSIC_U", "secret")
    assert len(jar) == 0

    # 2. ResolverRegistry encrypts api_key at rest (DPAPI on win32) & subscription never overwrites/injects tokens
    reg = ResolverRegistry(JsonStore(tmp_path))
    reg.upsert({"id": "my-res", "name": "My Resolver", "url": "http://127.0.0.1:9911", "api_key": "user-secret-tok"})
    raw_disk = (tmp_path / "resolvers_v2.json").read_text("utf-8")
    if sys.platform == "win32":
        assert "user-secret-tok" not in raw_disk
        assert '"encoding": "dpapi"' in raw_disk
    reg.import_subscription_payload(
        "http://127.0.0.1:9911/sub.json",
        {
            "resolvers": [
                {"id": "my-res", "name": "Updated Name", "url": "http://127.0.0.1:9911", "api_key": "hostile-tok"},
                {"id": "new-sub", "name": "New Sub", "url": "http://127.0.0.1:9912", "api_key": "injected-tok"},
            ]
        },
    )
    secrets_by_id = {r["id"]: r for r in reg.list_enabled_secrets()}
    assert secrets_by_id["my-res"]["api_key"] == "user-secret-tok"
    assert secrets_by_id["new-sub"]["api_key"] == ""

    # Oversized subscription streaming response rejected with 413
    oversized_client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * (300 * 1024))),
        cookies=NoOpCookies(),
    )
    try:
        with pytest.raises(AppError) as exc_sub:
            fetch_subscription_json("http://127.0.0.1:9911/big.json", http_client=oversized_client)
        assert exc_sub.value.status == 413
    finally:
        oversized_client.close()

    # 3. ChannelHealthTracker half-open admits only a single concurrent probe
    ht = ChannelHealthTracker(failure_threshold=2, base_cooldown_sec=10.0)
    ht.record_failure("qq", "err", now_mono=100.0)
    ht.record_failure("qq", "err", now_mono=101.0)
    assert ht.allow_request("qq", now_mono=105.0) is False
    assert ht.allow_request("qq", now_mono=112.0) is True  # single half-open probe
    assert ht.allow_request("qq", now_mono=112.1) is False  # second concurrent probe blocked
    ht.record_success("qq", latency_ms=25.0)
    assert ht.allow_request("qq", now_mono=113.0) is True

    # 4. SidecarProcessManager strictly respects timeout on hanging adapter (no readline deadlock)
    hanging_adapter = tmp_path / "hang_adapter.py"
    hanging_adapter.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    mgr = SidecarProcessManager()
    t0 = time.monotonic()
    with pytest.raises(AppError) as exc_sc:
        mgr.start(adapter_path=str(hanging_adapter), timeout_sec=0.8)
    elapsed = time.monotonic() - t0
    assert exc_sc.value.code == "SIDECAR_START_TIMEOUT"
    assert elapsed < 5.0
    with pytest.raises(AppError) as exc_port:
        mgr.start(port=80)
    assert exc_port.value.code == "SIDECAR_PORT_INVALID"

    # 5. Local metadata fixes: ID3v2 extended header + unsync, ID3v2.2 PIC, Opus R128 vs output_gain, 0.0 dB ReplayGain
    cover_cache = LocalCoverCache(tmp_path)
    jpg = _sample_jpeg_bytes()
    # ID3v2.3 with extended header (0x40) + tag unsync (0x80)
    ext_hdr = struct.pack(">I", 6) + b"\x00" * 6
    tit2_body = b"\x03" + "测试扩展头".encode("utf-8")
    tit2_frame = b"TIT2" + struct.pack(">I", len(tit2_body)) + b"\x00\x00" + tit2_body
    apic_body = b"\x00image/jpeg\x00\x03\x00" + jpg
    apic_frame = b"APIC" + struct.pack(">I", len(apic_body)) + b"\x00\x00" + apic_body
    unsync_payload = (ext_hdr + tit2_frame + apic_frame).replace(b"\xff", b"\xff\x00")
    s = len(unsync_payload)
    synch = bytes([(s >> 21) & 0x7F, (s >> 14) & 0x7F, (s >> 7) & 0x7F, s & 0x7F])
    mp3_ext = tmp_path / "ext_unsync.mp3"
    mp3_ext.write_bytes(b"ID3\x03\x00" + bytes([0xC0]) + synch + unsync_payload + b"\xff\xfb\x90\x00" + b"\x00" * 413)
    m_ext = parse_audio_metadata(mp3_ext, mp3_ext.stat().st_size, cover_cache=cover_cache)
    assert m_ext["title"] == "测试扩展头"
    assert m_ext["cover_id"]

    # ID3v2.2 PIC frame
    pic_body = b"\x00JPG\x03\x00" + jpg
    pic_frame = b"PIC" + len(pic_body).to_bytes(3, "big") + pic_body
    tt2_body = b"\x03LegacyTitle"
    tt2_frame = b"TT2" + len(tt2_body).to_bytes(3, "big") + tt2_body
    v22_tag = tt2_frame + pic_frame
    s2 = len(v22_tag)
    synch2 = bytes([(s2 >> 21) & 0x7F, (s2 >> 14) & 0x7F, (s2 >> 7) & 0x7F, s2 & 0x7F])
    mp3_v22 = tmp_path / "v22.mp3"
    mp3_v22.write_bytes(b"ID3\x02\x00\x00" + synch2 + v22_tag + b"\xff\xfb\x90\x00" + b"\x00" * 413)
    m_v22 = parse_audio_metadata(mp3_v22, mp3_v22.stat().st_size, cover_cache=cover_cache)
    assert m_v22["title"] == "LegacyTitle"
    assert m_v22["cover_id"]

    # Opus with non-zero output_gain (+512) AND R128_TRACK_GAIN=-512 (-2.0 dB @ -23 LUFS -> +3.0 dB @ -18 LUFS)
    opus_head = b"OpusHead" + struct.pack("<BBHIhB", 1, 2, 312, 48000, 512, 0)
    vendor = b"libopus"
    tags_list = [b"TITLE=OpusGainTest", b"R128_TRACK_GAIN=-512"]
    opus_tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(tags_list))
    for item in tags_list:
        opus_tags += struct.pack("<I", len(item)) + item

    def _ogg_page(payload: bytes, granule: int, seq: int) -> bytes:
        segs = [255] * (len(payload) // 255) + [len(payload) % 255]
        return b"OggS" + struct.pack("<BBQIIIB", 0, 0, granule, 1, seq, 0, len(segs)) + bytes(segs) + payload

    opus_path = tmp_path / "gain.opus"
    opus_path.write_bytes(_ogg_page(opus_head, 0, 0) + _ogg_page(opus_tags, 0, 1) + _ogg_page(b"\x00" * 20, 480312, 2))
    m_opus = parse_audio_metadata(opus_path, opus_path.stat().st_size, cover_cache=cover_cache)
    assert m_opus["replaygain_track_gain_db"] == 3.0

    # ReplayGain 0.0 dB preserved (not dropped as falsy 0)
    flac_zero = tmp_path / "zero_rg.flac"
    _build_flac_file(flac_zero, title="ZeroRG", artist="Art", album="Alb", isrc="", rg_gain="0.0 dB", rg_peak="0.9", cover=jpg)
    m_zero = parse_audio_metadata(flac_zero, flac_zero.stat().st_size, cover_cache=cover_cache)
    assert m_zero["replaygain_track_gain_db"] == 0.0

    # 6. BOM + bad numeric resilience in playlist_migration
    bom_json = "\ufeff" + json.dumps({"name": "BOM List", "tracks": [{"title": "T1", "duration_ms": "not-a-num"}]})
    parsed_bom = parse_playlist_import(bom_json, "auto")
    assert parsed_bom["format"] == "json"
    assert parsed_bom["entries"][0]["duration_ms"] == 0

    # 7. Hybrid playlist sorting + All-Catalog inclusion + Smart Radio non-NetEase inclusion + OpenSubsonic bad cover size=400
    app = _make_app(tmp_path / "app")
    pl = app.db.create_hybrid_playlist("local", "Multi Mix")
    app.db.add_tracks_to_hybrid_playlist(
        "local",
        pl["id"],
        [
            {"provider": "opensubsonic", "external_id": "s-b", "title": "Beta Song", "artists": ["NavArtist"], "duration": 200000},
            {"provider": "webdav", "external_id": "w-a", "title": "Alpha Song", "artists": ["DavArtist"], "duration": 180000, "webdav_url": "http://192.168.1.9:5244/dav/alpha.flac"},
        ],
    )
    sorted_view = app.db.query_view("local", playlist_id=pl["id"], sort="title", direction="asc")
    assert [t["title"] for t in sorted_view["tracks"]] == ["Alpha Song", "Beta Song"]
    all_view = app.db.query_view("local")
    assert len(all_view["tracks"]) == 2

    radio = app.recommend.smart_radio("local", explore_ratio=0.0, limit=10)
    assert len(radio["songs"]) == 2
    assert {s["provider"] for s in radio["songs"]} == {"opensubsonic", "webdav"}

    # WebDAV playback survives in-memory restart via DB canonical_tracks fallback
    app.channel_vault.update_channel("webdav", {"enabled": True, "api_base": "http://192.168.1.9:5244/dav", "username": "u", "password": "p"})
    dav_play = app.audio.resolve_external_track(provider="webdav", external_id="w-a")
    assert dav_play["provider"] if "provider" in dav_play else dav_play["source_provider"] == "webdav"
    assert dav_play["stream_token"]

    # OpenSubsonic cover endpoint returns 400 (not 500) on non-integer size
    asgi = create_asgi_app(app, 8765, require_local_token=False)
    with TestClient(asgi, base_url="http://127.0.0.1:8765") as tc:
        bad_sz = tc.get("/api/opensubsonic/cover/art1?size=bad")
        assert bad_sz.status_code == 400

    # 8. WebDAVEncryptedSync idempotent pull (zero play_count inflation) + playback_session restore + snapshot catch-up
    dav_store: dict[str, bytes] = {}

    def _dav_handler(req: httpx.Request) -> httpx.Response:
        key = req.url.path
        if req.method == "MKCOL":
            return httpx.Response(201)
        if req.method == "PUT":
            dav_store[key] = req.content
            return httpx.Response(201, headers={"ETag": f'"{len(dav_store)}"'})
        if req.method == "GET":
            if key not in dav_store:
                return httpx.Response(404)
            return httpx.Response(200, content=dav_store[key], headers={"ETag": f'"{len(dav_store)}"'})
        return httpx.Response(405)

    sync_client = httpx.Client(transport=httpx.MockTransport(_dav_handler), cookies=NoOpCookies())
    try:
        src_app = _make_app(tmp_path / "sync_src")
        dst_app = _make_app(tmp_path / "sync_dst")
        src_app.db.record_play("local", {"id": 0, "provider": "opensubsonic", "external_id": "s1", "track_key": "opensubsonic:s1", "title": "SyncSong", "artists": ["A"]}, listened_ms=60000, completed=True, event_id="ev-1")
        src_app.db.save_playback_session("local", {"current_track": {"id": 0, "provider": "opensubsonic", "external_id": "s1", "track_key": "opensubsonic:s1", "title": "SyncSong"}, "position_sec": 42.5, "duration_sec": 180.0, "queue": [], "queue_index": -1})
        syncer = WebDAVEncryptedSync(iterations=10_000)
        dav_secret = {"api_base": "http://127.0.0.1:5244/dav", "sync_path": "/nested/pruner-sync"}
        syncer.push_incremental(dav_secret, "passphrase-ok", account_id="local", db=src_app.db, rules_service=src_app.rules, device_id="devA12345678", http_client=sync_client)
        p1 = syncer.pull_incremental(dav_secret, "passphrase-ok", account_id="local", db=dst_app.db, rules_service=dst_app.rules, since_seq=0, http_client=sync_client)
        assert p1["session_restored"] is True
        h1 = dst_app.db.list_play_history("local")
        assert len(h1) == 1 and h1[0]["play_count"] == 1
        # Second push + pull from since_seq=0 must NOT inflate play_count or listen_ms
        syncer.push_incremental(dav_secret, "passphrase-ok", account_id="local", db=src_app.db, rules_service=src_app.rules, device_id="devA12345678", http_client=sync_client)
        syncer.pull_incremental(dav_secret, "passphrase-ok", account_id="local", db=dst_app.db, rules_service=dst_app.rules, since_seq=0, http_client=sync_client)
        h2 = dst_app.db.list_play_history("local")
        assert h2[0]["play_count"] == 1
        assert h2[0]["total_listen_ms"] == 60000
    finally:
        sync_client.close()


def test_multisource_fallback_sidecar_search_and_lan_stream_revocation(tmp_path):
    app = _make_app(tmp_path)

    # 1. OpenSubsonic candidate resolves cleanly during ExternalAudioResolver.resolve() auto-fallback
    app.channel_vault.update_channel(
        "opensubsonic",
        {"enabled": True, "api_base": "http://192.168.1.50:4533", "username": "alice", "password": "secret"},
    )
    os_cand = ExternalCandidate(
        title="稻香",
        artists=["周杰伦"],
        album="魔杰座",
        duration_ms=223000,
        source_provider="opensubsonic",
        provider_track_id="os-track-7",
        format="flac",
        bitrate=950_000,
        match_score=0.98,
        strict_match=True,
        needs_proxy=True,
    )
    resolved_os = app.audio.external_resolver.resolve_candidate(os_cand, verify_stream=True)
    assert resolved_os is not None
    assert resolved_os.url.startswith("http://192.168.1.50:4533/rest/stream.view?")
    assert resolved_os.extra.get("allow_self_hosted_origin") == "http://192.168.1.50:4533"

    # 2. Live Sidecar participates in search_multi_channel, resolve_direct_channel, and /api/lyrics
    adapter_file = tmp_path / "custom_sidecar_adapter.py"
    adapter_file.write_text(
        """
class Adapter:
    def manifest(self):
        return {"protocol_version": 2, "id": "sidecar", "name": "Test Sidecar", "capabilities": ["search", "resolve", "lyric"]}
    def search(self, query, *, limit=30, offset=0):
        return {"tracks": [{"id": "sc-1", "title": f"{query} Live", "artists": ["SidecarArtist"], "duration_ms": 200000, "format": "flac"}], "has_more": False}
    def resolve(self, payload):
        return {"candidates": [{"id": "sc-1", "title": payload.get("title", ""), "artists": payload.get("artists", []), "url": "https://cdn.example.invalid/sc.flac", "format": "flac", "bitrate": 900000}]}
    def lyric(self, payload):
        return {"lrc": "[00:01.00]Sidecar Lyric Line"}
""",
        encoding="utf-8",
    )
    sc_status = app.sidecar.start(adapter_path=str(adapter_file), timeout_sec=8.0)
    try:
        assert sc_status["running"] is True
        multi = app.audio.external_resolver.search_multi_channel("Echo", channels=["sidecar"], limit=10)
        assert len(multi["tracks"]) == 1
        assert multi["tracks"][0]["title"] == "Echo Live"
        assert multi["tracks"][0]["provider"] == "sidecar"

        # /api/lyrics hits Sidecar /lyric for non-NetEase tracks
        lyr = app.api("GET", "/api/lyrics?provider=sidecar&external_id=sc-1&title=Echo&artist=SidecarArtist")
        assert lyr.status == 200 and lyr.payload["ok"] is True
        assert "Sidecar Lyric Line" in lyr.payload["data"]["lyrics"]
    finally:
        app.sidecar.stop()

    # 3. Legacy local_files DB row with file_size==0 gets upgraded on next scan, then skipped incrementally
    music_dir = tmp_path / "music"
    music_dir.mkdir()
    flac_path = music_dir / "legacy.flac"
    _build_flac_file(
        flac_path,
        title="Legacy Track",
        artist="Artist L",
        album="Album L",
        isrc="USABC2400001",
        rg_gain="-4.2 dB",
        rg_peak="0.91",
        cover=_sample_jpeg_bytes(),
    )
    st = flac_path.stat()
    # Simulate pre-Milestone-1 row (file_size=0, cover_id='', replaygain=None)
    app.db.update_local_files_for_dirs(
        [music_dir],
        [{"path": str(flac_path), "title": "legacy", "artist": "", "album": "", "duration_ms": 0, "format": "flac", "bitrate": 0, "mtime": st.st_mtime, "file_size": 0}],
        [],
    )
    app.audio.external_resolver.local_scanner._records = []
    scan1 = app.audio.external_resolver.local_scanner.scan([music_dir])
    assert scan1["reused_unchanged"] == 0
    upgraded = app.db.get_local_file(str(flac_path))
    assert upgraded is not None and upgraded["cover_id"] and upgraded["file_size"] == st.st_size
    assert upgraded["replaygain_track_gain_db"] == -4.2
    scan2 = app.audio.external_resolver.local_scanner.scan([music_dir])
    assert scan2["reused_unchanged"] == 1

    # 4. LAN cover endpoint rejects non-integer size with 400 (not 500)
    app.lan_sync.enable_udp_discovery = False
    port = app.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
    try:
        pair = app.lan_sync.start_pairing()
        tls_ctx = ssl.create_default_context()
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode = ssl.CERT_NONE
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=tls_ctx, trust_env=False, timeout=5.0) as client:
            dev = client.post("/api/lan/v1/pair/claim", json={"code": pair["code"], "device_name": "Phone", "platform": "android"}).json()
            hdr = {"Authorization": f"Bearer {dev['token']}"}
            bad_cov = client.get("/api/lan/v1/cover?url=https://p1.music.126.net/a.jpg&size=notanint", headers=hdr)
            assert bad_cov.status_code == 400
    finally:
        app.lan_sync.stop()


def test_lan_local_library_cover_urls_and_malformed_id_rejection(tmp_path):
    """LAN local_page() and sync/pull(include_local=True) must emit validated local_cover_id URLs."""
    app = _make_app(tmp_path)
    music_dir = tmp_path / "local_covers"
    music_dir.mkdir()
    with_cover = music_dir / "a_with_cover.flac"
    without_cover = music_dir / "b_no_cover.mp3"
    png = _sample_png_bytes()
    _build_flac_file(
        with_cover,
        title="Alpha Cover",
        artist="Artist A",
        album="Album A",
        isrc="USRC17607840",
        rg_gain="-3.0 dB",
        rg_peak="0.9",
        cover=png,
    )
    without_cover.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 512)

    app.api("POST", "/api/local-library/scan", {"dirs": [str(music_dir)]})
    rows = {Path(r["path"]).name: r for r in app.db.list_local_files(limit=20)}
    valid_cid = rows["a_with_cover.flac"]["cover_id"]
    assert len(valid_cid) == 24
    assert rows["b_no_cover.mp3"]["cover_id"] == ""

    # Inject a third row with a malformed cover_id to verify strict 24-hex gating
    malformed_path = music_dir / "c_bad_cover.mp3"
    malformed_path.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 512)
    app.db.update_local_files_for_dirs(
        [music_dir],
        [
            rows["a_with_cover.flac"],
            rows["b_no_cover.mp3"],
            {
                "path": str(malformed_path),
                "title": "Gamma BadCid",
                "artist": "Artist C",
                "album": "Album C",
                "duration_ms": 1000,
                "format": "mp3",
                "bitrate": 320,
                "mtime": malformed_path.stat().st_mtime,
                "file_size": malformed_path.stat().st_size,
                "cover_id": "../evil_UPPER_not24hex",
            },
        ],
        [],
    )

    port = app.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
    try:
        ctx = ssl.create_default_context(cafile=str(tmp_path / "lan_tls_cert.pem"))
        pair = app.lan_sync.start_pairing()
        with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=ctx, trust_env=False, timeout=5.0) as client:
            dev = client.post("/api/lan/v1/pair/claim", json={"code": pair["code"], "device_name": "Phone", "platform": "android"}).json()
            hdr = {"Authorization": f"Bearer {dev['token']}"}

            page = client.get("/api/lan/v1/local-library?limit=50", headers=hdr)
            assert page.status_code == 200
            by_title = {item["title"]: item for item in page.json()["files"]}
            expected_cover_url = f"/api/lan/v1/cover?local_cover_id={valid_cid}"
            assert by_title["Alpha Cover"]["cover_url"] == expected_cover_url
            assert by_title["b_no_cover"]["cover_url"] == ""
            assert by_title["Gamma BadCid"]["cover_url"] == ""

            # Cover URL emitted by local-library actually serves the embedded cover image
            cov_resp = client.get(expected_cover_url, headers=hdr)
            assert cov_resp.status_code == 200
            assert cov_resp.content == png

            # sync/pull with include_local=True also carries cover_url
            pull_resp = client.post("/api/lan/v1/sync/pull", headers=hdr, json={"include_local": True})
            assert pull_resp.status_code == 200
            pulled = {item["title"]: item for item in pull_resp.json()["local_library"]}
            assert pulled["Alpha Cover"]["cover_url"] == expected_cover_url
            assert pulled["b_no_cover"]["cover_url"] == ""
            assert pulled["Gamma BadCid"]["cover_url"] == ""
    finally:
        app.lan_sync.stop()


def test_lan_external_cover_proxying_ssrf_and_handoff_roundtrip(tmp_path):
    """LAN track() and /api/lan/v1/cover safely proxy non-NetEase public HTTPS covers while blocking SSRF."""
    app = _make_app(tmp_path)
    png = _sample_png_bytes()

    # 1. track() cover projection for NetEase, external CDNs, Resolver v2 signed URLs, and SSRF rejections
    qq_cover = "https://y.gtimg.cn/music/photo_new/T002R300x300M000abc.jpg"
    bili_cover = "https://i0.hdslb.com/bfs/archive/abc123.jpg@320w_320h.webp"
    kugou_cover = "https://imge.kugou.com/stdmusic/240/20250101/abc.jpg"
    migu_cover = "https://d.musicapp.migu.cn/prod/file-service/file-down/abc.jpg"
    resolver_cover = "https://resolver.music-hub.org/covers/42.webp?sig=abc%2B1&exp=1999999999#ignored"
    resolver_cover_clean = "https://resolver.music-hub.org/covers/42.webp?sig=abc%2B1&exp=1999999999"
    netease_cover = "https://p1.music.126.net/abc/123.jpg"

    assert lan_sync.track({"provider": "netease", "id": 1, "cover_url": netease_cover})["cover_url"] == netease_cover
    # NetEase ?param=WxH and *.music.163.com stay direct (matching Android direct allowlist)
    netease_sized = "https://p1.music.126.net/abc/123.jpg?param=384y384"
    netease_music163 = "https://p1.music.163.com/abc/123.jpg"
    assert lan_sync.track({"provider": "netease", "id": 1, "cover_url": netease_sized})["cover_url"] == netease_sized
    assert lan_sync.track({"provider": "netease", "id": 1, "cover_url": netease_music163})["cover_url"] == netease_music163
    # Non-music *.163.com is NOT in Android's direct allowlist -> must proxy via /api/lan/v1/cover?url=...
    non_music_163 = "https://cdn.163.com/pic.jpg"
    assert lan_sync.track({"provider": "netease", "id": 1, "cover_url": non_music_163})["cover_url"] == (
        f"/api/lan/v1/cover?url={quote(non_music_163, safe='')}"
    )

    # Whitespace-only cover_url still falls back to cover_id, and query strings on desktop cover paths don't corrupt IDs
    sample_cid = "0123456789abcdef01234567"
    assert lan_sync.track({"provider": "local", "cover_url": "   ", "cover_id": sample_cid})["cover_url"] == (
        f"/api/lan/v1/cover?local_cover_id={sample_cid}"
    )
    assert lan_sync.track({"provider": "local", "cover_url": f"/api/local-cover/{sample_cid}?v=1"})["cover_url"] == (
        f"/api/lan/v1/cover?local_cover_id={sample_cid}"
    )
    assert lan_sync.track({"provider": "opensubsonic", "external_id": "s1", "cover_url": "/api/opensubsonic/cover/al%3A123?size=384"})["cover_url"] == (
        "/api/lan/v1/cover?opensubsonic_cover_id=al%3A123"
    )

    for raw_url, expected_clean in (
        (qq_cover, qq_cover),
        (bili_cover, bili_cover),
        (kugou_cover, kugou_cover),
        (migu_cover, migu_cover),
        (resolver_cover, resolver_cover_clean),
    ):
        projected = lan_sync.track({"provider": "qq", "external_id": "x", "cover_url": raw_url})["cover_url"]
        assert projected == f"/api/lan/v1/cover?url={quote(expected_clean, safe='')}", (raw_url, projected)
        # Round-trip idempotence: feeding the LAN proxy URL back into track() must NOT double-wrap or drop it
        assert lan_sync.track({"provider": "qq", "external_id": "x", "cover_url": projected})["cover_url"] == projected

    # Invalid / SSRF / private / non-HTTPS / traversal / control-char cover URLs must be dropped to ""
    for bad_cover in (
        "http://y.gtimg.cn/a.jpg",
        "https://127.0.0.1/a.jpg",
        "https://localhost/a.jpg",
        "https://sub.localhost/a.jpg",
        "https://nas.local/a.jpg",
        "https://meta.internal/a.jpg",
        "https://router.home.arpa/a.jpg",
        "https://10.0.0.1/a.jpg",
        "https://172.16.0.5/a.jpg",
        "https://192.168.1.5/a.jpg",
        "https://169.254.169.254/latest/meta-data/",
        "https://100.64.0.1/a.jpg",
        "https://[::1]/a.jpg",
        "https://[::ffff:127.0.0.1]/a.jpg",
        "https://[::ffff:8.8.8.8]/a.jpg",
        "https://[fe80::1]/a.jpg",
        "https://[fc00::1]/a.jpg",
        "https://[64:ff9b::127.0.0.1]/a.jpg",
        "https://2130706433/a.jpg",
        "https://127.1/a.jpg",
        "https://0177.0.0.1/a.jpg",
        "https://0x7f000001/a.jpg",
        "https://y.gtimg.cn:8443/a.jpg",
        "https://user:pass@y.gtimg.cn/a.jpg",
        "https://p1.music.126.net@evil.org/a.jpg",
        "https://p1.music.126.net/../../etc/passwd",
        "https://p1.music.126.net/%2e%2e/secret",
        "https://evil_sub.126.net/a.jpg",
        "https://p1.music.126.net/" + ("a" * 1100) + ".jpg",
        "https://y.gtimg.cn/a/../../etc/passwd",
        "https://y.gtimg.cn/%2e%2e/secret",
        "https://y.gtimg.cn/a%5cb.jpg",
        "https://y.gtimg.cn/a.jpg?k=a%5cb",
        "https://y.gtimg.cn/a.jpg\r\nX-Inject: 1",
        "https://y.gtimg.cn/a\x85b.jpg",
        "https://y.gtimg.cn/a\u2028b.jpg",
        "https://y.gtimg.cn/%00.jpg",
        "https://y.gtimg.cn/a.jpg?q=bad%0d%0aheader",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "file:///etc/passwd",
        "/api/lan/v1/cover?url=https%3A%2F%2F127.0.0.1%2Fsecret.jpg",
        "/api/lan/v1/cover?local_cover_id=not-24-hex",
    ):
        assert lan_sync.track({"provider": "qq", "external_id": "x", "cover_url": bad_cover})["cover_url"] == "", bad_cover

    # Desktop default validate_cover_url(allow_external=False) remains strictly NetEase-only
    with pytest.raises(AppError) as desktop_err:
        validate_cover_url(qq_cover, 64)
    assert desktop_err.value.code == "COVER_URL_REJECTED" and desktop_err.value.status == 403

    # 2. End-to-end LAN multi-search, cover proxy (with DNS pinning + query preservation + MIME/redirect SSRF checks), and handoff round-trip
    port = app.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
    real_client_cls = httpx.Client
    try:
        ctx = ssl.create_default_context(cafile=str(tmp_path / "lan_tls_cert.pem"))
        pair = app.lan_sync.start_pairing()
        with real_client_cls(base_url=f"https://127.0.0.1:{port}", verify=ctx, trust_env=False, timeout=5.0) as client:
            dev = client.post("/api/lan/v1/pair/claim", json={"code": pair["code"], "device_name": "Phone", "platform": "android"}).json()
            hdr = {"Authorization": f"Bearer {dev['token']}"}

            # proxy_search_multi routes external covers to /api/lan/v1/cover?url=...
            with patch.object(
                app.audio.external_resolver,
                "search_multi_channel",
                return_value={
                    "query": "Jay",
                    "offset": 0,
                    "limit": 10,
                    "has_more": False,
                    "channels_queried": ["qq", "bilibili", "custom"],
                    "tracks": [
                        {"id": 0, "provider": "qq", "external_id": "qq-1", "track_key": "qq:qq-1", "title": "QQ Song", "artists": ["A"], "cover_url": qq_cover},
                        {"id": 0, "provider": "bilibili", "external_id": "BV1xx", "track_key": "bilibili:BV1xx", "title": "Bili Song", "artists": ["B"], "cover_url": bili_cover},
                        {"id": 0, "provider": "custom", "external_id": "rv-1", "track_key": "custom:rv-1", "title": "Resolver Song", "artists": ["C"], "cover_url": resolver_cover},
                        {"id": 0, "provider": "custom", "external_id": "bad-1", "track_key": "custom:bad-1", "title": "Bad Cover", "artists": ["D"], "cover_url": "https://127.0.0.1/secret.jpg"},
                    ],
                    "channel_errors": {},
                },
            ):
                s = client.get("/api/lan/v1/search/multi?q=Jay&limit=10", headers=hdr)
                assert s.status_code == 200
                tracks = s.json()["tracks"]
                assert tracks[0]["cover_url"] == f"/api/lan/v1/cover?url={quote(qq_cover, safe='')}"
                assert tracks[1]["cover_url"] == f"/api/lan/v1/cover?url={quote(bili_cover, safe='')}"
                assert tracks[2]["cover_url"] == f"/api/lan/v1/cover?url={quote(resolver_cover_clean, safe='')}"
                assert tracks[3]["cover_url"] == ""

            # Fetch proxied Resolver v2 signed cover via /api/lan/v1/cover?url=...
            seen_requests = []

            def fake_upstream(req: httpx.Request) -> httpx.Response:
                seen_requests.append((req.url.host, str(req.url.raw_path, "ascii"), req.headers.get("host"), req.extensions.get("sni_hostname")))
                if req.headers.get("host") == "resolver.music-hub.org":
                    return httpx.Response(200, headers={"content-type": "image/png"}, content=png)
                if req.headers.get("host") == "svg.music-hub.org":
                    return httpx.Response(200, headers={"content-type": "image/svg+xml"}, content=b"<svg/>")
                if req.headers.get("host") == "redir-bad.music-hub.org":
                    return httpx.Response(302, headers={"location": "https://127.0.0.1/internal.png"})
                if req.headers.get("host") == "redir-good.music-hub.org":
                    return httpx.Response(302, headers={"location": "https://cdn2.music-hub.org/final.webp?ok=1"})
                if req.headers.get("host") == "cdn2.music-hub.org":
                    return httpx.Response(200, headers={"content-type": "image/webp"}, content=b"RIFF\x00\x00\x00\x00WEBP")
                if req.headers.get("host") == "dualstack.music-hub.org":
                    if req.url.host == "8.8.8.8":
                        raise httpx.ConnectError("IPv4 edge unavailable")
                    return httpx.Response(200, headers={"content-type": "image/png"}, content=png)
                return httpx.Response(404)

            def fake_dns(host, port_num, **kw):
                if host == "rebind.music-hub.org":
                    return [(2, 1, 6, "", ("127.0.0.1", port_num))]
                if host == "mapped.music-hub.org":
                    return [(10, 1, 6, "", ("::ffff:8.8.8.8", port_num, 0, 0))]
                if host == "dualstack.music-hub.org":
                    return [
                        (10, 1, 6, "", ("2606:4700:4700::1111", port_num, 0, 0)),
                        (2, 1, 6, "", ("8.8.8.8", port_num)),
                    ]
                return [(2, 1, 6, "", ("8.8.8.8", port_num))]

            with (
                patch("pruner_studio.external_sources._resolve_addresses", side_effect=fake_dns),
                patch(
                    "pruner_studio.app.httpx.Client",
                    side_effect=lambda **kw: real_client_cls(transport=httpx.MockTransport(fake_upstream), **kw),
                ),
            ):
                ok_cov = client.get(f"/api/lan/v1/cover?url={quote(resolver_cover_clean, safe='')}", headers=hdr)
                assert ok_cov.status_code == 200
                assert ok_cov.content == png
                assert ok_cov.headers["content-security-policy"] == "sandbox"
                assert ok_cov.headers["x-content-type-options"] == "nosniff"
                assert seen_requests[-1] == ("8.8.8.8", "/covers/42.webp?sig=abc%2B1&exp=1999999999", "resolver.music-hub.org", "resolver.music-hub.org")

                # Cache populated by LAN allow_external=True must NOT bypass desktop allow_external=False
                with pytest.raises(AppError) as cache_err:
                    app.fetch_cover(resolver_cover_clean, 64)
                assert cache_err.value.code == "COVER_URL_REJECTED"

                # Non-image MIME rejected (502)
                svg_cov = client.get(f"/api/lan/v1/cover?url={quote('https://svg.music-hub.org/x.svg', safe='')}", headers=hdr)
                assert svg_cov.status_code == 502 and svg_cov.json()["error"] == "COVER_CONTENT_INVALID"

                # Redirect to private loopback rejected (403)
                bad_redir = client.get(f"/api/lan/v1/cover?url={quote('https://redir-bad.music-hub.org/a.jpg', safe='')}", headers=hdr)
                assert bad_redir.status_code == 403 and bad_redir.json()["error"] == "COVER_URL_REJECTED"

                # DNS rebinding to 127.0.0.1 or IPv4-mapped IPv6 rejected (403)
                rebind = client.get(f"/api/lan/v1/cover?url={quote('https://rebind.music-hub.org/a.jpg', safe='')}", headers=hdr)
                assert rebind.status_code == 403 and rebind.json()["error"] == "COVER_URL_REJECTED"
                mapped = client.get(f"/api/lan/v1/cover?url={quote('https://mapped.music-hub.org/a.jpg', safe='')}", headers=hdr)
                assert mapped.status_code == 403 and mapped.json()["error"] == "COVER_URL_REJECTED"

                # Valid HTTPS redirect pins DNS on second hop and succeeds (200)
                good_redir = client.get(f"/api/lan/v1/cover?url={quote('https://redir-good.music-hub.org/start', safe='')}", headers=hdr)
                assert good_redir.status_code == 200 and good_redir.content.startswith(b"RIFF")
                assert seen_requests[-1] == ("8.8.8.8", "/final.webp?ok=1", "cdn2.music-hub.org", "cdn2.music-hub.org")

                # Dual-stack fallback: IPv4 tried first; on ConnectError falls back to validated IPv6 address
                dual = client.get(f"/api/lan/v1/cover?url={quote('https://dualstack.music-hub.org/art.png', safe='')}", headers=hdr)
                assert dual.status_code == 200 and dual.content == png
                assert seen_requests[-2][0] == "8.8.8.8"
                assert seen_requests[-1][0] == "2606:4700:4700::1111"

                # Malformed local_cover_id and opensubsonic_cover_id rejected by proxy_cover directly
                assert client.get("/api/lan/v1/cover?local_cover_id=../evil", headers=hdr).status_code == 404
                assert client.get("/api/lan/v1/cover?opensubsonic_cover_id=bad%0d%0aid", headers=hdr).status_code == 400
                assert client.get(f"/api/lan/v1/cover?opensubsonic_cover_id={'x' * 300}", headers=hdr).status_code == 400

            # Handoff round-trip preserves external cover for LAN peers and unwraps for desktop
            proxied_qq = f"/api/lan/v1/cover?url={quote(qq_cover, safe='')}"
            ho = client.post(
                "/api/lan/v1/handoff",
                headers=hdr,
                json={"track": {"provider": "qq", "external_id": "qq-1", "track_key": "qq:qq-1", "title": "QQ Song", "cover_url": proxied_qq}},
            )
            assert ho.status_code == 200
            assert ho.json()["track"]["cover_url"] == proxied_qq
            desktop_ho = app.api("GET", "/api/lan-sync/handoff")
            assert desktop_ho.status == 200
            assert desktop_ho.payload["data"]["pending_desktop_handoff"]["track"]["cover_url"] == qq_cover

            # OpenSubsonic cover ID with ':' survives desktop -> LAN -> desktop -> LAN without double-encoding,
            # and malformed /api/lan/v1/cover?... fails closed in restore_track
            os_lan = lan_sync.track({"provider": "opensubsonic", "external_id": "s1", "cover_url": "/api/opensubsonic/cover/al%3A123"})["cover_url"]
            assert os_lan == "/api/lan/v1/cover?opensubsonic_cover_id=al%3A123"
            os_restored = app.lan_sync.restore_track({"provider": "opensubsonic", "external_id": "s1", "cover_url": os_lan})
            assert os_restored["cover_url"] == "/api/opensubsonic/cover/al%3A123"
            assert lan_sync.track(os_restored)["cover_url"] == os_lan
            bad_restored = app.lan_sync.restore_track({"provider": "qq", "external_id": "x", "cover_url": "/api/lan/v1/cover?local_cover_id=INVALID"})
            assert bad_restored["cover_url"] == ""
    finally:
        app.lan_sync.stop()


def test_lan_composite_providers_checked_track_and_resolve_roundtrip(tmp_path):
    # 1. Unit & edge-case checks for lan_sync.checked_track() with composite providers (gdstudio:<sub_source>, resolver:<id>, resolver_v2:<id>)
    gd_full = lan_sync.checked_track(
        {
            "id": 0,
            "song_id": 0,
            "provider": "gdstudio:kuwo",
            "external_id": "kw-123",
            "track_key": "gdstudio:kuwo:kw-123",
            "title": "晴天",
            "artists": ["周杰伦"],
            "duration_ms": 269000,
        }
    )
    assert (gd_full["provider"], gd_full["external_id"], gd_full["track_key"], gd_full["song_id"]) == (
        "gdstudio:kuwo",
        "kw-123",
        "gdstudio:kuwo:kw-123",
        0,
    )

    # When external_id is omitted, identity must strip `provider + ":"` rather than splitting on the first `:` only
    gd_no_ext = lan_sync.checked_track({"provider": "gdstudio:kuwo", "track_key": "gdstudio:kuwo:kw-123", "title": "晴天"})
    assert (gd_no_ext["provider"], gd_no_ext["external_id"], gd_no_ext["track_key"]) == (
        "gdstudio:kuwo",
        "kw-123",
        "gdstudio:kuwo:kw-123",
    )

    # When provider is omitted or empty string (e.g. Android encodeDefaults), infer composite provider from track_key
    gd_key_only = lan_sync.checked_track({"provider": "", "track_key": "gdstudio:kuwo:kw-123"})
    assert (gd_key_only["provider"], gd_key_only["external_id"], gd_key_only["track_key"]) == (
        "gdstudio:kuwo",
        "kw-123",
        "gdstudio:kuwo:kw-123",
    )

    # resolver:<id> and resolver_v2:<id> including colons inside external_id and 48-char resolver IDs
    res_colon_id = lan_sync.checked_track({"provider": "resolver:hub-flac", "track_key": "resolver:hub-flac:trk:99"})
    assert (res_colon_id["provider"], res_colon_id["external_id"], res_colon_id["track_key"]) == (
        "resolver:hub-flac",
        "trk:99",
        "resolver:hub-flac:trk:99",
    )
    res_v2_key_only = lan_sync.checked_track({"track_key": "resolver_v2:hub-flac:rv:42"})
    assert (res_v2_key_only["provider"], res_v2_key_only["external_id"], res_v2_key_only["track_key"]) == (
        "resolver_v2:hub-flac",
        "rv:42",
        "resolver_v2:hub-flac:rv:42",
    )
    max_id = "r" + ("a" * 47)
    res_max = lan_sync.checked_track({"provider": f"resolver:{max_id}", "external_id": "t1"})
    assert res_max["track_key"] == f"resolver:{max_id}:t1"

    # Android feedback format (first segment as provider, full composite track_key) and OpenSubsonic colon external_id stay valid
    android_fb = lan_sync.checked_track({"provider": "gdstudio", "track_key": "gdstudio:kuwo:kw-123"})
    assert android_fb["track_key"] == "gdstudio:kuwo:kw-123"
    os_colon = lan_sync.checked_track({"track_key": "opensubsonic:al:456"})
    assert (os_colon["provider"], os_colon["external_id"]) == ("opensubsonic", "al:456")

    # Malformed / spoofed composite providers and mismatched keys are rejected
    for bad_row in (
        {"provider": ":kuwo", "external_id": "1"},
        {"provider": "gdstudio:", "external_id": "1"},
        {"provider": "gdstudio::kuwo", "external_id": "1"},
        {"provider": "gdstudio:kuwo:extra", "external_id": "1"},
        {"provider": "GDStudio:kuwo", "external_id": "1"},
        {"provider": "gdstudio:kuwo/bad", "external_id": "1"},
        {"provider": f"resolver:{'a' * 49}", "external_id": "1"},
        {"provider": "local:evil", "external_id": "1"},
        {"provider": "netease:evil", "external_id": "1"},
        {"provider": "..", "external_id": "1"},
        {"provider": "gdstudio:..", "external_id": "1"},
        {"provider": "resolver:.", "external_id": "1"},
        {"track_key": "gdstudio::kw-1"},
        {"track_key": "gdstudio:kuwo:"},
        {"provider": "gdstudio:kuwo", "track_key": "gdstudio:kuwo:"},
        {"provider": "gdstudio:kuwo", "track_key": "gdstudio:kugou:kw-1"},
        {"provider": "gdstudio:kuwo", "external_id": "kw-2", "track_key": "gdstudio:kuwo:kw-1"},
    ):
        with pytest.raises(ValueError):
            lan_sync.checked_track(bad_row)

    # 2. End-to-end LAN multi-search -> audio/resolve -> handoff -> sync round-trip for gdstudio:kuwo and resolver:<id> / resolver_v2:<id>
    app = _make_app(tmp_path)
    # Direct db.record_play also strips composite `provider:` cleanly when external_id is omitted
    rec_direct = app.db.record_play(
        "local",
        {"provider": "gdstudio:kuwo", "track_key": "gdstudio:kuwo:kw-direct", "title": "Direct"},
        completed=True,
    )
    assert rec_direct["recorded"] is True

    # Register an HTTP Resolver v2 endpoint
    reg = app.api(
        "POST",
        "/api/resolvers",
        {"id": "hub-flac", "name": "Hub FLAC", "url": "http://127.0.0.1:19911", "api_key": "tok-v2"},
    )
    assert reg.status == 201

    observed_resolve_bodies: list[dict] = []
    observed_gd_url_sources: list[str] = []

    def mock_upstream(request: httpx.Request) -> httpx.Response:
        host = (request.headers.get("host") or request.url.host or "").split(":")[0].lower()
        if host == "music-api.gdstudio.xyz":
            qtype = request.url.params.get("types")
            src = request.url.params.get("source") or ""
            if qtype == "search":
                if src == "kuwo":
                    return httpx.Response(
                        200,
                        json=[{"id": "kw-1", "name": "晴天", "artist": ["周杰伦"], "album": "叶惠美"}],
                    )
                return httpx.Response(200, json=[])
            if qtype == "url":
                observed_gd_url_sources.append(src)
                if src == "kuwo" and request.url.params.get("id") == "kw-1":
                    return httpx.Response(
                        200,
                        json={"url": "https://cdn.gdstudio.test/kw-1.flac", "br": 999, "size": 4096},
                    )
            return httpx.Response(404)
        if host == "cdn.gdstudio.test":
            payload = b"fLaC" + b"\x00" * 4092
            if request.headers.get("range"):
                return httpx.Response(
                    206,
                    headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/4096"},
                    content=payload,
                )
            return httpx.Response(200, headers={"Content-Type": "audio/flac", "Content-Length": str(len(payload))}, content=payload)
        if host == "127.0.0.1":
            if request.url.path.endswith("/search"):
                assert request.headers.get("x-resolver-token") == "tok-v2"
                return httpx.Response(
                    200,
                    json={
                        "has_more": False,
                        "tracks": [
                            {
                                "id": "rv-42",
                                "provider": "resolver:hub-flac",
                                "title": "夜曲",
                                "artists": ["周杰伦"],
                                "album": "十一月的萧邦",
                                "duration_ms": 226000,
                                "url": "http://127.0.0.1:19911/audio/rv-42.flac",
                                "format": "flac",
                                "bitrate": 980000,
                            }
                        ],
                    },
                )
            if request.url.path.endswith("/resolve"):
                assert request.headers.get("x-resolver-token") == "tok-v2"
                payload = json.loads(request.content.decode("utf-8")) if request.content else {}
                observed_resolve_bodies.append(payload)
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "id": "rv-42",
                                "title": "夜曲",
                                "artists": ["周杰伦"],
                                "duration_ms": 226000,
                                "url": "http://127.0.0.1:19911/audio/rv-42.flac",
                                "format": "flac",
                                "bitrate": 980000,
                            }
                        ]
                    },
                )
            if request.url.path == "/audio/rv-42.flac":
                payload = b"fLaC" + b"\x00" * 4092
                if request.headers.get("range"):
                    return httpx.Response(
                        206,
                        headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/4096"},
                        content=payload,
                    )
                return httpx.Response(200, headers={"Content-Type": "audio/flac", "Content-Length": str(len(payload))}, content=payload)
        return httpx.Response(404)

    def fake_dns(host, port_num, **kw):
        if host in {"127.0.0.1", "localhost"}:
            return [(2, 1, 6, "", ("127.0.0.1", port_num))]
        return [(2, 1, 6, "", ("8.8.8.8", port_num))]

    real_client = httpx.Client
    real_async_client = httpx.AsyncClient
    mock_ext_client = real_client(transport=httpx.MockTransport(mock_upstream), follow_redirects=False)
    app.audio.external_resolver._http_client = mock_ext_client
    port = app.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
    try:
        ctx = ssl.create_default_context(cafile=str(tmp_path / "lan_tls_cert.pem"))
        pair = app.lan_sync.start_pairing()
        with (
            patch("pruner_studio.external_sources._resolve_addresses", side_effect=fake_dns),
            patch(
                "pruner_studio.app.httpx.AsyncClient",
                side_effect=lambda **kw: real_async_client(transport=httpx.MockTransport(mock_upstream), **kw),
            ),
            real_client(base_url=f"https://127.0.0.1:{port}", verify=ctx, trust_env=False, timeout=5.0) as client,
        ):
            dev = client.post("/api/lan/v1/pair/claim", json={"code": pair["code"], "device_name": "Phone", "platform": "android"}).json()
            hdr = {"Authorization": f"Bearer {dev['token']}"}

            # A. gdstudio:kuwo search -> audio/resolve (full track & omitted external_id) -> handoff -> sync
            gd_search = client.get("/api/lan/v1/search/multi?q=%E6%99%B4%E5%A4%A9&channels=gdstudio", headers=hdr)
            assert gd_search.status_code == 200
            gd_tracks = gd_search.json()["tracks"]
            assert len(gd_tracks) == 1
            gd_track = gd_tracks[0]
            assert (gd_track["provider"], gd_track["external_id"], gd_track["track_key"]) == (
                "gdstudio:kuwo",
                "kw-1",
                "gdstudio:kuwo:kw-1",
            )

            gd_resolved = client.post("/api/lan/v1/audio/resolve", headers=hdr, json={"track": gd_track, "quality": "jymaster"})
            assert gd_resolved.status_code == 200, gd_resolved.text
            gd_body = gd_resolved.json()
            assert gd_body["track_key"] == "gdstudio:kuwo:kw-1"
            assert gd_body["provider"] == "gdstudio:kuwo"
            assert gd_body["format"] == "flac"
            assert gd_body["stream_url"].startswith("/api/lan/v1/audio/stream/")
            assert "kuwo" in observed_gd_url_sources
            gd_sid = gd_body["stream_url"].rsplit("/", 1)[-1]
            gd_target = app.lan_sync.proxy_stream_target(dev["device_id"], gd_sid)
            assert gd_target is not None and gd_target.url == "https://cdn.gdstudio.test/kw-1.flac"
            gd_stream = client.get(gd_body["stream_url"], headers=hdr)
            assert gd_stream.status_code == 200 and gd_stream.content.startswith(b"fLaC") and len(gd_stream.content) == 4096

            # Resolve gdstudio:kuwo when external_id is omitted (only provider + track_key)
            gd_min_resolved = client.post(
                "/api/lan/v1/audio/resolve",
                headers=hdr,
                json={"track": {"provider": "gdstudio:kuwo", "track_key": "gdstudio:kuwo:kw-1", "title": "晴天", "artist": "周杰伦"}},
            )
            assert gd_min_resolved.status_code == 200, gd_min_resolved.text
            assert gd_min_resolved.json()["track_key"] == "gdstudio:kuwo:kw-1"

            # Handoff & sync push/pull accept composite provider tracks
            ho = client.post("/api/lan/v1/handoff", headers=hdr, json={"track": gd_track, "position_sec": 12.5, "is_playing": True})
            assert ho.status_code == 200, ho.text
            assert ho.json()["track"]["track_key"] == "gdstudio:kuwo:kw-1"

            push = client.post(
                "/api/lan/v1/sync/push",
                headers=hdr,
                json={
                    "play_events": [{"track": gd_track, "event_id": "gd-play-1", "listen_ms": 30000, "completed": True}],
                    "feedback": [{"provider": "gdstudio:kuwo", "track_key": "gdstudio:kuwo:kw-1", "action": "replay", "event_id": "gd-fb-1"}],
                },
            )
            assert push.status_code == 200, push.text
            assert push.json()["accepted_events"] == 1 and push.json()["accepted_feedback"] == 1
            pulled = client.post("/api/lan/v1/sync/pull", headers=hdr, json={})
            assert pulled.status_code == 200, pulled.text
            pulled_body = pulled.json()
            assert any(r["track_key"] == "gdstudio:kuwo:kw-1" and r["provider"] == "gdstudio:kuwo" for r in pulled_body["play_history"])
            assert any(r["track_key"] == "gdstudio:kuwo:kw-1" and r["action"] == "replay" for r in pulled_body["feedback"])

            # B. resolver:<id> search -> audio/resolve & resolver_v2:<id> resolve
            rv_search = client.get("/api/lan/v1/search/multi?q=%E5%A4%9C%E6%9B%B2&channels=resolver:hub-flac", headers=hdr)
            assert rv_search.status_code == 200
            rv_tracks = rv_search.json()["tracks"]
            assert len(rv_tracks) == 1
            rv_track = rv_tracks[0]
            assert (rv_track["provider"], rv_track["external_id"], rv_track["track_key"]) == (
                "resolver:hub-flac",
                "rv-42",
                "resolver:hub-flac:rv-42",
            )
            assert "stream_url_hint" not in rv_track

            rv_resolved = client.post("/api/lan/v1/audio/resolve", headers=hdr, json={"track": rv_track, "quality": "lossless"})
            assert rv_resolved.status_code == 200, rv_resolved.text
            rv_body = rv_resolved.json()
            assert rv_body["track_key"] == "resolver:hub-flac:rv-42"
            assert rv_body["provider"] == "resolver:hub-flac"
            assert rv_body["format"] == "flac"
            assert rv_body["stream_url"].startswith("/api/lan/v1/audio/stream/")
            assert observed_resolve_bodies[-1].get("id") == "rv-42"
            rv_sid = rv_body["stream_url"].rsplit("/", 1)[-1]
            rv_target = app.lan_sync.proxy_stream_target(dev["device_id"], rv_sid)
            assert rv_target is not None and rv_target.url == "http://127.0.0.1:19911/audio/rv-42.flac"
            rv_stream = client.get(rv_body["stream_url"], headers=hdr)
            assert rv_stream.status_code == 200 and rv_stream.content.startswith(b"fLaC") and len(rv_stream.content) == 4096

            # resolver_v2:<id> with omitted external_id and singular `artist` also resolves cleanly
            rv2_resolved = client.post(
                "/api/lan/v1/audio/resolve",
                headers=hdr,
                json={
                    "track": {
                        "provider": "resolver_v2:hub-flac",
                        "track_key": "resolver_v2:hub-flac:rv-42",
                        "title": "夜曲",
                        "artist": "周杰伦",
                        "duration_ms": 226000,
                    }
                },
            )
            assert rv2_resolved.status_code == 200, rv2_resolved.text
            assert rv2_resolved.json()["track_key"] == "resolver_v2:hub-flac:rv-42"
            assert rv2_resolved.json()["provider"] == "resolver_v2:hub-flac"
            assert observed_resolve_bodies[-1].get("artists") == ["周杰伦"]

            # Malformed composite providers return 400 INVALID_INPUT over LAN HTTP boundary
            for bad_track in (
                {"provider": "gdstudio:kuwo:extra", "external_id": "1"},
                {"provider": "local:evil", "external_id": "1"},
                {"provider": "gdstudio:kuwo", "track_key": "gdstudio:kugou:kw-1"},
                {"provider": "gdstudio:kuwo", "external_id": "kw-1", "track_key": "file:/etc/passwd"},
                {"provider": "gdstudio:kuwo", "external_id": "kw-1", "track_key": r"C:\Windows\win.ini"},
                {"provider": "gdstudio:kuwo", "external_id": "file:evil", "track_key": "gdstudio:kuwo:kw-1"},
                {"provider": "gdstudio:kuwo", "external_id": True},
                {"provider": "gdstudio:kuwo", "external_id": "   "},
            ):
                bad_res = client.post("/api/lan/v1/audio/resolve", headers=hdr, json={"track": bad_track})
                assert bad_res.status_code == 400 and bad_res.json()["error"] == "INVALID_INPUT"
    finally:
        app.lan_sync.stop()
        mock_ext_client.close()


def test_lan_composite_providers_hardening_lyrics_and_live_sidecar(tmp_path):
    # 1. Adversarial checked_track() payloads where fields() strips dirty values or preserves wrong scalar types
    for bad_row in (
        {"provider": "gdstudio:kuwo", "external_id": "kw-1", "track_key": "file:/etc/passwd"},
        {"provider": "gdstudio:kuwo", "external_id": "kw-1", "track_key": r"C:\Windows\system.ini"},
        {"provider": "gdstudio:kuwo", "external_id": "kw-1", "track_key": "gdstudio:kuwo:kw-1\nINJECT"},
        {"provider": "gdstudio:kuwo", "external_id": "kw-1", "track_key": "MUSIC_U=secret"},
        {"provider": "gdstudio:kuwo", "external_id": "kw-1", "track_key": ["list"]},
        {"provider": "gdstudio:kuwo", "external_id": "file:evil", "track_key": "gdstudio:kuwo:kw-1"},
        {"provider": "gdstudio:kuwo", "external_id": True},
        {"provider": "gdstudio:kuwo", "external_id": False, "track_key": "gdstudio:kuwo:kw-1"},
        {"provider": "gdstudio:kuwo", "external_id": 12.5},
        {"provider": "gdstudio:kuwo", "external_id": ["list"], "track_key": "gdstudio:kuwo:kw-1"},
        {"provider": "gdstudio:kuwo", "id": True},
        {"provider": "gdstudio:kuwo", "song_id": False, "external_id": "kw-1"},
        {"provider": "gdstudio:kuwo", "external_id": "   "},
        {"provider": "gdstudio:kuwo", "track_key": "gdstudio:kuwo:   "},
        {"provider": "gdstudio:kuwo", "external_id": " kw-1 "},
        {"provider": "gdstudio:kuwo", "external_id": "kw-1\x7f"},
        {"track_key": "file:/etc/passwd", "id": 123},
        {"provider": "", "track_key": "file:/etc/passwd", "external_id": "kw-1"},
    ):
        with pytest.raises(ValueError):
            lan_sync.checked_track(bad_row)

    # Android Kotlin `wire` serialized Track shape (encodeDefaults=true) stays accepted
    android_wire_track = {
        "id": 0,
        "title": "晴天",
        "artists": ["周杰伦"],
        "artist": "周杰伦",
        "album": "叶惠美",
        "provider": "gdstudio:kuwo",
        "external_id": "kw-1",
        "track_key": "gdstudio:kuwo:kw-1",
        "duration": 0.0,
        "duration_ms": 0,
        "cover_url": "",
        "play_count": 0,
        "completed_count": 0,
        "total_listen_ms": 0,
        "max_br_level": "",
        "source_playlist_name": "",
        "artist_ids": [],
        "liked": False,
        "source_id": "lan:desktop",
    }
    checked_android = lan_sync.checked_track(android_wire_track)
    assert (checked_android["provider"], checked_android["external_id"], checked_android["track_key"]) == (
        "gdstudio:kuwo",
        "kw-1",
        "gdstudio:kuwo:kw-1",
    )

    # CustomEndpointProvider sanitizes uppercase and spoofed providers from untrusted resolvers
    assert CustomEndpointProvider._sanitize_provider("Resolver:Hub-Flac", "hub-flac") == "resolver:hub-flac"
    assert CustomEndpointProvider._sanitize_provider("local", "hub-flac") == "hub-flac"
    assert CustomEndpointProvider._sanitize_provider("local:evil", "hub-flac") == "hub-flac"
    assert CustomEndpointProvider._sanitize_provider("gdstudio:kuwo:extra", "hub-flac") == "hub-flac"

    app = _make_app(tmp_path)

    # 2. db.record_play infers composite provider from track_key when provider is omitted
    rec_gd = app.db.record_play(
        "local",
        {"track_key": "gdstudio:kuwo:kw-omit", "external_id": "kw-omit", "title": "Omit GD"},
        completed=True,
    )
    assert rec_gd["recorded"] is True
    rec_rv = app.db.record_play(
        "local",
        {"track_key": "resolver:hub-flac:rv-omit", "title": "Omit RV"},
        completed=True,
    )
    assert rec_rv["recorded"] is True
    hist_by_key = {row["track_key"]: row for row in app.db.list_play_history("local")}
    assert (hist_by_key["gdstudio:kuwo:kw-omit"]["provider"], hist_by_key["gdstudio:kuwo:kw-omit"]["external_id"]) == (
        "gdstudio:kuwo",
        "kw-omit",
    )
    assert (hist_by_key["resolver:hub-flac:rv-omit"]["provider"], hist_by_key["resolver:hub-flac:rv-omit"]["external_id"]) == (
        "resolver:hub-flac",
        "rv-omit",
    )
    with pytest.raises(ValueError):
        app.db.record_play("local", {"track_key": "gdstudio::kw-bad", "title": "Bad"}, completed=True)

    # 3. Resolver ID hijacking prevention, empty-title /resolve, resolver_v2 channel search, and /api/lan/v1/lyrics routing
    assert app.api("POST", "/api/resolvers", {"id": "resolver", "name": "Decoy Resolver", "url": "http://127.0.0.1:19921", "priority": 90, "capabilities": ["search", "resolve", "lyric"]}).status == 201
    assert app.api("POST", "/api/resolvers", {"id": "gdstudio", "name": "Decoy GD", "url": "http://127.0.0.1:19922", "priority": 85, "capabilities": ["search", "resolve", "lyric"]}).status == 201
    assert app.api("POST", "/api/resolvers", {"id": "hub-flac", "name": "Hub FLAC", "url": "http://127.0.0.1:19923", "priority": 50, "capabilities": ["search", "resolve", "lyric"]}).status == 201

    observed_hosts_and_paths: list[tuple[int, str, dict]] = []

    def mock_multi_resolver(request: httpx.Request) -> httpx.Response:
        host = (request.headers.get("host") or request.url.host or "").split(":")[0].lower()
        port_num = request.url.port or 0
        if host == "music-api.gdstudio.xyz":
            if request.url.params.get("types") == "url" and request.url.params.get("source") == "kuwo" and request.url.params.get("id") == "kw-1":
                return httpx.Response(200, json={"url": "https://cdn.gdstudio.test/kw-1.flac", "br": 999, "size": 4096})
            return httpx.Response(404)
        if host == "cdn.gdstudio.test":
            payload = b"fLaC" + b"\x00" * 4092
            return httpx.Response(206, headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/4096"}, content=payload)
        if host == "127.0.0.1":
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            observed_hosts_and_paths.append((port_num, request.url.path, body))
            if port_num == 19923 and request.url.path.endswith("/search"):
                return httpx.Response(
                    200,
                    json={"has_more": False, "tracks": [{"id": "rv-42", "provider": "Resolver:Hub-Flac", "title": "夜曲", "artists": ["周杰伦"], "duration_ms": 226000}]},
                )
            if port_num == 19923 and request.url.path.endswith("/resolve"):
                return httpx.Response(
                    200,
                    json={"candidates": [{"id": "rv-42", "title": "夜曲", "artists": ["周杰伦"], "url": "http://127.0.0.1:19923/audio/rv-42.flac", "format": "flac", "bitrate": 980000}]},
                )
            if port_num == 19923 and request.url.path == "/audio/rv-42.flac":
                payload = b"fLaC" + b"\x00" * 4092
                return httpx.Response(206, headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/4096"}, content=payload)
            if port_num == 19923 and request.url.path.endswith("/lyric"):
                return httpx.Response(200, json={"lrc": f"[00:01.00]HubLyric:{body.get('id')}"})
            if port_num in {19921, 19922} and request.url.path.endswith("/lyric"):
                return httpx.Response(200, json={"lrc": f"[00:01.00]WrongDecoy:{port_num}"})
            if port_num in {19921, 19922} and request.url.path.endswith("/resolve"):
                raise AssertionError(f"Unexpected hijack to decoy resolver port {port_num}")
        return httpx.Response(404)

    def fake_dns(host, port_num, **kw):
        if host in {"127.0.0.1", "localhost"}:
            return [(2, 1, 6, "", ("127.0.0.1", port_num))]
        return [(2, 1, 6, "", ("8.8.8.8", port_num))]

    real_client = httpx.Client
    real_async_client = httpx.AsyncClient
    mock_ext_client = real_client(transport=httpx.MockTransport(mock_multi_resolver), follow_redirects=False)
    app.audio.external_resolver._http_client = mock_ext_client
    lan_port = app.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
    try:
        ctx = ssl.create_default_context(cafile=str(tmp_path / "lan_tls_cert.pem"))
        pair = app.lan_sync.start_pairing()
        with (
            patch("pruner_studio.external_sources._resolve_addresses", side_effect=fake_dns),
            real_client(base_url=f"https://127.0.0.1:{lan_port}", verify=ctx, trust_env=False, timeout=5.0) as client,
        ):
            dev = client.post("/api/lan/v1/pair/claim", json={"code": pair["code"], "device_name": "Phone", "platform": "android"}).json()
            hdr = {"Authorization": f"Bearer {dev['token']}"}

            # channels=resolver_v2:<id> works and normalizes mixed-case provider from resolver /search
            s_v2 = client.get("/api/lan/v1/search/multi?q=%E5%A4%9C%E6%9B%B2&channels=resolver_v2:hub-flac", headers=hdr)
            assert s_v2.status_code == 200
            assert len(s_v2.json()["tracks"]) == 1
            assert s_v2.json()["tracks"][0]["provider"] == "resolver:hub-flac"

            # Nonexistent resolver:<id> is NOT hijacked by a resolver with id="resolver"
            missing = client.post("/api/lan/v1/audio/resolve", headers=hdr, json={"track": {"provider": "resolver:nonexistent", "external_id": "t1"}})
            assert missing.status_code == 404

            # gdstudio:kuwo is NOT hijacked by a resolver with id="gdstudio", and Android first-colon split also resolves
            ok_gd = client.post("/api/lan/v1/audio/resolve", headers=hdr, json={"track": android_wire_track})
            assert ok_gd.status_code == 200, ok_gd.text
            ok_gd_split = client.post("/api/lan/v1/audio/resolve", headers=hdr, json={"track": {"provider": "gdstudio", "track_key": "gdstudio:kuwo:kw-1"}})
            assert ok_gd_split.status_code == 200, ok_gd_split.text

            # resolver:<id> with NO title sends title="" (not fabricated track_id) and Android split provider="resolver" also resolves
            ok_rv_notitle = client.post("/api/lan/v1/audio/resolve", headers=hdr, json={"track": {"provider": "resolver:hub-flac", "track_key": "resolver:hub-flac:rv-42"}})
            assert ok_rv_notitle.status_code == 200, ok_rv_notitle.text
            last_resolve = [b for p, path, b in observed_hosts_and_paths if p == 19923 and path.endswith("/resolve")][-1]
            assert last_resolve.get("id") == "rv-42" and last_resolve.get("title") == ""

            ok_rv_split = client.post("/api/lan/v1/audio/resolve", headers=hdr, json={"track": {"provider": "resolver", "track_key": "resolver:hub-flac:rv-42"}})
            assert ok_rv_split.status_code == 200, ok_rv_split.text

            # /api/lan/v1/lyrics with track_key-only composite key and with id=0 both route to matching hub-flac first
            ly_key_only = client.get("/api/lan/v1/lyrics?track_key=resolver:hub-flac:rv-42&title=%E5%A4%9C%E6%9B%B2", headers=hdr)
            assert ly_key_only.status_code == 200, ly_key_only.text
            assert ly_key_only.json()["lyrics"] == "[00:01.00]HubLyric:rv-42"

            ly_id_zero = client.get("/api/lan/v1/lyrics?id=0&provider=resolver:hub-flac&external_id=rv-42&title=%E5%A4%9C%E6%9B%B2", headers=hdr)
            assert ly_id_zero.status_code == 200, ly_id_zero.text
            assert ly_id_zero.json()["lyrics"] == "[00:01.00]HubLyric:rv-42"

            # Android first-colon-split feedback sync round-trip for gdstudio:kuwo and resolver:hub-flac
            fb_push = client.post(
                "/api/lan/v1/sync/push",
                headers=hdr,
                json={
                    "feedback": [
                        {"client_event_id": "fb-android-gd", "event_id": "fb-android-gd", "provider": "gdstudio", "track_key": "gdstudio:kuwo:kw-1", "action": "replay", "context": ""},
                        {"client_event_id": "fb-android-rv", "event_id": "fb-android-rv", "provider": "resolver", "track_key": "resolver:hub-flac:rv-42", "action": "like_explicit", "context": ""},
                    ]
                },
            )
            assert fb_push.status_code == 200, fb_push.text
            assert fb_push.json()["accepted_feedback"] == 2
            fb_pull = client.post("/api/lan/v1/sync/pull", headers=hdr, json={}).json()
            pulled_keys = {item["track_key"] for item in fb_pull["feedback"]}
            assert {"gdstudio:kuwo:kw-1", "resolver:hub-flac:rv-42"}.issubset(pulled_keys)
    finally:
        app.lan_sync.stop()
        mock_ext_client.close()

    # 4. Real isolated scripts/pruner_sidecar.py subprocess resolved over live TLS LAN with provider="resolver:sidecar"
    app2 = _make_app(tmp_path / "sidecar_app")
    adapter_file = tmp_path / "lan_sidecar_adapter.py"
    adapter_file.write_text(
        """
class Adapter:
    def manifest(self):
        return {"protocol_version": 2, "id": "sidecar", "name": "Live LAN Sidecar", "capabilities": ["search", "resolve", "lyric"]}
    def search(self, query, *, limit=30, offset=0):
        return {
            "tracks": [{
                "id": "sc-99",
                "provider": "resolver:sidecar",
                "title": f"{query} Sidecar",
                "artists": ["SidecarArtist"],
                "duration_ms": 210000,
                "format": "flac",
            }],
            "has_more": False,
        }
    def resolve(self, payload):
        tid = str(payload.get("id") or "")
        if tid != "sc-99":
            return {"candidates": []}
        return {
            "candidates": [{
                "id": tid,
                "title": payload.get("title") or "Echo Sidecar",
                "artists": payload.get("artists") or ["SidecarArtist"],
                "url": "https://cdn.sidecar.test/sc-99.flac",
                "format": "flac",
                "bitrate": 960000,
            }]
        }
    def lyric(self, payload):
        return {"lrc": f"[00:02.00]LiveSidecar:{payload.get('id')}"}
""",
        encoding="utf-8",
    )
    sc_status = app2.sidecar.start(adapter_path=str(adapter_file), timeout_sec=8.0)
    assert sc_status["running"] is True
    sidecar_port = int(sc_status["port"])

    # Hybrid transport: REAL TCP sockets to the live sidecar process on 127.0.0.1:<sidecar_port>; mock ONLY the fake CDN host
    real_transport = httpx.HTTPTransport()

    def hybrid_sidecar_and_cdn(request: httpx.Request) -> httpx.Response:
        host = (request.headers.get("host") or request.url.host or "").split(":")[0].lower()
        if host == "cdn.sidecar.test":
            payload = b"fLaC" + b"\x00" * 4092
            if request.headers.get("range"):
                return httpx.Response(206, headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/4096"}, content=payload)
            return httpx.Response(200, headers={"Content-Type": "audio/flac", "Content-Length": str(len(payload))}, content=payload)
        assert host == "127.0.0.1" and request.url.port == sidecar_port
        return real_transport.handle_request(request)

    hybrid_client = real_client(transport=httpx.MockTransport(hybrid_sidecar_and_cdn), follow_redirects=False)
    app2.audio.external_resolver._http_client = hybrid_client
    lan_port2 = app2.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
    try:
        ctx2 = ssl.create_default_context(cafile=str(tmp_path / "sidecar_app" / "lan_tls_cert.pem"))
        pair2 = app2.lan_sync.start_pairing()
        with (
            patch("pruner_studio.external_sources._resolve_addresses", side_effect=fake_dns),
            patch(
                "pruner_studio.app.httpx.AsyncClient",
                side_effect=lambda **kw: real_async_client(
                    transport=httpx.MockTransport(
                        lambda req: httpx.Response(200, headers={"Content-Type": "audio/flac", "Content-Length": "4096"}, content=b"fLaC" + b"\x00" * 4092)
                    ),
                    **kw,
                ),
            ),
            real_client(base_url=f"https://127.0.0.1:{lan_port2}", verify=ctx2, trust_env=False, timeout=8.0) as client2,
        ):
            dev2 = client2.post("/api/lan/v1/pair/claim", json={"code": pair2["code"], "device_name": "Phone", "platform": "android"}).json()
            hdr2 = {"Authorization": f"Bearer {dev2['token']}"}

            sc_search = client2.get("/api/lan/v1/search/multi?q=Echo&channels=resolver:sidecar", headers=hdr2)
            assert sc_search.status_code == 200, sc_search.text
            sc_tracks = sc_search.json()["tracks"]
            assert len(sc_tracks) == 1
            sc_track = sc_tracks[0]
            assert (sc_track["provider"], sc_track["external_id"], sc_track["track_key"]) == (
                "resolver:sidecar",
                "sc-99",
                "resolver:sidecar:sc-99",
            )

            sc_res = client2.post("/api/lan/v1/audio/resolve", headers=hdr2, json={"track": sc_track})
            assert sc_res.status_code == 200, sc_res.text
            sc_body = sc_res.json()
            assert sc_body["track_key"] == "resolver:sidecar:sc-99"
            assert sc_body["provider"] == "resolver:sidecar"
            sc_stream = client2.get(sc_body["stream_url"], headers=hdr2)
            assert sc_stream.status_code == 200 and sc_stream.content.startswith(b"fLaC") and len(sc_stream.content) == 4096

            sc_lyric = client2.get("/api/lan/v1/lyrics?track_key=resolver:sidecar:sc-99&id=0", headers=hdr2)
            assert sc_lyric.status_code == 200, sc_lyric.text
            assert sc_lyric.json()["lyrics"] == "[00:02.00]LiveSidecar:sc-99"
    finally:
        app2.lan_sync.stop()
        app2.sidecar.stop()
        hybrid_client.close()
        real_transport.close()
