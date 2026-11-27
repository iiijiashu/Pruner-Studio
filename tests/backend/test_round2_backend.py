"""Round 2 Backend regression coverage:
- Phase B: True 0.0..1.0 smart_radio_explore_ratio (pure familiar & pure exploration)
- Phase C.1: Concurrent POST /api/channels/test-all with timeout, capability & SSE broadcast
- Phase C.2: Isolated Bilibili native Web QR login + zero cookie/secret leakage
- Phase C.3: Isolated WebView2 official auth window allowlist, headless fallback & domain cookie scoping
- Phase D: Stub track hydration (like_track, sync chunk retry, POST /api/songs/{id}/hydrate)
"""
from __future__ import annotations

import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from pruner_studio.app import build_application, create_asgi_app
from pruner_studio.channel_auth import (
    BilibiliQrService,
    CHANNEL_AUTH_CONFIG,
    domain_matches_allowlist,
    extract_channel_auth_cookies,
)
from pruner_studio.config import Settings
from pruner_studio.db import is_stub_song
from pruner_studio.desktop import ChannelAuthWindowController
from pruner_studio.models import PlaylistSummary, Song
from pruner_studio.netease_client import NeteaseClient, NeteaseWebTransport, TransportResponse, UrllibTransport
from pruner_studio.storage import SUPPORTED_CHANNELS
from test_backend import FakeTransport, account_payload, api_song, playlist_payload


class Round2BackendTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.transport = FakeTransport()
        self.app = build_application(Settings(self.root, self.root), self.transport)
        self.db = self.app.db
        self.asgi = create_asgi_app(self.app, 8765)
        self.http = self.enterContext(TestClient(self.asgi, base_url="http://127.0.0.1:8765"))

    def login(self):
        self.transport.handlers["/user/account"] = account_payload()
        self.app.client.set_cookie("MUSIC_U=secret-netease-cookie-do-not-leak")
        self.http.get("/api/status").raise_for_status()
        self.transport.calls.clear()

    def seed_library(self, count: int = 12, aid: str = "42", kind: str = "liked"):
        songs = [
            Song(i, f"Library Track {i}", [f"Artist {i % 5}"], album="Lib", duration=200_000)
            for i in range(1, count + 1)
        ]
        self.db.replace_playlist_tracks(aid, PlaylistSummary(10, "Liked", count, kind, "private"), songs)

    # ------------------------------------------------------------------
    # Phase B: 0.0..1.0 smart_radio_explore_ratio
    # ------------------------------------------------------------------

    def test_explore_ratio_settings_accepts_full_0_to_1_and_rejects_out_of_bounds(self):
        for valid in (0.0, 0.05, 0.5, 0.95, 1.0):
            resp = self.http.put("/api/settings", json={"smart_radio_explore_ratio": valid})
            self.assertEqual(200, resp.status_code, resp.text)
            self.assertAlmostEqual(valid, resp.json()["data"]["smart_radio_explore_ratio"], places=3)

        for invalid in (-0.01, -1.0, 1.01, 2.0, True):
            resp = self.http.put("/api/settings", json={"smart_radio_explore_ratio": invalid})
            self.assertEqual(400, resp.status_code)
            self.assertEqual("SETTINGS_INVALID", resp.json()["error"]["code"])

    def test_smart_radio_ratio_zero_is_strictly_familiar_with_zero_online_discovery(self):
        self.login()
        self.seed_library(12, "42", "liked")
        self.transport.handlers["/playmode/intelligence/list"] = lambda *_: {
            "code": 200,
            "data": [{"songInfo": api_song(i, f"Novel {i}")} for i in range(500, 530)],
        }

        resp = self.http.post(
            "/api/recommend/smart-radio",
            json={
                "radio_session_id": "pure-familiar",
                "request_id": "req-0",
                "explore_ratio": 0.0,
                "limit": 10,
            },
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()["data"]
        self.assertEqual(0.0, data["explore_ratio"])
        self.assertEqual(10, len(data["songs"]))
        self.assertEqual(0, data["breakdown"]["novel_count"])
        self.assertEqual(0, data["breakdown"]["similar_discovery"])
        self.assertEqual(0, data["breakdown"]["online_exploration"])
        self.assertEqual(10, data["breakdown"]["library_count"])
        self.assertEqual([], self.transport.calls)

        # GET /api/recommend/smart-radio?explore_ratio=0.0 also stays strictly familiar
        get_resp = self.http.get("/api/recommend/smart-radio?explore_ratio=0&limit=6")
        self.assertEqual(200, get_resp.status_code, get_resp.text)
        get_data = get_resp.json()["data"]
        self.assertEqual(0.0, get_data["explore_ratio"])
        self.assertEqual(0, get_data["breakdown"]["novel_count"])
        self.assertEqual([], self.transport.calls)

    def test_smart_radio_ratio_one_is_pure_exploration_without_familiar_backfill(self):
        self.login()
        self.seed_library(20, "42", "liked")
        # Return ONLY 3 novel tracks + 2 library tracks (ids 1, 2) even though limit=10
        self.transport.handlers["/playmode/intelligence/list"] = {
            "code": 200,
            "data": [
                {"songInfo": api_song(1, "Library Track 1")},
                {"songInfo": api_song(2, "Library Track 2")},
                {"songInfo": api_song(901, "Brand New 1")},
                {"songInfo": api_song(902, "Brand New 2")},
                {"songInfo": api_song(903, "Brand New 3")},
            ],
        }
        for path in ("/simi/song", "/simi/playlist", "/personal_fm", "/recommend/resource"):
            self.transport.handlers[path] = {"code": 200}

        resp = self.http.post(
            "/api/recommend/smart-radio",
            json={
                "radio_session_id": "pure-explore",
                "request_id": "req-1",
                "explore_ratio": 1.0,
                "limit": 10,
            },
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()["data"]
        self.assertEqual(1.0, data["explore_ratio"])
        self.assertEqual({901, 902, 903}, {t["id"] for t in data["songs"]})
        self.assertEqual(0, data["breakdown"]["dusty_gems"])
        self.assertEqual(0, data["breakdown"]["fresh_rotation"])
        self.assertEqual(0, data["breakdown"]["library_count"])
        self.assertEqual(3, data["breakdown"]["novel_count"])
        self.assertEqual("partial", data["status"])
        self.assertTrue(data["reason"])

        # Reject out-of-range explore_ratio on POST /api/recommend/smart-radio
        for bad in (-0.05, 1.05):
            rejected = self.http.post(
                "/api/recommend/smart-radio",
                json={"radio_session_id": "bad", "request_id": f"bad-{bad}", "explore_ratio": bad},
            )
            self.assertEqual(400, rejected.status_code)

    def test_smart_radio_ratio_one_offline_returns_honest_exhausted_without_library_backfill(self):
        self.seed_library(10, "local", "imported")
        resp = self.http.post(
            "/api/recommend/smart-radio",
            json={"radio_session_id": "offline-explore", "request_id": "req-off", "explore_ratio": 1.0, "limit": 10},
        )
        self.assertEqual(200, resp.status_code, resp.text)
        data = resp.json()["data"]
        self.assertEqual([], data["songs"])
        self.assertEqual("exhausted", data["status"])
        self.assertTrue(data["reason"])
        self.assertEqual(0, data["breakdown"]["library_count"])

    # ------------------------------------------------------------------
    # Phase C.1: POST /api/channels/test-all
    # ------------------------------------------------------------------

    def test_channels_test_all_runs_concurrently_records_probes_handles_timeout_and_broadcasts_sse(self):
        self.app.channel_vault.update_channel("bilibili", {"enabled": True, "sessdata": "top-secret-bili-sess"})
        self.app.channel_vault.update_channel("qq", {"enabled": True, "api_base": "http://127.0.0.1:9999"})
        self.app._channel_probe_timeout_sec = 0.25

        active = 0
        peak = 0
        lock = threading.Lock()
        release_kuwo = threading.Event()
        kuwo_finished = threading.Event()

        def fake_test_channel(cid: str, *, custom_api: str = ""):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if cid == "kuwo":
                    release_kuwo.wait(3.0)  # Hang until released after response returns
                    kuwo_finished.set()
                else:
                    time.sleep(0.05)
                if cid == "custom":
                    return {
                        "channel": cid,
                        "ok": False,
                        "latency_ms": 0,
                        "status": "unconfigured",
                        "capability": "unconfigured",
                        "message": "尚未配置自定义外部音源接口地址",
                        "max_quality_hint": "",
                    }
                return {
                    "channel": cid,
                    "ok": cid != "migu",
                    "latency_ms": 42,
                    "status": "connected" if cid != "migu" else "empty",
                    "capability": "",  # Intentionally blank to verify app fills capability
                    "message": "ok" if cid != "migu" else "empty",
                    "max_quality_hint": "",
                }
            finally:
                with lock:
                    active -= 1

        observer = self.app._sync_bus.subscribe()
        try:
            with patch.object(self.app.audio.external_resolver, "test_channel", side_effect=fake_test_channel):
                started = time.monotonic()
                resp = self.http.post("/api/channels/test-all", json={})
                elapsed = time.monotonic() - started
                returned_before_hung = not kuwo_finished.is_set()
        finally:
            release_kuwo.set()
            self.app._sync_bus.unsubscribe(observer)

        self.assertEqual(200, resp.status_code, resp.text)
        self.assertGreaterEqual(peak, 3, "Expected concurrent probing across channels")
        self.assertTrue(returned_before_hung, "Batch probe must return before hung channel completes")
        self.assertLess(elapsed, 1.5, "Batch probe must bound hanging channel by timeout")

        data = resp.json()["data"]
        self.assertEqual(set(SUPPORTED_CHANNELS), set(data["results"].keys()))
        self.assertEqual("timeout", data["results"]["kuwo"]["status"])
        self.assertFalse(data["results"]["kuwo"]["ok"])
        self.assertEqual("authenticated_search", data["results"]["bilibili"]["capability"])
        self.assertEqual("custom_endpoint", data["results"]["qq"]["capability"])
        self.assertEqual("public_search", data["results"]["gdstudio"]["capability"])
        self.assertEqual("unconfigured", data["results"]["custom"]["capability"])

        # Vault recorded probe verification state
        summary_map = {row["channel"]: row for row in data["channels"]}
        self.assertTrue(summary_map["bilibili"]["verified"])
        self.assertFalse(summary_map["migu"]["verified"])
        self.assertFalse(summary_map["kuwo"]["verified"])

        # SSE broadcast emitted with zero secrets
        sse_event = observer.get_nowait()
        self.assertEqual("channels", sse_event["_event_type"])
        self.assertEqual("all", sse_event["channel"])
        self.assertEqual("probed", sse_event["status"])
        self.assertNotIn("top-secret-bili-sess", json.dumps(data) + json.dumps(sse_event))

    # ------------------------------------------------------------------
    # Phase C.2: Bilibili Native Web QR Login
    # ------------------------------------------------------------------

    def test_bilibili_qr_state_machine_dpapi_storage_probe_and_zero_leakage(self):
        self.login()  # Ensure NetEase cookie exists and must NEVER leak to Bilibili!
        outbound_requests: list[httpx.Request] = []
        poll_steps = iter(
            [
                httpx.Response(200, json={"code": 0, "data": {"code": 86101, "message": "未扫码"}}),
                httpx.Response(200, json={"code": 0, "data": {"code": 86090, "message": "已扫码未确认"}}),
                httpx.Response(200, json={"code": 0, "data": {"code": 86038, "message": "二维码已失效"}}),
                httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "code": 0,
                            "url": "https://passport.biligame.com/x/passport-login/web/crossDomain?DedeUserID=9988&bili_jct=csrf_token_xyz",
                        },
                    },
                    headers=[
                        ("Set-Cookie", "SESSDATA=ultra_secret_sessdata_val%2C123; Path=/; Domain=.bilibili.com; HttpOnly"),
                        ("Set-Cookie", "bili_jct=csrf_token_xyz; Path=/; Domain=.bilibili.com"),
                    ],
                ),
            ]
        )

        def mock_bili(request: httpx.Request) -> httpx.Response:
            outbound_requests.append(request)
            # STRICT ZERO-COOKIE ISOLATION: NetEase cookie must NEVER touch Bilibili!
            self.assertNotIn("MUSIC_U", request.headers.get("cookie", ""))
            self.assertNotIn("secret-netease-cookie", request.headers.get("cookie", ""))
            self.assertEqual("passport.bilibili.com", request.url.host)
            if request.url.path.endswith("/qrcode/generate"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "url": "https://passport.bilibili.com/h5-app/passport/login/scan?navhide=1&qrcode_key=bili_qr_key_001",
                            "qrcode_key": "bili_qr_key_001",
                        },
                    },
                )
            if request.url.path.endswith("/qrcode/poll"):
                self.assertEqual("bili_qr_key_001", request.url.params.get("qrcode_key"))
                return next(poll_steps)
            raise AssertionError(str(request.url))

        mock_client = httpx.Client(transport=httpx.MockTransport(mock_bili))
        self.addCleanup(mock_client.close)
        self.app.bilibili_qr = BilibiliQrService(http_client=mock_client)

        created = self.http.post("/api/channels/bilibili/qr/create", json={})
        self.assertEqual(200, created.status_code, created.text)
        create_data = created.json()["data"]
        self.assertEqual("bili_qr_key_001", create_data["qrcode_key"])
        self.assertTrue(create_data["url"].startswith("https://passport.bilibili.com/"))
        self.assertTrue(create_data["qr_image"].startswith("data:image/png;base64,"))

        # waiting -> scanned -> expired
        for expected_state in ("waiting", "scanned", "expired"):
            polled = self.http.get("/api/channels/bilibili/qr/poll?qrcode_key=bili_qr_key_001")
            self.assertEqual(200, polled.status_code, polled.text)
            self.assertEqual(expected_state, polled.json()["data"]["state"])

        # confirmed -> saves to DPAPI vault, clears cache, probes bilibili, broadcasts SSE, zero secret leak
        observer = self.app._sync_bus.subscribe()
        probe_calls: list[str] = []

        def fake_probe(cid: str, *, custom_api: str = ""):
            probe_calls.append(cid)
            return {
                "channel": cid,
                "ok": True,
                "latency_ms": 25,
                "status": "connected",
                "capability": "authenticated_search",
                "message": "搜索可用（命中 3 条结果）",
                "max_quality_hint": "",
            }

        try:
            with patch.object(self.app.audio.external_resolver, "test_channel", side_effect=fake_probe):
                confirmed = self.http.get("/api/channels/bilibili/qr/poll?qrcode_key=bili_qr_key_001")
        finally:
            self.app._sync_bus.unsubscribe(observer)

        self.assertEqual(200, confirmed.status_code, confirmed.text)
        conf_data = confirmed.json()["data"]
        self.assertEqual("confirmed", conf_data["state"])
        self.assertEqual("bilibili", conf_data["channel"])
        self.assertEqual(["bilibili"], probe_calls)
        self.assertTrue(conf_data["probe"]["ok"])

        # Secret stored in vault ONLY (decrypted internally)
        secret = self.app.channel_vault.get_channel_secret("bilibili")
        self.assertEqual("ultra_secret_sessdata_val%2C123", secret["sessdata"])
        self.assertIn("bili_jct=csrf_token_xyz", secret["cookie"])
        self.assertTrue(secret["enabled"])

        # Public summary shows configured + verified, NEVER raw secret
        bili_pub = self.app.channel_vault.get_channel_public("bilibili")
        self.assertTrue(bili_pub["has_credential"])
        self.assertTrue(bili_pub["verified"])

        # SSE broadcast verified + zero secret leakage
        sse_event = observer.get_nowait()
        self.assertEqual("channels", sse_event["_event_type"])
        self.assertEqual("bilibili", sse_event["channel"])
        self.assertEqual("authorized", sse_event["status"])

        leaked_surface = confirmed.text + json.dumps(sse_event) + json.dumps(bili_pub)
        self.assertNotIn("ultra_secret_sessdata_val", leaked_surface)
        self.assertNotIn("csrf_token_xyz", leaked_surface)

        # Invalid qrcode_key rejected with 400
        bad_key = self.http.get("/api/channels/bilibili/qr/poll?qrcode_key=bad%20key!!")
        self.assertEqual(400, bad_key.status_code)
        self.assertEqual("BILIBILI_QR_KEY_INVALID", bad_key.json()["error"]["code"])

    # ------------------------------------------------------------------
    # Phase C.3: Isolated Desktop WebView2 Official Auth Window
    # ------------------------------------------------------------------

    def test_channel_auth_window_allowlist_headless_fallback_and_domain_cookie_isolation(self):
        # Reject channels outside strict allowlist
        for bad_ch in ("gdstudio", "custom", "netease", "unknown", ""):
            resp = self.http.post("/api/desktop/channel-auth-window", json={"channel": bad_ch})
            self.assertEqual(400, resp.status_code, (bad_ch, resp.text))
            self.assertEqual("CHANNEL_AUTH_UNSUPPORTED", resp.json()["error"]["code"])

        # Headless fallback returns clean 200 { supported: false } for all 5 allowlisted channels
        for ch, cfg in CHANNEL_AUTH_CONFIG.items():
            resp = self.http.post("/api/desktop/channel-auth-window", json={"channel": ch})
            self.assertEqual(200, resp.status_code, (ch, resp.text))
            data = resp.json()["data"]
            self.assertFalse(data["supported"])
            self.assertFalse(data["opened"])
            self.assertEqual(ch, data["channel"])
            self.assertEqual(cfg["login_url"], data["login_url"])

        # Domain allowlist strictness
        self.assertTrue(domain_matches_allowlist(".qq.com", ("y.qq.com", "qq.com")))
        self.assertTrue(domain_matches_allowlist("y.qq.com", ("y.qq.com", "qq.com")))
        self.assertFalse(domain_matches_allowlist("evilqq.com", ("y.qq.com", "qq.com")))
        self.assertFalse(domain_matches_allowlist("qq.com.evil.org", ("y.qq.com", "qq.com")))
        self.assertFalse(domain_matches_allowlist("music.163.com", ("y.qq.com", "qq.com")))

        # Cookie extraction rejects foreign domains even with matching cookie names!
        foreign_only = [
            {"name": "qm_keyst", "value": "evil_stolen_token", "domain": "qq.com.attacker.invalid"},
            {"name": "MUSIC_U", "value": "netease_cookie", "domain": ".music.163.com"},
        ]
        self.assertIsNone(extract_channel_auth_cookies("qq", foreign_only))

        mixed = [
            *foreign_only,
            {"name": "qm_keyst", "value": "Q_H_L_real_qq_key", "domain": ".y.qq.com"},
            {"name": "uin", "value": "o123456789", "domain": ".qq.com"},
        ]
        extracted = extract_channel_auth_cookies("qq", mixed)
        self.assertIsNotNone(extracted)
        self.assertEqual("123456789", extracted["uin"])
        self.assertIn("qm_keyst=Q_H_L_real_qq_key", extracted["cookie"])
        self.assertNotIn("evil_stolen_token", extracted["cookie"])
        self.assertNotIn("MUSIC_U", extracted["cookie"])

    def test_channel_auth_window_controller_captures_cookies_closes_popup_and_broadcasts_sse(self):
        class FakeEvent:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, fn):
                self.handlers.append(fn)
                return self

            def fire(self):
                for fn in list(self.handlers):
                    fn()

        class FakePopup:
            def __init__(self, title, url):
                self.title = title
                self.url = url
                self.destroyed = threading.Event()
                self.events = type("Events", (), {"closed": FakeEvent()})()

            def destroy(self):
                self.destroyed.set()

        popups: list[FakePopup] = []
        poll_count = 0

        def fake_cookie_reader(_popup, _urls):
            nonlocal poll_count
            poll_count += 1
            if poll_count == 1:
                return [{"name": "qm_keyst", "value": "ignored_foreign", "domain": "evil.com"}]
            return [
                {"name": "qm_keyst", "value": "real_qq_session_key_777", "domain": ".y.qq.com"},
                {"name": "uin", "value": "o888777666", "domain": ".qq.com"},
            ]

        ctrl = ChannelAuthWindowController(
            on_authorized=self.app.complete_channel_auth,
            on_status=self.app.broadcast_channel_auth_status,
            window_factory=lambda title, url: (popups.append(FakePopup(title, url)) or popups[-1]),
            cookie_reader=fake_cookie_reader,
            poll_interval_sec=0.03,
            timeout_sec=2.0,
        )
        self.app.channel_auth_window = ctrl
        observer = self.app._sync_bus.subscribe()
        try:
            with patch.object(
                self.app.audio.external_resolver,
                "test_channel",
                return_value={
                    "channel": "qq",
                    "ok": True,
                    "latency_ms": 18,
                    "status": "connected",
                    "capability": "authenticated_search",
                    "message": "搜索可用",
                    "max_quality_hint": "",
                },
            ):
                resp = self.http.post("/api/desktop/channel-auth-window", json={"channel": "qq"})
                self.assertEqual(200, resp.status_code, resp.text)
                self.assertTrue(resp.json()["data"]["supported"])
                self.assertTrue(resp.json()["data"]["opened"])
                self.assertEqual(1, len(popups))
                self.assertEqual("https://y.qq.com/", popups[0].url)
                self.assertTrue(popups[0].destroyed.wait(2.0), "Popup must close automatically after auth")
                sse_event = observer.get(timeout=2.0)
        finally:
            self.app._sync_bus.unsubscribe(observer)
            ctrl.close_all()

        self.assertEqual("channels", sse_event["_event_type"])
        self.assertEqual("qq", sse_event["channel"])
        self.assertEqual("authorized", sse_event["status"])
        self.assertNotIn("real_qq_session_key_777", json.dumps(sse_event))

        secret = self.app.channel_vault.get_channel_secret("qq")
        self.assertIn("qm_keyst=real_qq_session_key_777", secret["cookie"])
        self.assertEqual("888777666", secret["uin"])
        self.assertTrue(self.app.channel_vault.get_channel_public("qq")["verified"])

        # Verify manual user close emits cancelled SSE event
        cancel_popups: list[FakePopup] = []
        cancel_ctrl = ChannelAuthWindowController(
            on_authorized=self.app.complete_channel_auth,
            on_status=self.app.broadcast_channel_auth_status,
            window_factory=lambda title, url: (cancel_popups.append(FakePopup(title, url)) or cancel_popups[-1]),
            cookie_reader=lambda *_: [],
            poll_interval_sec=0.05,
            timeout_sec=2.0,
        )
        observer2 = self.app._sync_bus.subscribe()
        try:
            self.assertTrue(cancel_ctrl.open_auth_window("kuwo"))
            cancel_popups[0].events.closed.fire()
            cancel_event = observer2.get(timeout=2.0)
        finally:
            self.app._sync_bus.unsubscribe(observer2)
            cancel_ctrl.close_all()
        self.assertEqual("kuwo", cancel_event["channel"])
        self.assertEqual("cancelled", cancel_event["status"])

        # Verify timeout auto-closes popup and emits timeout SSE event
        timeout_popups: list[FakePopup] = []
        timeout_ctrl = ChannelAuthWindowController(
            on_authorized=self.app.complete_channel_auth,
            on_status=self.app.broadcast_channel_auth_status,
            window_factory=lambda title, url: (timeout_popups.append(FakePopup(title, url)) or timeout_popups[-1]),
            cookie_reader=lambda *_: [],
            poll_interval_sec=0.02,
            timeout_sec=0.12,
        )
        observer3 = self.app._sync_bus.subscribe()
        try:
            self.assertTrue(timeout_ctrl.open_auth_window("migu"))
            self.assertTrue(timeout_popups[0].destroyed.wait(2.0), "Popup must close on timeout")
            timeout_event = observer3.get(timeout=2.0)
        finally:
            self.app._sync_bus.unsubscribe(observer3)
            timeout_ctrl.close_all()
        self.assertEqual("migu", timeout_event["channel"])
        self.assertEqual("timeout", timeout_event["status"])

    # ------------------------------------------------------------------
    # Phase D: Stub Track Metadata Hydration
    # ------------------------------------------------------------------

    def test_like_track_hydrates_real_metadata_instead_of_inserting_stub(self):
        self.login()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        remote_liked: set[int] = set()
        self.transport.handlers["/like"] = lambda params, *_: (
            remote_liked.add(int(params["id"])),
            {"code": 200},
        )[1]
        self.transport.handlers["/likelist"] = lambda *_: {"code": 200, "ids": sorted(remote_liked)}
        self.transport.handlers["/song/detail"] = lambda params, *_: {
            "code": 200,
            "songs": [api_song(int(params["id"]), "晴天")],
            "privileges": [{"id": int(params["id"]), "fee": 0, "st": 0, "maxBrLevel": "lossless"}],
        }

        liked = self.http.post("/api/operations/like", json={"song_id": 3384272710, "like": True, "session_generation": self.app.session_generation})
        self.assertEqual(200, liked.status_code, liked.text)
        song = self.db.get_song(3384272710, account_id="42")
        self.assertIsNotNone(song)
        self.assertFalse(is_stub_song(song))
        self.assertEqual("晴天", song.title)
        self.assertEqual(["Artist"], song.artists)
        self.assertGreater(song.duration, 0)

    def test_sync_song_detail_batch_retries_smaller_chunks_on_partial_or_error(self):
        all_ids = list(range(1, 25))
        seen_chunk_sizes: list[int] = []

        def raw_request(_transport, path, params=None, **_kwargs):
            if path == "/api/v6/playlist/detail":
                return TransportResponse(
                    200,
                    {
                        "code": 200,
                        "playlist": {
                            "trackCount": len(all_ids),
                            "tracks": [api_song(1, "Track 1")],
                            "trackIds": [{"id": sid} for sid in all_ids],
                        },
                    },
                )
            if path == "/api/v3/song/detail":
                requested = json.loads(params["ids"])
                seen_chunk_sizes.append(len(requested))
                # Fail initial large batch (>10 IDs) and omit 1 track on first medium batch (>1 ID)
                if len(requested) > 10:
                    return TransportResponse(500, {"code": 500})
                if len(requested) > 1 and 20 in requested:
                    filtered = [sid for sid in requested if sid != 20]
                    return TransportResponse(200, {"code": 200, "songs": [api_song(sid, f"Track {sid}") for sid in filtered]})
                return TransportResponse(200, {"code": 200, "songs": [api_song(sid, f"Track {sid}") for sid in requested]})
            raise AssertionError(path)

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            client = NeteaseClient(NeteaseWebTransport("https://music.163.com"))
            songs = client.playlist_tracks(99, fresh=True)
        self.assertEqual(all_ids, [s.id for s in songs])
        self.assertIn(23, seen_chunk_sizes)
        self.assertIn(10, seen_chunk_sizes)
        self.assertIn(1, seen_chunk_sizes)

    def test_post_song_hydrate_updates_stub_in_sqlite_and_handles_delisted_cleanly(self):
        self.login()
        # Plant a real stub row matching the user's bug report (歌曲 #3384272710 / [] / 0)
        liked = PlaylistSummary(10, "我喜欢的音乐", 0, "liked", "private")
        self.db.append_liked_members("42", liked, [3384272710, 1955086147])
        before = self.db.get_song(3384272710, account_id="42")
        self.assertTrue(is_stub_song(before))
        self.assertEqual("歌曲 #3384272710", before.title)

        def detail_handler(params, *_):
            sid = int(params["id"])
            if sid == 3384272710:
                return {
                    "code": 200,
                    "songs": [api_song(3384272710, "青花瓷")],
                    "privileges": [{"id": 3384272710, "fee": 0, "st": 0, "maxBrLevel": "hires"}],
                }
            # Delisted track returns empty songs list
            return {"code": 200, "songs": [], "privileges": []}

        self.transport.handlers["/song/detail"] = detail_handler

        # 1. Resolvable stub -> hydrated=True + SQLite updated
        ok_resp = self.http.post("/api/songs/3384272710/hydrate", json={})
        self.assertEqual(200, ok_resp.status_code, ok_resp.text)
        ok_data = ok_resp.json()["data"]
        self.assertTrue(ok_data["hydrated"])
        self.assertFalse(ok_data["unavailable_metadata"])
        self.assertEqual("青花瓷", ok_data["song"]["title"])
        self.assertEqual("hires", ok_data["song"]["max_br_level"])

        after = self.db.get_song(3384272710, account_id="42")
        self.assertFalse(is_stub_song(after))
        self.assertEqual("青花瓷", after.title)

        # 2. Delisted track -> HTTP 200 with hydrated=False, unavailable_metadata=True (never 500)
        delisted_resp = self.http.post("/api/songs/1955086147/hydrate", json={})
        self.assertEqual(200, delisted_resp.status_code, delisted_resp.text)
        delisted_data = delisted_resp.json()["data"]
        self.assertFalse(delisted_data["hydrated"])
        self.assertTrue(delisted_data["unavailable_metadata"])
        self.assertTrue(delisted_data["reason"])
        self.assertEqual(1955086147, delisted_data["song"]["id"])

        # 3. Upstream outage -> HTTP 200 with hydrated=False, unavailable_metadata=True (never 500)
        self.transport.handlers["/song/detail"] = TransportResponse(503, {"code": 503})
        outage_resp = self.http.post("/api/songs/1955086147/hydrate", json={})
        self.assertEqual(200, outage_resp.status_code, outage_resp.text)
        self.assertFalse(outage_resp.json()["data"]["hydrated"])
        self.assertTrue(outage_resp.json()["data"]["unavailable_metadata"])


if __name__ == "__main__":
    unittest.main()
