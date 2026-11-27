"""Phase 7F Ship-Gate fixes & Phase 8 listening/game-escort backend tests."""
import json
import tempfile
import unittest
from pathlib import Path

from pruner_studio.app import build_application
from pruner_studio.config import Settings
from pruner_studio.desktop import DesktopLyricsController, HOTKEY_BINDINGS
from pruner_studio.errors import AppError
from pruner_studio.models import PlaylistSummary, Song
from pruner_studio.netease_client import TransportResponse
from test_backend import FakeTransport, account_payload


def _acct(uid: int, name: str) -> dict:
    return {"code": 200, "profile": {"userId": uid, "nickname": name, "avatarUrl": "https://img.test/a.jpg"}}


class Phase7FAndPhase8Tests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(root, root)
        self.transport = FakeTransport()
        self.app = build_application(self.settings, self.transport)
        self.app.client.pacer.sleep_fn = lambda _: None

    def data(self, method, path, body=None):
        response = self.app.api(method, path, body, http_request=True)
        self.assertTrue(response.payload["ok"], response.payload.get("error"))
        return response.payload["data"]

    def test_cookieless_qr_creation_and_polling_clears_offline_binding(self):
        self.transport.handlers["/user/account"] = _acct(101, "AccountA")
        self.app.client.set_cookie("MUSIC_U=accountA")
        self.data("GET", "/api/status")
        self.data("POST", "/api/accounts/offline", {"account_id": "17"})
        target = self.app.audio.register_stream_token(
            kind="remote",
            url="https://cdn.example/a.mp3",
            session_generation=self.app.session_generation,
        )
        self.assertIsNotNone(self.app.audio.resolve_stream_token(target.token, expected_generation=self.app.session_generation))

        self.transport.calls.clear()
        self.transport.handlers["/login/qr/key"] = {"code": 200, "data": {"unikey": "qr_cookieless_1"}}
        self.transport.handlers["/login/qr/create"] = {"code": 200, "data": {"qrurl": "https://music.test/qr", "qrimg": "data:image/png;base64,AA=="}}
        self.transport.handlers["/login/qr/check"] = {"code": 803, "cookie": "MUSIC_U=accountB"}

        created = self.data("POST", "/api/auth/qr")
        self.assertEqual("qr_cookieless_1", created["key"])

        # Switch /user/account handler to Account B for verification inside poll_qr
        self.transport.handlers["/user/account"] = _acct(202, "AccountB")
        polled = self.data("GET", "/api/auth/qr/qr_cookieless_1")
        self.assertEqual("authorized", polled["state"])
        self.assertEqual(202, polled["user"]["user_id"])

        qr_calls = [c for c in self.transport.calls if c[0] in {"/login/qr/key", "/login/qr/create", "/login/qr/check"}]
        self.assertEqual(3, len(qr_calls))
        for path, _params, _method, cookie in qr_calls:
            self.assertIsNone(cookie, f"QR call {path} leaked existing cookie!")

        self.assertIsNone(self.app.audio.resolve_stream_token(target.token))
        self.assertIsNone(self.app._offline_account_id)
        self.assertIsNone(self.app._offline_generation)
        status = self.data("GET", "/api/status")
        self.assertTrue(status["authenticated"])
        self.assertFalse(status.get("offline", False))
        self.assertEqual(202, status["user"]["user_id"])

    def test_late_audio_error_after_session_switch_returns_409(self):
        self.transport.handlers["/user/account"] = _acct(101, "UserA")
        self.app.client.set_cookie("MUSIC_U=userA")
        self.data("GET", "/api/status")

        def failing_resolve(_query, _cookie):
            self.app.api("POST", "/api/accounts/offline", {"account_id": "local"})
            raise AppError("AUDIO_UNAVAILABLE", "音源暂不可用", 404)

        orig_resolve = self.app._resolve_audio
        self.app._resolve_audio = failing_resolve
        try:
            resp = self.app.api("GET", "/api/audio?id=12345")
        finally:
            self.app._resolve_audio = orig_resolve

        self.assertEqual(409, resp.status)
        self.assertEqual("SESSION_CHANGED", resp.payload["error"]["code"])

    def test_reconcile_unlike_validates_explicit_playlist_id_before_account_call(self):
        self.transport.handlers["/user/account"] = _acct(101, "UserA")
        self.app.client.set_cookie("MUSIC_U=userA")
        self.transport.calls.clear()

        resp = self.app.api(
            "POST",
            "/api/operations/reconcile",
            {"action": "unlike", "playlist_id": 10.5, "song_ids": [1], "source_playlist_id": 10},
        )
        self.assertEqual(400, resp.status)
        self.assertEqual([], self.transport.calls)

        # playlist_id=None remains accepted
        self.transport.handlers["/user/playlist"] = {"code": 200, "playlist": [{"id": 10, "name": "我喜欢的音乐", "trackCount": 0, "specialType": 5, "privacy": 0, "creator": {"userId": 101}}]}
        self.transport.handlers["/likelist"] = {"code": 200, "ids": []}
        self.transport.handlers["/playlist/track/all"] = {"code": 200, "songs": []}
        ok_res = self.data(
            "POST",
            "/api/operations/reconcile",
            {"action": "unlike", "playlist_id": None, "song_ids": [1], "source_playlist_id": 10},
        )
        self.assertEqual([1], ok_res["successful_ids"])

    def test_explicit_offline_empty_daily_history_returns_empty_not_auth_required(self):
        # Unauthenticated without explicit offline -> auth_required
        unauth = self.data("GET", "/api/recommend/daily/history")
        self.assertEqual("auth_required", unauth["status"])
        self.assertEqual([], unauth["dates"])

        # Explicit offline binding with zero cached history dates -> status="empty"
        self.data("POST", "/api/accounts/offline", {"account_id": "17"})
        offline_empty = self.data("GET", "/api/recommend/daily/history")
        self.assertEqual("empty", offline_empty["status"])
        self.assertEqual([], offline_empty["dates"])
        self.assertEqual([], offline_empty["cached_dates"])

        # Specific uncached date while offline -> auth_required
        offline_day = self.data("GET", "/api/recommend/daily/history?date=2026-11-20")
        self.assertEqual("auth_required", offline_day["status"])

    def test_shared_rule_validation_parity(self):
        cases = json.loads((Path(__file__).resolve().parents[1] / "rule_validation.json").read_text(encoding="utf-8"))
        for field, operator, value, expected in cases:
            with self.subTest(field=field, operator=operator, value=value):
                res = self.app.api(
                    "PUT",
                    "/api/rules",
                    {
                        "rules": [
                            {
                                "id": "parity_rule",
                                "name": "parity",
                                "enabled": True,
                                "match_clauses": [{"field": field, "operator": operator, "value": value}],
                            }
                        ]
                    },
                )
                if expected:
                    self.assertEqual(200, res.status, res.payload)
                else:
                    self.assertEqual(400, res.status, res.payload)

    def test_phase8_settings_hotkeys_and_desktop_commands(self):
        # 1. Settings: celadon, parchment, game_mode, stage_lyric_size, stage_lyric_align, new presets
        saved = self.data(
            "PUT",
            "/api/settings",
            {
                "appearance": {"theme": "celadon"},
                "game_mode": True,
                "stage_lyric_size": 30,
                "stage_lyric_align": "center",
                "desktop_lyrics_style": {"preset": "game_contrast"},
            },
        )
        self.assertEqual("celadon", saved["appearance"]["theme"])
        self.assertTrue(saved["game_mode"])
        self.assertEqual(30, saved["stage_lyric_size"])
        self.assertEqual("center", saved["stage_lyric_align"])
        self.assertEqual("game_contrast", saved["desktop_lyrics_style"]["preset"])

        saved2 = self.data("PUT", "/api/settings", {"appearance": {"theme": "parchment"}, "desktop_lyrics_style": {"preset": "apple_glass"}})
        self.assertEqual("parchment", saved2["appearance"]["theme"])
        self.assertEqual("apple_glass", saved2["desktop_lyrics_style"]["preset"])

        for bad_payload in (
            {"game_mode": "yes"},
            {"stage_lyric_size": 12},
            {"stage_lyric_size": 50},
            {"stage_lyric_align": "right"},
        ):
            self.assertEqual(400, self.app.api("PUT", "/api/settings", bad_payload).status)

        # 2. Hotkeys & DesktopLyricsController.toggle_locked
        actions = [action for _, _, action in HOTKEY_BINDINGS]
        for expected_action in ("volume_up", "volume_down", "toggle_mute", "toggle_desktop_lyrics_lock", "toggle_game_mode"):
            self.assertIn(expected_action, actions)

        ctrl = DesktopLyricsController("http://127.0.0.1:0", None)
        self.assertFalse(ctrl.state()["locked"])
        self.assertTrue(ctrl.toggle_locked()["locked"])
        self.assertFalse(ctrl.toggle_locked()["locked"])

        # 3. /api/desktop/command new actions + set_volume validation + toggle_desktop_lyrics remains 400
        for valid_cmd in (
            {"action": "volume_up"},
            {"action": "volume_down"},
            {"action": "toggle_mute"},
            {"action": "toggle_desktop_lyrics_lock"},
            {"action": "toggle_game_mode"},
            {"action": "set_volume", "value": 0.45},
        ):
            res = self.data("POST", "/api/desktop/command", valid_cmd)
            self.assertTrue(res["dispatched"])

        for bad_cmd in (
            {"action": "toggle_desktop_lyrics"},
            {"action": "set_volume", "value": True},
            {"action": "set_volume", "value": -0.1},
            {"action": "set_volume", "value": 1.1},
            {"action": "set_volume", "value": "0.5"},
        ):
            self.assertEqual(400, self.app.api("POST", "/api/desktop/command", bad_cmd).status)


if __name__ == "__main__":
    unittest.main()
