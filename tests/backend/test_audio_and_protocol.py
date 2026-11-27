from __future__ import annotations

import json
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

from pruner_studio.amll_lyrics import clear_amll_cache, fetch_amll_ttml_as_yrc, parse_ttml_to_yrc
from pruner_studio.audio_service import AudioService
from pruner_studio.catalog_service import CatalogService
from pruner_studio.errors import AppError, UpstreamError
from pruner_studio.external_sources import (
    ExternalAudioResolver,
    ExternalCandidate,
    LocalLibraryScanner,
    TrackQuery,
    evaluate_match,
    is_safe_external_url,
    probe_audio_url,
    sanitize_outbound_headers,
    score_audio_quality,
    validate_custom_external_endpoint,
)
from pruner_studio.models import Song
from pruner_studio.netease_client import (
    NeteaseClient,
    NeteaseWebTransport,
    TransportResponse,
    UrllibTransport,
)
from pruner_studio.netease_crypto import (
    DEFAULT_PC_APPVER,
    DEFAULT_PC_CHANNEL,
    DEFAULT_PC_OS,
    DEFAULT_PC_OSVER,
    EAPI_SEPARATOR,
    build_eapi_header,
    build_player_url_v1_payload,
    eapi_decrypt,
    eapi_decrypt_response,
    eapi_encrypt,
    weapi_encrypt,
)
from pruner_studio.recommend_service import RecommendService, rerank_recommendations
from pruner_studio.shuffle_engine import balanced_shuffle, recency_bucket
from pruner_studio.storage import ChannelCredentialVault, JsonStore


class FakeTransport:
    def __init__(self):
        self.handlers: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any], str, str | None]] = []

    def request(self, path, params=None, *, method="GET", cookie=None):
        params = params or {}
        self.calls.append((path, params, method, cookie))
        handler = self.handlers.get(path)
        if handler is None:
            raise AssertionError(f"unexpected transport path: {path}")
        value = handler(params, method, cookie) if callable(handler) else handler
        return value if isinstance(value, TransportResponse) else TransportResponse(200, value)


def _make_minimal_flac(path: Path, *, title: str, artist: str, album: str = "Album", sample_rate: int = 44100, duration_sec: int = 180) -> None:
    """Create a minimal valid FLAC header (STREAMINFO + VORBIS_COMMENT) for scanner tests."""
    total_samples = sample_rate * duration_sec
    channels = 2
    bps = 16
    packed = (
        ((sample_rate & 0xFFFFF) << 44)
        | (((channels - 1) & 0x7) << 41)
        | (((bps - 1) & 0x1F) << 36)
        | (total_samples & 0xFFFFFFFFF)
    )
    streaminfo = (
        struct.pack(">HH", 4096, 4096)
        + b"\x00\x00\x00"
        + b"\x00\x00\x00"
        + packed.to_bytes(8, "big")
        + b"\x00" * 16
    )
    comments = [f"TITLE={title}", f"ARTIST={artist}", f"ALBUM={album}"]
    vendor = b"pruner-test"
    vc_body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    for entry in comments:
        encoded = entry.encode("utf-8")
        vc_body += struct.pack("<I", len(encoded)) + encoded

    hdr_si = bytes([0x00]) + len(streaminfo).to_bytes(3, "big")
    hdr_vc = bytes([0x84]) + len(vc_body).to_bytes(3, "big")
    path.write_bytes(b"fLaC" + hdr_si + streaminfo + hdr_vc + vc_body + b"\x00" * 2048)


def _make_minimal_wav(path: Path, *, sample_rate: int = 44100, duration_sec: int = 180) -> None:
    channels = 2
    bits = 16
    byte_rate = sample_rate * channels * (bits // 8)
    data_size = byte_rate * duration_sec
    fmt = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, channels * (bits // 8), bits)
    # Write accurate header chunk sizes without allocating megabytes on disk.
    header = b"RIFF" + struct.pack("<I", 36 + 1024) + b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", data_size) + b"\x00" * 1024
    path.write_bytes(header)


class NeteaseCryptoTestCase(unittest.TestCase):
    def test_eapi_round_trip_and_digest_structure(self):
        url = "/api/song/enhance/player/url/v1"
        payload = {"ids": "[12345]", "level": "jymaster", "encodeType": "flac"}
        encrypted = eapi_encrypt(url, payload)
        self.assertIn("params", encrypted)
        params_hex = encrypted["params"]
        self.assertEqual(params_hex, params_hex.upper())
        decrypted = eapi_decrypt(params_hex).decode("utf-8")
        parts = decrypted.split(EAPI_SEPARATOR)
        self.assertEqual(3, len(parts))
        self.assertEqual(url, parts[0])
        self.assertEqual(payload, json.loads(parts[1]))
        self.assertEqual(32, len(parts[2]))

    def test_eapi_decrypt_response_handles_plain_and_encrypted_bytes(self):
        expected = {"code": 200, "data": [{"id": 1, "level": "jymaster"}]}
        plain = json.dumps(expected).encode("utf-8")
        self.assertEqual(expected, eapi_decrypt_response(plain))
        from Crypto.Cipher import AES
        from pruner_studio.netease_crypto import EAPI_KEY, pkcs7_pad

        cipher = AES.new(EAPI_KEY, AES.MODE_ECB)
        raw_encrypted = cipher.encrypt(pkcs7_pad(plain, 16))
        self.assertEqual(expected, eapi_decrypt_response(raw_encrypted))
        self.assertEqual(expected, eapi_decrypt_response(raw_encrypted.hex().encode("ascii")))

    def test_weapi_encrypt_is_valid_and_deterministic_with_key(self):
        result = weapi_encrypt({"id": 42}, secret_key="0123456789abcdef")
        self.assertEqual(256, len(result["encSecKey"]))
        self.assertTrue(all(ch in "0123456789abcdef" for ch in result["encSecKey"]))
        self.assertTrue(len(result["params"]) > 16)
        again = weapi_encrypt({"id": 42}, secret_key="0123456789abcdef")
        self.assertEqual(result, again)

    def test_player_url_v1_payload_pc_headers_and_quality_encoding(self):
        header = build_eapi_header("MUSIC_U=token123; __csrf=csrf99")
        self.assertEqual(DEFAULT_PC_OS, header["os"])
        self.assertEqual(DEFAULT_PC_APPVER, header["appver"])
        self.assertEqual(DEFAULT_PC_CHANNEL, header["channel"])
        self.assertEqual(DEFAULT_PC_OSVER, header["osver"])
        self.assertEqual("csrf99", header["__csrf"])

        for lossless_level in ("jymaster", "hires", "lossless"):
            payload = build_player_url_v1_payload([100], level=lossless_level, cookie="MUSIC_U=tok")
            self.assertEqual("flac", payload["encodeType"])
            self.assertNotIn("immerseType", payload)
            parsed_header = json.loads(payload["header"])
            self.assertEqual("pc", parsed_header["os"])
            self.assertEqual(DEFAULT_PC_APPVER, parsed_header["appver"])

        sky_payload = build_player_url_v1_payload([100], level="sky", cookie="MUSIC_U=tok")
        self.assertEqual("flac", sky_payload["encodeType"])
        self.assertEqual("c51", sky_payload["immerseType"])

        for lossy_level in ("exhigh", "standard"):
            payload = build_player_url_v1_payload([100], level=lossy_level)
            self.assertEqual("aac", payload["encodeType"])
            self.assertNotEqual("mp3", payload["encodeType"])


class NeteaseProtocolTestCase(unittest.TestCase):
    def test_cross_host_redirect_strips_cookie_and_untrusted_or_downgrade_blocked(self):
        seen_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            if request.url.host == "music.163.com" and request.url.path == "/redirect-trusted":
                return httpx.Response(302, headers={"Location": "https://interface.music.163.com/final"})
            if request.url.host == "interface.music.163.com" and request.url.path == "/final":
                return httpx.Response(200, json={"code": 200, "ok": True})
            if request.url.path == "/redirect-evil":
                return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
            if request.url.path == "/redirect-downgrade":
                return httpx.Response(302, headers={"Location": "http://music.163.com/plain"})
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        mock_client.cookies.set("shared", "fixture", domain=".music.163.com")
        transport = UrllibTransport("https://music.163.com", http_client=mock_client)
        try:
            res = transport.request("/redirect-trusted", cookie="MUSIC_U=supersecret")
            self.assertEqual(200, res.status)
            self.assertEqual(2, len(seen_requests))
            self.assertIn("MUSIC_U=supersecret", seen_requests[0].headers.get("cookie", ""))
            self.assertIsNone(seen_requests[1].headers.get("cookie"))

            seen_requests.clear()
            with self.assertRaises(UpstreamError) as evil_err:
                transport.request("/redirect-evil", cookie="MUSIC_U=supersecret")
            self.assertEqual("UPSTREAM_REDIRECT_BLOCKED", evil_err.exception.code)
            self.assertEqual(1, len(seen_requests))

            seen_requests.clear()
            with self.assertRaises(UpstreamError) as down_err:
                transport.request("/redirect-downgrade", cookie="MUSIC_U=supersecret")
            self.assertEqual("UPSTREAM_REDIRECT_BLOCKED", down_err.exception.code)
            self.assertEqual(1, len(seen_requests))
        finally:
            mock_client.close()

    def test_poll_qr_validates_candidate_cookie_atomically_before_mutating(self):
        fake = FakeTransport()
        client = NeteaseClient(fake, cookie="MUSIC_U=existing-valid")
        fake.handlers["/login/qr/check"] = {
            "code": 803,
            "message": "ok",
            "cookie": "MUSIC_U=new-candidate; Path=/",
        }
        fake.handlers["/user/account"] = lambda _p, _m, cookie: (
            {"code": 301} if cookie == "MUSIC_U=new-candidate" else {"code": 200, "profile": {"userId": 1, "nickname": "Old"}}
        )

        with self.assertRaises(AppError) as caught:
            client.poll_qr("qr-1")
        self.assertEqual("SESSION_EXPIRED", caught.exception.code)
        # Existing cookie MUST remain untouched!
        self.assertEqual("MUSIC_U=existing-valid", client.cookie)

        fake.handlers["/user/account"] = {
            "code": 200,
            "account": {"vipType": 11},
            "profile": {"userId": 77, "nickname": "SVIP User", "avatarUrl": "https://img/u.jpg"},
        }
        ok = client.poll_qr("qr-2")
        self.assertEqual("authorized", ok["state"])
        self.assertEqual(77, ok["user"]["user_id"])
        self.assertEqual(11, ok["user"]["vip_type"])
        self.assertNotIn("cookie", json.dumps(ok))
        self.assertEqual("MUSIC_U=new-candidate", client.cookie)

    def test_playlist_detail_merges_privileges_and_fails_closed_on_missing_or_mismatched_track_ids(self):
        def raw_request(_transport, path, params=None, **_kwargs):
            if path == "/api/v6/playlist/detail":
                pid = int(params["id"])
                if pid == 1:
                    return TransportResponse(
                        200,
                        {
                            "code": 200,
                            "playlist": {
                                "trackCount": 3,
                                "tracks": [
                                    {"id": 101, "name": "VIP Song", "ar": [{"name": "A"}], "al": {"name": "Al"}, "dt": 200_000, "fee": 1},
                                ],
                                "trackIds": [{"id": 101}, {"id": 102}, {"id": 103}],
                            },
                            "privileges": [
                                {"id": 101, "fee": 1, "st": 0, "maxBrLevel": "jymaster", "cs": False},
                            ],
                        },
                    )
                if pid == 2:
                    # Missing trackIds -> fail closed!
                    return TransportResponse(200, {"code": 200, "playlist": {"trackCount": 10, "tracks": []}})
                if pid == 3:
                    # Mismatched trackCount vs trackIds -> fail closed on fresh read!
                    return TransportResponse(
                        200,
                        {
                            "code": 200,
                            "playlist": {
                                "trackCount": 50,
                                "tracks": [{"id": 1, "name": "Only One"}],
                                "trackIds": [{"id": 1}],
                            },
                        },
                    )
            if path == "/api/v3/song/detail":
                return TransportResponse(
                    200,
                    {
                        "code": 200,
                        "songs": [
                            {"id": 102, "name": "Grey Song", "ar": [{"name": "B"}], "al": {"name": "Al"}, "dt": 180_000, "fee": 0},
                            {"id": 103, "name": "Cloud Song", "ar": [{"name": "C"}], "al": {"name": "Al"}, "dt": 210_000, "fee": 0},
                        ],
                        "privileges": [
                            {"id": 102, "fee": 0, "st": -200, "maxBrLevel": "standard", "cs": False},
                            {"id": 103, "fee": 0, "st": 0, "playMaxBrLevel": "lossless", "cs": True},
                        ],
                    },
                )
            raise AssertionError(path)

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            client = NeteaseClient(NeteaseWebTransport("https://music.163.com"))
            songs = client.playlist_tracks(1, fresh=True)
            self.assertEqual([101, 102, 103], [s.id for s in songs])
            self.assertEqual("vip", songs[0].availability)
            self.assertEqual(1, songs[0].fee)
            self.assertEqual(0, songs[0].privilege_st)
            self.assertEqual("jymaster", songs[0].max_br_level)
            self.assertFalse(songs[0].is_cloud)

            self.assertEqual("unavailable", songs[1].availability)
            self.assertEqual(-200, songs[1].privilege_st)

            self.assertEqual("available", songs[2].availability)
            self.assertTrue(songs[2].is_cloud)
            self.assertEqual("lossless", songs[2].max_br_level)

            with self.assertRaises(UpstreamError) as missing_err:
                client.playlist_tracks(2, fresh=True)
            self.assertEqual("TRACKS_INCOMPLETE", missing_err.exception.code)

            with self.assertRaises(UpstreamError) as mismatch_err:
                client.playlist_tracks(3, fresh=True)
            self.assertEqual("TRACKS_INCOMPLETE", mismatch_err.exception.code)

    def test_playlist_lock_not_held_during_network_and_stale_fetch_does_not_repopulate_after_clear(self):
        entered = threading.Event()
        release = threading.Event()

        def raw_request(_transport, path, params=None, **_kwargs):
            if path == "/api/v6/playlist/detail":
                entered.set()
                release.wait(timeout=2)
                return TransportResponse(
                    200,
                    {
                        "code": 200,
                        "playlist": {
                            "tracks": [{"id": 1, "name": "S", "ar": [{"name": "A"}], "dt": 100_000}],
                            "trackIds": [{"id": 1}],
                        },
                    },
                )
            raise AssertionError(path)

        transport = NeteaseWebTransport("https://music.163.com")
        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            worker = threading.Thread(target=lambda: transport.request("/playlist/track/all", {"id": 9}))
            worker.start()
            self.assertTrue(entered.wait(timeout=1))
            # clear_caches() MUST NOT deadlock or block waiting for the network request!
            t0 = time.monotonic()
            transport.clear_caches()
            self.assertLess(time.monotonic() - t0, 0.5)
            release.set()
            worker.join(timeout=2)
        # Because clear_caches() bumped generation mid-flight, stale result is NOT cached!
        self.assertEqual({}, transport._playlist_cache)

    def test_playlists_subscribed_and_new_endpoints_and_eapi_song_url(self):
        fake = FakeTransport()
        client = NeteaseClient(fake, cookie="MUSIC_U=tok")
        fake.handlers["/user/account"] = {
            "code": 200,
            "account": {"vipType": 11},
            "profile": {"userId": 10, "nickname": "U", "avatarUrl": "https://img/a.jpg"},
        }
        fake.handlers["/user/playlist"] = {
            "code": 200,
            "playlist": [
                {"id": 1, "name": "Liked", "trackCount": 5, "specialType": 5, "privacy": 0, "coverImgUrl": "https://c/1.jpg", "creator": {"userId": 10}},
                {"id": 2, "name": "Mine", "trackCount": 3, "specialType": 0, "privacy": 10, "coverImgUrl": "https://c/2.jpg", "creator": {"userId": 10}},
                {"id": 3, "name": "Sub", "trackCount": 8, "specialType": 0, "privacy": 0, "coverImgUrl": "https://c/3.jpg", "creator": {"userId": 99}},
            ],
        }
        owned = client.playlists()
        self.assertEqual([1, 2], [p.id for p in owned])
        self.assertEqual("https://c/1.jpg", owned[0].cover_url)

        all_pls = client.playlists(include_subscribed=True)
        self.assertEqual([1, 2, 3], [p.id for p in all_pls])
        self.assertEqual(["liked", "self_created", "subscribed"], [p.kind for p in all_pls])

        # lyrics_v2 fallback when mock only registers /lyric
        fake.handlers["/lyric"] = {"code": 200, "lrc": {"lyric": "[00:01.00]Hi"}, "tlyric": {"lyric": "[00:01.00]你好"}}
        fb_lrc = client.lyrics_v2(55)
        self.assertEqual("[00:01.00]Hi", fb_lrc["lyrics"])
        self.assertEqual("", fb_lrc["yrc"])

        # lyrics_v2 with /lyric/new returning YRC & romalrc
        fake.handlers["/lyric/new"] = {
            "code": 200,
            "lrc": {"lyric": "[00:01.00]Hi"},
            "tlyric": {"lyric": "[00:01.00]你好"},
            "romalrc": {"lyric": "[00:01.00]hai"},
            "yrc": {"lyric": "[1000,500](1000,500,0)Hi"},
        }
        full_lrc = client.lyrics_v2(55)
        self.assertIn("(1000,500,0)Hi", full_lrc["yrc"])
        self.assertEqual("[00:01.00]hai", full_lrc["romalrc"])

        # chorus, like, cloud, recommend endpoints
        fake.handlers["/song/chorus"] = {"code": 200, "chorus": [{"startTime": 45_000, "endTime": 78_000}]}
        self.assertEqual((45_000, 78_000), client.chorus(55))

        fake.handlers["/like"] = {"code": 200}
        client.request_like(55, like=True)
        self.assertEqual("true", fake.calls[-1][1]["like"])

        fake.handlers["/user/cloud"] = {
            "code": 200,
            "data": [{"songId": 701, "songName": "CloudTrack", "artist": "CA", "album": "CAl", "simpleSong": {"id": 701, "name": "CloudTrack", "ar": [{"name": "CA"}], "dt": 195_000}}],
        }
        clouds = client.cloud_songs()
        self.assertEqual(1, len(clouds))
        self.assertTrue(clouds[0].is_cloud)
        self.assertEqual("cloud", clouds[0].source_playlist_id)

        fake.handlers["/recommend/songs"] = {"code": 200, "data": {"dailySongs": [{"id": 801, "name": "D1", "ar": [{"name": "A1"}], "dt": 180_000}]}}
        self.assertEqual([801], [s.id for s in client.daily_recommend_songs()])

        fake.handlers["/personal_fm"] = {"code": 200, "data": [{"id": 802, "name": "FM1", "ar": [{"name": "A2"}], "dt": 180_000}]}
        self.assertEqual([802], [s.id for s in client.personal_fm()])

        fake.handlers["/playmode/intelligence/list"] = {"code": 200, "data": [{"songInfo": {"id": 803, "name": "H1", "ar": [{"name": "A3"}], "dt": 180_000}}]}
        self.assertEqual([803], [s.id for s in client.intelligence_list(1, 10)])

        fake.handlers["/simi/song"] = {"code": 200, "songs": [{"id": 804, "name": "S1", "ar": [{"name": "A4"}], "dt": 180_000}]}
        self.assertEqual([804], [s.id for s in client.similar_songs(1)])

        # Verify NeteaseWebTransport uses EAPI /eapi/song/enhance/player/url/v1 with flac
        captured: list[tuple[str, dict[str, Any]]] = []

        def raw_eapi(_transport, path, params=None, **_kwargs):
            captured.append((path, dict(params or {})))
            return TransportResponse(
                200,
                {
                    "code": 200,
                    "data": [
                        {
                            "id": 900,
                            "url": "https://cdn.example/900.flac",
                            "level": "jymaster",
                            "type": "flac",
                            "br": 1_920_000,
                            "sr": 96_000,
                            "size": 48_000_000,
                            "time": 200_000,
                            "freeTrialInfo": None,
                        }
                    ],
                },
            )

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_eapi):
            web_client = NeteaseClient(NeteaseWebTransport("https://music.163.com"), cookie="MUSIC_U=vip")
            audio = web_client.audio_url(900, level="jymaster")
        self.assertEqual("/eapi/song/enhance/player/url/v1", captured[0][0])
        decrypted = eapi_decrypt(captured[0][1]["params"]).decode("utf-8")
        eapi_json = json.loads(decrypted.split(EAPI_SEPARATOR)[1])
        self.assertEqual("flac", eapi_json["encodeType"])
        self.assertEqual("jymaster", eapi_json["level"])
        self.assertEqual("flac", audio["format"])
        self.assertEqual(96_000, audio["sample_rate"])
        self.assertFalse(audio["free_trial"])

    def test_parallel_song_detail_chunks_merge_privileges_in_track_order(self):
        total = 1_150
        all_ids = list(range(1, total + 1))

        def raw_request(_transport, path, params=None, **_kwargs):
            if path == "/api/v6/playlist/detail":
                return TransportResponse(
                    200,
                    {
                        "code": 200,
                        "playlist": {
                            "trackCount": total,
                            "tracks": [{"id": 1, "name": "Track 1", "ar": [{"name": "A"}], "dt": 120_000}],
                            "trackIds": [{"id": sid} for sid in all_ids],
                        },
                        "privileges": [{"id": 1, "fee": 0, "st": 0, "maxBrLevel": "hires"}],
                    },
                )
            if path == "/api/v3/song/detail":
                requested = json.loads(params["ids"])
                return TransportResponse(
                    200,
                    {
                        "code": 200,
                        "songs": [
                            {"id": sid, "name": f"Track {sid}", "ar": [{"name": "A"}], "dt": 120_000}
                            for sid in requested
                        ],
                        "privileges": [
                            {
                                "id": sid,
                                "fee": 1 if sid % 2 == 0 else 0,
                                "st": -200 if sid == 1_150 else 0,
                                "maxBrLevel": "lossless",
                            }
                            for sid in requested
                        ],
                    },
                )
            raise AssertionError(path)

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            client = NeteaseClient(NeteaseWebTransport("https://music.163.com"))
            songs = client.playlist_tracks(77, fresh=True)
        self.assertEqual(all_ids, [s.id for s in songs])
        self.assertEqual("hires", songs[0].max_br_level)
        self.assertEqual("vip", songs[1].availability)
        self.assertEqual("unavailable", songs[-1].availability)

    def test_search_songs_forwards_offset_through_client_and_web_transport(self):
        captured: list[tuple[str, dict[str, Any]]] = []

        def raw_search(_transport, path, params=None, **_kwargs):
            captured.append((path, dict(params or {})))
            return TransportResponse(
                200,
                {
                    "code": 200,
                    "result": {
                        "songs": [
                            {"id": 501, "name": "Page2", "ar": [{"name": "A"}], "al": {"name": "Al"}, "dt": 180_000}
                        ]
                    },
                },
            )

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_search):
            client = NeteaseClient(NeteaseWebTransport("https://music.163.com"))
            results = client.search_songs("Page2", limit=15, offset=30)
        self.assertEqual([501], [s.id for s in results])
        self.assertEqual("/api/search/get/web", captured[0][0])
        self.assertEqual(30, captured[0][1]["offset"])
        self.assertEqual(15, captured[0][1]["limit"])

    def test_lyrics_v1_and_chorus_use_interface_transport_and_fallback_on_rejection_or_empty(self):
        captured: list[tuple[str, str, dict[str, Any], str]] = []

        def raw_request(transport, path, params=None, *, method="GET", **_kwargs):
            captured.append((transport.base_url, path, dict(params or {}), method))
            if path == "/api/song/lyric/v1":
                sid = int(params["id"])
                if sid == 209996:
                    return TransportResponse(
                        200,
                        {
                            "code": 200,
                            "lrc": {"lyric": "[00:01.00]Line"},
                            "tlyric": {"lyric": "[00:01.00]翻译"},
                            "romalrc": {"lyric": "[00:01.00]romaji"},
                            "yrc": {"lyric": "[1000,500](1000,500,0)Line"},
                        },
                    )
                if sid == 40404:
                    return TransportResponse(200, {"code": 404, "message": "接口未找到！"})
                if sid == 50505:
                    return TransportResponse(200, {"code": 200, "lrc": {"lyric": ""}, "yrc": {"lyric": ""}})
            if path == "/api/song/lyric":
                return TransportResponse(
                    200,
                    {
                        "code": 200,
                        "lrc": {"lyric": "[00:02.00]Fallback LRC"},
                        "tlyric": {"lyric": "[00:02.00]回退翻译"},
                    },
                )
            if path == "/api/song/chorus":
                self.assertEqual("[2149019899]", params["ids"])
                return TransportResponse(
                    200,
                    {"code": 200, "chorus": [{"id": 2149019899, "startTime": 62500, "endTime": 83500}]},
                )
            raise AssertionError((transport.base_url, path))

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            client = NeteaseClient(NeteaseWebTransport("https://music.163.com"))

            # 1. /lyric/new hits https://interface.music.163.com/api/song/lyric/v1 with full params
            yrc_bundle = client.lyrics_v2(209996)
            self.assertEqual("[00:01.00]Line", yrc_bundle["lyrics"])
            self.assertEqual("[00:01.00]翻译", yrc_bundle["tlyrics"])
            self.assertEqual("[00:01.00]romaji", yrc_bundle["romalrc"])
            self.assertIn("(1000,500,0)Line", yrc_bundle["yrc"])
            self.assertEqual("https://interface.music.163.com", captured[0][0])
            self.assertEqual("/api/song/lyric/v1", captured[0][1])
            self.assertEqual("POST", captured[0][3])
            self.assertEqual("false", captured[0][2]["cp"])
            self.assertEqual(0, captured[0][2]["yv"])

            # 2. UPSTREAM_REJECTED (404) cleanly falls back to /lyric
            rejected_fb = client.lyrics_v2(40404)
            self.assertEqual("[00:02.00]Fallback LRC", rejected_fb["lyrics"])
            self.assertEqual("[00:02.00]回退翻译", rejected_fb["tlyrics"])

            # 3. Empty lrc & yrc from /lyric/new cleanly falls back to /lyric
            empty_fb = client.lyrics_v2(50505)
            self.assertEqual("[00:02.00]Fallback LRC", empty_fb["lyrics"])

            # 4. /song/chorus hits https://interface.music.163.com/api/song/chorus with ids=[id]
            start_ms, end_ms = client.chorus(2149019899)
            self.assertEqual((62500, 83500), (start_ms, end_ms))
            chorus_call = captured[-1]
            self.assertEqual("https://interface.music.163.com", chorus_call[0])
            self.assertEqual("/api/song/chorus", chorus_call[1])
            self.assertEqual("POST", chorus_call[3])
            self.assertEqual("[2149019899]", chorus_call[2]["ids"])


class ExternalSourcesTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("pruner_studio.external_sources._resolve_addresses",
                                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))

    def test_sanitize_headers_and_custom_endpoint_validation(self):
        cleaned = sanitize_outbound_headers({"Cookie": "MUSIC_U=secret", "Authorization": "Bearer x", "Referer": "https://www.bilibili.com/"})
        self.assertNotIn("Cookie", cleaned)
        self.assertNotIn("Authorization", cleaned)
        self.assertEqual("https://www.bilibili.com/", cleaned["Referer"])

        self.assertEqual("http://127.0.0.1:8080/match", validate_custom_external_endpoint("http://127.0.0.1:8080/match"))
        self.assertEqual("https://resolver.example/api", validate_custom_external_endpoint("https://resolver.example/api"))
        for bad in (
            "http://external.example/api",
            "file:///etc/passwd",
            "https://10.0.0.5/api",
            "http://169.254.169.254/latest",
            "https://user:pass@resolver.example/api",
            "http://user@127.0.0.1:8080/match",
        ):
            with self.assertRaises(ValueError):
                validate_custom_external_endpoint(bad)
        self.assertFalse(is_safe_external_url("https://user:pass@cdn.example/track.flac"))
        self.assertFalse(is_safe_external_url("http://user@127.0.0.1:9000/track.mp3", allow_loopback=True))

    def test_variant_rejection_for_strict_auto_play(self):
        query = TrackQuery(title="七里香", artists=("周杰伦",), duration_ms=299_000)
        strict_ok, score_ok = evaluate_match(query, "七里香", ["周杰伦"], 300_000)
        self.assertTrue(strict_ok)
        self.assertGreaterEqual(score_ok, 0.85)

        for variant in ("七里香 (Live)", "七里香 (翻唱)", "七里香 (伴奏)", "七里香 DJ Remix"):
            strict_bad, _ = evaluate_match(query, variant, ["周杰伦"], 299_000)
            self.assertFalse(strict_bad, variant)

    def test_local_scanner_reads_flac_wav_and_prefers_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_minimal_flac(root / "track01.flac", title="晴天", artist="周杰伦", album="叶惠美", sample_rate=96_000, duration_sec=269)
            _make_minimal_wav(root / "周杰伦 - 稻香.wav", sample_rate=44_100, duration_sec=223)
            (root / "周杰伦 - 晴天.mp3").write_bytes(b"ID3" + b"\x00" * 2048)

            scanner = LocalLibraryScanner()
            report = scanner.scan([root])
            self.assertEqual(3, report["scanned_files"])

            match = scanner.match(TrackQuery(title="晴天", artists=("周杰伦",), duration_ms=269_000))
            self.assertIsNotNone(match)
            self.assertEqual("flac", match.format)
            self.assertEqual(96_000, match.sample_rate)

            wav_match = scanner.match(TrackQuery(title="稻香", artists=("周杰伦",), duration_ms=223_000))
            self.assertIsNotNone(wav_match)
            self.assertEqual("wav", wav_match.format)

            wrong_artist = scanner.match(TrackQuery(title="晴天", artists=("林俊杰",), duration_ms=269_000))
            self.assertIsNone(wrong_artist)

    def test_stream_probe_rejects_html_and_private_ip_and_accepts_real_flac(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/html":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<!DOCTYPE html><html>error</html>")
            if request.url.path == "/json":
                return httpx.Response(200, headers={"Content-Type": "application/json"}, content=b'{"error": "forbidden"}')
            if request.url.path == "/real.flac":
                return httpx.Response(
                    206,
                    headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/31457280"},
                    content=b"fLaC" + b"\x00" * 4092,
                )
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            self.assertFalse(probe_audio_url("https://cdn.example/html", client).ok)
            self.assertFalse(probe_audio_url("https://cdn.example/json", client).ok)
            self.assertFalse(probe_audio_url("http://169.254.169.254/latest", client).ok)
            ok = probe_audio_url("https://cdn.example/real.flac", client)
            self.assertTrue(ok.ok)
            self.assertEqual("flac", ok.format)
            self.assertEqual(31_457_280, ok.size)
        finally:
            client.close()

    def test_external_resolver_zero_cookie_isolation_across_providers(self):
        observed_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            observed_requests.append(request)
            self.assertIsNone(request.headers.get("cookie"))
            if "music-api.gdstudio.xyz" in request.headers["host"].split(":")[0]:
                if request.url.params.get("types") == "search":
                    return httpx.Response(
                        200,
                        json=[
                            {"id": "kw-1", "name": "晴天", "artist": ["周杰伦"], "album": "叶惠美"},
                            {"id": "kw-live", "name": "晴天 (Live)", "artist": ["周杰伦"], "album": "演唱会"},
                        ],
                    )
                if request.url.params.get("types") == "url":
                    return httpx.Response(200, json={"url": "https://cdn.gdstudio.test/kw-1.flac", "br": 999, "size": 30_000_000})
            if request.headers["host"].split(":")[0] == "cdn.gdstudio.test":
                return httpx.Response(206, headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/30000000"}, content=b"fLaC" + b"\x00" * 4092)
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        resolver = ExternalAudioResolver(http_client=mock_client)
        try:
            song = Song(id=186016, title="晴天", artists=["周杰伦"], album="叶惠美", duration=269_000)
            matched = resolver.resolve(song, enabled_providers=["gdstudio", "kuwo"])
            self.assertIsNotNone(matched)
            self.assertEqual("gdstudio:kuwo", matched.source_provider)
            self.assertEqual("flac", matched.format)
            self.assertEqual(999_000, matched.bitrate)
            self.assertTrue(observed_requests)
            for req in observed_requests:
                self.assertIsNone(req.headers.get("cookie"))
                self.assertNotIn("186016", str(req.url))
        finally:
            mock_client.close()

    def test_kuwo_and_custom_endpoint_providers_zero_cookie(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertIsNone(request.headers.get("cookie"))
            if request.headers["host"].split(":")[0] == "search.kuwo.cn":
                body = "{'abslist': [{'MUSICRID': 'MUSIC_555', 'SONGNAME': '夜曲', 'ARTIST': '周杰伦', 'ALBUM': '十一月的萧邦', 'DURATION': '226'}]}"
                return httpx.Response(200, text=body)
            if request.headers["host"].split(":")[0] == "antiserver.kuwo.cn":
                return httpx.Response(200, json={"url": "https://sycdn.kuwo.test/555.flac"})
            if request.headers["host"].split(":")[0] == "sycdn.kuwo.test":
                return httpx.Response(206, headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/25000000"}, content=b"fLaC" + b"\x00" * 4092)
            if request.headers["host"].split(":")[0] == "127.0.0.1" and request.url.path == "/match":
                if request.method == "POST":
                    return httpx.Response(405)
                return httpx.Response(
                    200,
                    json={
                        "provider": "kuwo",
                        "url": "http://127.0.0.1:9000/stream.mp3",
                        "format": "mp3",
                        "bitrate": 320_000,
                        "title": "夜曲",
                        "artists": ["周杰伦"],
                        "duration_ms": 226_000,
                    },
                )
            if request.headers["host"].split(":")[0] == "127.0.0.1" and request.url.path == "/stream.mp3":
                return httpx.Response(206, headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 0-4095/8000000"}, content=b"ID3" + b"\x00" * 4093)
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        resolver = ExternalAudioResolver(http_client=mock_client)
        try:
            song = Song(id=10, title="夜曲", artists=["周杰伦"], album="十一月的萧邦", duration=226_000)
            kuwo_res = resolver.resolve(song, enabled_providers=["kuwo"])
            self.assertIsNotNone(kuwo_res)
            self.assertEqual("kuwo", kuwo_res.source_provider)
            self.assertEqual("flac", kuwo_res.format)
            self.assertTrue(kuwo_res.needs_proxy)

            # Loopback custom endpoint returning custom provider name ("kuwo") MUST still pass allow_loopback!
            custom_res = resolver.resolve(song, enabled_providers=[], custom_api="http://127.0.0.1:9000/match")
            self.assertIsNotNone(custom_res)
            self.assertEqual("kuwo", custom_res.source_provider)
            self.assertTrue(custom_res.extra.get("from_custom_endpoint"))
            self.assertEqual("mp3", custom_res.format)
        finally:
            mock_client.close()


class AudioServiceTieredTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("pruner_studio.external_sources._resolve_addresses",
                                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
        self.temp = tempfile.TemporaryDirectory()
        self.store = JsonStore(Path(self.temp.name))
        self.transport = FakeTransport()
        self.client = NeteaseClient(self.transport, cookie="MUSIC_U=vip-cookie")
        self.catalog = CatalogService(self.store, self.client)
        self.catalog.import_content(
            json.dumps(
                [
                    {"id": 10, "title": "夜曲", "artists": ["周杰伦"], "album": "十一月的萧邦", "duration": 226_000},
                    {"id": 20, "title": "晴天", "artists": ["周杰伦"], "album": "叶惠美", "duration": 269_000},
                ]
            ),
            "json",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_tier1_single_compatibility_fallback_and_no_blind_retries(self):
        calls: list[str] = []

        def audio_handler(params, _method, _cookie):
            level = params["level"]
            calls.append(level)
            if level == "sky":
                return {"code": 200, "data": [{"id": 10, "url": "https://cdn/trial.mp3", "level": "sky", "type": "dolby", "freeTrialInfo": {"start": 0, "end": 30}}]}
            return {"code": 200, "data": [{"id": 10, "url": "https://cdn/full.flac", "level": "lossless", "type": "flac", "br": 980_000, "sr": 44_100, "size": 25_000_000, "time": 226_000}]}

        self.transport.handlers["/song/url/v1"] = audio_handler
        service = AudioService(self.client, self.catalog)
        resolved = service.resolve(10, quality="sky", allow_external=False)
        self.assertEqual(["sky", "lossless"], calls)
        self.assertEqual("original", resolved["source"])
        self.assertEqual("flac", resolved["format"])
        self.assertEqual(980_000, resolved["bitrate"])

        # Cached hit does NOT call transport again; force_refresh bypasses cache!
        calls.clear()
        service.resolve(10, quality="sky", allow_external=False)
        self.assertEqual([], calls)
        service.resolve(10, quality="sky", allow_external=False, force_refresh=True)
        self.assertEqual(["sky", "lossless"], calls)

    def test_tier2_cloud_disk_tier4_local_and_tier5_external_with_stream_tokens(self):
        # Tier 2: Original fails with UpstreamError -> gracefully falls through to Cloud Disk!
        self.transport.handlers["/song/url/v1"] = lambda params, *_a: (
            (_ for _ in ()).throw(UpstreamError("UPSTREAM_UNAVAILABLE", "orig down"))
            if int(params["id"]) == 10
            else {"code": 200, "data": [{"id": int(params["id"]), "url": "https://cdn/cloud.flac", "level": "lossless", "type": "flac", "br": 950_000}]}
        )
        self.transport.handlers["/user/cloud"] = {
            "code": 200,
            "data": [{"songId": 777, "simpleSong": {"id": 777, "name": "夜曲", "ar": [{"name": "周杰伦"}], "dt": 226_000}}],
        }
        service = AudioService(self.client, self.catalog)
        cloud_res = service.resolve(10, allow_external=False)
        self.assertEqual("cloud", cloud_res["source"])
        self.assertEqual(777, cloud_res["id"])

        # Tier 4: Local Lossless Folder fallback
        music_dir = Path(self.temp.name) / "music"
        music_dir.mkdir()
        _make_minimal_flac(music_dir / "qingtian.flac", title="晴天", artist="周杰伦", duration_sec=269)
        self.transport.handlers["/song/url/v1"] = {"code": 200, "data": [{"id": 20, "url": None}]}
        self.transport.handlers["/user/cloud"] = {"code": 200, "data": []}
        self.transport.handlers["/cloudsearch"] = {"code": 200, "result": {"songs": []}}
        service.clear_caches()
        service.scan_local_library([music_dir])
        local_res = service.resolve(20, allow_external=False)
        self.assertEqual("local", local_res["source"])
        self.assertTrue(local_res["stream_url"].startswith("/api/stream/"))
        token = local_res["stream_token"]
        target = service.resolve_stream_token(token)
        self.assertIsNotNone(target)
        self.assertEqual("local", target.kind)
        self.assertEqual("flac", target.format)
        self.assertFalse(target.allow_cookies)

        # Tier 5: External source (Bilibili proxy requiring Referer, zero NetEase cookie)
        def ext_handler(request: httpx.Request) -> httpx.Response:
            self.assertIsNone(request.headers.get("cookie"))
            if "api.bilibili.com/x/web-interface/search/type" in (request.headers["host"] + request.url.path):
                return httpx.Response(200, json={"data": {"result": [{"bvid": "BV1test", "title": "<em>周杰伦</em> - 晴天", "author": "Official", "duration": "04:29"}]}})
            if "api.bilibili.com/x/player/pagelist" in (request.headers["host"] + request.url.path):
                return httpx.Response(200, json={"data": [{"cid": 999}]})
            if "api.bilibili.com/x/player/playurl" in (request.headers["host"] + request.url.path):
                return httpx.Response(
                    200,
                    json={"data": {"dash": {"audio": [{"baseUrl": "https://upos.bilivideo.test/a.m4s", "bandwidth": 192_000}]}}},
                )
            if request.headers["host"].split(":")[0] == "upos.bilivideo.test":
                return httpx.Response(206, headers={"Content-Type": "audio/mp4", "Content-Range": "bytes 0-4095/5000000"}, content=b"\x00\x00\x00\x18ftyp" + b"\x00" * 4088)
            return httpx.Response(404)

        ext_client = httpx.Client(transport=httpx.MockTransport(ext_handler), follow_redirects=False)
        try:
            ext_service = AudioService(
                self.client,
                self.catalog,
                external_resolver=ExternalAudioResolver(http_client=ext_client),
                settings_provider=lambda: {"enable_external_sources": True, "external_providers": ["bilibili"]},
            )
            ext_res = ext_service.resolve(20)
            self.assertEqual("external", ext_res["source"])
            self.assertEqual("bilibili", ext_res["source_provider"])
            self.assertTrue(ext_res["url"].startswith("/api/stream/"))
            ext_target = ext_service.resolve_stream_token(ext_res["stream_token"])
            self.assertIsNotNone(ext_target)
            self.assertEqual("remote", ext_target.kind)
            self.assertEqual("https://www.bilibili.com/", ext_target.headers.get("Referer"))
            self.assertNotIn("Cookie", ext_target.headers)
            self.assertFalse(ext_target.allow_cookies)

            # clear_caches revokes stream tokens on logout!
            ext_service.clear_caches()
            self.assertIsNone(ext_service.resolve_stream_token(ext_res["stream_token"]))

            # Disabling external sources respects toggle
            with self.assertRaises(AppError) as no_ext:
                ext_service.resolve(20, allow_external=False)
            self.assertEqual("AUDIO_UNAVAILABLE", no_ext.exception.code)
        finally:
            ext_client.close()

    def test_manual_candidates_include_live_versions_and_stream_tokens_expire_and_bound(self):
        self.transport.handlers["/cloudsearch"] = {
            "code": 200,
            "result": {
                "songs": [
                    {"id": 301, "name": "夜曲 (Live)", "ar": [{"name": "周杰伦"}], "al": {"name": "演唱会"}, "dt": 245_000, "fee": 0, "privilege": {"st": 0}},
                ]
            },
        }
        self.transport.handlers["/song/url/v1"] = {
            "code": 200,
            "data": [{"id": 301, "url": "https://cdn/live-301.mp3", "level": "exhigh", "type": "mp3", "br": 320_000}],
        }
        service = AudioService(
            self.client,
            self.catalog,
            cache_max_entries=2,
            token_max_entries=2,
            token_ttl_sec=60.0,
        )
        listed = service.candidates(10, allow_external=False)
        self.assertEqual(10, listed["song_id"])
        self.assertEqual(1, len(listed["candidates"]))
        self.assertFalse(listed["candidates"][0]["strict_match"])
        chosen = service.resolve(10, candidate_id=listed["candidates"][0]["candidate_id"], allow_external=False)
        self.assertEqual(301, chosen["id"])
        self.assertTrue(chosen["is_alternative"])

        # Token registry LRU eviction bounded to token_max_entries=2
        t1 = service.register_stream_token(kind="remote", url="https://a.test/1.mp3", headers={"Cookie": "leak=1"})
        t2 = service.register_stream_token(kind="remote", url="https://a.test/2.mp3")
        t3 = service.register_stream_token(kind="remote", url="https://a.test/3.mp3")
        self.assertNotIn("Cookie", t1.headers)
        self.assertIsNone(service.resolve_stream_token(t1.token))
        self.assertIsNotNone(service.resolve_stream_token(t2.token))
        self.assertIsNotNone(service.resolve_stream_token(t3.token))

        # Manual candidate resolution for loopback custom endpoint with custom provider name
        def custom_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/match":
                return httpx.Response(
                    200,
                    json={
                        "provider": "migu",
                        "url": "http://127.0.0.1:9001/live.mp3",
                        "format": "mp3",
                        "bitrate": 320_000,
                        "title": "夜曲 (Live)",
                        "artists": ["周杰伦"],
                        "duration_ms": 245_000,
                    },
                )
            if request.url.path == "/live.mp3":
                return httpx.Response(206, headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 0-4095/5000000"}, content=b"ID3" + b"\x00" * 4093)
            return httpx.Response(404)

        custom_client = httpx.Client(transport=httpx.MockTransport(custom_handler), follow_redirects=False)
        try:
            custom_svc = AudioService(
                self.client,
                self.catalog,
                external_resolver=ExternalAudioResolver(http_client=custom_client),
                settings_provider=lambda: {
                    "enable_external_sources": True,
                    "external_providers": [],
                    "custom_external_api": "http://127.0.0.1:9001/match",
                },
            )
            cands = custom_svc.candidates(10)
            ext_cand = next(item for item in cands["candidates"] if item["source"] == "external")
            self.assertEqual("migu", ext_cand["source_provider"])
            resolved_custom = custom_svc.resolve(10, candidate_id=ext_cand["candidate_id"])
            self.assertEqual("external", resolved_custom["source"])
            self.assertEqual("migu", resolved_custom["source_provider"])
            self.assertTrue(resolved_custom["url"].startswith("/api/stream/"))
        finally:
            custom_client.close()


class RecommendServiceTestCase(unittest.TestCase):
    def test_reranker_filters_not_interested_spreads_artists_ignores_pruning_and_preserves_exploration(self):
        songs = [
            Song(1, "Blocked", ["ArtistA"]),
            Song(2, "Boosted 1", ["ArtistB"]),
            Song(3, "Boosted 2", ["ArtistB"]),
            Song(4, "Boosted 3", ["ArtistB"]),
            Song(5, "Novel 1", ["ArtistC"]),
            Song(6, "Novel 2", ["ArtistD"]),
            Song(2, "Duplicate", ["ArtistB"]),
        ]
        feedback = [
            {"song_id": 1, "action": "not_interested", "context": "{}"},
            {"song_id": 999, "action": "like_explicit", "context": json.dumps({"artists": ["ArtistB"]})},
            # Pruning & skip noise MUST be strictly ignored!
            {"song_id": 5, "action": "unlike", "context": json.dumps({"artists": ["ArtistC"]})},
            {"song_id": 6, "action": "skip", "context": json.dumps({"artists": ["ArtistD"]})},
            {"song_id": 5, "action": "prune", "context": "{}"},
        ]
        ranked = rerank_recommendations(songs, feedback, "daily")
        ids = [item["id"] for item in ranked]
        self.assertNotIn(1, ids)
        self.assertEqual(len(ids), len(set(ids)))  # deduplicated
        self.assertIn(5, ids)  # pruning/unlike did NOT filter out song 5
        self.assertIn(6, ids)  # skip did NOT filter out song 6

        # Consecutive same-artist spread: no adjacent ArtistB tracks when other artists exist!
        artists = [item["artists"][0] for item in ranked]
        for idx in range(len(artists) - 1):
            if artists[idx] == "ArtistB":
                self.assertNotEqual("ArtistB", artists[idx + 1], f"adjacent ArtistB in {artists}")

        # Honest recommend_reason only references real explicit feedback used
        by_id = {item["id"]: item for item in ranked}
        self.assertIn("主动点赞", by_id[2]["recommend_reason"])
        self.assertEqual("网易云每日推荐", by_id[5]["recommend_reason"])
        self.assertNotIn("删", by_id[5]["recommend_reason"])

    def test_fault_isolation_and_fm_empty_retry_cap(self):
        fake = FakeTransport()
        client = NeteaseClient(fake, cookie="MUSIC_U=tok")
        service = RecommendService(client)

        fake.handlers["/recommend/songs"] = lambda *_a: (_ for _ in ()).throw(UpstreamError("UPSTREAM_UNAVAILABLE", "down"))
        daily_degraded = service.daily("42")
        self.assertTrue(daily_degraded["degraded"])
        self.assertEqual([], daily_degraded["songs"])
        self.assertEqual("daily", daily_degraded["source"])

        fm_calls = 0

        def empty_fm(*_a):
            nonlocal fm_calls
            fm_calls += 1
            return {"code": 200, "data": []}

        fake.handlers["/personal_fm"] = empty_fm
        fm_degraded = service.personal_fm("42")
        self.assertTrue(fm_degraded["degraded"])
        # Initial attempt + at most 2 retries = 3 calls total
        self.assertEqual(3, fm_calls)

    def test_recommend_db_feedback_lookup_intelligence_similar_and_fm_concurrency_guard(self):
        class FakeDb:
            def list_feedback(self, account_id: str, limit: int = 2000):
                return [
                    {"id": 1, "account_id": account_id, "song_id": 500, "action": "replay", "context": ""},
                    {"id": 2, "account_id": account_id, "song_id": 901, "action": "not_interested", "context": ""},
                ]

            def get_song(self, song_id: int, account_id: str | None = None):
                if song_id == 500:
                    return Song(500, "Old Fav", ["FavArtist"])
                return None

        fake = FakeTransport()
        client = NeteaseClient(fake, cookie="MUSIC_U=tok")
        service = RecommendService(client, db=FakeDb())

        fake.handlers["/playmode/intelligence/list"] = {
            "code": 200,
            "data": [
                {"songInfo": {"id": 901, "name": "SkipMe", "ar": [{"name": "X"}], "dt": 180_000}},
                {"songInfo": {"id": 902, "name": "Other", "ar": [{"name": "Y"}], "dt": 180_000}},
                {"songInfo": {"id": 903, "name": "Boosted", "ar": [{"name": "FavArtist"}], "dt": 180_000}},
            ],
        }
        intel = service.intelligence(1, 10, "42")
        self.assertFalse(intel["degraded"])
        self.assertEqual([903, 902], [s["id"] for s in intel["songs"]])
        self.assertIn("重播", intel["songs"][0]["recommend_reason"])

        fake.handlers["/simi/song"] = {
            "code": 200,
            "songs": [{"id": 904, "name": "Simi", "ar": [{"name": "Z"}], "dt": 180_000}],
        }
        simi = service.similar(1, "42")
        self.assertEqual("与当前歌曲相似", simi["songs"][0]["recommend_reason"])

        # Concurrency guard on personal_fm serializes overlapping calls
        active = 0
        max_active = 0
        lock = threading.Lock()

        def slow_fm(*_a):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"code": 200, "data": [{"id": 905, "name": "FM", "ar": [{"name": "M"}], "dt": 180_000}]}

        fake.handlers["/personal_fm"] = slow_fm
        results: list[dict[str, Any]] = []
        t1 = threading.Thread(target=lambda: results.append(service.personal_fm("42")))
        t2 = threading.Thread(target=lambda: results.append(service.personal_fm("42")))
        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(2, len(results))
        self.assertEqual(1, max_active)

    def test_real_sqlite_db_and_settings_store_integration(self):
        from pruner_studio.db import CatalogDatabase
        from pruner_studio.models import PlaylistSummary
        from pruner_studio.storage import SettingsStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = CatalogDatabase(root)
            store = JsonStore(root)
            settings_store = SettingsStore(store)
            music_dir = root / "lossless"
            music_dir.mkdir()
            _make_minimal_flac(music_dir / "track.flac", title="稻香", artist="周杰伦", duration_sec=223)
            settings_store.save({"local_music_dirs": [str(music_dir)], "external_providers": []})

            # Scan and persist local files into real SQLite DB, then reload into a fresh resolver!
            scanner = LocalLibraryScanner(db=db)
            scan_out = scanner.scan([music_dir])
            self.assertEqual(1, scan_out["scanned_files"])
            reloaded_resolver = ExternalAudioResolver(db=db)
            local_match = reloaded_resolver.match_local(Song(1, "稻香", ["周杰伦"], duration=223_000))
            self.assertIsNotNone(local_match)
            self.assertEqual("flac", local_match.format)

            # Seed songs + explicit feedback into real SQLite DB and verify RecommendService + AudioService
            db.replace_playlist_tracks(
                "42",
                PlaylistSummary(10, "Liked", 1, "liked", "public"),
                [Song(50, "稻香", ["周杰伦"], duration=223_000)],
            )
            db.add_feedback("42", 50, "replay", "")
            db.add_feedback("42", 99, "not_interested", "")

            fake = FakeTransport()
            fake.handlers["/recommend/songs"] = {
                "code": 200,
                "data": {
                    "dailySongs": [
                        {"id": 99, "name": "Hide", "ar": [{"name": "Other"}], "dt": 180_000},
                        {"id": 100, "name": "OtherSong", "ar": [{"name": "Other"}], "dt": 180_000},
                        {"id": 101, "name": "七里香", "ar": [{"name": "周杰伦"}], "dt": 299_000},
                    ]
                },
            }
            rec = RecommendService(NeteaseClient(fake, cookie="MUSIC_U=tok"), db=db)
            daily = rec.daily("42")
            self.assertEqual([101, 100], [s["id"] for s in daily["songs"]])
            self.assertIn("重播", daily["songs"][0]["recommend_reason"])

            # Verify AudioService works with SettingsStore directly (via .load()) & empty external_providers
            fake.handlers["/song/url/v1"] = {"code": 200, "data": [{"id": 50, "url": None}]}
            fake.handlers["/user/cloud"] = {"code": 200, "data": []}
            fake.handlers["/cloudsearch"] = {"code": 200, "result": {"songs": []}}
            audio_svc = AudioService(
                NeteaseClient(fake, cookie="MUSIC_U=tok"),
                CatalogService(store, NeteaseClient(fake)),
                settings_provider=settings_store,
                external_resolver=reloaded_resolver,
                db=db,
            )
            # Scoped catalog reads require selecting the owner of the imported playlist.
            audio_svc.catalog.set_active_account("42")
            resolved = audio_svc.resolve(50)
            self.assertEqual("local", resolved["source"])
            self.assertEqual("flac", resolved["format"])


class Wave3MultiChannelAndArbitrationTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("pruner_studio.external_sources._resolve_addresses",
                                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))

    def test_score_audio_quality_objective_ordering(self):
        s_master = score_audio_quality("flac", "jymaster", 1_920_000, 96_000, 24)
        s_hires = score_audio_quality("flac", "hires", 1_600_000, 96_000, 24)
        s_flac = score_audio_quality("flac", "lossless", 999_000, 44_100, 16)
        s_exhigh = score_audio_quality("mp3", "exhigh", 320_000, 44_100, 16)
        s_std = score_audio_quality("mp3", "standard", 128_000, 44_100, 16)
        self.assertGreater(s_master, s_hires)
        self.assertGreater(s_hires, s_flac)
        self.assertGreater(s_flac, s_exhigh + 200)
        self.assertGreater(s_exhigh, s_std)

    def test_strict_per_channel_credential_isolation_and_redirect_stripping(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = ChannelCredentialVault(JsonStore(Path(tmp)))
            vault.update_channel(
                "kugou_lite",
                {
                    "enabled": True,
                    "api_base": "http://127.0.0.1:9101",
                    "token": "kg-secret-token",
                    "userid": "8888",
                    "dfid": "kg-dfid-1",
                    "cookie": "kg_cookie=kg-secret-cookie",
                },
            )
            vault.update_channel(
                "migu",
                {"enabled": True, "cookie": "migu_secret=mg-only"},
            )
            vault.update_channel(
                "qq",
                {"enabled": True, "cookie": "qq_secret=qq-only", "uin": "12345"},
            )
            vault.update_channel(
                "bilibili",
                {"enabled": True, "sessdata": "bili-sess-secret"},
            )

            seen: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                host = request.headers["host"].split(":")[0]
                cookie = request.headers.get("cookie") or ""
                # NEVER cross-leak any channel credential to a foreign host!
                if host != "127.0.0.1":
                    self.assertNotIn("kg-secret", cookie)
                    self.assertNotIn("kg-secret", str(request.url))
                if not host.endswith("migu.cn"):
                    self.assertNotIn("mg-only", cookie)
                if not host.endswith("qq.com"):
                    self.assertNotIn("qq-only", cookie)
                if not host.endswith("bilibili.com"):
                    self.assertNotIn("bili-sess-secret", cookie)

                if host == "127.0.0.1" and request.url.path == "/search":
                    self.assertIn("kg-secret-cookie", cookie)
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "lists": [
                                    {
                                        "FileHash": "HASH_KG_1",
                                        "SQFileHash": "HASH_KG_SQ",
                                        "SongName": "晴天",
                                        "SingerName": "周杰伦",
                                        "AlbumName": "叶惠美",
                                        "Duration": 269,
                                    }
                                ]
                            }
                        },
                    )
                if host == "127.0.0.1" and request.url.path == "/song/url":
                    self.assertEqual("kg-secret-token", request.url.params.get("token"))
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "url": "https://cdn.kugou-audio.test/qingtian.flac",
                                "extName": "flac",
                                "bitRate": 1024,
                                "sampleRate": 48_000,
                                "timeLength": 269,
                            }
                        },
                    )
                if host == "cdn.kugou-audio.test":
                    self.assertIsNone(request.headers.get("cookie"))
                    return httpx.Response(
                        206,
                        headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/35000000"},
                        content=b"fLaC" + b"\x00" * 4092,
                    )
                if host == "api.bilibili.com" and request.url.path == "/x/web-interface/search/type":
                    self.assertIn("SESSDATA=bili-sess-secret", cookie)
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "result": [
                                    {"bvid": "BV1hires", "title": "周杰伦 - 晴天", "author": "Official", "duration": "04:29"}
                                ]
                            }
                        },
                    )
                if host == "api.bilibili.com" and request.url.path == "/x/player/pagelist":
                    return httpx.Response(200, json={"data": [{"cid": 777}]})
                if host == "api.bilibili.com" and request.url.path == "/x/player/playurl":
                    self.assertEqual("4048", request.url.params.get("fnval"))
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "dash": {
                                    "flac": {
                                        "audio": {
                                            "id": 30251,
                                            "baseUrl": "https://upos.bilivideo.test/hires.flac",
                                            "bandwidth": 1_800_000,
                                        }
                                    }
                                }
                            }
                        },
                    )
                if host == "upos.bilivideo.test":
                    # SESSDATA must NEVER reach Bilibili CDN!
                    self.assertIsNone(request.headers.get("cookie"))
                    return httpx.Response(
                        206,
                        headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/42000000"},
                        content=b"fLaC" + b"\x00" * 4092,
                    )
                if host == "music-api.gdstudio.xyz":
                    self.assertIsNone(request.headers.get("cookie"))
                    return httpx.Response(200, json=[])
                if host == "pd.musicapp.migu.cn":
                    self.assertIn("migu_secret=mg-only", cookie)
                    return httpx.Response(200, json={"songResultData": {"result": []}})
                if host == "u.y.qq.com":
                    self.assertIn("qq_secret=qq-only", cookie)
                    return httpx.Response(200, json={"req": {"data": {"body": {"song": {"list": []}}}}})
                return httpx.Response(404)

            client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
            resolver = ExternalAudioResolver(credential_vault=vault, http_client=client)
            try:
                song = Song(id=186016, title="晴天", artists=["周杰伦"], album="叶惠美", duration=269_000)
                kg = resolver.resolve(song, enabled_providers=["kugou_lite"])
                self.assertIsNotNone(kg)
                self.assertEqual("kugou_lite", kg.source_provider)
                self.assertEqual("flac", kg.format)
                self.assertNotIn("Cookie", kg.headers)

                bili = resolver.resolve(song, enabled_providers=["bilibili"])
                self.assertIsNotNone(bili)
                self.assertEqual("bilibili", bili.source_provider)
                self.assertEqual("flac", bili.format)
                self.assertEqual("hires", bili.level)
                self.assertNotIn("Cookie", bili.headers)

                # Multi-channel search deduplicates & tracks available_channels
                fake_ncm = FakeTransport()
                fake_ncm.handlers["/cloudsearch"] = {
                    "code": 200,
                    "result": {
                        "songs": [
                            {"id": 186016, "name": "晴天", "ar": [{"name": "周杰伦"}], "al": {"name": "叶惠美"}, "dt": 269_000, "fee": 1, "privilege": {"st": 0}}
                        ]
                    },
                }
                multi = resolver.search_multi_channel(
                    "晴天 周杰伦",
                    channels=["netease", "kugou_lite", "migu", "qq", "gdstudio"],
                    netease_client=NeteaseClient(fake_ncm),
                )
                self.assertEqual(1, len(multi["tracks"]))
                self.assertEqual(186016, multi["tracks"][0]["id"])
                self.assertIn("netease", multi["tracks"][0]["available_channels"])
                self.assertIn("kugou_lite", multi["tracks"][0]["available_channels"])

                # test_channel NEVER leaks raw secret values!
                diag = resolver.test_channel("kugou_lite")
                self.assertTrue(diag["ok"])
                self.assertNotIn("kg-secret", json.dumps(diag))
            finally:
                client.close()

    def test_best_quality_race_upgrades_lossy_netease_and_skips_race_when_already_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonStore(root)
            vault = ChannelCredentialVault(store)
            vault.update_channel("migu", {"enabled": True})

            fake_ncm = FakeTransport()
            ncm_client = NeteaseClient(fake_ncm, cookie="MUSIC_U=std-user")
            catalog = CatalogService(store, ncm_client)
            catalog.import_content(
                json.dumps([{"id": 100, "title": "稻香", "artists": ["周杰伦"], "album": "魔杰座", "duration": 223_000}]),
                "json",
            )

            migu_hits = 0

            def ext_handler(request: httpx.Request) -> httpx.Response:
                nonlocal migu_hits
                self.assertNotIn("std-user", request.headers.get("cookie") or "")
                if request.headers["host"].split(":")[0] == "pd.musicapp.migu.cn":
                    migu_hits += 1
                    return httpx.Response(
                        200,
                        json={
                            "songResultData": {
                                "result": [
                                    {
                                        "copyrightId": "MG_DX_1",
                                        "contentId": "MG_C_1",
                                        "name": "稻香",
                                        "singers": [{"name": "周杰伦"}],
                                        "albums": [{"name": "魔杰座"}],
                                        "duration": 223,
                                    }
                                ]
                            }
                        },
                    )
                if request.headers["host"].split(":")[0] == "app.pd.nf.migu.cn":
                    return httpx.Response(
                        200,
                        json={"url": "https://cdn.migu.test/daoxiang.flac", "format": "flac", "bitrate": 1_800_000, "sample_rate": 96_000},
                    )
                if request.headers["host"].split(":")[0] == "cdn.migu.test":
                    return httpx.Response(
                        206,
                        headers={"Content-Type": "audio/flac", "Content-Range": "bytes 0-4095/40000000"},
                        content=b"fLaC" + b"\x00" * 4092,
                    )
                return httpx.Response(404)

            mock_http = httpx.Client(transport=httpx.MockTransport(ext_handler), follow_redirects=False)
            resolver = ExternalAudioResolver(credential_vault=vault, http_client=mock_http)
            try:
                # 1. NetEase already returns verified lossless -> 0 external calls!
                fake_ncm.handlers["/song/url/v1"] = {
                    "code": 200,
                    "data": [{"id": 100, "url": "https://cdn.ncm/100.flac", "level": "jymaster", "type": "flac", "br": 1_900_000, "sr": 96_000}],
                }
                svc = AudioService(
                    ncm_client,
                    catalog,
                    external_resolver=resolver,
                    credential_vault=vault,
                    settings_provider=lambda: {
                        "quality_arbitration": "best_quality_race",
                        "enable_external_sources": True,
                        "external_providers": ["migu"],
                    },
                )
                fast = svc.resolve(100)
                self.assertEqual("original", fast["source"])
                self.assertEqual(0, migu_hits)

                # 2. NetEase only yields 128k lossy MP3 -> best_quality_race upgrades to Migu Hi-Res FLAC!
                svc.clear_caches()
                fake_ncm.handlers["/song/url/v1"] = {
                    "code": 200,
                    "data": [{"id": 100, "url": "https://cdn.ncm/100.mp3", "level": "standard", "type": "mp3", "br": 128_000}],
                }
                fake_ncm.handlers["/user/cloud"] = {"code": 200, "data": []}
                upgraded = svc.resolve(100)
                self.assertEqual("external", upgraded["source"])
                self.assertEqual("migu", upgraded["source_provider"])
                self.assertEqual("flac", upgraded["format"])
                self.assertIn("quality_upgraded_from", upgraded)
                self.assertEqual(128_000, upgraded["quality_upgraded_from"]["bitrate"])
                self.assertGreater(migu_hits, 0)

                # 3. netease_first keeps NetEase original without external race!
                svc.clear_caches()
                prev_hits = migu_hits
                kept = svc.resolve(100, quality_arbitration="netease_first")
                self.assertEqual("original", kept["source"])
                self.assertEqual(prev_hits, migu_hits)

                # 4. Direct cross-channel playback via resolve_external_track (song_id=0)
                direct = svc.resolve_external_track(
                    provider="migu",
                    external_id="MG_DX_1",
                    title="稻香",
                    artists=["周杰伦"],
                    duration_ms=223_000,
                    song_id=0,
                )
                self.assertEqual("external", direct["source"])
                self.assertEqual("migu", direct["source_provider"])
                self.assertTrue(direct["url"].startswith("/api/stream/"))
                target = svc.resolve_stream_token(direct["stream_token"])
                self.assertIsNotNone(target)
                self.assertFalse(target.allow_cookies)
            finally:
                mock_http.close()


class Wave3ShuffleSmartRadioAndAmllTestCase(unittest.TestCase):
    def test_fiedler_balanced_shuffle_spreads_artists_and_applies_recency_cooldown(self):
        now = 1_750_000_000.0
        tracks: list[dict[str, Any]] = []
        # 9 tracks by DominantArtist + 11 tracks by diverse artists (20 total, max artist <= 10 -> zero adjacent possible!)
        for idx in range(1, 10):
            tracks.append({"id": idx, "title": f"Dom {idx}", "artists": ["DominantArtist"], "album": f"Alb{idx % 3}"})
        for idx in range(10, 21):
            tracks.append({"id": idx, "title": f"Other {idx}", "artists": [f"Artist{idx % 5}"], "album": "Misc"})

        # Mark tracks 1..5 as played 1 day ago (cooldown) and tracks 6..15 as dusty/unplayed (>45d or never)
        play_history = {
            sid: {"play_count": 3, "last_played_at": now - 1 * 86_400}
            for sid in range(1, 6)
        }
        for sid in range(6, 12):
            play_history[sid] = {"play_count": 1, "last_played_at": now - 45 * 86_400}

        shuffled = balanced_shuffle(tracks, play_history, seed=42, now_ts=now)
        self.assertEqual(len(tracks), len(shuffled))
        self.assertEqual({t["id"] for t in tracks}, {t["id"] for t in shuffled})
        # Deterministic with same seed
        self.assertEqual([t["id"] for t in shuffled], [t["id"] for t in balanced_shuffle(tracks, play_history, seed=42, now_ts=now)])

        # Zero back-to-back tracks by the same primary artist!
        for i in range(1, len(shuffled)):
            self.assertNotEqual(
                shuffled[i - 1]["artists"][0],
                shuffled[i]["artists"][0],
                f"adjacent artist collision at {i}: {[t['artists'][0] for t in shuffled]}",
            )

        # Cooldown (<7d) tracks should have a higher average index than Dusty Gems (>30d / unplayed)
        pos_by_id = {t["id"]: idx for idx, t in enumerate(shuffled)}
        avg_cooldown = sum(pos_by_id[sid] for sid in range(1, 6)) / 5.0
        avg_dusty = sum(pos_by_id[sid] for sid in range(6, 12)) / 6.0
        self.assertGreater(avg_cooldown, avg_dusty)
        self.assertEqual("cooldown", recency_bucket(play_history[1], now_ts=now))
        self.assertEqual("dusty", recency_bucket(play_history[6], now_ts=now))
        self.assertEqual("unplayed", recency_bucket(None, now_ts=now))

    def test_smart_radio_4way_intersection_noise_filter_and_dusty_gems_with_real_db(self):
        from pruner_studio.db import CatalogDatabase
        from pruner_studio.models import PlaylistSummary

        with tempfile.TemporaryDirectory() as tmp:
            db = CatalogDatabase(Path(tmp))
            catalog_songs = [
                Song(1, "Dusty 1", ["ArtistA"], duration=200_000, availability="available"),
                Song(2, "Dusty 2", ["ArtistB"], duration=200_000, availability="available"),
                Song(3, "Dusty 3", ["ArtistC"], duration=200_000, availability="available"),
                Song(4, "Fresh Fav", ["ArtistD"], duration=200_000, availability="available"),
                Song(5, "Recent Cool", ["ArtistE"], duration=200_000, availability="available"),
                Song(6, "Blocked Track", ["ArtistF"], duration=200_000, availability="available"),
            ]
            db.replace_playlist_tracks("42", PlaylistSummary(10, "Liked", len(catalog_songs), "liked", "public"), catalog_songs)
            # Record plays: song 5 played now (cooldown), song 4 played 15d ago (fresh), song 1 played 45d ago (dusty)
            db.record_play("42", catalog_songs[4].to_dict(), listened_ms=180_000, completed=True)
            db.record_play("42", catalog_songs[3].to_dict(), listened_ms=180_000, completed=True)
            db.record_play("42", catalog_songs[0].to_dict(), listened_ms=180_000, completed=True)
            with db._connect() as conn:
                conn.execute(
                    "UPDATE play_history SET last_played_at = '2026-10-01T00:00:00+00:00' WHERE account_id = '42' AND song_id = 1"
                )
                conn.execute(
                    "UPDATE play_history SET last_played_at = '2026-11-05T00:00:00+00:00' WHERE account_id = '42' AND song_id = 4"
                )
            db.add_feedback("42", 6, "not_interested", "")

            fake = FakeTransport()

            def simi_handler(params, *_args):
                # Song 901 appears as similar for EVERY seed -> multi-seed intersection!
                # Song 902 is DJ/cover noise -> must be filtered out!
                return {
                    "code": 200,
                    "songs": [
                        {"id": 901, "name": "Shared Gem", "ar": [{"name": "ArtistX"}], "dt": 210_000},
                        {"id": 902, "name": "Shared Gem (DJ版 慢摇)", "ar": [{"name": "DJ"}], "dt": 210_000},
                        {"id": 903 + int(params["id"]), "name": f"Simi {params['id']}", "ar": [{"name": f"SimArt{params['id']}"}], "dt": 200_000},
                    ],
                }

            fake.handlers["/simi/song"] = simi_handler
            fake.handlers["/recommend/songs"] = {
                "code": 200,
                "data": {"dailySongs": [{"id": 950, "name": "Explore Track", "ar": [{"name": "ArtistZ"}], "dt": 200_000}]},
            }
            fake.handlers["/personal_fm"] = {
                "code": 200,
                "data": [{"id": 951, "name": "FM Explore", "ar": [{"name": "ArtistW"}], "dt": 200_000}],
            }

            now_ts = 1_795_200_000.0  # ~2026-11-21
            service = RecommendService(NeteaseClient(fake, cookie="MUSIC_U=tok"), db=db)

            gems = service.dusty_gems("42", limit=10, now_ts=now_ts, seed=7)
            gem_ids = {s["id"] for s in gems["songs"]}
            self.assertEqual({1, 2, 3}, gem_ids)  # excludes fresh(4), cooldown(5), not_interested(6)!
            by_gem = {s["id"]: s for s in gems["songs"]}
            self.assertEqual("50 天未听 · 曾播 1 次", by_gem[1]["recommend_reason"])
            self.assertEqual("尚未播放 · 来自「Liked」", by_gem[2]["recommend_reason"])

            radio = service.smart_radio("42", explore_ratio=0.4, exclude_ids={3}, limit=10, now_ts=now_ts, seed=7)
            self.assertFalse(radio["degraded"])
            radio_ids = {s["id"] for s in radio["songs"]}
            self.assertNotIn(3, radio_ids)    # exclude_ids honored
            self.assertNotIn(6, radio_ids)    # not_interested honored
            self.assertNotIn(902, radio_ids)  # DJ/cover noise filtered out
            self.assertIn(901, radio_ids)     # multi-seed intersection included
            by_radio = {s["id"]: s for s in radio["songs"]}
            self.assertEqual("多首红心共同相似", by_radio[901]["recommend_reason"])
            self.assertGreaterEqual(radio["breakdown"]["dusty_gems"], 1)
            self.assertGreaterEqual(radio["breakdown"]["multi_seed_similar"], 1)

            # Offline / upstream error fault isolation: local Smart Radio still returns tracks!
            fake.handlers["/simi/song"] = lambda *_a: (_ for _ in ()).throw(UpstreamError("UPSTREAM_UNAVAILABLE", "offline"))
            fake.handlers["/recommend/songs"] = lambda *_a: (_ for _ in ()).throw(UpstreamError("UPSTREAM_UNAVAILABLE", "offline"))
            fake.handlers["/personal_fm"] = lambda *_a: (_ for _ in ()).throw(UpstreamError("UPSTREAM_UNAVAILABLE", "offline"))
            offline_radio = service.smart_radio("42", explore_ratio=0.4, limit=10, now_ts=now_ts, seed=7)
            self.assertFalse(offline_radio["degraded"])
            self.assertGreaterEqual(len(offline_radio["songs"]), 3)

    def test_amll_ttml_parser_xxe_guard_and_zero_cookie_lru_fetch(self):
        clear_amll_cache()
        sample_ttml = """<?xml version="1.0" encoding="UTF-8"?>
        <tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata">
          <body><div>
            <p begin="00:01.200" end="00:03.500">
              <span begin="00:01.200" end="00:02.000">Hello</span> <span begin="00:02.100" end="00:03.500">world</span>
              <span ttm:role="x-translation">你好世界</span>
              <span ttm:role="x-roman">he lo wor ld</span>
            </p>
          </div></body>
        </tt>
        """
        parsed = parse_ttml_to_yrc(sample_ttml)
        self.assertIsNotNone(parsed)
        self.assertIn("[1200,2300](1200,800,0)Hello (2100,1400,0)world", parsed["yrc"])
        self.assertIn("[00:01.20]Hello world", parsed["lrc"])
        self.assertIn("[00:01.20]你好世界", parsed["tlyrics"])
        self.assertIn("[00:01.20]he lo wor ld", parsed["romalrc"])

        # DTD / XXE payload MUST be rejected!
        xxe_payload = '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><tt><body><p begin="0s">&xxe;</p></body></tt>'
        with self.assertRaises(ValueError):
            parse_ttml_to_yrc(xxe_payload)

        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertIsNone(request.headers.get("cookie"))
            if request.url.path.endswith("/123456.ttml"):
                return httpx.Response(200, text=sample_ttml)
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            first = fetch_amll_ttml_as_yrc(123456, http_client=mock_client)
            self.assertIsNotNone(first)
            self.assertEqual("amll-ttml-db", first["ttml_source"])
            # Cached second call does not hit network again!
            second = fetch_amll_ttml_as_yrc(123456, http_client=mock_client)
            self.assertEqual(first, second)
            self.assertEqual(1, calls)
            # 404 miss is also cached
            self.assertIsNone(fetch_amll_ttml_as_yrc(999999, http_client=mock_client))
            self.assertIsNone(fetch_amll_ttml_as_yrc(999999, http_client=mock_client))
            self.assertEqual(2, calls)
        finally:
            mock_client.close()
            clear_amll_cache()


class Wave4PaginatedMultiSearchTestCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("pruner_studio.external_sources._resolve_addresses",
                                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))

    def test_unthrottled_netease_limit_100_offset_and_has_more(self):
        fake = FakeTransport()

        def cloudsearch_handler(params, _method, _cookie):
            limit = int(params.get("limit", 30))
            offset = int(params.get("offset", 0))
            if offset == 0:
                songs = [
                    {"id": 1000 + i, "name": f"Track {i}", "ar": [{"name": "Artist"}], "al": {"name": "Alb"}, "dt": 200_000}
                    for i in range(limit)
                ]
                return {"code": 200, "result": {"songs": songs, "songCount": 140, "hasMore": True}}
            songs = [
                {"id": 2000 + i, "name": f"Page2 Track {i}", "ar": [{"name": "Artist"}], "al": {"name": "Alb"}, "dt": 200_000}
                for i in range(40)
            ]
            return {"code": 200, "result": {"songs": songs, "songCount": 140, "hasMore": False}}

        fake.handlers["/cloudsearch"] = cloudsearch_handler
        ncm = NeteaseClient(fake)
        resolver = ExternalAudioResolver()

        page1 = resolver.search_multi_channel("Artist", channels=["netease"], limit=100, offset=0, netease_client=ncm)
        self.assertEqual(100, len(page1["tracks"]))
        self.assertEqual(100, page1["limit"])
        self.assertEqual(0, page1["offset"])
        self.assertTrue(page1["has_more"])
        # Confirm NetEase was called with limit=100 (NOT capped at 20!)
        self.assertEqual(100, fake.calls[0][1]["limit"])

        page2 = resolver.search_multi_channel("Artist", channels=["netease"], limit=100, offset=100, netease_client=ncm)
        self.assertEqual(40, len(page2["tracks"]))
        self.assertEqual(100, page2["offset"])
        self.assertFalse(page2["has_more"])
        self.assertEqual(100, fake.calls[1][1]["offset"])

    def test_external_search_mode_keeps_unmatched_titles_and_pages_natively_with_credential_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = ChannelCredentialVault(JsonStore(Path(tmp)))
            vault.update_channel("kugou_lite", {"enabled": True, "cookie": "kg=only-kugou", "token": "kg-tok"})
            vault.update_channel("migu", {"enabled": True, "cookie": "mg=only-migu"})
            vault.update_channel("qq", {"enabled": True, "cookie": "qq=only-qq"})
            vault.update_channel("bilibili", {"enabled": True, "sessdata": "bili-only-sess"})

            observed_params: dict[str, dict[str, Any]] = {}

            def handler(request: httpx.Request) -> httpx.Response:
                host = request.headers["host"].split(":")[0]
                cookie = request.headers.get("cookie") or ""
                if not host.endswith("kugou.com"):
                    self.assertNotIn("only-kugou", cookie)
                if not host.endswith("migu.cn"):
                    self.assertNotIn("only-migu", cookie)
                if not host.endswith("qq.com"):
                    self.assertNotIn("only-qq", cookie)
                if not host.endswith("bilibili.com"):
                    self.assertNotIn("bili-only-sess", cookie)

                if host == "songsearch.kugou.com":
                    self.assertIn("only-kugou", cookie)
                    observed_params["kugou_lite"] = dict(request.url.params)
                    # Searching artist name "周杰伦" returns songs whose titles do NOT equal "周杰伦"!
                    return httpx.Response(
                        200,
                        json={
                            "data": {
                                "total": 120,
                                "lists": [
                                    {"FileHash": "KG1", "SongName": "晴天", "SingerName": "周杰伦", "AlbumName": "叶惠美", "Duration": 269},
                                    {"FileHash": "KG2", "SongName": "稻香", "SingerName": "周杰伦", "AlbumName": "魔杰座", "Duration": 223},
                                    {"FileHash": "KG3", "SongName": "七里香 (Live)", "SingerName": "周杰伦", "AlbumName": "演唱会", "Duration": 310},
                                ],
                            }
                        },
                    )
                if host == "pd.musicapp.migu.cn":
                    self.assertIn("only-migu", cookie)
                    observed_params["migu"] = dict(request.url.params)
                    return httpx.Response(
                        200,
                        json={
                            "songResultData": {
                                "totalCount": 5,
                                "result": [
                                    {"copyrightId": "MG1", "name": "夜曲", "singers": [{"name": "周杰伦"}], "albums": [{"name": "十一月的萧邦"}], "duration": 226},
                                ],
                            }
                        },
                    )
                if host == "u.y.qq.com":
                    self.assertIn("only-qq", cookie)
                    observed_params["qq"] = json.loads(request.content.decode("utf-8"))
                    return httpx.Response(
                        200,
                        json={
                            "req": {
                                "data": {
                                    "body": {
                                        "song": {
                                            "totalnum": 4,
                                            "list": [
                                                {"mid": "QQ1", "name": "花海", "singer": [{"name": "周杰伦"}], "album": {"name": "魔杰座"}, "interval": 264}
                                            ],
                                        }
                                    }
                                }
                            }
                        },
                    )
                if host == "music-api.gdstudio.xyz":
                    self.assertIsNone(request.headers.get("cookie"))
                    return httpx.Response(200, json=[{"id": "GD1", "name": "搁浅", "artist": ["周杰伦"], "album": "七里香"}])
                return httpx.Response(404)

            mock_client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
            resolver = ExternalAudioResolver(credential_vault=vault, http_client=mock_client)
            try:
                res = resolver.search_multi_channel(
                    "周杰伦",
                    channels=["kugou_lite", "migu", "qq", "gdstudio"],
                    limit=50,
                    offset=50,
                )
                titles = {t["title"] for t in res["tracks"]}
                # None of these titles equal "周杰伦" — search mode MUST preserve ALL of them!
                self.assertTrue({"晴天", "稻香", "七里香 (Live)", "夜曲", "花海", "搁浅"}.issubset(titles))
                # Verify native page=2 calculated for offset=50, per_channel_limit=50
                self.assertEqual("2", observed_params["kugou_lite"]["page"])
                self.assertEqual("50", observed_params["kugou_lite"]["pagesize"])
                self.assertEqual("2", observed_params["migu"]["pageNo"])
                self.assertEqual(2, observed_params["qq"]["req"]["param"]["page_num"])
                # KuGou total=120 > 50+3 -> has_more is True!
                self.assertTrue(res["has_more"])
            finally:
                mock_client.close()

    def test_dedup_3s_tolerance_relevance_interleaving_and_shared_module_executor(self):
        import pruner_studio.external_sources as ext_mod

        fake = FakeTransport()
        fake.handlers["/cloudsearch"] = {
            "code": 200,
            "result": {
                "hasMore": False,
                "songs": [
                    # Rank 0: matches KuGou rank 0 within ±3s (269s vs 271s -> merged!)
                    {"id": 101, "name": "晴天", "ar": [{"name": "周杰伦"}], "al": {"name": "叶惠美"}, "dt": 269_000},
                    # Rank 1: NetEase-only song
                    {"id": 102, "name": "兰亭序", "ar": [{"name": "周杰伦"}], "al": {"name": "魔杰座"}, "dt": 253_000},
                    # Rank 2: same title+artist as KuGou rank 1, but duration differs by 6s (>3s -> NOT merged!)
                    {"id": 103, "name": "稻香", "ar": [{"name": "周杰伦"}], "al": {"name": "魔杰座"}, "dt": 223_000},
                ],
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["host"].split(":")[0] == "songsearch.kugou.com":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "total": 2,
                            "lists": [
                                # Rank 0: within +2s of NetEase 101 -> merges into NetEase 101!
                                {"FileHash": "KG_QT", "SongName": "晴天", "SingerName": "周杰伦", "Duration": 271},
                                # Rank 1: +6s diff vs NetEase 103 (229s vs 223s) -> kept separate!
                                {"FileHash": "KG_DX_EXT", "SongName": "稻香", "SingerName": "周杰伦", "Duration": 229},
                            ],
                        }
                    },
                )
            if request.headers["host"].split(":")[0] == "pd.musicapp.migu.cn":
                return httpx.Response(
                    200,
                    json={
                        "songResultData": {
                            "totalCount": 1,
                            "result": [
                                # Rank 0 on Migu -> MUST interleave ahead of NetEase rank 1 ("兰亭序")!
                                {"copyrightId": "MG_ONLY", "name": "枫", "singers": [{"name": "周杰伦"}], "duration": 275},
                            ],
                        }
                    },
                )
            return httpx.Response(404)

        mock_client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        resolver = ExternalAudioResolver(http_client=mock_client)
        try:
            # Verify shared module-level executor is used (no new ThreadPoolExecutor instantiated!)
            self.assertEqual("pruner-search", ext_mod._SEARCH_EXECUTOR._thread_name_prefix)
            with patch.object(ext_mod, "ThreadPoolExecutor", side_effect=AssertionError("must reuse module _SEARCH_EXECUTOR")):
                res = resolver.search_multi_channel(
                    "周杰伦",
                    channels=["netease", "kugou_lite", "migu"],
                    limit=30,
                    offset=0,
                    netease_client=NeteaseClient(fake),
                )

            tracks = res["tracks"]
            self.assertFalse(res["has_more"])
            # 1. ±3s merge: "晴天" merged (101 + KG_QT), "稻香" NOT merged (223s vs 229s -> 6s > 3s)!
            qingtian = [t for t in tracks if t["title"] == "晴天"]
            self.assertEqual(1, len(qingtian))
            self.assertEqual(101, qingtian[0]["id"])
            self.assertEqual(["netease", "kugou_lite"], qingtian[0]["available_channels"])

            daoxiang = [t for t in tracks if t["title"] == "稻香"]
            self.assertEqual(2, len(daoxiang))

            # 2. Relevance interleaving: rank-0 NetEase ("晴天") then rank-0 Migu ("枫") BEFORE rank-1 NetEase ("兰亭序")!
            titles_order = [t["title"] for t in tracks]
            self.assertEqual("晴天", titles_order[0])
            self.assertEqual("枫", titles_order[1])
            self.assertEqual("兰亭序", titles_order[2])
        finally:
            mock_client.close()


if __name__ == "__main__":
    unittest.main()
