from pathlib import Path
import ctypes
import json
import os
import sqlite3
import tempfile
import sys
import time
import unittest
from unittest.mock import Mock, patch

import httpx
from pytest import MonkeyPatch
from starlette.testclient import TestClient

from pruner_studio.app import build_application, create_asgi_app, serve
from pruner_studio.config import Settings
from pruner_studio.db import CatalogDatabase
from pruner_studio.desktop import (
    BackgroundUvicornServer,
    DesktopLyricsController,
    GlobalHotkeyDaemon,
    HOTKEY_BINDINGS,
    wake_running_instance,
)
from pruner_studio.errors import AppError
from pruner_studio.external_sources import is_safe_external_url, probe_audio_url
from pruner_studio.models import Song, PlaylistSummary


class StorageMigrationTests(unittest.TestCase):
    def test_legacy_rows_survive_and_accounts_diverge(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            conn = sqlite3.connect(directory / CatalogDatabase.FILE_NAME)
            conn.executescript("""
                CREATE TABLE songs(song_id INTEGER PRIMARY KEY,title TEXT,artists_json TEXT,album TEXT,
                    duration INTEGER,cover_url TEXT,fee INTEGER,privilege_st INTEGER,max_br_level TEXT,
                    is_cloud INTEGER,chorus_start INTEGER,chorus_end INTEGER,availability TEXT);
                INSERT INTO songs VALUES(1,'legacy','["old"]','',10,'',1,-1,'',0,NULL,NULL,'vip');
                CREATE TABLE playlist_tracks(account_id TEXT, playlist_id TEXT, position INTEGER,song_id INTEGER,
                    PRIMARY KEY(account_id,playlist_id,position));
                INSERT INTO playlist_tracks VALUES ('a','77',0,1),('b','77',0,1);
                CREATE TABLE play_history(account_id TEXT,song_id INTEGER,title TEXT,artists_json TEXT,
                    album TEXT,duration INTEGER,cover_url TEXT,play_count INTEGER,completed_count INTEGER,
                    total_listen_ms INTEGER,last_played_at TEXT,PRIMARY KEY(account_id,song_id));
                INSERT INTO play_history VALUES('a',1,'legacy','["old"]','',10,'',3,2,90000,'2020-01-01');
            """)
            conn.close()
            db = CatalogDatabase(directory)
            self.assertEqual("2", db.get_meta("schema_version"))
            playlist = PlaylistSummary(77, "items", 1, "liked", "private")
            db.replace_playlist_tracks(
                "b", playlist, [Song(1, "different", ["new"], fee=0, privilege_st=0)]
            )
            db = CatalogDatabase(directory)
            song = db.get_song(1, "a")
            self.assertEqual(
                ("legacy", ["old"], 1, -1),
                (song.title, song.artists, song.fee, song.privilege_st),
            )
            self.assertEqual("different", db.query_view("b")["tracks"][0]["title"])
            self.assertIsNone(db.get_song(1, "other"))
            (row,) = db.list_play_history("a")
            self.assertEqual(
                ("netease:1", 3, 90000),
                (row["track_key"], row["play_count"], row["total_listen_ms"]),
            )


class PlaybackApiTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(
            data_dir=tmp / "data",
            web_dir=tmp,
            api_base="http://fake.invalid",
            port=8765,
        )
        self.app = build_application(self.settings, Mock())
        self.http = self.enterContext(
            TestClient(
                create_asgi_app(self.app, 8765), base_url="http://127.0.0.1:8765"
            )
        )

    def test_guest_and_offline_sessions(self):
        path = "/api/playback/session"
        self.assertIsNone(self.http.get(path).json()["data"])
        payload = {
            "queue": [{"id": 1}],
            "queue_index": 0,
            "current_track": {"id": 1},
            "position_sec": 5.25,
        }
        saved = self.http.put(path, json=payload).json()["data"]
        self.assertEqual("local", saved["account_id"])
        self.assertEqual(5.25, saved["position_sec"])
        self.assertEqual(
            saved,
            build_application(self.settings, Mock()).api("GET", path).payload["data"],
        )
        for invalid in (
            {"queue": [{}] * 2001},
            {"queue": "x"},
            {"position_sec": True},
            {"muted": "false"},
        ):
            self.assertEqual(400, self.http.put(path, json=invalid).status_code)
        self.http.get("/api/catalog?account_id=offline")
        self.assertIsNone(self.http.get(path).json()["data"])
        self.assertEqual(
            "offline", self.http.put(path, json={}).json()["data"]["account_id"]
        )

    def test_event_deltas_query_delete_and_replay(self):
        history = "/api/playback/history"
        record = "/api/playback/record"
        for ms, done in [
            (30000, False),
            (180000, True),
            (30000, False),
            (180000, True),
        ]:
            resp = self.http.post(
                record,
                json=dict(
                    track_key="qq:abc",
                    event_id="one",
                    title="QQ title",
                    listen_ms=ms,
                    completed=done,
                ),
            )
            self.assertEqual(200, resp.status_code, resp.text)
        local = {
            "provider": "local",
            "local_path": "D:/Music/a.flac",
            "completed": True,
        }
        self.http.post(record, json=local)
        (row,) = self.http.get(history + "?q=QQ").json()["data"]["items"]
        self.assertEqual(
            ("qq", "abc", 1, 1, 180000),
            tuple(
                row[k]
                for k in (
                    "provider",
                    "external_id",
                    "play_count",
                    "completed_count",
                    "total_listen_ms",
                )
            ),
        )
        self.assertEqual(
            400, self.http.post(record, json={**local, "event_id": "one"}).status_code
        )
        self.assertEqual(
            1, len(self.http.get(history + "?offset=1&limit=1").json()["data"]["items"])
        )
        self.assertEqual(
            1, self.http.delete(history + "?track_key=qq:abc").json()["data"]["deleted"]
        )
        self.assertEqual(1, self.http.delete(history).json()["data"]["deleted"])
        self.assertEqual([], self.http.get(history).json()["data"]["items"])

    def test_wake_token_picker_and_lyrics_bridge(self):
        path = "/api/desktop/window/activate"
        self.app.wake_window = Mock(return_value=True)
        self.assertEqual(
            403, self.http.post(path, json={"wake_token": "bad"}).status_code
        )
        self.app.wake_window.assert_not_called()
        self.assertTrue(
            self.http.post(
                path, headers={"X-Pruner-Wake-Token": self.app.wake_token}
            ).json()["data"]["activated"]
        )
        picker = "/api/desktop/pick-folder"
        self.assertEqual(
            {"supported": False, "path": None},
            self.http.post(picker, json={}).json()["data"],
        )
        self.app.folder_picker = lambda: "D:/Music"
        self.assertEqual(
            "D:/Music", self.http.post(picker, json={}).json()["data"]["path"]
        )
        payload = dict(
            playback_seq=9,
            offset_sec=-1,
            playback_rate=1.5,
            buffering=True,
            liked=False,
        )
        with patch.object(self.app, "publish_sse") as publish:
            self.http.post("/api/desktop/lyrics", json=payload)
            publish.assert_called_once_with("desktop_lyrics", payload)
        self.assertFalse(DesktopLyricsController().start_native_drag())

    def test_netease_complete_range_and_cache_reuse(self):
        self.app.user_settings.save({"quality_arbitration": "netease_first"})
        calls = []

        def send(req):
            calls.append(req)
            return httpx.Response(
                206,
                headers={"content-range": "bytes 0-7/8", "content-type": "audio/mpeg"},
                content=b"ID3AUDIO",
            )

        factory = httpx.AsyncClient
        with patch(
            "pruner_studio.app.httpx.AsyncClient",
            side_effect=lambda **kw: factory(transport=httpx.MockTransport(send), **kw),
        ):
            for sig in ["first", "second"]:
                original = {
                    "id": 1,
                    "url": "https://8.8.8.8/a?sig=" + sig,
                    "level": "exhigh",
                    "format": "mp3",
                }
                with patch.object(type(self.app.client), "audio_url", return_value=original):
                    resolved = self.http.get("/api/audio?id=1&force_refresh=1&allow_external=0")
                    self.assertEqual(200, resolved.status_code, resolved.text)
                    data = resolved.json()["data"]
                self.assertEqual(original["url"], data["direct_url"])
                self.assertTrue(data["url"].startswith("/api/stream/"))
                resp = self.http.get(data["url"], headers={"Range": "bytes=0-"})
                self.assertEqual(206, resp.status_code, resp.text)
                self.assertEqual(b"ID3AUDIO", resp.content)
        self.assertEqual(1, len(calls))
        self.assertNotIn("cookie", calls[0].headers)
        self.assertIsNotNone(self.app.audio_cache.get("netease:1:exhigh"))

    def test_dns_and_redirect_policy(self):
        def resolve(host, port, **kwargs):
            addresses = (
                ["8.8.8.8"] if host == "public.test" else ["8.8.8.8", "192.168.1.42"]
            )
            return [(2, 1, 6, "", (ip, port)) for ip in addresses]

        hits = []

        def redirect(req):
            hits.append(req.url.host)
            return httpx.Response(302, headers={"location": "https://mixed.test/cache"})

        with patch("pruner_studio.external_sources._resolve_addresses", resolve):
            self.assertFalse(is_safe_external_url("https://mixed.test"))
            with httpx.Client(
                transport=httpx.MockTransport(redirect), follow_redirects=True
            ) as client:
                self.assertFalse(probe_audio_url("https://public.test/a", client).ok)
            self.assertEqual(["8.8.8.8"], hits)
            resp = self.http.get(
                "/api/audio",
                params={
                    "provider": "qq",
                    "external_id": "a",
                    "stream_url_hint": "https://mixed.test/a",
                },
            )
            self.assertEqual(403, resp.status_code)

    def test_cover_body_stops_at_limit(self):
        def chunks():
            yield b"x" * 65536
            yield b"y" * 65536
            self.fail("Cover exceeded byte budget before this read")

        transport = httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "image/png"}, content=chunks()
            )
        )
        self.enterContext(
            patch(
                "pruner_studio.external_sources._resolve_addresses",
                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
            )
        )
        factory = httpx.Client
        with (
            patch("pruner_studio.app._COVER_MAX_BYTES", 65536),
            patch(
                "pruner_studio.app.httpx.Client",
                side_effect=lambda **kw: factory(transport=transport, **kw),
            ),
        ):
            with self.assertRaises(AppError) as exc:
                self.app.fetch_cover("https://p1.music.126.net/a")
        self.assertEqual("COVER_TOO_LARGE", exc.exception.code)
        self.assertFalse(self.app._cover_cache)

    def test_hotkeys_release_and_reregister(self):
        daemon = GlobalHotkeyDaemon(lambda action: None)
        win32 = Mock()
        daemon._sync_bindings(win32)
        self.assertEqual(len(HOTKEY_BINDINGS), win32.RegisterHotKey.call_count)
        daemon.set_enabled(False)
        daemon._sync_bindings(win32)
        self.assertEqual(len(HOTKEY_BINDINGS), win32.UnregisterHotKey.call_count)
        daemon.set_enabled(True)
        daemon._sync_bindings(win32)
        self.assertEqual(2 * len(HOTKEY_BINDINGS), win32.RegisterHotKey.call_count)

    def test_single_instance_ipc_with_ephemeral_port(self):
        descriptor = self.settings.data_dir / ".instance.json"

        def run(server):
            server.start()
            try:
                record = json.loads(descriptor.read_text("utf-8"))
                self.assertEqual(os.getpid(), record["pid"])
                self.assertEqual("browser", record["mode"])
                self.assertTrue(wake_running_instance(self.settings.data_dir))
            finally:
                server.stop()

        from dataclasses import replace

        with (
            MonkeyPatch.context() as launch_env,
            patch.object(BackgroundUvicornServer, "run_foreground", run),
            patch("pruner_studio.desktop.webbrowser.open", return_value=True) as opened,
            patch("pruner_studio.desktop.threading.Timer") as timer,
        ):
            launch_env.setenv("PRUNER_STUDIO_BROWSER", "1")
            launch_env.setenv("PRUNER_STUDIO_NO_BROWSER", "0")
            serve(replace(self.settings, port=0), Mock())
            self.assertTrue(timer.called)
            self.assertTrue(opened.called)
        self.assertFalse(descriptor.exists())

    def test_new_settings_and_native_callbacks(self):
        self.app.apply_theme = Mock()
        self.app.set_hotkeys_enabled = Mock()
        raw = dict(
            window_bounds=dict(x=-450, y=100, width=900, height=700, maximized=True),
            global_hotkeys_enabled=False,
            gpu_performance_mode="eco",
            stage_theme="poster",
            appearance={"theme": "espresso"},
            visualizer_mode="wave3d",
            playback_rate=3,
            eq_enabled=True,
            eq_bands=[-20, 20] * 5,
            enable_3d_effects=False,
            audio_output_device_id="speaker",
            desktop_lyrics_style={"preset": "sakura_mist"},
        )
        saved = self.http.put("/api/settings", json=raw).json()["data"]
        self.assertEqual([-12, 12] * 5, saved["eq_bands"])
        self.assertEqual(2, saved["playback_rate"])
        self.assertEqual(raw["window_bounds"], saved["window_bounds"])
        self.assertEqual(saved, self.app.user_settings.load())
        self.app.apply_theme.assert_called_once_with("espresso")
        self.app.set_hotkeys_enabled.assert_called_once_with(False)
        self.assertEqual(
            400, self.http.put("/api/settings", json={"eq_bands": [0]}).status_code
        )

    @unittest.skipUnless(sys.platform == "win32", "Win32 message pump")
    def test_hotkey_message_pump_toggles_registration(self):
        daemon = GlobalHotkeyDaemon(lambda action: None, enabled_provider=lambda: False)
        with patch("pruner_studio.desktop.HOTKEY_BINDINGS", ((800, 0x87, "test"),)):
            self.assertTrue(daemon.start())
            try:
                for enabled in (True, False, True):
                    daemon.set_enabled(enabled)
                    deadline = time.monotonic() + 1
                    while (
                        bool(daemon._registered_ids) != enabled
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertEqual([800] if enabled else [], daemon._registered_ids)
            finally:
                daemon.stop()

    @unittest.skipUnless(sys.platform == "win32", "Win32 window styles")
    def test_hidden_native_overlay_has_toolwindow_style(self):
        from pruner_studio.desktop import _configure_win32_seamless_overlay

        api = ctypes.WinDLL("user32", use_last_error=True)
        api.CreateWindowExW.restype = ctypes.c_void_p
        hwnd = api.CreateWindowExW(
            0x40000,
            "STATIC",
            "Pruner test overlay",
            0x80080000,  # WS_POPUP | WS_SYSMENU
            0,
            0,
            80,
            40,
            None,
            None,
            None,
            None,
        )
        self.assertTrue(hwnd)
        handle = ctypes.c_void_p(hwnd)
        try:
            _configure_win32_seamless_overlay(hwnd)
            style = api.GetWindowLongW(handle, -20)
            self.assertTrue(style & 0x80)
            self.assertFalse(style & 0x40000)
            self.assertTrue(api.IsWindow(handle))
            self.assertFalse(api.GetWindowLongW(handle, -16) & 0x80000)
        finally:
            api.DestroyWindow(handle)
