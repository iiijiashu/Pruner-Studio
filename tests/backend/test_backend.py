from __future__ import annotations

import json
import http.client
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from pathlib import Path
from typing import Any

from pruner_studio.app import build_application, make_handler
from pruner_studio.audio_service import is_strict_match
from pruner_studio.config import Settings
from pruner_studio.models import PlaylistSummary, Song
from pruner_studio.netease_client import NeteaseClient, NeteaseWebTransport, TransportResponse, UpstreamError, UrllibTransport, _slice_playlist_tracks


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


def account_payload():
    return {"code": 200, "profile": {"userId": 42, "nickname": "Test User", "avatarUrl": "https://img.test/a.jpg"}}


def playlist_payload():
    return {
        "code": 200,
        "playlist": [
            {"id": 10, "name": "Liked", "trackCount": 2, "specialType": 5, "privacy": 0, "creator": {"userId": 42}},
            {"id": 11, "name": "Mine", "trackCount": 1, "specialType": 0, "privacy": 10, "creator": {"userId": 42}},
            {"id": 12, "name": "Followed", "trackCount": 8, "creator": {"userId": 99}},
        ],
    }


def api_song(song_id=1, title="Song", artist="Artist", duration=180_000):
    return {
        "id": song_id,
        "name": title,
        "ar": [{"name": artist}],
        "al": {"name": "Album", "picUrl": "https://img.test/cover.jpg"},
        "dt": duration,
        "fee": 0,
        "privilege": {"st": 0},
    }


class BackendTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_dir = root / "runtime"
        self.web_dir = root / "web"
        self.web_dir.mkdir()
        (self.web_dir / "index.html").write_text("<h1>Pruner</h1>", encoding="utf-8")
        (self.web_dir / "app.js").write_text("console.log('ok')", encoding="utf-8")
        (self.web_dir / "brand.ttf").write_bytes(b"font")
        (self.web_dir / "secret.json").write_text("{}", encoding="utf-8")
        self.settings = Settings(data_dir=self.data_dir, web_dir=self.web_dir, api_base="http://fake.invalid", port=0)
        self.transport = FakeTransport()
        self.app = build_application(self.settings, self.transport)

    def tearDown(self):
        self.temp.cleanup()

    def authenticate(self):
        self.app.client.set_cookie("MUSIC_U=sanitized")
        self.app.session.save_cookie("MUSIC_U=sanitized")
        self.transport.handlers["/user/account"] = account_payload()

    def test_qr_login_is_remembered_without_exposing_cookie(self):
        self.transport.handlers.update(
            {
                "/login/qr/key": {"code": 200, "data": {"unikey": "qr-key"}},
                "/login/qr/create": {"code": 200, "data": {"qrurl": "https://music.test/qr", "qrimg": "data:image/png;base64,AA=="}},
                "/login/qr/check": {
                    "code": 803,
                    "message": "ok",
                    "cookie": "MUSIC_U=sanitized-session; Path=/; HttpOnly; Max-Age=3600",
                },
                "/user/account": account_payload(),
            }
        )
        created = self.app.api("POST", "/api/auth/qr", {})
        self.assertEqual(201, created.status)
        self.assertEqual("https://music.test/qr", created.payload["data"]["qr_url"])
        polled = self.app.api("GET", "/api/auth/qr/qr-key")
        self.assertEqual(200, polled.status)
        self.assertEqual("authorized", polled.payload["data"]["state"])
        self.assertNotIn("cookie", json.dumps(polled.payload))
        self.assertEqual("MUSIC_U=sanitized-session", self.app.session.load_cookie())
        self.assertTrue((self.data_dir / "session.json").is_file())
        restarted = build_application(self.settings, self.transport)
        self.assertEqual("MUSIC_U=sanitized-session", restarted.client.cookie)
        self.app.catalog.store.write("catalog.json", {"version": 1, "playlists": {}})
        logout = self.app.api("POST", "/api/auth/logout", {})
        self.assertEqual({"authenticated": False}, logout.payload["data"])
        self.assertFalse((self.data_dir / "session.json").exists())
        self.assertTrue((self.data_dir / "catalog.json").exists())
        created_again = self.app.api("POST", "/api/auth/qr", {})
        self.assertEqual(201, created_again.status)
        self.app.api("POST", "/api/auth/logout", {})
        cancelled = self.app.api("GET", "/api/auth/qr/qr-key")
        self.assertEqual(409, cancelled.status)
        self.assertEqual("QR_CANCELLED", cancelled.payload["error"]["code"])

    def test_status_and_playlist_list_use_real_account_and_filter_owned(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        status = self.app.api("GET", "/api/status")
        self.assertTrue(status.payload["data"]["authenticated"])
        self.assertEqual(42, status.payload["data"]["user"]["user_id"])
        playlists = self.app.api("GET", "/api/playlists")
        self.assertEqual([10, 11], [item["id"] for item in playlists.payload["data"]["playlists"]])
        self.assertEqual(["liked", "self_created"], [item["kind"] for item in playlists.payload["data"]["playlists"]])

    def test_sync_import_and_rules_are_persisted_atomically(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        self.transport.handlers["/playlist/track/all"] = {"code": 200, "songs": [api_song(1), api_song(2)]}
        synced = self.app.api("POST", "/api/playlists/10/sync", {})
        self.assertEqual(200, synced.status)
        self.assertEqual(2, synced.payload["data"]["synced_count"])
        imported = self.app.api(
            "POST",
            "/api/catalog/import",
            {"format": "csv", "playlist_name": "CSV", "content": "id,title,artist,duration\n3,Third,Artist,123000\ninvalid,Bad,A,1\n"},
        )
        self.assertEqual(201, imported.status)
        self.assertEqual(1, imported.payload["data"]["imported_count"])
        self.assertEqual(1, len(imported.payload["data"]["failed_rows"]))
        self.assertEqual("imported", imported.payload["data"]["tracks"][0]["source_playlist_id"])
        catalog_view = self.app.api("GET", "/api/catalog")
        self.assertEqual([3], [track["id"] for track in catalog_view.payload["data"]["tracks"] if track["source_playlist_id"] == "imported"])
        stored = self.app.catalog.get()
        stored["playlists"]["imported"]["songs"][0]["source_playlist_id"] = None
        self.app.catalog.store.write("catalog.json", stored)
        migrated_view = self.app.api("GET", "/api/catalog")
        imported_track = next(track for track in migrated_view.payload["data"]["tracks"] if track["id"] == 3)
        self.assertEqual("imported", imported_track["source_playlist_id"])
        rules = [{"id": "jp", "name": "J-Pop", "enabled": True, "match_clauses": [{"field": "artist", "contains": "A"}], "output_playlist_name": "J-Pop"}]
        updated = self.app.api("PUT", "/api/rules", {"rules": rules})
        self.assertEqual(rules, updated.payload["data"]["rules"])
        self.assertEqual(rules, self.app.api("GET", "/api/rules").payload["data"]["rules"])
        self.assertFalse(list(self.data_dir.glob("*.tmp")))

    def test_rules_output_playlist_name_is_optional(self):
        # The rule editor labels the output playlist name as 可选; saving a rule
        # without one must succeed instead of failing with RULE_INVALID.
        optional_rules = [
            {"id": "short", "name": "短片段", "enabled": True, "match_clauses": [{"field": "duration", "operator": "lt", "value": 60}], "output_playlist_name": ""},
            {"id": "no_output", "name": "无输出", "enabled": False, "match_clauses": [{"field": "title", "operator": "contains", "value": "demo"}]},
        ]
        updated = self.app.api("PUT", "/api/rules", {"rules": optional_rules})
        self.assertEqual(200, updated.status)
        self.assertEqual("", updated.payload["data"]["rules"][0]["output_playlist_name"])
        self.assertEqual("", updated.payload["data"]["rules"][1]["output_playlist_name"])
        fetched = self.app.api("GET", "/api/rules")
        self.assertEqual(200, fetched.status)
        self.assertEqual(["short", "no_output"], [rule["id"] for rule in fetched.payload["data"]["rules"]])
        oversized = [{"id": "bad", "name": "Bad", "enabled": True, "match_clauses": [{"field": "duration", "operator": "lt", "value": 60}], "output_playlist_name": "x" * 101}]
        rejected = self.app.api("PUT", "/api/rules", {"rules": oversized})
        self.assertEqual(400, rejected.status)
        self.assertEqual("RULE_INVALID", rejected.payload["error"]["code"])

    def test_audio_uses_only_strict_title_artist_duration_match(self):
        content = json.dumps([{"id": 1, "title": "Hello!", "artists": ["Alice"], "duration": 180000}])
        self.app.api("POST", "/api/catalog/import", {"format": "json", "content": content})

        def audio(params, method, cookie):
            if int(params["id"]) == 1:
                return {"code": 200, "data": [{"id": 1, "url": None}]}
            return {"code": 200, "data": [{"id": int(params["id"]), "url": "https://audio.test/file.mp3", "level": "exhigh"}]}

        self.transport.handlers["/song/url/v1"] = audio
        self.transport.handlers["/cloudsearch"] = {
            "code": 200,
            "result": {"songs": [api_song(2, "Hello", "Bob", 180000), api_song(3, "HELLO", "Alice", 190000), api_song(4, "hello", "ALICE", 182000)]},
        }
        response = self.app.api("GET", "/api/audio?id=1")
        self.assertEqual(200, response.status)
        self.assertTrue(response.payload["data"]["is_alternative"])
        self.assertEqual(4, response.payload["data"]["id"])
        self.assertEqual(4, response.payload["data"]["alternative_info"]["id"])

    def test_unlike_removes_only_remote_verified_successes(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        liked = {
            "version": 1,
            "playlists": {
                "10": {
                    "summary": {"id": 10, "name": "Liked", "track_count": 2, "kind": "liked", "privacy": "public"},
                    "songs": [
                        Song(1, "One", ["A"], source_playlist_id=10).to_dict(),
                        Song(2, "Two", ["A"], source_playlist_id=10).to_dict(),
                    ],
                }
            },
        }
        self.app.catalog.store.write("catalog.json", liked)
        remote_liked = {1, 2}

        def unlike(params, method, cookie):
            if int(params["id"]) == 1:
                remote_liked.discard(1)
            return {"code": 200}

        self.transport.handlers["/like"] = unlike
        self.transport.handlers["/likelist"] = lambda *_: {"code": 200, "ids": sorted(remote_liked)}
        result = self.app.api(
            "POST",
            "/api/operations/unlike",
            {"song_ids": [1, 2, 2], "source_playlist_id": 10},
        )
        self.assertEqual([1], result.payload["data"]["successful_ids"])
        self.assertEqual([2], [item["id"] for item in result.payload["data"]["failed_items"]])
        remaining = self.app.catalog.get()["playlists"]["10"]["songs"]
        self.assertEqual([2], [song["id"] for song in remaining])

    def test_remove_tracks_prechecks_owned_playlist_and_verifies_each_result(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        remote_ids = {1, 2}
        self.transport.handlers["/playlist/track/all"] = lambda *_: {
            "code": 200,
            "songs": [api_song(song_id) for song_id in sorted(remote_ids)],
        }

        def remove(params, method, cookie):
            self.assertEqual("del", params["op"])
            self.assertEqual(11, params["pid"])
            self.assertEqual("1,2", params["tracks"])
            remote_ids.discard(1)
            return {"code": 200}

        self.transport.handlers["/playlist/tracks"] = remove
        self.app.catalog.store.write(
            "catalog.json",
            {
                "version": 1,
                "playlists": {
                    "11": {
                        "summary": {"id": 11, "name": "Mine", "track_count": 3, "kind": "self_created", "privacy": "private"},
                        "songs": [Song(song_id, str(song_id), ["A"], source_playlist_id=11).to_dict() for song_id in (1, 2, 3)],
                    }
                },
            },
        )

        result = self.app.api(
            "POST",
            "/api/playlists/11/remove-tracks",
            {"song_ids": [1, 2, 3, 2], "source_playlist_id": 11},
        )

        self.assertEqual(200, result.status)
        self.assertEqual([1, 2, 3], result.payload["data"]["requested_ids"])
        self.assertEqual([1], result.payload["data"]["successful_ids"])
        self.assertEqual([2, 3], [item["id"] for item in result.payload["data"]["failed_items"]])
        self.assertEqual("TRACK_NOT_IN_PLAYLIST", result.payload["data"]["failed_items"][1]["code"])
        track_reads = [call for call in self.transport.calls if call[0] == "/playlist/track/all"]
        self.assertEqual(2, len(track_reads))
        self.assertTrue(all(call[1].get("fresh") is True for call in track_reads))
        remaining = self.app.catalog.get()["playlists"]["11"]
        self.assertEqual([2, 3], [song["id"] for song in remaining["songs"]])
        self.assertEqual(2, remaining["summary"]["track_count"])

    def test_remove_tracks_rejects_liked_playlist_without_writing(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        result = self.app.api(
            "POST",
            "/api/playlists/10/remove-tracks",
            {"song_ids": [1], "source_playlist_id": 10},
        )
        self.assertEqual(409, result.status)
        self.assertEqual("PLAYLIST_KIND_INVALID", result.payload["error"]["code"])
        self.assertFalse(any(call[0] == "/playlist/tracks" for call in self.transport.calls))

    def test_unknown_remove_result_stays_local_until_read_only_reconcile(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        read_count = 0

        def tracks(params, method, cookie):
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                return {"code": 200, "songs": [api_song(4)]}
            if read_count == 2:
                raise UpstreamError("UPSTREAM_UNAVAILABLE", "temporary")
            return {"code": 200, "songs": []}

        self.transport.handlers["/playlist/track/all"] = tracks
        self.transport.handlers["/playlist/tracks"] = lambda *_: (_ for _ in ()).throw(
            UpstreamError("UPSTREAM_UNAVAILABLE", "write result unknown")
        )
        self.app.catalog.store.write(
            "catalog.json",
            {
                "version": 1,
                "playlists": {
                    "11": {
                        "summary": {"id": 11, "name": "Mine", "track_count": 1, "kind": "self_created", "privacy": "private"},
                        "songs": [Song(4, "Four", ["A"], source_playlist_id=11).to_dict()],
                    }
                },
            },
        )
        uncertain = self.app.api(
            "POST",
            "/api/playlists/11/remove-tracks",
            {"song_ids": [4], "source_playlist_id": 11},
        )
        self.assertEqual([], uncertain.payload["data"]["successful_ids"])
        self.assertEqual([], uncertain.payload["data"]["failed_items"])
        self.assertEqual("REMOVE_TRACK_RESULT_UNKNOWN", uncertain.payload["data"]["unknown_items"][0]["code"])
        self.assertEqual([4], [song["id"] for song in self.app.catalog.get()["playlists"]["11"]["songs"]])
        write_calls = sum(call[0] == "/playlist/tracks" for call in self.transport.calls)

        reconciled = self.app.api(
            "POST",
            "/api/operations/reconcile",
            {"action": "remove_tracks", "playlist_id": 11, "song_ids": [4], "source_playlist_id": 11},
        )
        self.assertEqual([4], reconciled.payload["data"]["successful_ids"])
        self.assertEqual([], self.app.catalog.get()["playlists"]["11"]["songs"])
        self.assertEqual(write_calls, sum(call[0] == "/playlist/tracks" for call in self.transport.calls))
        self.assertTrue(
            all(
                call[1].get("fresh") is True
                for call in self.transport.calls
                if call[0] == "/playlist/track/all"
            )
        )

    def test_operation_result_separates_success_failure_and_unknown(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        remote_liked = {1, 2, 3}
        fail_next_read = False

        def unlike(params, method, cookie):
            nonlocal fail_next_read
            song_id = int(params["id"])
            if song_id == 1:
                remote_liked.discard(1)
            elif song_id == 3:
                fail_next_read = True
                raise UpstreamError("UPSTREAM_UNAVAILABLE", "write result unknown")
            return {"code": 200}

        def liked_ids(params, method, cookie):
            nonlocal fail_next_read
            if fail_next_read:
                fail_next_read = False
                raise UpstreamError("UPSTREAM_UNAVAILABLE", "read unavailable")
            return {"code": 200, "ids": sorted(remote_liked)}

        self.transport.handlers["/like"] = unlike
        self.transport.handlers["/likelist"] = liked_ids
        self.app.catalog.store.write(
            "catalog.json",
            {
                "version": 1,
                "playlists": {
                    "10": {
                        "summary": {"id": 10, "name": "Liked", "track_count": 3, "kind": "liked", "privacy": "public"},
                        "songs": [Song(song_id, str(song_id), ["A"], source_playlist_id=10).to_dict() for song_id in (1, 2, 3)],
                    }
                },
            },
        )

        result = self.app.api(
            "POST",
            "/api/operations/unlike",
            {"song_ids": [1, 2, 3], "source_playlist_id": 10},
        )

        self.assertEqual([1], result.payload["data"]["successful_ids"])
        self.assertEqual([2], [item["id"] for item in result.payload["data"]["failed_items"]])
        self.assertEqual([3], [item["id"] for item in result.payload["data"]["unknown_items"]])
        self.assertEqual("UNLIKE_RESULT_UNKNOWN", result.payload["data"]["unknown_items"][0]["code"])
        self.assertEqual([2, 3], [song["id"] for song in self.app.catalog.get()["playlists"]["10"]["songs"]])

    def test_destructive_routes_require_matching_remote_source_context_before_write(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        cases = [
            ("/api/operations/unlike", {"song_ids": [1]}),
            ("/api/operations/unlike", {"song_ids": [1], "source_playlist_id": "imported"}),
            ("/api/operations/unlike", {"song_ids": [1], "source_playlist_id": 11}),
            ("/api/playlists/11/remove-tracks", {"song_ids": [1]}),
            ("/api/playlists/11/remove-tracks", {"song_ids": [1], "source_playlist_id": "imported"}),
            ("/api/playlists/11/remove-tracks", {"song_ids": [1], "source_playlist_id": 10}),
        ]
        for path, body in cases:
            with self.subTest(path=path, body=body):
                result = self.app.api("POST", path, body)
                self.assertIn(result.status, {400, 409})
        reconcile_cases = [
            {"action": "unlike", "playlist_id": None, "song_ids": [1]},
            {"action": "unlike", "playlist_id": None, "song_ids": [1], "source_playlist_id": "imported"},
            {"action": "unlike", "playlist_id": None, "song_ids": [1], "source_playlist_id": 11},
            {"action": "remove_tracks", "playlist_id": 11, "song_ids": [1]},
            {"action": "remove_tracks", "playlist_id": 11, "song_ids": [1], "source_playlist_id": "imported"},
            {"action": "remove_tracks", "playlist_id": 11, "song_ids": [1], "source_playlist_id": 10},
        ]
        for body in reconcile_cases:
            with self.subTest(reconcile=body):
                result = self.app.api("POST", "/api/operations/reconcile", body)
                self.assertIn(result.status, {400, 409})
        self.assertFalse(any(call[0] in {"/like", "/playlist/tracks"} for call in self.transport.calls))
        self.assertFalse(any(call[0] in {"/likelist", "/playlist/track/all"} for call in self.transport.calls))

    def test_reconcile_unlike_is_read_only_and_updates_only_verified_absence(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        self.transport.handlers["/likelist"] = {"code": 200, "ids": [1]}
        self.app.catalog.store.write(
            "catalog.json",
            {
                "version": 1,
                "playlists": {
                    "10": {
                        "summary": {"id": 10, "name": "Liked", "track_count": 2, "kind": "liked", "privacy": "public"},
                        "songs": [Song(song_id, str(song_id), ["A"], source_playlist_id=10).to_dict() for song_id in (1, 2)],
                    }
                },
            },
        )
        result = self.app.api(
            "POST",
            "/api/operations/reconcile",
            {"action": "unlike", "playlist_id": None, "song_ids": [1, 2], "source_playlist_id": 10},
        )
        self.assertEqual([2], result.payload["data"]["successful_ids"])
        self.assertEqual([1], [item["id"] for item in result.payload["data"]["failed_items"]])
        self.assertFalse(any(call[0] == "/like" for call in self.transport.calls))
        self.assertEqual([1], [song["id"] for song in self.app.catalog.get()["playlists"]["10"]["songs"]])

    def test_reconcile_rejects_invalid_action_and_playlist_id(self):
        bad_action = self.app.api(
            "POST",
            "/api/operations/reconcile",
            {"action": "retry", "playlist_id": None, "song_ids": [1]},
        )
        self.assertEqual(400, bad_action.status)
        self.assertEqual("RECONCILE_ACTION_INVALID", bad_action.payload["error"]["code"])
        bad_playlist = self.app.api(
            "POST",
            "/api/operations/reconcile",
            {"action": "remove_tracks", "playlist_id": "imported", "song_ids": [1], "source_playlist_id": "imported"},
        )
        self.assertEqual(400, bad_playlist.status)
        self.assertEqual("PLAYLIST_ID_INVALID", bad_playlist.payload["error"]["code"])

    def test_reconcile_remote_read_failure_is_an_api_error(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        self.transport.handlers["/likelist"] = lambda *_: (_ for _ in ()).throw(
            UpstreamError("UPSTREAM_UNAVAILABLE", "read unavailable")
        )
        result = self.app.api(
            "POST",
            "/api/operations/reconcile",
            {"action": "unlike", "playlist_id": None, "song_ids": [1], "source_playlist_id": 10},
        )
        self.assertEqual(502, result.status)
        self.assertFalse(result.payload["ok"])
        self.assertIsNone(result.payload["data"])
        self.assertEqual("UPSTREAM_UNAVAILABLE", result.payload["error"]["code"])

    def test_create_playlist_and_backup_are_verified(self):
        self.authenticate()
        self.transport.handlers["/playlist/create"] = {"code": 200, "playlist": {"id": 99}}
        self.transport.handlers["/user/playlist"] = lambda *_: {
            "code": 200,
            "playlist": [{"id": 99, "name": "Sorted", "trackCount": 0, "creator": {"userId": 42}}],
        }
        self.transport.handlers["/playlist/tracks"] = {"code": 200}
        self.transport.handlers["/playlist/track/all"] = {"code": 200, "songs": [api_song(1)]}
        created = self.app.api(
            "POST",
            "/api/operations/create-playlist",
            {"name": "Sorted", "song_ids": [1, 2], "privacy": 10},
        )
        self.assertEqual(201, created.status)
        self.assertEqual(99, created.payload["data"]["resource_id"])
        self.assertEqual([1], created.payload["data"]["successful_ids"])
        self.assertEqual([2], [item["id"] for item in created.payload["data"]["failed_items"]])
        self.assertIn("reason", created.payload["data"]["failed_items"][0])
        backup = self.app.api("POST", "/api/operations/backup", {})
        self.assertEqual(201, backup.status)
        self.assertEqual(1, len(list(self.data_dir.glob("backup-*.json"))))
        for _ in range(11):
            self.app.api("POST", "/api/operations/backup", {})
        self.assertEqual(10, len(list(self.data_dir.glob("backup-*.json"))))

    def test_sync_holds_catalog_lock_across_remote_read_and_local_replace(self):
        entered = threading.Event()
        release = threading.Event()
        removal_finished = threading.Event()

        def tracks(params, method, cookie):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return {"code": 200, "songs": [api_song(7)]}

        self.transport.handlers["/playlist/track/all"] = tracks
        playlist = PlaylistSummary(11, "Mine", 1, "self_created", "private")
        sync_thread = threading.Thread(target=lambda: self.app.catalog.sync(playlist))
        sync_thread.start()
        self.assertTrue(entered.wait(timeout=1))

        def remove_local():
            self.app.catalog.remove_confirmed_tracks(11, [7])
            removal_finished.set()

        remove_thread = threading.Thread(target=remove_local)
        remove_thread.start()
        self.assertFalse(removal_finished.wait(timeout=0.05))
        release.set()
        sync_thread.join(timeout=2)
        remove_thread.join(timeout=2)
        self.assertTrue(removal_finished.is_set())
        self.assertEqual([], self.app.catalog.get()["playlists"]["11"]["songs"])

    def test_structured_statuses_input_bounds_and_static_allowlist(self):
        missing = self.app.api("GET", "/api/nope")
        self.assertEqual(404, missing.status)
        self.assertEqual({"ok": False, "data": None}, {key: missing.payload[key] for key in ("ok", "data")})
        invalid = self.app.api("GET", "/api/audio?id=-1")
        self.assertEqual(400, invalid.status)
        self.assertEqual("SONG_ID_INVALID", invalid.payload["error"]["code"])
        self.assertEqual(200, self.app.static("/")[0])
        self.assertEqual(200, self.app.static("/app.js")[0])
        self.assertEqual(200, self.app.static("/brand.ttf")[0])
        self.assertEqual(404, self.app.static("/secret.json")[0])
        self.assertEqual(404, self.app.static("/../session.json")[0])
        self.transport.handlers["/lyric"] = []
        malformed = self.app.api("GET", "/api/lyrics?id=1")
        self.assertEqual(502, malformed.status)
        self.assertEqual("UPSTREAM_INVALID_RESPONSE", malformed.payload["error"]["code"])

    def test_http_server_is_loopback_and_has_no_cors_header(self):
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer((self.settings.host, 0), make_handler(self.app))
        self.assertEqual("127.0.0.1", server.server_address[0])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/health") as response:
                self.assertEqual(200, response.status)
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                payload = json.load(response)
                self.assertTrue(payload["ok"])
            request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/nope")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(404, caught.exception.code)
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.putrequest("POST", "/api/catalog/import")
            connection.putheader("Content-Length", str(self.settings.max_body_bytes + 1))
            connection.endheaders()
            oversized = connection.getresponse()
            self.assertEqual(413, oversized.status)
            self.assertEqual("BODY_TOO_LARGE", json.loads(oversized.read())["error"]["code"])
            self.assertIsNone(oversized.getheader("Access-Control-Allow-Origin"))
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class MatchTestCase(unittest.TestCase):
    def test_strict_match_requires_all_three_dimensions(self):
        source = Song(1, "Ａ Song!", ["Artist"], duration=100_000)
        self.assertTrue(is_strict_match(source, Song(2, "a song", ["ARTIST"], duration=104_000), 5_000))
        self.assertFalse(is_strict_match(source, Song(2, "another", ["ARTIST"], duration=100_000), 5_000))
        self.assertFalse(is_strict_match(source, Song(2, "a song", ["Other"], duration=100_000), 5_000))
        self.assertFalse(is_strict_match(source, Song(2, "a song", ["Artist"], duration=110_000), 5_000))

    def test_playlist_pages_do_not_repeat(self):
        tracks = [api_song(song_id) for song_id in range(1, 1_201)]
        first = _slice_playlist_tracks(tracks, 500, 0)
        second = _slice_playlist_tracks(tracks, 500, 500)
        third = _slice_playlist_tracks(tracks, 500, 1_000)
        self.assertEqual([1, 500], [first[0]["id"], first[-1]["id"]])
        self.assertEqual([501, 1_000], [second[0]["id"], second[-1]["id"]])
        self.assertEqual([1_001, 1_200], [third[0]["id"], third[-1]["id"]])

    def test_web_transport_fills_missing_playlist_track_details(self):
        tracks = [api_song(song_id) for song_id in range(1, 1_201)]

        def raw_request(_transport, path, params=None, **_kwargs):
            if path == "/api/v6/playlist/detail":
                return TransportResponse(200, {"code": 200, "playlist": {"tracks": tracks[:1_000], "trackIds": [{"id": item["id"]} for item in tracks]}})
            if path == "/api/v3/song/detail":
                requested = json.loads(params["ids"])
                return TransportResponse(200, {"code": 200, "songs": [tracks[song_id - 1] for song_id in requested]})
            raise AssertionError(path)

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            transport = NeteaseWebTransport("https://music.163.com")
            first = transport.request("/playlist/track/all", {"id": 88, "limit": 500, "offset": 0}).data["songs"]
            third = transport.request("/playlist/track/all", {"id": 88, "limit": 500, "offset": 1_000}).data["songs"]
        self.assertEqual([1, 500], [first[0]["id"], first[-1]["id"]])
        self.assertEqual([1_001, 1_200], [third[0]["id"], third[-1]["id"]])

    def test_web_transport_rejects_non_object_payload(self):
        with patch.object(UrllibTransport, "request", return_value=TransportResponse(200, [])):
            with self.assertRaises(UpstreamError):
                NeteaseWebTransport("https://music.163.com").request("/login/qr/key")

    def test_web_transport_invalidates_playlist_cache_before_track_write(self):
        remote_tracks = [api_song(1)]
        detail_calls = 0

        def raw_request(_transport, path, params=None, **_kwargs):
            nonlocal detail_calls, remote_tracks
            if path == "/api/v6/playlist/detail":
                detail_calls += 1
                return TransportResponse(
                    200,
                    {
                        "code": 200,
                        "playlist": {
                            "tracks": list(remote_tracks),
                            "trackIds": [{"id": item["id"]} for item in remote_tracks],
                        },
                    },
                )
            if path == "/api/playlist/manipulate/tracks":
                remote_tracks = []
                return TransportResponse(200, {"code": 200})
            raise AssertionError(path)

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            transport = NeteaseWebTransport("https://music.163.com")
            self.assertEqual([1], [item["id"] for item in transport.request("/playlist/track/all", {"id": 88}).data["songs"]])
            transport.request("/playlist/tracks", {"pid": 88, "op": "del", "tracks": "1"}, method="POST")
            self.assertEqual([], transport.request("/playlist/track/all", {"id": 88}).data["songs"])
        self.assertEqual(2, detail_calls)

    def test_fresh_playlist_read_bypasses_stale_web_transport_cache(self):
        remote_tracks = [api_song(1)]
        detail_calls = 0

        def raw_request(_transport, path, params=None, **_kwargs):
            nonlocal detail_calls
            if path == "/api/v6/playlist/detail":
                detail_calls += 1
                return TransportResponse(
                    200,
                    {
                        "code": 200,
                        "playlist": {
                            "tracks": list(remote_tracks),
                            "trackIds": [{"id": item["id"]} for item in remote_tracks],
                        },
                    },
                )
            raise AssertionError(path)

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            client = NeteaseClient(NeteaseWebTransport("https://music.163.com"))
            self.assertEqual([1], [song.id for song in client.playlist_tracks(88)])
            remote_tracks.clear()
            self.assertEqual([1], [song.id for song in client.playlist_tracks(88)])
            self.assertEqual([], client.playlist_tracks(88, fresh=True))
        self.assertEqual(2, detail_calls)


if __name__ == "__main__":
    unittest.main()
