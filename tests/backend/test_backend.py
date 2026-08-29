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

from pruner_studio.app import build_application, make_handler, validate_local_request
from pruner_studio.audio_service import is_strict_match
from pruner_studio.config import Settings
from pruner_studio.errors import AppError
from pruner_studio.models import PlaylistSummary, Song
from pruner_studio.netease_client import NeteaseClient, NeteaseWebTransport, TransportResponse, UpstreamError, UrllibTransport, _slice_playlist_tracks


class FakeTransport:
    def __init__(self):
        self.handlers: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any], str, str | None]] = []
        self.clear_count = 0

    def clear_caches(self):
        self.clear_count += 1

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

    def test_release_version_has_one_source_and_windows_metadata(self):
        from pruner_studio import __version__
        from pruner_studio.windows_version_info import write_version_file

        self.assertEqual("0.2.0", __version__)
        health = self.app.api("GET", "/api/health")
        self.assertEqual(__version__, health.payload["data"]["version"])
        self.assertIn(__version__, make_handler(self.app).server_version)
        project = Path(__file__).resolve().parents[2]
        pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertNotIn('version = "0.2.0"', pyproject)
        version_file = Path(self.temp.name) / "windows-version-info.txt"
        write_version_file(version_file)
        metadata = version_file.read_text(encoding="utf-8")
        self.assertIn("ProductName', 'Pruner Studio'", metadata)
        self.assertIn("FileVersion', '0.2.0'", metadata)
        self.assertIn("OriginalFilename', 'Pruner-Studio.exe'", metadata)

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
        self.assertEqual(1, self.transport.clear_count)
        restarted = build_application(self.settings, self.transport)
        self.assertEqual("MUSIC_U=sanitized-session", restarted.client.cookie)
        self.app.catalog.store.write("catalog.json", {"version": 1, "playlists": {}})
        logout = self.app.api("POST", "/api/auth/logout", {})
        self.assertEqual({"authenticated": False}, logout.payload["data"])
        self.assertFalse((self.data_dir / "session.json").exists())
        self.assertTrue((self.data_dir / "catalog.json").exists())
        self.assertEqual(2, self.transport.clear_count)
        created_again = self.app.api("POST", "/api/auth/qr", {})
        self.assertEqual(201, created_again.status)
        self.app.api("POST", "/api/auth/logout", {})
        self.assertEqual(3, self.transport.clear_count)
        cancelled = self.app.api("GET", "/api/auth/qr/qr-key")
        self.assertEqual(409, cancelled.status)
        self.assertEqual("QR_CANCELLED", cancelled.payload["error"]["code"])

    def test_creating_a_new_qr_cancels_the_previous_key(self):
        keys = iter(["first-key", "second-key"])
        self.transport.handlers.update(
            {
                "/login/qr/key": lambda *_args: {"code": 200, "data": {"unikey": next(keys)}},
                "/login/qr/create": lambda params, *_args: {
                    "code": 200,
                    "data": {"qrurl": f"https://music.test/{params['key']}"},
                },
                "/login/qr/check": {"code": 801, "message": "waiting"},
            }
        )

        first = self.app.api("POST", "/api/auth/qr", {})
        second = self.app.api("POST", "/api/auth/qr", {})
        self.assertEqual("first-key", first.payload["data"]["key"])
        self.assertEqual("second-key", second.payload["data"]["key"])

        cancelled = self.app.api("GET", "/api/auth/qr/first-key")
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

    def test_corrupt_local_json_fails_closed_without_overwriting_original_bytes(self):
        catalog_path = self.data_dir / "catalog.json"
        catalog_bytes = b'{"version": 1, "playlists": '
        catalog_path.write_bytes(catalog_bytes)

        viewed = self.app.api("GET", "/api/catalog")
        self.assertEqual(500, viewed.status)
        self.assertEqual("LOCAL_DATA_CORRUPT", viewed.payload["error"]["code"])
        imported = self.app.api(
            "POST",
            "/api/catalog/import",
            {"format": "json", "content": '[{"id": 1, "title": "One"}]'},
        )
        self.assertEqual(500, imported.status)
        self.assertEqual("LOCAL_DATA_CORRUPT", imported.payload["error"]["code"])
        self.assertEqual(catalog_bytes, catalog_path.read_bytes())

        rules_path = self.data_dir / "rules.json"
        rules_bytes = b'[{"id": "truncated"}'
        rules_path.write_bytes(rules_bytes)
        viewed_rules = self.app.api("GET", "/api/rules")
        self.assertEqual(500, viewed_rules.status)
        self.assertEqual("LOCAL_DATA_CORRUPT", viewed_rules.payload["error"]["code"])
        replaced_rules = self.app.api("PUT", "/api/rules", {"rules": []})
        self.assertEqual(500, replaced_rules.status)
        self.assertEqual("LOCAL_DATA_CORRUPT", replaced_rules.payload["error"]["code"])
        self.assertEqual(rules_bytes, rules_path.read_bytes())

    def test_corrupt_session_is_quarantined_and_app_starts_logged_out(self):
        session_path = self.data_dir / "session.json"
        original = b'{"cookie": "truncated"'
        session_path.write_bytes(original)
        restarted = build_application(self.settings, self.transport)
        self.assertIsNone(restarted.client.cookie)
        self.assertFalse(session_path.exists())
        quarantined = list(self.data_dir.glob("session.corrupt-*.json"))
        self.assertEqual(1, len(quarantined))
        self.assertEqual(original, quarantined[0].read_bytes())

    def test_invalid_session_shape_is_quarantined_but_unreadable_session_is_preserved(self):
        from pruner_studio.storage import JsonStore, SessionStore

        session_path = self.data_dir / "session.json"
        invalid = b'{"cookie": 123}'
        session_path.write_bytes(invalid)
        restarted = build_application(self.settings, self.transport)
        self.assertIsNone(restarted.client.cookie)
        self.assertFalse(session_path.exists())
        quarantined = list(self.data_dir.glob("session.corrupt-*.json"))
        self.assertEqual(1, len(quarantined))
        self.assertEqual(invalid, quarantined[0].read_bytes())

        isolated = JsonStore(Path(self.temp.name) / "unreadable-session")
        session = SessionStore(isolated)
        with patch.object(isolated, "read", side_effect=AppError("LOCAL_DATA_UNREADABLE", "unreadable", 500)):
            with patch.object(isolated, "quarantine") as quarantine:
                with self.assertRaisesRegex(AppError, "unreadable"):
                    session.load_cookie()
                quarantine.assert_not_called()

    def test_wrong_shape_local_json_is_reported_instead_of_silently_reset(self):
        self.app.catalog.store.write("catalog.json", {"version": 1, "playlists": []})
        invalid_catalog = self.app.api("GET", "/api/catalog")
        self.assertEqual(500, invalid_catalog.status)
        self.assertEqual("LOCAL_DATA_INVALID", invalid_catalog.payload["error"]["code"])

        self.app.catalog.store.write("rules.json", {"rules": []})
        invalid_rules = self.app.api("GET", "/api/rules")
        self.assertEqual(500, invalid_rules.status)
        self.assertEqual("LOCAL_DATA_INVALID", invalid_rules.payload["error"]["code"])

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
        self.assertEqual([], result.payload["data"]["failed_items"])
        self.assertEqual([2], [item["id"] for item in result.payload["data"]["unknown_items"]])
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
        self.assertEqual([3], [item["id"] for item in result.payload["data"]["failed_items"]])
        self.assertEqual("TRACK_NOT_IN_PLAYLIST", result.payload["data"]["failed_items"][0]["code"])
        self.assertEqual([2], [item["id"] for item in result.payload["data"]["unknown_items"]])
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
            elif song_id == 2:
                raise UpstreamError("UPSTREAM_REJECTED", "write rejected")
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

    def test_unknown_unlike_is_recovered_after_restart_and_blocks_blind_retry(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        self.app.catalog.store.write(
            "catalog.json",
            {
                "version": 1,
                "playlists": {
                    "10": {
                        "summary": {"id": 10, "name": "Liked", "track_count": 1, "kind": "liked", "privacy": "public"},
                        "songs": [Song(1, "One", ["A"], source_playlist_id=10).to_dict()],
                    }
                },
            },
        )
        remote_liked = {1}
        verification_available = False
        liked_reads = 0

        def liked_ids(*_args):
            nonlocal liked_reads
            liked_reads += 1
            if liked_reads > 1 and not verification_available:
                raise UpstreamError("UPSTREAM_UNAVAILABLE", "verification unavailable")
            return {"code": 200, "ids": sorted(remote_liked)}

        def unlike(params, *_args):
            remote_liked.discard(int(params["id"]))
            raise UpstreamError("UPSTREAM_UNAVAILABLE", "response lost after apply")

        self.transport.handlers["/likelist"] = liked_ids
        self.transport.handlers["/like"] = unlike
        uncertain = self.app.api(
            "POST",
            "/api/operations/unlike",
            {"song_ids": [1], "source_playlist_id": 10},
        )
        self.assertEqual(200, uncertain.status)
        self.assertEqual([1], [item["id"] for item in uncertain.payload["data"]["unknown_items"]])
        operation_id = uncertain.payload["data"]["pending_operation_ids"][0]
        self.assertTrue((self.data_dir / "pending-operations.json").is_file())

        restarted = build_application(self.settings, self.transport)
        status = restarted.api("GET", "/api/status")
        self.assertEqual([operation_id], [item["operation_id"] for item in status.payload["data"]["pending_operations"]])
        write_count = sum(call[0] == "/like" for call in self.transport.calls)
        retry = restarted.api(
            "POST",
            "/api/operations/unlike",
            {"song_ids": [1], "source_playlist_id": 10},
        )
        self.assertEqual(409, retry.status)
        self.assertEqual("OPERATION_PENDING", retry.payload["error"]["code"])
        self.assertEqual(write_count, sum(call[0] == "/like" for call in self.transport.calls))

        verification_available = True
        reconciled = restarted.api("POST", f"/api/operations/{operation_id}/reconcile", {})
        self.assertEqual([1], reconciled.payload["data"]["successful_ids"])
        self.assertEqual([], restarted.api("GET", "/api/operations/pending").payload["data"]["operations"])
        self.assertEqual([], restarted.catalog.get()["playlists"]["10"]["songs"])

    def test_unknown_create_and_add_are_journaled_and_reconciled_without_rewrite(self):
        self.authenticate()
        current_playlists = playlist_payload()["playlist"]
        created = False
        create_verification_available = False
        create_calls = 0

        def playlists(*_args):
            if created and not create_verification_available:
                raise UpstreamError("UPSTREAM_UNAVAILABLE", "playlist verification unavailable")
            return {"code": 200, "playlist": list(current_playlists)}

        def create(*_args):
            nonlocal created, create_calls
            create_calls += 1
            created = True
            current_playlists.append(
                {"id": 99, "name": "Recovery List", "trackCount": 0, "specialType": 0, "privacy": 10, "creator": {"userId": 42}}
            )
            raise UpstreamError("UPSTREAM_UNAVAILABLE", "create response lost after apply")

        self.transport.handlers["/user/playlist"] = playlists
        self.transport.handlers["/playlist/create"] = create
        uncertain_create = self.app.api(
            "POST",
            "/api/operations/create-playlist",
            {"name": "Recovery List", "privacy": 10, "song_ids": [7]},
        )
        self.assertEqual(201, uncertain_create.status)
        self.assertEqual("CREATE_PLAYLIST_RESULT_UNKNOWN", uncertain_create.payload["data"]["unknown_items"][0]["code"])
        create_operation_id = uncertain_create.payload["data"]["pending_operation_ids"][0]
        retry = self.app.api(
            "POST",
            "/api/operations/create-playlist",
            {"name": "Recovery List", "privacy": 10, "song_ids": [7]},
        )
        self.assertEqual(409, retry.status)
        self.assertEqual(1, create_calls)
        create_verification_available = True
        reconciled_create = self.app.api("POST", f"/api/operations/{create_operation_id}/reconcile", {})
        self.assertEqual(99, reconciled_create.payload["data"]["resource_id"])
        self.assertFalse(any(call[0] == "/playlist/tracks" for call in self.transport.calls))

        remote_tracks: set[int] = set()
        track_verification_available = False

        def add_tracks(params, *_args):
            remote_tracks.update(int(value) for value in str(params["tracks"]).split(",") if value)
            return {"code": 200}

        def playlist_tracks(*_args):
            if not track_verification_available:
                raise UpstreamError("UPSTREAM_UNAVAILABLE", "track verification unavailable")
            return {"code": 200, "songs": [api_song(song_id) for song_id in sorted(remote_tracks)]}

        self.transport.handlers["/playlist/create"] = {"code": 200, "playlist": {"id": 100}}
        current_playlists.append(
            {"id": 100, "name": "Add Recovery", "trackCount": 0, "specialType": 0, "privacy": 0, "creator": {"userId": 42}}
        )
        self.transport.handlers["/playlist/tracks"] = add_tracks
        self.transport.handlers["/playlist/track/all"] = playlist_tracks
        uncertain_add = self.app.api(
            "POST",
            "/api/operations/create-playlist",
            {"name": "Add Recovery", "privacy": 0, "song_ids": [8]},
        )
        self.assertEqual(201, uncertain_add.status)
        self.assertEqual([8], [item["id"] for item in uncertain_add.payload["data"]["unknown_items"]])
        add_operation_id = uncertain_add.payload["data"]["pending_operation_ids"][0]
        write_count = sum(call[0] == "/playlist/tracks" for call in self.transport.calls)
        track_verification_available = True
        reconciled_add = self.app.api("POST", f"/api/operations/{add_operation_id}/reconcile", {})
        self.assertEqual([8], reconciled_add.payload["data"]["successful_ids"])
        self.assertEqual(write_count, sum(call[0] == "/playlist/tracks" for call in self.transport.calls))

    def test_confirmed_create_transitions_atomically_before_add_tracks(self):
        self.authenticate()
        current_playlists = playlist_payload()["playlist"]

        def create(*_args):
            current_playlists.append(
                {"id": 99, "name": "Atomic List", "trackCount": 0, "specialType": 0, "privacy": 10, "creator": {"userId": 42}}
            )
            return {"code": 200, "playlist": {"id": 99}}

        self.transport.handlers["/user/playlist"] = lambda *_args: {"code": 200, "playlist": list(current_playlists)}
        self.transport.handlers["/playlist/create"] = create
        with patch.object(
            self.app.operations.journal,
            "transition_create_to_add",
            side_effect=AppError("LOCAL_DATA_UNREADABLE", "transition failed", 500),
        ):
            result = self.app.api(
                "POST",
                "/api/operations/create-playlist",
                {"name": "Atomic List", "privacy": 10, "song_ids": [7]},
            )
        self.assertEqual(500, result.status)
        pending = self.app.api("GET", "/api/operations/pending").payload["data"]["operations"]
        self.assertEqual(1, len(pending))
        self.assertEqual("create_playlist", pending[0]["action"])
        self.assertFalse(any(call[0] == "/playlist/tracks" for call in self.transport.calls))

    def test_ignored_add_recovery_preserves_the_confirmed_create_guard(self):
        self.authenticate()
        current_playlists = playlist_payload()["playlist"]
        create_calls = 0

        def create(*_args):
            nonlocal create_calls
            create_calls += 1
            current_playlists.append(
                {"id": 99, "name": "Guarded List", "trackCount": 0, "specialType": 0, "privacy": 10, "creator": {"userId": 42}}
            )
            return {"code": 200, "playlist": {"id": 99}}

        self.transport.handlers["/user/playlist"] = lambda *_args: {"code": 200, "playlist": list(current_playlists)}
        self.transport.handlers["/playlist/create"] = create
        self.transport.handlers["/playlist/tracks"] = {"code": 200}
        self.transport.handlers["/playlist/track/all"] = lambda *_args: (_ for _ in ()).throw(
            UpstreamError("UPSTREAM_UNAVAILABLE", "track verification unavailable")
        )
        uncertain = self.app.api(
            "POST",
            "/api/operations/create-playlist",
            {"name": "Guarded List", "privacy": 10, "song_ids": [7]},
        )
        operation_id = uncertain.payload["data"]["pending_operation_ids"][0]
        self.assertEqual("add_tracks", self.app.api("GET", "/api/operations/pending").payload["data"]["operations"][0]["action"])
        self.app.api("POST", f"/api/operations/{operation_id}/ignore", {})

        retry = self.app.api(
            "POST",
            "/api/operations/create-playlist",
            {"name": "Guarded List", "privacy": 10, "song_ids": [8]},
        )
        self.assertEqual(409, retry.status)
        self.assertEqual("OPERATION_IGNORED", retry.payload["error"]["code"])
        self.assertEqual(1, create_calls)

    def test_ignored_unknown_operation_remains_blocked_after_restart(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        self.transport.handlers["/likelist"] = lambda *_args: (_ for _ in ()).throw(
            UpstreamError("UPSTREAM_UNAVAILABLE", "verification unavailable")
        )
        # Seed a pending operation through the journal boundary without any
        # successful remote mutation, then explicitly ignore it.
        self.transport.handlers["/like"] = lambda *_args: (_ for _ in ()).throw(
            UpstreamError("UPSTREAM_UNAVAILABLE", "write result unknown")
        )
        # First precheck must be available, later reads fail.
        reads = 0
        def liked_ids(*_args):
            nonlocal reads
            reads += 1
            if reads == 1:
                return {"code": 200, "ids": [1]}
            raise UpstreamError("UPSTREAM_UNAVAILABLE", "verification unavailable")
        self.transport.handlers["/likelist"] = liked_ids
        uncertain = self.app.api("POST", "/api/operations/unlike", {"song_ids": [1], "source_playlist_id": 10})
        operation_id = uncertain.payload["data"]["pending_operation_ids"][0]
        ignored = self.app.api("POST", f"/api/operations/{operation_id}/ignore", {})
        self.assertEqual("ignored", ignored.payload["data"]["state"])

        restarted = build_application(self.settings, self.transport)
        self.assertEqual([], restarted.api("GET", "/api/operations/pending").payload["data"]["operations"])
        retry = restarted.api("POST", "/api/operations/unlike", {"song_ids": [1], "source_playlist_id": 10})
        self.assertEqual(409, retry.status)
        self.assertEqual("OPERATION_IGNORED", retry.payload["error"]["code"])

    def test_ignored_create_cannot_be_retried_by_reordering_tracks(self):
        self.authenticate()
        create_calls = 0

        def create(*_args):
            nonlocal create_calls
            create_calls += 1
            raise UpstreamError("UPSTREAM_UNAVAILABLE", "create result unknown")

        self.transport.handlers["/user/playlist"] = playlist_payload()
        self.transport.handlers["/playlist/create"] = create
        first = self.app.api(
            "POST",
            "/api/operations/create-playlist",
            {"name": "Stable Key", "privacy": 10, "song_ids": [1, 2]},
        )
        operation_id = first.payload["data"]["pending_operation_ids"][0]
        self.app.api("POST", f"/api/operations/{operation_id}/ignore", {})
        retry = self.app.api(
            "POST",
            "/api/operations/create-playlist",
            {"name": "Stable Key", "privacy": 10, "song_ids": [2, 1]},
        )
        self.assertEqual(409, retry.status)
        self.assertEqual("OPERATION_IGNORED", retry.payload["error"]["code"])
        self.assertEqual(1, create_calls)

    def test_ignored_journal_tombstones_are_not_pruned_by_count(self):
        journal = self.app.operations.journal
        for index in range(201):
            key = f"unlike:10:{index + 1}"
            entry = journal.begin(42, "unlike", [key], {"source_playlist_id": 10, "song_ids": [index + 1]})
            journal.ignore(42, entry["operation_id"])
        with self.assertRaisesRegex(AppError, "禁止盲目重试") as caught:
            journal.assert_allowed(42, ["unlike:10:1"])
        self.assertEqual("OPERATION_IGNORED", caught.exception.code)

    def test_data_directory_lock_is_exclusive_and_released(self):
        from pruner_studio.instance_lock import DataDirectoryLock

        first = DataDirectoryLock(self.data_dir)
        first.acquire()
        try:
            with self.assertRaises(AppError) as caught:
                DataDirectoryLock(self.data_dir.parent / "." / self.data_dir.name).acquire()
            self.assertEqual("INSTANCE_ALREADY_RUNNING", caught.exception.code)
        finally:
            first.release()

        second = DataDirectoryLock(self.data_dir)
        second.acquire()
        second.release()

    def test_corrupt_operation_journal_fails_closed(self):
        self.authenticate()
        journal_path = self.data_dir / "pending-operations.json"
        original = b'{"version": 1, "operations": '
        journal_path.write_bytes(original)
        pending = self.app.api("GET", "/api/operations/pending")
        self.assertEqual(500, pending.status)
        self.assertEqual("LOCAL_DATA_CORRUPT", pending.payload["error"]["code"])
        self.assertEqual(original, journal_path.read_bytes())

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
        self.assertEqual([], created.payload["data"]["failed_items"])
        self.assertEqual([2], [item["id"] for item in created.payload["data"]["unknown_items"]])
        self.assertIn("reason", created.payload["data"]["unknown_items"][0])
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
            connection.putheader("Content-Type", "application/json")
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
            self.assertFalse(thread.is_alive())

    def test_request_origin_validation_blocks_cross_site_writes(self):
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer((self.settings.host, 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def raw_request(method, path, headers, body=b""):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            effective = {"Host": f"127.0.0.1:{server.server_port}", **headers}
            connection.putrequest(method, path, skip_host=True)
            for name, value in effective.items():
                connection.putheader(name, value)
            if body:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders()
            if body:
                connection.send(body)
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            return response.status, payload

        try:
            status, payload = raw_request("GET", "/api/health", {"Host": "evil.example"})
            self.assertEqual(403, status)
            self.assertEqual("REQUEST_ORIGIN_REJECTED", payload["error"]["code"])
            status, payload = raw_request(
                "GET",
                "/api/health",
                {
                    "Host": f"127.0.0.1:{server.server_port}",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
            self.assertEqual(403, status)
            self.assertEqual("REQUEST_ORIGIN_REJECTED", payload["error"]["code"])
            status, payload = raw_request(
                "POST",
                "/api/operations/unlike",
                {"Host": f"127.0.0.1:{server.server_port}", "Origin": "http://evil.example"},
                b'{"song_ids": [1]}',
            )
            self.assertEqual(403, status)
            status, payload = raw_request(
                "POST",
                "/api/operations/unlike",
                {
                    "Host": f"127.0.0.1:{server.server_port}",
                    "Origin": f"http://127.0.0.1:{server.server_port}",
                    "Content-Type": "text/plain",
                },
                b'{"song_ids": [1]}',
            )
            self.assertEqual(415, status)
            self.assertEqual("CONTENT_TYPE_INVALID", payload["error"]["code"])
            status, payload = raw_request(
                "POST",
                "/api/auth/logout",
                {"Host": f"127.0.0.1:{server.server_port}"},
            )
            self.assertEqual(415, status)
            self.assertEqual("CONTENT_TYPE_INVALID", payload["error"]["code"])
            status, payload = raw_request(
                "POST",
                "/api/auth/logout",
                {
                    "Host": f"127.0.0.1:{server.server_port}",
                    "Content-Type": "application/json",
                },
            )
            self.assertEqual(200, status)
            status, payload = raw_request("GET", "/api/health", {"Host": f"localhost:{server.server_port}"})
            self.assertEqual(200, status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())

    def test_request_origin_validator_uses_exact_allowlists(self):
        port = 8765
        for host in ("127.0.0.1:8765", "localhost:8765", "LOCALHOST:8765", "[::1]:8765"):
            with self.subTest(host=host):
                self.assertIsNone(validate_local_request({"Host": host}, port))

        for fetch_site in ("same-origin", "none", "SAME-ORIGIN"):
            with self.subTest(fetch_site=fetch_site):
                self.assertIsNone(
                    validate_local_request(
                        {"Host": "127.0.0.1:8765", "Sec-Fetch-Site": fetch_site},
                        port,
                    )
                )

        for origin in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
            with self.subTest(origin=origin):
                self.assertIsNone(
                    validate_local_request(
                        {"Host": "127.0.0.1:8765", "Origin": origin},
                        port,
                    )
                )

        rejected_headers = (
            {},
            {"Host": "127.0.0.1"},
            {"Host": "127.0.0.1:8765.evil.example"},
            {"Host": "2130706433:8765"},
            {"Host": "127.0.0.1:8765", "Sec-Fetch-Site": "same-site"},
            {"Host": "127.0.0.1:8765", "Sec-Fetch-Site": "cross-site"},
            {"Host": "127.0.0.1:8765", "Origin": "null"},
            {"Host": "127.0.0.1:8765", "Origin": "https://127.0.0.1:8765"},
            {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8766"},
            {"Host": "127.0.0.1:8765", "Origin": "http://localhost.:8765"},
            {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765@evil.example"},
        )
        for headers in rejected_headers:
            with self.subTest(headers=headers):
                error = validate_local_request(headers, port)
                self.assertIsNotNone(error)
                self.assertEqual("REQUEST_ORIGIN_REJECTED", error.code)

    def test_expired_upstream_session_reports_unauthenticated_status(self):
        self.authenticate()
        self.transport.handlers["/user/account"] = {"code": 301}
        self.transport.clear_count = 0
        self.app._active_qr_keys.add("stale-key")
        response = self.app.api("GET", "/api/status")
        self.assertEqual(200, response.status)
        self.assertFalse(response.payload["data"]["authenticated"])
        self.assertIsNone(self.app.client.cookie)
        self.assertFalse((self.data_dir / "session.json").exists())
        self.assertEqual(1, self.transport.clear_count)
        self.assertEqual(set(), self.app._active_qr_keys)
        with self.assertRaises(AppError) as caught:
            self.app.client.account()
        self.assertEqual(401, caught.exception.status)

    def test_malformed_account_payload_does_not_discard_a_potentially_valid_session(self):
        self.authenticate()
        self.transport.handlers["/user/account"] = {"code": 200, "profile": None}
        self.transport.clear_count = 0

        response = self.app.api("GET", "/api/status")

        self.assertEqual(502, response.status)
        self.assertEqual("ACCOUNT_INVALID", response.payload["error"]["code"])
        self.assertEqual("MUSIC_U=sanitized", self.app.client.cookie)
        self.assertTrue((self.data_dir / "session.json").exists())
        self.assertEqual(0, self.transport.clear_count)

    def test_logout_waits_for_inflight_unlike_and_operation_uses_one_session(self):
        self.authenticate()
        self.transport.handlers["/user/playlist"] = playlist_payload()
        precheck_entered = threading.Event()
        release_precheck = threading.Event()
        liked_call_count = 0

        def liked_ids(_params, _method, _cookie):
            nonlocal liked_call_count
            liked_call_count += 1
            if liked_call_count == 1:
                precheck_entered.set()
                self.assertTrue(release_precheck.wait(timeout=2))
                return {"code": 200, "ids": [1]}
            return {"code": 200, "ids": []}

        self.transport.handlers["/likelist"] = liked_ids
        self.transport.handlers["/like"] = {"code": 200}
        operation_result: dict[str, Any] = {}
        logout_result: dict[str, Any] = {}
        logout_finished = threading.Event()

        operation_thread = threading.Thread(
            target=lambda: operation_result.setdefault(
                "response",
                self.app.api(
                    "POST",
                    "/api/operations/unlike",
                    {"song_ids": [1], "source_playlist_id": 10},
                ),
            )
        )
        operation_thread.start()
        self.assertTrue(precheck_entered.wait(timeout=1))

        def logout():
            logout_result["response"] = self.app.api("POST", "/api/auth/logout", {})
            logout_finished.set()

        logout_thread = threading.Thread(target=logout)
        logout_thread.start()
        logout_was_blocked = not logout_finished.wait(timeout=0.05)
        release_precheck.set()
        operation_thread.join(timeout=2)
        logout_thread.join(timeout=2)

        self.assertTrue(logout_was_blocked, "Logout must wait until the account-bound write chain finishes")
        self.assertFalse(operation_thread.is_alive())
        self.assertFalse(logout_thread.is_alive())
        self.assertEqual([1], operation_result["response"].payload["data"]["successful_ids"])
        self.assertEqual(200, logout_result["response"].status)
        account_bound_calls = [
            call
            for call in self.transport.calls
            if call[0] in {"/user/account", "/user/playlist", "/likelist", "/like"}
        ]
        self.assertTrue(account_bound_calls)
        self.assertEqual({"MUSIC_U=sanitized"}, {call[3] for call in account_bound_calls})

    def test_transport_playlist_cache_is_cleared_and_bounded(self):
        transport = NeteaseWebTransport("http://fake.invalid")
        transport._playlist_cache[1] = (1.0, [])
        transport._playlist_cache[2] = (2.0, [])
        transport.clear_caches()
        self.assertEqual({}, transport._playlist_cache)

        def raw_request(_transport, path, params=None, **_kwargs):
            if path == "/api/v6/playlist/detail":
                return TransportResponse(200, {"code": 200, "playlist": {"tracks": [api_song(1)], "trackIds": [{"id": 1}]}})
            raise AssertionError(path)

        with patch.object(UrllibTransport, "request", autospec=True, side_effect=raw_request):
            for playlist_id in range(10, 15):
                transport.request("/playlist/track/all", {"id": playlist_id})
        self.assertEqual({11, 12, 13, 14}, set(transport._playlist_cache))


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
