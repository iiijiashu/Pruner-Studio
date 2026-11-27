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

from pytest import MonkeyPatch

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


def mock_liked_detail(transport, members):
    def summaries(*args):
        result = playlist_payload()
        result["playlist"][0]["trackCount"] = len(members())
        return result

    transport.handlers["/user/playlist"] = summaries
    transport.handlers["/playlist/track/all"] = lambda *_: {
        "code": 200, "songs": [api_song(sid) for sid in sorted(members())]}



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
        self.app.client.pacer.sleep_fn = lambda _: None

    def tearDown(self):
        self.temp.cleanup()

    def authenticate(self):
        self.app.client.set_cookie("MUSIC_U=sanitized")
        self.app.session.save_cookie("MUSIC_U=sanitized")
        self.transport.handlers["/user/account"] = account_payload()

    def test_release_version_has_one_source_and_windows_metadata(self):
        from pruner_studio import __version__
        from pruner_studio.windows_version_info import write_version_file

        self.assertEqual("0.4.0", __version__)
        health = self.app.api("GET", "/api/health")
        self.assertEqual(__version__, health.payload["data"]["version"])
        self.assertIn(__version__, make_handler(self.app).server_version)
        project = Path(__file__).resolve().parents[2]
        pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('dynamic = ["version"]', pyproject)
        self.assertNotIn('version = "0.4.0"', pyproject)
        version_file = Path(self.temp.name) / "windows-version-info.txt"
        write_version_file(version_file)
        metadata = version_file.read_text(encoding="utf-8")
        self.assertIn("ProductName', 'Pruner Studio'", metadata)
        self.assertIn("FileVersion', '0.4.0'", metadata)
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
        rules = [{"id": "jp", "name": "J-Pop", "enabled": True, "match_clauses": [{"field": "artist", "operator": "contains", "value": "A"}], "output_playlist_name": "J-Pop"}]
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
        mock_liked_detail(self.transport, lambda: remote_liked)

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
        mock_liked_detail(self.transport, lambda: remote_liked)
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
        mock_liked_detail(self.transport, lambda: remote_liked)
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
        mock_liked_detail(self.transport, lambda: {1})
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
        self.assertEqual(1, len(list((self.data_dir / "backups").rglob("*.json"))))
        for _ in range(11):
            self.app.api("POST", "/api/operations/backup", {})
        self.assertEqual(10, len(list((self.data_dir / "backups").rglob("*.json"))))

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
            connection.putheader("Content-Length", str(self.settings.import_max_body_bytes + 1))
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
        mock_liked_detail(self.transport, lambda: {1} if liked_call_count < 2 else set())

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


class Phase2CoreAndSafetyTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_dir = root / "runtime"
        self.web_dir = root / "web"
        self.web_dir.mkdir()
        (self.web_dir / "index.html").write_text("<h1>Pruner</h1>", encoding="utf-8")
        (self.web_dir / "app.js").write_text("console.log('ok')", encoding="utf-8")
        (self.web_dir / "secret.json").write_text("{}", encoding="utf-8")
        self.settings = Settings(data_dir=self.data_dir, web_dir=self.web_dir, api_base="http://fake.invalid", port=0)
        self.transport = FakeTransport()
        self.app = build_application(self.settings, self.transport)
        self.app.client.pacer.sleep_fn = lambda _: None

    def tearDown(self):
        self.temp.cleanup()

    def _auth_as(self, user_id: int, nickname: str, cookie: str):
        self.app.client.set_cookie(cookie)
        self.app.session.save_cookie(cookie)
        self.transport.handlers["/user/account"] = {
            "code": 200,
            "profile": {"userId": user_id, "nickname": nickname, "avatarUrl": "https://p1.music.126.net/a.jpg"},
            "account": {"vipType": 11},
        }

    def test_session_store_dpapi_encrypts_at_rest_and_migrates_legacy_plaintext(self):
        import sys
        from pruner_studio.storage import JsonStore, SessionStore

        store = JsonStore(self.data_dir / "dpapi-test")
        session = SessionStore(store)
        secret_cookie = "MUSIC_U=super-secret-token-value"
        session.save_cookie(secret_cookie)
        raw_bytes = (store.data_dir / "session.json").read_bytes()
        raw_doc = json.loads(raw_bytes.decode("utf-8"))
        if sys.platform == "win32":
            self.assertEqual(2, raw_doc.get("version"))
            self.assertEqual("dpapi", raw_doc.get("encoding"))
            self.assertNotIn(b"super-secret-token-value", raw_bytes)
        self.assertEqual(secret_cookie, session.load_cookie())

        # Legacy plaintext session migrates transparently to DPAPI on Windows.
        legacy_cookie = "MUSIC_U=legacy-plain-cookie"
        store.write("session.json", {"cookie": legacy_cookie})
        self.assertEqual(legacy_cookie, session.load_cookie())
        if sys.platform == "win32":
            migrated_doc = json.loads((store.data_dir / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(2, migrated_doc.get("version"))
            self.assertEqual("dpapi", migrated_doc.get("encoding"))
            self.assertNotIn("legacy-plain-cookie", json.dumps(migrated_doc))

        # Corrupt DPAPI ciphertext is quarantined fail-closed.
        store.write("session.json", {"version": 2, "encoding": "dpapi", "data": "bm90LXZhbGlkLWRwYXBp"})
        self.assertIsNone(session.load_cookie())
        self.assertFalse((store.data_dir / "session.json").exists())
        self.assertEqual(1, len(list(store.data_dir.glob("session.corrupt-*.json"))))

    def test_api_base_restricts_runtime_to_trusted_netease_https_hosts(self):
        from pruner_studio.config import validate_api_base

        for trusted in (
            "https://music.163.com",
            "https://interface.music.163.com",
            "https://interface3.music.163.com",
        ):
            self.assertEqual(trusted, validate_api_base(trusted, allow_test_hosts=False))

        for untrusted in (
            "http://music.163.com",
            "https://evil.example",
            "https://music.163.com:8443",
            "https://user:pass@music.163.com",
            "http://fake.invalid",
        ):
            with self.assertRaises(ValueError):
                validate_api_base(untrusted, allow_test_hosts=False)

        with MonkeyPatch.context() as env:
            env.setenv("PRUNER_STUDIO_API_BASE", "https://evil.example")
            with self.assertRaises(ValueError):
                Settings.default()

        with self.assertRaises(ValueError):
            build_application(
                Settings(data_dir=self.data_dir, web_dir=self.web_dir, api_base="http://fake.invalid", port=0),
                transport=None,
            )

    def test_settings_store_defaults_validation_and_persistence(self):
        initial = self.app.api("GET", "/api/settings")
        self.assertEqual(200, initial.status)
        self.assertEqual("jymaster", initial.payload["data"]["quality_preference"])
        self.assertTrue(initial.payload["data"]["enable_external_sources"])
        self.assertEqual("soft_dynamic", initial.payload["data"]["visual_mode"])

        updated = self.app.api(
            "PUT",
            "/api/settings",
            {
                "quality_preference": "lossless",
                "visual_mode": "quiet_eco",
                "enable_external_sources": False,
                "custom_external_api": "http://127.0.0.1:8088/match",
                "minimize_to_tray": False,
            },
        )
        self.assertEqual(200, updated.status)
        self.assertEqual("lossless", updated.payload["data"]["quality_preference"])
        self.assertEqual("quiet_eco", updated.payload["data"]["visual_mode"])
        self.assertFalse(updated.payload["data"]["enable_external_sources"])

        bad_quality = self.app.api("PUT", "/api/settings", {"quality_preference": "ultra999"})
        self.assertEqual(400, bad_quality.status)
        self.assertEqual("SETTINGS_INVALID", bad_quality.payload["error"]["code"])

        for bad_custom in (
            "file:///etc/passwd",
            "http://example.com/api",
            "http://169.254.169.254/latest/meta-data",
            "https://10.0.0.1/api",
            "https://192.168.1.10/api",
            "https://0.0.0.0/api",
            "https://user:pass@resolver.example.com/api",
            "https://svc.internal/api",
            "https://box.local/api",
        ):
            with self.subTest(bad_custom=bad_custom):
                bad_url = self.app.api("PUT", "/api/settings", {"custom_external_api": bad_custom})
                self.assertEqual(400, bad_url.status)
                self.assertEqual("SETTINGS_INVALID", bad_url.payload["error"]["code"])

        ok_https = self.app.api("PUT", "/api/settings", {"custom_external_api": "https://resolver.example.com/api"})
        self.assertEqual(200, ok_https.status)
        self.assertEqual("https://resolver.example.com/api", ok_https.payload["data"]["custom_external_api"])

    def test_sqlite_wal_multi_account_isolation_and_offline_cached_accounts(self):
        self.assertEqual("wal", self.app.db.journal_mode())
        self.assertTrue((self.data_dir / "catalog.sqlite3").is_file())

        # Account A syncs playlist 10 with songs 1, 2
        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.transport.handlers["/user/playlist"] = {
            "code": 200,
            "playlist": [
                {"id": 10, "name": "Liked A", "trackCount": 2, "specialType": 5, "privacy": 0, "creator": {"userId": 42}},
            ],
        }
        self.transport.handlers["/playlist/track/all"] = {
            "code": 200,
            "songs": [api_song(1, "Alpha", "Artist A", 200_000), api_song(2, "Beta", "Artist B", 120_000)],
        }
        synced_a = self.app.api("POST", "/api/playlists/10/sync", {})
        self.assertEqual(200, synced_a.status)

        # Logout A, login as Account B (99)
        self.app.api("POST", "/api/auth/logout", {})
        self._auth_as(99, "User B", "MUSIC_U=token-b")
        view_b_empty = self.app.api("GET", "/api/catalog")
        self.assertEqual(200, view_b_empty.status)
        self.assertEqual([], view_b_empty.payload["data"]["playlists"])
        self.assertEqual([], view_b_empty.payload["data"]["tracks"])

        # Account B syncs playlist 20 with song 3
        self.transport.handlers["/user/playlist"] = {
            "code": 200,
            "playlist": [
                {"id": 20, "name": "Liked B", "trackCount": 1, "specialType": 5, "privacy": 0, "creator": {"userId": 99}},
            ],
        }
        self.transport.handlers["/playlist/track/all"] = {
            "code": 200,
            "songs": [api_song(3, "Gamma", "Artist C", 240_000)],
        }
        self.app.api("POST", "/api/playlists/20/sync", {})
        view_b = self.app.api("GET", "/api/catalog")
        self.assertEqual([3], [t["id"] for t in view_b.payload["data"]["tracks"]])

        # Scoped removal for B must NOT affect Account A
        self.app.catalog.remove_confirmed_unlikes([1], account_id="99", playlist_id=10)
        self.app.catalog.remove_confirmed_tracks(10, [1], account_id="99")
        view_a_direct = self.app.catalog.view(account_id="42", sort="duration", direction="asc", page=1, page_size=1)
        self.assertEqual(2, view_a_direct["total"])
        self.assertEqual([2], [t["id"] for t in view_a_direct["tracks"]])

        # Search query & pagination
        view_a_search = self.app.catalog.view(account_id="42", q="alpha")
        self.assertEqual([1], [t["id"] for t in view_a_search["tracks"]])

        # Offline status reports cached_accounts and preserves cookie without revoking
        self.transport.handlers["/user/account"] = lambda *_: (_ for _ in ()).throw(
            UpstreamError("UPSTREAM_UNAVAILABLE", "network offline")
        )
        offline_status = self.app.api("GET", "/api/status")
        self.assertEqual(200, offline_status.status)
        self.assertFalse(offline_status.payload["data"]["authenticated"])
        self.assertTrue(offline_status.payload["data"]["offline"])
        cached_ids = {item["account_id"] for item in offline_status.payload["data"]["cached_accounts"]}
        self.assertTrue({"42", "99"}.issubset(cached_ids))
        self.assertEqual("MUSIC_U=token-b", self.app.client.cookie)

        # Logout and browse Account A offline cache explicitly
        self.app.api("POST", "/api/auth/logout", {})
        offline_browse = self.app.api("GET", "/api/catalog?account_id=42")
        self.assertEqual(200, offline_browse.status)
        self.assertEqual([1, 2], [t["id"] for t in offline_browse.payload["data"]["tracks"]])

    def test_reconcile_does_not_prematurely_resolve_delayed_write_without_rejection(self):
        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.transport.handlers["/user/playlist"] = playlist_payload()
        self.app.catalog.db.replace_playlist_tracks(
            "42",
            PlaylistSummary(10, "Liked", 1, "liked", "public"),
            [Song(1, "One", ["A"], source_playlist_id=10)],
        )
        remote_liked = {1}
        mock_liked_detail(self.transport, lambda: remote_liked)
        fail_verify = True

        def liked_ids(*_args):
            nonlocal fail_verify
            if fail_verify:
                fail_verify = False
                return {"code": 200, "ids": sorted(remote_liked)}
            return {"code": 200, "ids": sorted(remote_liked)}

        self.transport.handlers["/likelist"] = liked_ids
        self.transport.handlers["/like"] = lambda *_: (_ for _ in ()).throw(
            UpstreamError("UPSTREAM_UNAVAILABLE", "timeout after server accepted write")
        )
        uncertain = self.app.api("POST", "/api/operations/unlike", {"song_ids": [1], "source_playlist_id": 10})
        self.assertEqual([1], [item["id"] for item in uncertain.payload["data"]["unknown_items"]])
        op_id = uncertain.payload["data"]["pending_operation_ids"][0]

        # Delayed write: remote STILL shows {1} -> reconcile MUST NOT prematurely resolve!
        first_reconcile = self.app.api("POST", f"/api/operations/{op_id}/reconcile", {})
        self.assertEqual(200, first_reconcile.status)
        self.assertEqual([], first_reconcile.payload["data"]["failed_items"])
        self.assertEqual([1], [item["id"] for item in first_reconcile.payload["data"]["unknown_items"]])
        self.assertEqual([op_id], first_reconcile.payload["data"]["pending_operation_ids"])

        # Blind retry is STILL blocked!
        retry = self.app.api("POST", "/api/operations/unlike", {"song_ids": [1], "source_playlist_id": 10})
        self.assertEqual(409, retry.status)
        self.assertEqual("OPERATION_PENDING", retry.payload["error"]["code"])

        # Explicit reconcile route also keeps active pending entry guarded
        reconcile_route = self.app.api(
            "POST",
            "/api/operations/reconcile",
            {"action": "unlike", "playlist_id": None, "song_ids": [1], "source_playlist_id": 10},
        )
        self.assertEqual([1], [item["id"] for item in reconcile_route.payload["data"]["unknown_items"]])
        self.assertEqual(1, len(self.app.operations.pending_for(42)))

        # Once delayed write lands remotely, reconcile resolves cleanly and removes locally
        remote_liked.discard(1)
        final_reconcile = self.app.api("POST", f"/api/operations/{op_id}/reconcile", {})
        self.assertEqual([1], final_reconcile.payload["data"]["successful_ids"])
        self.assertEqual([], final_reconcile.payload["data"]["unknown_items"])
        self.assertEqual([], self.app.operations.pending_for(42))
        self.assertEqual([], self.app.catalog.view(account_id="42")["tracks"])

    def test_destructive_writes_verify_session_generation_and_manifest_hash(self):
        from pruner_studio.operations import compute_fnv1a_manifest_hash, compute_manifest_hash

        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.transport.handlers["/user/playlist"] = playlist_payload()
        remote_liked = {1, 2}
        mock_liked_detail(self.transport, lambda: remote_liked)
        self.transport.handlers["/likelist"] = lambda *_: {"code": 200, "ids": sorted(remote_liked)}
        self.transport.handlers["/like"] = lambda params, *_: (remote_liked.discard(int(params["id"])), {"code": 200})[1]

        status = self.app.api("GET", "/api/status")
        gen = status.payload["data"]["session_generation"]

        stale = self.app.api(
            "POST",
            "/api/operations/unlike",
            {"song_ids": [1], "source_playlist_id": 10, "session_generation": gen + 99},
        )
        self.assertEqual(409, stale.status)
        self.assertEqual("SESSION_GENERATION_MISMATCH", stale.payload["error"]["code"])

        bad_hash = self.app.api(
            "POST",
            "/api/operations/unlike",
            {"song_ids": [1], "source_playlist_id": 10, "session_generation": gen, "manifest_hash": "m_deadbeef"},
        )
        self.assertEqual(409, bad_hash.status)
        self.assertEqual("MANIFEST_HASH_MISMATCH", bad_hash.payload["error"]["code"])

        # Matching frontend FNV-1a manifest hash succeeds
        fnv_hash = compute_fnv1a_manifest_hash("unlike", 10, [1])
        ok_fnv = self.app.api(
            "POST",
            "/api/operations/unlike",
            {"song_ids": [1], "source_playlist_id": 10, "session_generation": gen, "manifest_hash": fnv_hash},
        )
        self.assertEqual(200, ok_fnv.status)
        self.assertEqual([1], ok_fnv.payload["data"]["successful_ids"])

        # Matching canonical SHA-256 manifest hash also succeeds
        sha_hash = compute_manifest_hash("unlike", None, 10, [2])
        ok_sha = self.app.api(
            "POST",
            "/api/operations/unlike",
            {"song_ids": [2], "source_playlist_id": 10, "session_generation": gen, "manifest_hash": sha_hash},
        )
        self.assertEqual(200, ok_sha.status)
        self.assertEqual([2], ok_sha.payload["data"]["successful_ids"])

        # Logout increments session_generation
        self.app.api("POST", "/api/auth/logout", {})
        self.assertGreater(self.app.session_generation, gen)

    def test_unlike_mini_batches_reads_and_circuit_breaks_on_transport_error(self):
        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.transport.handlers["/user/playlist"] = playlist_payload()
        remote_liked = set(range(1, 26))
        mock_liked_detail(self.transport, lambda: remote_liked)
        self.transport.handlers["/likelist"] = lambda *_: {"code": 200, "ids": sorted(remote_liked)}
        self.transport.handlers["/like"] = lambda params, *_: (remote_liked.discard(int(params["id"])), {"code": 200})[1]

        res = self.app.api("POST", "/api/operations/unlike", {"song_ids": list(range(1, 26)), "source_playlist_id": 10})
        self.assertEqual(200, res.status)
        self.assertEqual(25, len(res.payload["data"]["successful_ids"]))
        likelist_calls = sum(call[0] == "/likelist" for call in self.transport.calls)
        # 1 precheck + 4 mini-batch verifications (8 + 8 + 8 + 1).
        self.assertEqual(5, likelist_calls)

        # Circuit-breaker test: fail on song 103 out of 101..115 -> 104..115 NEVER sent!
        self.transport.calls.clear()
        remote_liked = set(range(101, 116))

        def flaky_unlike(params, *_args):
            sid = int(params["id"])
            if sid == 103:
                raise UpstreamError("UPSTREAM_UNAVAILABLE", "rate limited / network drop")
            remote_liked.discard(sid)
            return {"code": 200}

        self.transport.handlers["/like"] = flaky_unlike
        cb_res = self.app.api("POST", "/api/operations/unlike", {"song_ids": list(range(101, 116)), "source_playlist_id": 10})
        self.assertEqual(200, cb_res.status)
        self.assertEqual([101, 102], cb_res.payload["data"]["successful_ids"])
        self.assertEqual([103], [item["id"] for item in cb_res.payload["data"]["unknown_items"]])
        aborted_ids = [item["id"] for item in cb_res.payload["data"]["failed_items"] if item["code"] == "UNLIKE_ABORTED_CIRCUIT_OPEN"]
        self.assertEqual(list(range(104, 116)), aborted_ids)
        sent_like_ids = [int(call[1]["id"]) for call in self.transport.calls if call[0] == "/like"]
        self.assertEqual([101, 102, 103], sent_like_ids)

    def test_risk_signals_pacing_and_cookie_isolation(self):
        from pruner_studio.netease_client import NeteaseRequestPacer
        now = [0.0]
        sleeps = []

        def advance(delay):
            sleeps.append(delay)
            now[0] += delay

        pacer = NeteaseRequestPacer(advance, lambda: now[0], lambda lo, hi: lo)
        client = NeteaseClient(self.transport, "cookie-A", pacer)
        self.transport.handlers["/like"] = {"code": 200}
        client.request_like(1)
        client.with_cookie("cookie-A").request_like(2)
        self.assertEqual([0.45], sleeps)
        for response in [TransportResponse(status, None) for status in (405, 429, 503)] + [
            {"code": code} for code in (-460, -462, -447, 405, 429)
        ] + [{"code": 403, "msg": word} for word in ("频繁", "风控", "CHEATING", "too fast", "Risk", "拥挤", "验证")]:
            with self.subTest(response=response):
                self.transport.handlers["/likelist"] = response
                with self.assertRaises(AppError) as caught:
                    client.liked_ids(42)
                self.assertEqual(("UPSTREAM_RISK_CONTROL", 429), (caught.exception.code, caught.exception.status))
                calls = len(self.transport.calls)
                with self.assertRaises(AppError) as cooled:
                    client.with_cookie("cookie-A").request_like(3)
                self.assertEqual("RISK_CONTROL_COOLDOWN", cooled.exception.code)
                self.assertEqual(calls, len(self.transport.calls))
                self.assertAlmostEqual(60, client.cooldown_seconds())
                client.with_cookie("cookie-B").request_like(4)
                now[0] += 61
                client.request_like(5)

    def test_concurrent_snapshot_clients_serialize_writes(self):
        from concurrent.futures import ThreadPoolExecutor
        import time
        client = self.app.client.with_cookie("same")
        workers = 5
        barrier = threading.Barrier(workers)
        guard = threading.Lock()
        active = 0
        peak = 0

        def transport_write(*args):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with guard:
                active -= 1
            return {"code": 200}

        def like(sid):
            barrier.wait(timeout=3)
            client.with_cookie("same").request_like(sid)

        self.transport.handlers["/like"] = transport_write
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(like, range(1, workers + 1)))
        self.assertEqual(1, peak)

    def test_verification_read_rejection_keeps_heart_pending(self):
        self._auth_as(42, "Test", "cookie")
        self.transport.handlers["/user/playlist"] = playlist_payload()
        self.transport.handlers["/like"] = {"code": 200}
        self.transport.handlers["/likelist"] = {"code": 403}
        result = self.app.operations.like_track(5)
        self.transport.handlers["/likelist"] = {"code": 200, "ids": []}
        verified = self.app.operations.reconcile_pending(42, result.operation_id)
        self.assertEqual([result.operation_id], verified.pending_operation_ids)
        self.assertEqual([], verified.failed_items)

    def test_playlist_chunks_break_and_journal_only_sent_unknowns(self):
        self._auth_as(42, "Test", "cookie")
        self.transport.handlers["/user/playlist"] = playlist_payload()
        ids = list(range(1, 116))
        for adding in (True, False):
            with self.subTest(adding=adding):
                self.app.client.pacer.risk_cooldown_until.clear()
                self.transport.calls.clear()
                remote = set() if adding else set(ids)
                writes = []

                def write(params, *_):
                    chunk = [int(sid) for sid in params["tracks"].split(",")]
                    writes.append(chunk)
                    if len(writes) == 2:
                        return {"code": -460}
                    if adding:
                        remote.update(chunk)
                    else:
                        remote.difference_update(chunk)
                    return {"code": 200}

                self.transport.handlers["/playlist/tracks"] = write
                self.transport.handlers["/playlist/track/all"] = lambda *_: {
                    "code": 200, "songs": [api_song(sid) for sid in sorted(remote)]}
                events = self.app._sync_bus.subscribe()
                action = "add-tracks" if adding else "remove-tracks"
                res = self.app.api("POST", f"/api/playlists/11/{action}", {"song_ids": ids, "source_playlist_id": 11})
                data = res.payload["data"]
                progress = []
                while not events.empty():
                    progress.append(events.get_nowait())
                self.app._sync_bus.unsubscribe(events)
                self.assertEqual("operation_progress", progress[-1]["_event_type"])
                self.assertEqual(("42", self.app.session_generation), (progress[-1]["account_id"], progress[-1]["session_generation"]))
                self.assertEqual((100, 115, 50, "aborted"), tuple(progress[-1][key] for key in
                                 ("processed", "total", "succeeded", "status")))
                self.assertEqual([ids[:50], ids[50:100]], writes)
                self.assertEqual(ids[:50], data["confirmed_ids"])
                self.assertEqual(ids[50:100], [item["id"] for item in data["unknown_items"]])
                self.assertEqual(ids[100:], [item["id"] for item in data["failed_items"]])
                self.assertEqual({"ABORTED_RISK_CONTROL"}, {item["code"] for item in data["failed_items"]})
                self.assertGreater(data["cooldown_sec"], 0)
                self.assertEqual(1 if adding else 2, sum(c[0] == "/playlist/track/all" for c in self.transport.calls))
                entry, = self.app.operations.pending_for(42)
                self.assertEqual(ids[50:100], entry["target"]["song_ids"])
                self.app.operations.journal.resolve(entry["operation_id"])

    def test_like_wal_reconcile_and_bidirectional_membership(self):
        self._auth_as(42, "Test", "cookie")
        self.transport.handlers["/user/playlist"] = playlist_payload()
        remote = set()
        mock_liked_detail(self.transport, lambda: remote)
        self.transport.handlers["/likelist"] = lambda *_: {"code": 200, "ids": list(remote)}

        def write(*_):
            entry, = self.app.operations.pending_for(42)
            self.assertEqual([71], entry["target"]["song_ids"])
            return {"code": 200}

        self.transport.handlers["/like"] = write
        result = self.app.operations.like_track(71)
        self.assertEqual([71], [item.id for item in result.unknown_items])
        self.assertEqual([result.operation_id], result.pending_operation_ids)
        with self.assertRaises(AppError):
            self.app.operations.like_track(71)
        remote.add(71)
        verified = self.app.operations.reconcile_pending(42, result.operation_id)
        self.assertEqual([71], verified.successful_ids)
        self.assertEqual([71], [row["id"] for row in self.app.catalog.get("42")["playlists"]["10"]["songs"]])
        remote.clear()
        self.assertEqual([71], self.app.operations.like_track(71, False).successful_ids)
        self.assertEqual([], self.app.catalog.get("42")["playlists"]["10"]["songs"])
        self.transport.handlers["/like"] = {"code": 403, "message": "denied"}
        rejected = self.app.operations.like_track(72)
        self.assertEqual(["UPSTREAM_REJECTED"], [item.code for item in rejected.failed_items])
        self.assertEqual([], self.app.operations.pending_for(42))

    def test_settings_cache_and_stream_lifetime(self):
        audio = self.app.audio
        audio._audio_cache["sentinel"] = True
        audio._stream_tokens["active"] = object()
        self.app.api("PUT", "/api/settings", {"crossfade_ms": 300})
        self.assertIn("sentinel", audio._audio_cache)
        self.app.api("PUT", "/api/settings", {"quality_preference": "standard"})
        self.assertNotIn("sentinel", audio._audio_cache)
        self.assertIn("active", audio._stream_tokens)
        self.app._revoke_session()
        self.assertNotIn("active", audio._stream_tokens)

    def test_snapshot_membership_replace_backup_and_tamper_rejection(self):
        cat = self.app.catalog
        first = Song(1, "A", ["a"], chorus_start=15000)
        second = Song(2, "B", ["b"])
        for pid, songs in [(11, [second, first]), (12, [first]), (13, [])]:
            cat.db.replace_playlist_tracks("local", PlaylistSummary(pid, str(pid), len(songs), "self_created", "private"), songs)
        response = self.app.api("POST", "/api/operations/backup", {}).payload["data"]
        route = f"/api/operations/backup/{response['backup_id']}/download"
        raw = self.app.api("GET", route).payload.decode()
        cat.import_catalog(raw, "json", account_id="other", mode="replace")
        self.assertEqual(cat.get("local"), cat.get("other"))
        result = cat.import_catalog(raw, "json", account_id="other", mode="replace")
        self.assertTrue(any((self.data_dir / "backups").rglob(result["backup_id"] + ".json")))
        path = Path(response["snapshot_path"])
        snapshot = json.loads(raw)
        snapshot["tracks"][0]["title"] = "tampered"
        path.write_text(json.dumps(snapshot), encoding="utf-8")
        self.assertEqual(404, self.app.api("GET", route).status)
        self.app.api("GET", "/api/catalog?account_id=local")
        self.assertEqual(500, self.app.api("GET", route).status)
        with self.assertRaises(AppError):
            cat.import_catalog(path.read_text(encoding="utf-8"), "json", account_id="other")
        self.assertEqual(cat.get("local"), cat.get("other"))
        self.assertEqual(404, self.app.api("GET", "/api/operations/backup/../download").status)

    def test_empty_import_and_verified_download(self):
        def upload(rows, mode="merge"):
            return self.app.api("POST", "/api/catalog/import", {"content": json.dumps(rows), "format": "json", "mode": mode})
        self.assertEqual(201, upload([{"id": 1, "title": "First"}]).status)
        rejected = upload([], "replace")
        self.assertEqual(400, rejected.status)
        self.assertEqual("IMPORT_NO_VALID_TRACKS", rejected.payload["error"]["code"])
        self.assertEqual(400, upload([{"id": 2, "title": "Second"}], "overwrite").status)
        upload([{"id": 1, "title": "Duplicate"}, {"id": 2, "title": "Second"}])
        backup = self.app.api("POST", "/api/operations/backup", {}).payload["data"]
        raw = self.app.api("GET", f"/api/operations/backup/{backup['backup_id']}/download").payload
        self.assertEqual([1, 2], json.loads(raw)["playlists"][0]["track_ids"])
        self.assertEqual(Path(backup["snapshot_path"]).read_bytes(), raw)
        replaced = upload([{"id": 3, "title": "Third"}], "replace").payload["data"]
        self.assertEqual([3], [row["id"] for row in replaced["tracks"]])
        prior = self.app.api("GET", f"/api/operations/backup/{replaced['backup_id']}/download")
        self.assertEqual([1, 2], json.loads(prior.payload)["playlists"][0]["track_ids"])

    def test_like_track_and_add_tracks_enforce_ownership_and_verify(self):
        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.transport.handlers["/user/playlist"] = playlist_payload()
        remote_liked: set[int] = set()
        self.transport.handlers["/like"] = lambda params, *_: (
            remote_liked.add(int(params["id"])) if str(params.get("like")).lower() == "true" else remote_liked.discard(int(params["id"])),
            {"code": 200},
        )[1]
        self.transport.handlers["/likelist"] = lambda *_: {"code": 200, "ids": sorted(remote_liked)}

        liked = self.app.api("POST", "/api/operations/like", {"song_id": 5, "like": True})
        self.assertEqual(200, liked.status)
        self.assertEqual([5], liked.payload["data"]["successful_ids"])

        # Add tracks to owned self_created playlist 11
        remote_pl: set[int] = set()
        self.transport.handlers["/playlist/tracks"] = lambda params, *_: (
            remote_pl.update(int(x) for x in str(params["tracks"]).split(",") if x),
            {"code": 200},
        )[1]
        self.transport.handlers["/playlist/track/all"] = lambda *_: {
            "code": 200,
            "songs": [api_song(sid) for sid in sorted(remote_pl)],
        }
        added = self.app.api("POST", "/api/playlists/11/add-tracks", {"song_ids": [7, 8]})
        self.assertEqual(200, added.status)
        self.assertEqual([7, 8], added.payload["data"]["successful_ids"])

        # Reject subscribed playlist 12 and liked playlist 10
        sub_rej = self.app.api("POST", "/api/playlists/12/add-tracks", {"song_ids": [9]})
        self.assertEqual(404, sub_rej.status)
        liked_rej = self.app.api("POST", "/api/playlists/10/add-tracks", {"song_ids": [9]})
        self.assertEqual(409, liked_rej.status)
        self.assertEqual("PLAYLIST_KIND_INVALID", liked_rej.payload["error"]["code"])

    def test_playback_and_lyrics_do_not_block_during_playlist_sync(self):
        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.transport.handlers["/user/playlist"] = playlist_payload()
        sync_entered = threading.Event()
        release_sync = threading.Event()

        def slow_tracks(*_args):
            sync_entered.set()
            self.assertTrue(release_sync.wait(timeout=3))
            return {"code": 200, "songs": [api_song(1)]}

        self.transport.handlers["/playlist/track/all"] = slow_tracks
        self.transport.handlers["/song/url/v1"] = {
            "code": 200,
            "data": [{"id": 1, "url": "https://m8.music.126.net/a.flac", "level": "lossless", "type": "flac", "br": 999000}],
        }
        self.transport.handlers["/lyric/new"] = {
            "code": 200,
            "lrc": {"lyric": "[00:01.00]Hello"},
            "tlyric": {"lyric": ""},
            "romalrc": {"lyric": ""},
            "yrc": {"lyric": "[1000,500](1000,500,0)Hello"},
        }
        self.transport.handlers["/song/chorus"] = {"code": 200, "chorus": [{"startTime": 45000, "endTime": 75000}]}

        sync_out: dict[str, Any] = {}
        sync_thread = threading.Thread(target=lambda: sync_out.setdefault("resp", self.app.api("POST", "/api/playlists/10/sync", {})))
        sync_thread.start()
        self.assertTrue(sync_entered.wait(timeout=2))

        # Audio and lyrics must return IMMEDIATELY while sync is blocked in-flight!
        audio_resp = self.app.api("GET", "/api/audio?id=1")
        lyrics_resp = self.app.api("GET", "/api/lyrics?id=1")
        self.assertEqual(200, audio_resp.status)
        self.assertEqual("lossless", audio_resp.payload["data"]["level"])
        self.assertEqual(200, lyrics_resp.status)
        self.assertEqual(45000, lyrics_resp.payload["data"]["chorus_start"])
        self.assertEqual(75000, lyrics_resp.payload["data"]["chorus_end"])
        self.assertIn("(1000,500,0)Hello", lyrics_resp.payload["data"]["yrc"])

        release_sync.set()
        sync_thread.join(timeout=3)
        self.assertEqual(200, sync_out["resp"].status)

    def test_search_cloud_recommend_feedback_and_local_scan_routes(self):
        import struct

        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.transport.handlers["/cloudsearch"] = {"code": 200, "result": {"songs": [api_song(11, "Search Hit", "Jay")]}}
        self.transport.handlers["/user/cloud"] = {
            "code": 200,
            "data": [{"songId": 21, "songName": "Cloud Song", "artist": "Cloud Artist", "album": "Cloud Album", "simpleSong": api_song(21, "Cloud Song", "Cloud Artist")}],
        }
        self.transport.handlers["/recommend/songs"] = {"code": 200, "data": {"dailySongs": [api_song(31, "Daily", "Singer")]}}
        self.transport.handlers["/personal_fm"] = {"code": 200, "data": [api_song(32, "FM Track", "Singer")]}
        self.transport.handlers["/playmode/intelligence/list"] = {"code": 200, "data": [{"songInfo": api_song(33, "Heartbeat", "Singer")}]}
        self.transport.handlers["/simi/song"] = {"code": 200, "songs": [api_song(34, "Similar", "Singer")]}

        self.assertEqual([11], [s["id"] for s in self.app.api("GET", "/api/search?q=Jay").payload["data"]["songs"]])
        self.assertEqual([21], [s["id"] for s in self.app.api("GET", "/api/cloud").payload["data"]["songs"]])
        self.assertEqual([31], [s["id"] for s in self.app.api("GET", "/api/recommend/daily").payload["data"]["songs"]])
        self.assertEqual([32], [s["id"] for s in self.app.api("GET", "/api/recommend/fm").payload["data"]["songs"]])
        self.assertEqual(
            [33],
            [s["id"] for s in self.app.api("GET", "/api/recommend/intelligence?song_id=1&playlist_id=10").payload["data"]["songs"]],
        )
        self.assertEqual([34], [s["id"] for s in self.app.api("GET", "/api/recommend/similar?id=1").payload["data"]["songs"]])

        # Explicit feedback CRUD
        fb = self.app.api("POST", "/api/feedback", {"song_id": 31, "action": "not_interested", "context": "daily"})
        self.assertEqual(201, fb.status)
        # Daily recommend now filters out song 31!
        self.assertEqual([], self.app.api("GET", "/api/recommend/daily").payload["data"]["songs"])
        deleted = self.app.api("DELETE", "/api/feedback", {})
        self.assertEqual(1, deleted.payload["data"]["deleted"])
        self.assertEqual([31], [s["id"] for s in self.app.api("GET", "/api/recommend/daily").payload["data"]["songs"]])

        # Local library scan with a minimal WAV file
        music_dir = Path(self.temp.name) / "music"
        music_dir.mkdir()
        wav_path = music_dir / "Artist X - Demo Track.wav"
        wav_bytes = b"RIFF" + struct.pack("<I", 40) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 176400, 4, 16) + b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
        wav_path.write_bytes(wav_bytes)
        scanned = self.app.api("POST", "/api/local-library/scan", {"dirs": [str(music_dir)]})
        self.assertEqual(200, scanned.status)
        self.assertEqual(1, scanned.payload["data"]["scanned_count"])
        self.assertEqual(1, scanned.payload["data"]["scanned_files"])
        self.assertEqual(1, len(self.app.db.list_local_files()))

    def test_starlette_asgi_security_cover_stream_and_sse(self):
        import struct
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import httpx
        from pruner_studio.app import create_asgi_app
        from pruner_studio.desktop import BackgroundUvicornServer, bind_loopback_socket

        # Prepare a local WAV file & stream token
        audio_file = Path(self.temp.name) / "sample.wav"
        payload_bytes = b"RIFF" + struct.pack("<I", 52) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 176400, 4, 16) + b"data" + struct.pack("<I", 16) + b"0123456789abcdef"
        audio_file.write_bytes(payload_bytes)
        local_target = self.app.audio.register_stream_token(
            session_generation=self.app.session_generation, kind="local",
            path=audio_file,
            content_type="audio/wav",
            format_name="wav",
            provider="local_library",
        )
        bad_remote_target = self.app.audio.register_stream_token(
            session_generation=self.app.session_generation, kind="remote",
            url="http://127.0.0.1:1/private.mp3",
            headers={"Cookie": "MUSIC_U=MUST_NEVER_LEAK"},
            content_type="audio/mpeg",
            format_name="mp3",
            provider="external",
        )

        # Mock remote audio server allowed via custom_external_api to verify zero-Cookie streaming,
        # redirect SSRF blocking, and strict non-audio MIME rejection.
        received_remote_headers: list[dict[str, str]] = []
        pruner_port_holder: dict[str, int] = {"port": 0}

        class MockRemoteAudio(BaseHTTPRequestHandler):
            def do_GET(self):
                received_remote_headers.append({k.lower(): v for k, v in self.headers.items()})
                if self.path == "/redirect-meta":
                    self.send_response(302)
                    self.send_header("Location", "http://169.254.169.254/latest/meta-data")
                    self.end_headers()
                    return
                if self.path == "/redirect-other-loopback":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{pruner_port_holder['port']}/api/health")
                    self.end_headers()
                    return
                if self.path == "/redirect-ok":
                    self.send_response(302)
                    self.send_header("Location", "/track.mp3")
                    self.end_headers()
                    return
                if self.path == "/json":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "11")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                if self.path == "/plain":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"error")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", "8")
                self.end_headers()
                self.wfile.write(b"ID3AUDIO")

            def log_message(self, format, *args):
                return

        remote_srv = HTTPServer(("127.0.0.1", 0), MockRemoteAudio)
        remote_port = remote_srv.server_address[1]
        remote_thread = threading.Thread(target=remote_srv.serve_forever, daemon=True)
        remote_thread.start()

        sock, port = bind_loopback_socket("127.0.0.1", 0)
        pruner_port_holder["port"] = port
        asgi = create_asgi_app(self.app, port)
        server = BackgroundUvicornServer(asgi, sock)
        server.start()
        try:
            # 1. Health check, no CORS
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", "/api/health")
            resp = conn.getresponse()
            self.assertEqual(200, resp.status)
            self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
            self.assertTrue(json.loads(resp.read())["ok"])
            conn.close()

            # 2. Invalid Host rejected (403)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.putrequest("GET", "/api/health", skip_host=True)
            conn.putheader("Host", "evil.example")
            conn.endheaders()
            resp = conn.getresponse()
            self.assertEqual(403, resp.status)
            self.assertEqual("REQUEST_ORIGIN_REJECTED", json.loads(resp.read())["error"]["code"])
            conn.close()

            # 3. /api/cover SSRF blocked for non-126.net and HTTP
            for bad_cover in ("http://127.0.0.1/x.jpg", "https://evil.example/x.jpg", "http://p1.music.126.net/x.jpg"):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", f"/api/cover?url={urllib.request.quote(bad_cover, safe='')}&size=64")
                resp = conn.getresponse()
                self.assertEqual(403, resp.status)
                self.assertEqual("COVER_URL_REJECTED", json.loads(resp.read())["error"]["code"])
                conn.close()

            # Save a resolution-affecting setting with an active token, then check
            # the same token through the HTTP streaming route below.
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as loopback:
                self.assertEqual(200, loopback.put("/api/settings", json={"quality_preference": "standard"}).status_code)
                backup = loopback.post("/api/operations/backup", json={}).json()["data"]
                downloaded = loopback.get(f"/api/operations/backup/{backup['backup_id']}/download")
                self.assertEqual(200, downloaded.status_code)
                self.assertEqual(Path(backup["snapshot_path"]).read_bytes(), downloaded.content)

            # 4. /api/stream/{token} local Range 200 / 206 / 416
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", f"/api/stream/{local_target.token}")
            resp = conn.getresponse()
            self.assertEqual(200, resp.status)
            self.assertEqual(payload_bytes, resp.read())
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", f"/api/stream/{local_target.token}", headers={"Range": "bytes=0-15"})
            resp = conn.getresponse()
            self.assertEqual(206, resp.status)
            self.assertEqual(f"bytes 0-15/{len(payload_bytes)}", resp.getheader("Content-Range"))
            self.assertEqual(payload_bytes[:16], resp.read())
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", f"/api/stream/{local_target.token}", headers={"Range": "bytes=99999-100000"})
            resp = conn.getresponse()
            self.assertEqual(416, resp.status)
            resp.read()
            conn.close()

            # 5. Private remote stream URL blocked (403) when custom_external_api is unset
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", f"/api/stream/{bad_remote_target.token}")
            resp = conn.getresponse()
            self.assertEqual(403, resp.status)
            self.assertEqual("STREAM_URL_REJECTED", json.loads(resp.read())["error"]["code"])
            conn.close()

            # 6. Allowed remote stream strips Cookie completely (ZERO-COOKIE ISOLATION)
            self.app.client.set_cookie("MUSIC_U=TOP_SECRET_NETEASE_SESSION")
            self.app.user_settings.save({"custom_external_api": f"http://127.0.0.1:{remote_port}/resolve"})
            ok_remote_target = self.app.audio.register_stream_token(
                session_generation=self.app.session_generation, kind="remote",
                url=f"http://127.0.0.1:{remote_port}/track.mp3",
                headers={"Referer": "https://www.bilibili.com/", "Cookie": "MUSIC_U=MUST_NEVER_LEAK"},
                content_type="audio/mpeg",
                format_name="mp3",
                provider="custom_endpoint",
            )
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", f"/api/stream/{ok_remote_target.token}")
            resp = conn.getresponse()
            self.assertEqual(200, resp.status)
            self.assertEqual(b"ID3AUDIO", resp.read())
            conn.close()
            self.assertEqual(1, len(received_remote_headers))
            self.assertNotIn("cookie", received_remote_headers[0])
            self.assertEqual("https://www.bilibili.com/", received_remote_headers[0].get("referer"))

            # 6b. Unsafe 302 redirects (link-local metadata & other loopback port) are blocked with 403
            for bad_path in ("/redirect-meta", "/redirect-other-loopback"):
                redir_target = self.app.audio.register_stream_token(
                    session_generation=self.app.session_generation, kind="remote",
                    url=f"http://127.0.0.1:{remote_port}{bad_path}",
                    headers={"Cookie": "MUSIC_U=NEVER"},
                    content_type="audio/mpeg",
                    format_name="mp3",
                    provider="custom_endpoint",
                )
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", f"/api/stream/{redir_target.token}")
                resp = conn.getresponse()
                self.assertEqual(403, resp.status)
                self.assertEqual("STREAM_URL_REJECTED", json.loads(resp.read())["error"]["code"])
                conn.close()

            # 6c. Safe same-endpoint 302 redirect succeeds while keeping Cookie stripped on every hop
            before_count = len(received_remote_headers)
            ok_redir = self.app.audio.register_stream_token(
                session_generation=self.app.session_generation, kind="remote",
                url=f"http://127.0.0.1:{remote_port}/redirect-ok",
                headers={"Cookie": "MUSIC_U=NEVER"},
                content_type="audio/mpeg",
                format_name="mp3",
                provider="custom_endpoint",
            )
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", f"/api/stream/{ok_redir.token}")
            resp = conn.getresponse()
            self.assertEqual(200, resp.status)
            self.assertEqual(b"ID3AUDIO", resp.read())
            conn.close()
            self.assertEqual(before_count + 2, len(received_remote_headers))
            self.assertTrue(all("cookie" not in hdrs for hdrs in received_remote_headers[before_count:]))

            # 6d. Non-audio responses (application/json, text/plain) are rejected with 502
            for bad_mime_path in ("/json", "/plain"):
                mime_target = self.app.audio.register_stream_token(
                    session_generation=self.app.session_generation, kind="remote",
                    url=f"http://127.0.0.1:{remote_port}{bad_mime_path}",
                    headers={},
                    content_type="audio/mpeg",
                    format_name="mp3",
                    provider="custom_endpoint",
                )
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", f"/api/stream/{mime_target.token}")
                resp = conn.getresponse()
                self.assertEqual(502, resp.status)
                self.assertEqual("STREAM_UPSTREAM_INVALID", json.loads(resp.read())["error"]["code"])
                conn.close()

            # 6e. Direct _is_remote_stream_url_allowed checks for credentials, .internal, 0.0.0.0
            from pruner_studio.app import _is_remote_stream_url_allowed

            for unsafe in (
                "https://user:pass@cdn.example.com/a.mp3",
                "https://audio.internal/a.mp3",
                "https://0.0.0.0/a.mp3",
                f"http://user:pass@127.0.0.1:{remote_port}/track.mp3",
            ):
                self.assertFalse(_is_remote_stream_url_allowed(unsafe, f"http://127.0.0.1:{remote_port}/resolve"))

            # 7. On-demand indexed local library playback via POST /api/local-library/stream + rejection of unindexed paths
            scanned_dir = Path(self.temp.name) / "scanned-lib"
            scanned_dir.mkdir()
            indexed_wav = scanned_dir / "Artist Z - Local Lossless.wav"
            indexed_wav.write_bytes(payload_bytes)
            unindexed_wav = Path(self.temp.name) / "outside.wav"
            unindexed_wav.write_bytes(payload_bytes)

            scan_res = self.app.api("POST", "/api/local-library/scan", {"dirs": [str(scanned_dir)]})
            self.assertEqual(200, scan_res.status)
            self.assertEqual(1, scan_res.payload["data"]["scanned_count"])

            # Unindexed files (even valid .wav outside index or arbitrary system/app files) MUST be rejected with 403
            for bad_local in (str(unindexed_wav), str(self.web_dir / "index.html"), "../session.json"):
                rejected_local = self.app.api("POST", "/api/local-library/stream", {"path": bad_local})
                self.assertEqual(403, rejected_local.status)
                self.assertEqual("LOCAL_FILE_NOT_INDEXED", rejected_local.payload["error"]["code"])

            # Indexed file registers a stream token playable with 200 and 206 Range via /api/stream/{token}
            stream_info = self.app.api("POST", "/api/local-library/stream", {"path": str(indexed_wav)})
            self.assertEqual(200, stream_info.status)
            sdata = stream_info.payload["data"]
            self.assertEqual(0, sdata["id"])
            self.assertEqual("lossless", sdata["level"])
            self.assertEqual("wav", sdata["format"])
            self.assertEqual("local", sdata["source"])
            self.assertEqual("local_library", sdata["source_provider"])
            self.assertFalse(sdata["free_trial"])
            self.assertFalse(sdata["is_alternative"])
            self.assertTrue(sdata["stream_url"].startswith("/api/stream/"))

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", sdata["stream_url"])
            resp = conn.getresponse()
            self.assertEqual(200, resp.status)
            self.assertEqual(payload_bytes, resp.read())
            conn.close()

            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", sdata["stream_url"], headers={"Range": "bytes=0-15"})
            resp = conn.getresponse()
            self.assertEqual(206, resp.status)
            self.assertEqual(payload_bytes[:16], resp.read())
            conn.close()

            # 8. SSE /api/events sends connected + published sync event cleanly
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                with client.stream("GET", f"http://127.0.0.1:{port}/api/events") as stream:
                    self.assertEqual(200, stream.status_code)
                    self.assertIn("text/event-stream", stream.headers.get("content-type", ""))
                    lines = stream.iter_lines()
                    first = next(lines)
                    second = next(lines)
                    third = next(lines)
                    self.assertEqual("event: connected", first)
                    self.assertIn('"ok": true', second)
                    self.assertEqual("", third)
                    self.assertEqual("event: desktop_lyrics", next(lines))
                    self.assertIn("window", json.loads(next(lines)[6:]))
                    self.assertEqual("", next(lines))
                    self.app._sync_bus.publish({"stage": "fetching", "playlist_id": 10, "progress": 0.5})
                    ev_line = next(lines)
                    data_line = next(lines)
                    self.assertEqual("event: sync", ev_line)
                    self.assertIn('"playlist_id": 10', data_line)
        finally:
            server.stop()
            remote_srv.shutdown()
            remote_srv.server_close()
            remote_thread.join(timeout=2)

    def test_lyrics_audio_id_fallback_subscribed_playlists_and_feedback_bounds(self):
        from datetime import datetime, timedelta, timezone
        from pruner_studio import db as db_mod

        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.transport.handlers["/user/playlist"] = playlist_payload()

        # Default /api/playlists returns owned only; include_subscribed=1 includes subscribed
        owned_only = self.app.api("GET", "/api/playlists")
        self.assertEqual([10, 11], [p["id"] for p in owned_only.payload["data"]["playlists"]])
        with_sub = self.app.api("GET", "/api/playlists?include_subscribed=1")
        self.assertEqual([10, 11, 12], [p["id"] for p in with_sub.payload["data"]["playlists"]])
        self.assertEqual("subscribed", with_sub.payload["data"]["playlists"][2]["kind"])

        # /api/lyrics?id=1&audio_id=9 uses audio_id=9 lyrics when present, falls back to id=1 when empty
        def lyric_handler(params, *_args):
            sid = int(params["id"])
            if sid == 9:
                return {"code": 200, "lrc": {"lyric": ""}, "yrc": {"lyric": ""}}
            if sid == 10:
                return {"code": 200, "lrc": {"lyric": "[00:01.00]Alt"}, "yrc": {"lyric": ""}}
            return {"code": 200, "lrc": {"lyric": "[00:01.00]Orig"}, "romalrc": {"lyric": "[00:01.00]roma"}}

        self.transport.handlers["/lyric/new"] = lyric_handler
        self.transport.handlers["/song/chorus"] = {"code": 200, "chorus": []}
        fb_res = self.app.api("GET", "/api/lyrics?id=1&audio_id=9")
        self.assertEqual(200, fb_res.status)
        self.assertIn("Orig", fb_res.payload["data"]["lyrics"])
        self.assertIn("roma", fb_res.payload["data"]["romalrc"])
        alt_res = self.app.api("GET", "/api/lyrics?id=1&audio_id=10")
        self.assertEqual(200, alt_res.status)
        self.assertIn("Alt", alt_res.payload["data"]["lyrics"])

        # Explicit feedback bounds: expired (>30d) rows pruned & per-account row cap enforced
        old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        with self.app.db._connect() as conn:
            conn.execute(
                "INSERT INTO explicit_feedback(account_id, song_id, action, context, created_at) VALUES (?, ?, ?, ?, ?)",
                ("42", 9999, "not_interested", "old", old_ts),
            )
            conn.execute(
                "INSERT INTO explicit_feedback(account_id, song_id, action, context, created_at) VALUES (?, ?, ?, ?, ?)",
                ("99", 8888, "replay", "other-account", datetime.now(timezone.utc).isoformat()),
            )
        with patch.object(db_mod, "FEEDBACK_MAX_ROWS_PER_ACCOUNT", 5):
            for sid in range(1, 9):
                self.app.db.add_feedback("42", sid, "replay", "test")
        rows_42 = self.app.db.list_feedback("42")
        self.assertEqual(5, len(rows_42))
        self.assertNotIn(9999, [r["song_id"] for r in rows_42])
        self.assertEqual([8, 7, 6, 5, 4], [r["song_id"] for r in rows_42])
        # Account 99 feedback untouched
        rows_99 = self.app.db.list_feedback("99")
        self.assertEqual([8888], [r["song_id"] for r in rows_99])

    def test_brand_icons_and_desktop_icon_lookup(self):
        import sys
        from PIL import Image
        from pruner_studio.config import bundled_web_dir
        from pruner_studio.desktop import (
            APP_USER_MODEL_ID,
            apply_win32_window_icons,
            load_tray_icon_image,
            resolve_brand_icon_paths,
            set_windows_app_user_model_id,
            _lookup_window_hwnd,
        )

        web_root = bundled_web_dir()
        ico_path, png_path = resolve_brand_icon_paths(web_root)
        self.assertIsNotNone(ico_path)
        self.assertIsNotNone(png_path)
        self.assertEqual(web_root / "assets" / "icon.ico", ico_path)
        self.assertEqual(web_root / "assets" / "icon.png", png_path)

        with Image.open(png_path) as png_img:
            self.assertEqual((256, 256), png_img.size)
            self.assertEqual("RGBA", png_img.mode)

        with Image.open(ico_path) as ico_img:
            sizes = set(ico_img.info.get("sizes", set()))
            expected_sizes = {(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)}
            self.assertTrue(expected_sizes.issubset(sizes), f"missing ICO sizes: {expected_sizes - sizes}")

        # Tray icon loads from bundled PNG and scales cleanly; missing dir falls back safely.
        tray_img = load_tray_icon_image(web_root)
        self.assertEqual((64, 64), tray_img.size)
        self.assertEqual("RGBA", tray_img.mode)

        missing_ico, missing_png = resolve_brand_icon_paths(self.web_dir)
        self.assertIsNone(missing_ico)
        self.assertIsNone(missing_png)
        fallback_img = load_tray_icon_image(self.web_dir)
        self.assertEqual((64, 64), fallback_img.size)

        # Win32 helpers are safe and functional
        self.assertEqual(0, _lookup_window_hwnd(object()))
        self.assertEqual([], apply_win32_window_icons(0, ico_path))
        if sys.platform == "win32":
            self.assertTrue(set_windows_app_user_model_id(APP_USER_MODEL_ID))

    def test_channel_credential_vault_dpapi_isolation_ssrf_and_zero_secret_leakage(self):
        import sys
        from pruner_studio.storage import CHANNEL_SECRET_KEYS, ChannelCredentialVault, JsonStore

        # Save KuGou Lite credentials via API
        saved = self.app.api(
            "PUT",
            "/api/channels/kugou_lite",
            {
                "enabled": True,
                "api_base": "http://127.0.0.1:3000",
                "vip_label": "概念版VIP",
                "cookie": "kg_mid=super-secret-kugou",
                "token": "kg-secret-token",
                "userid": "123456",
                "dfid": "dfid-secret",
            },
        )
        self.assertEqual(200, saved.status)
        pub = saved.payload["data"]
        self.assertTrue(pub["enabled"])
        self.assertFalse(pub["connected"])
        self.assertTrue(pub["configured"])
        self.assertTrue(pub["has_credential"])
        self.assertEqual("http://127.0.0.1:3000", pub["api_base"])
        for secret_key in CHANNEL_SECRET_KEYS:
            self.assertNotIn(secret_key, pub)
        self.assertNotIn("super-secret-kugou", json.dumps(saved.payload))
        self.assertNotIn("kg-secret-token", json.dumps(saved.payload))

        # Raw file on disk is DPAPI encrypted on Windows
        cred_path = self.data_dir / "channel_credentials.json"
        raw_bytes = cred_path.read_bytes()
        if sys.platform == "win32":
            raw_doc = json.loads(raw_bytes.decode("utf-8"))
            self.assertEqual(2, raw_doc.get("version"))
            self.assertEqual("dpapi", raw_doc.get("encoding"))
            self.assertNotIn(b"super-secret-kugou", raw_bytes)
            self.assertNotIn(b"kg-secret-token", raw_bytes)

        # GET /api/channels NEVER exposes raw secrets
        listed = self.app.api("GET", "/api/channels")
        self.assertEqual(200, listed.status)
        self.assertEqual("best_quality_race", listed.payload["data"]["quality_arbitration"])
        self.assertNotIn("super-secret-kugou", json.dumps(listed.payload))
        self.assertNotIn("kg-secret-token", json.dumps(listed.payload))

        # Per-channel strict secret isolation
        kg_secret = self.app.channel_vault.get_channel_secret("kugou_lite")
        self.assertEqual("kg_mid=super-secret-kugou", kg_secret["cookie"])
        self.assertEqual("kg-secret-token", kg_secret["token"])
        bili_secret = self.app.channel_vault.get_channel_secret("bilibili")
        self.assertEqual("", bili_secret["cookie"])
        self.assertEqual("", bili_secret["sessdata"])

        # SSRF api_base rejected on channel update
        for bad_api in ("http://169.254.169.254/api", "http://example.com/api", "https://10.0.0.1/api"):
            rej = self.app.api("PUT", "/api/channels/migu", {"api_base": bad_api})
            self.assertEqual(400, rej.status)
            self.assertEqual("CHANNEL_CONFIG_INVALID", rej.payload["error"]["code"])

        # Clear credentials wipes secrets cleanly
        cleared = self.app.api("PUT", "/api/channels/kugou_lite", {"clear_credentials": True})
        self.assertEqual(200, cleared.status)
        self.assertFalse(cleared.payload["data"]["has_credential"])
        self.assertEqual("", self.app.channel_vault.get_channel_secret("kugou_lite")["token"])

        # A failed decryption leaves the existing vault available for recovery.
        isolated_store = JsonStore(self.data_dir / "corrupt-vault")
        path = isolated_store.path("channel_credentials.json")
        original = b'{"version": 2, "encoding": "dpapi", "data": "bad"}'
        path.write_bytes(original)
        vault = ChannelCredentialVault(isolated_store)
        with self.assertRaises(AppError) as corrupt:
            vault.update_channel("qq", {"enabled": False})
        self.assertEqual("CHANNEL_VAULT_UNAVAILABLE", corrupt.exception.code)
        self.assertEqual(original, path.read_bytes())

    def test_wave3_settings_validation_and_defaults(self):
        defaults = self.app.api("GET", "/api/settings").payload["data"]
        self.assertEqual("best_quality_race", defaults["quality_arbitration"])
        self.assertTrue(defaults["loudness_normalization"])
        self.assertEqual(150, defaults["crossfade_ms"])
        self.assertEqual(512, defaults["audio_cache_limit_mb"])
        self.assertFalse(defaults["lyrics_blur_depth"])
        self.assertTrue(defaults["audio_reactive_glow"])
        self.assertTrue(defaults["enable_amll_ttml_fallback"])
        self.assertTrue(defaults["global_hotkeys_enabled"])
        self.assertAlmostEqual(0.4, defaults["smart_radio_explore_ratio"])
        self.assertIn("kugou_lite", defaults["external_providers"])

        ok = self.app.api(
            "PUT",
            "/api/settings",
            {
                "quality_arbitration": "netease_first",
                "loudness_normalization": False,
                "crossfade_ms": 250,
                "audio_cache_limit_mb": 1024,
                "lyrics_blur_depth": True,
                "audio_reactive_glow": False,
                "enable_amll_ttml_fallback": False,
                "global_hotkeys_enabled": False,
                "smart_radio_explore_ratio": 0.65,
            },
        )
        self.assertEqual(200, ok.status)
        self.assertEqual("netease_first", ok.payload["data"]["quality_arbitration"])
        self.assertEqual(250, ok.payload["data"]["crossfade_ms"])
        self.assertEqual(1024, ok.payload["data"]["audio_cache_limit_mb"])

        for bad_payload in (
            {"quality_arbitration": "random_mode"},
            {"crossfade_ms": -1},
            {"crossfade_ms": 999},
            {"audio_cache_limit_mb": 99999},
            {"smart_radio_explore_ratio": -0.05},
            {"smart_radio_explore_ratio": 1.05},
        ):
            res = self.app.api("PUT", "/api/settings", bad_payload)
            self.assertEqual(400, res.status)
            self.assertEqual("SETTINGS_INVALID", res.payload["error"]["code"])

    def test_play_history_insights_smart_shuffle_amll_fallback_and_wave3_routes(self):
        self._auth_as(42, "User A", "MUSIC_U=token-a")
        self.app.catalog.set_active_account("42")
        self.app.db.replace_playlist_tracks(
            "42",
            PlaylistSummary(10, "Liked", 3, "liked", "public"),
            [
                Song(1, "Song One", ["Artist A"], album="Al1", duration=200_000, max_br_level="jymaster"),
                Song(2, "Song Two", ["Artist A"], album="Al2", duration=180_000, max_br_level="lossless"),
                Song(3, "Song Three", ["Artist B"], album="Al3", duration=210_000, max_br_level="exhigh", is_cloud=True),
            ],
        )

        # Skip < 25s when not completed is ignored
        skipped = self.app.api(
            "POST",
            "/api/playback/record",
            {"song": {"id": 1, "title": "Song One", "artists": ["Artist A"], "duration": 200_000}, "listened_ms": 12_000, "completed": False},
        )
        self.assertEqual(200, skipped.status)
        self.assertFalse(skipped.payload["data"]["recorded"])
        self.assertEqual("skip_ignored", skipped.payload["data"]["reason"])

        # Genuine listen >= 25s is recorded
        rec1 = self.app.api(
            "POST",
            "/api/playback/record",
            {"song": {"id": 1, "title": "Song One", "artists": ["Artist A"], "duration": 200_000}, "listened_ms": 180_000, "completed": True},
        )
        self.assertEqual(200, rec1.status)
        self.assertTrue(rec1.payload["data"]["recorded"])
        self.assertEqual(1, rec1.payload["data"]["play_count"])

        # Listening insights strictly reflect account 42
        insights = self.app.api("GET", "/api/insights")
        self.assertEqual(200, insights.status)
        idata = insights.payload["data"]
        self.assertEqual(1, idata["total_plays"])
        self.assertAlmostEqual(3.0, idata["total_listen_minutes"])
        self.assertEqual("Artist A", idata["top_artists"][0]["artist"])
        self.assertEqual(2, idata["dusty_gems_count"])  # songs 2 and 3 unplayed
        self.assertEqual(1, idata["cloud_count"])
        self.assertEqual(1, idata["quality_breakdown"]["jymaster"])

        # Smart shuffle spreads artists cleanly
        queue_tracks = [
            {"id": i, "title": f"A{i}", "artists": ["Artist A"]} for i in range(1, 5)
        ] + [
            {"id": i, "title": f"B{i}", "artists": ["Artist B"]} for i in range(5, 9)
        ]
        shuffled_resp = self.app.api("POST", "/api/queue/smart-shuffle", {"tracks": queue_tracks, "seed": 7})
        self.assertEqual(200, shuffled_resp.status)
        shuffled = shuffled_resp.payload["data"]["tracks"]
        self.assertEqual(8, len(shuffled))
        adjacent_same = sum(
            1 for a, b in zip(shuffled, shuffled[1:]) if a["artists"][0] == b["artists"][0]
        )
        self.assertEqual(0, adjacent_same)

        # AMLL TTML fallback upgrades empty YRC when enabled, skips when disabled
        self.transport.handlers["/lyric/new"] = {"code": 200, "lrc": {"lyric": "[00:01.00]Plain"}, "yrc": {"lyric": ""}}
        self.transport.handlers["/song/chorus"] = {"code": 200, "chorus": []}
        self.app._amll_fetcher = lambda sid: {
            "yrc": f"[1000,500](1000,500,0)AMLL-{sid}",
            "lrc": "[00:01.00]AMLL",
            "tlyrics": "[00:01.00]译文",
            "romalrc": "",
            "ttml_source": "amll-ttml-db",
        }
        upgraded = self.app.api("GET", "/api/lyrics?id=1")
        self.assertEqual(200, upgraded.status)
        self.assertEqual("amll-ttml-db", upgraded.payload["data"]["ttml_source"])
        self.assertIn("AMLL-1", upgraded.payload["data"]["yrc"])
        self.assertIn("译文", upgraded.payload["data"]["tlyrics"])

        self.app.api("PUT", "/api/settings", {"enable_amll_ttml_fallback": False})
        not_upgraded = self.app.api("GET", "/api/lyrics?id=1")
        self.assertEqual("", not_upgraded.payload["data"]["ttml_source"])
        self.assertEqual("", not_upgraded.payload["data"]["yrc"])

        # Wave 3 routes: smart-radio, dusty-gems, multi-search, channel test, direct external audio
        self.transport.handlers["/recommend/songs"] = {"code": 200, "data": {"dailySongs": [api_song(41, "Explore Song", "Artist C")]}}
        self.transport.handlers["/personal_fm"] = {"code": 200, "data": [api_song(42, "FM Song", "Artist D")]}
        self.transport.handlers["/simi/song"] = {"code": 200, "songs": [api_song(43, "Simi Song", "Artist E")]}
        self.transport.handlers["/cloudsearch"] = {"code": 200, "result": {"songs": [api_song(51, "Multi Hit", "Artist F")]}}

        dusty = self.app.api("GET", "/api/recommend/dusty-gems?limit=10")
        self.assertEqual(200, dusty.status)
        self.assertEqual("dusty_gems", dusty.payload["data"]["source"])
        self.assertTrue({2, 3}.issubset({s["id"] for s in dusty.payload["data"]["songs"]}))

        radio = self.app.api("GET", "/api/recommend/smart-radio?explore_ratio=0.5&exclude_ids=1&limit=10")
        self.assertEqual(200, radio.status)
        self.assertEqual("smart_radio", radio.payload["data"]["source"])
        self.assertNotIn(1, {s["id"] for s in radio.payload["data"]["songs"]})

        multi = self.app.api("GET", "/api/search/multi?q=Multi&channels=netease&limit=10")
        self.assertEqual(200, multi.status)
        self.assertEqual("Multi", multi.payload["data"]["query"])
        self.assertEqual([51], [t["id"] for t in multi.payload["data"]["tracks"]])

        with patch.object(
            self.app.audio.external_resolver,
            "test_channel",
            return_value={"channel": "kugou_lite", "ok": True, "latency_ms": 12, "status": "ok", "message": "ok", "max_quality_hint": "flac"},
        ):
            ch_test = self.app.api("POST", "/api/channels/kugou_lite/test", {})
            self.assertEqual(200, ch_test.status)
            self.assertTrue(ch_test.payload["data"]["ok"])
            self.assertEqual("kugou_lite", ch_test.payload["data"]["channel"])

        # Unknown channel test returns 404
        unknown_ch = self.app.api("POST", "/api/channels/nonexistent_ch/test", {})
        self.assertEqual(404, unknown_ch.status)

        # Direct cross-channel audio route delegates cleanly when provider & external_id are supplied
        fake_ext_result = {"id": 0, "url": "/api/stream/tok123", "stream_url": "/api/stream/tok123", "source": "external", "source_provider": "kugou_lite", "format": "flac"}
        with patch.object(self.app.audio, "resolve_external_track", create=True, return_value=fake_ext_result) as mock_ext:
            ext_audio = self.app.api(
                "GET",
                "/api/audio?id=0&provider=kugou_lite&external_id=hash123&title=Song&artist=Singer&duration=200000",
            )
            self.assertEqual(200, ext_audio.status)
            self.assertEqual("kugou_lite", ext_audio.payload["data"]["source_provider"])
            mock_ext.assert_called_once()

    def test_audio_disk_cache_lru_and_desktop_sse_lifecycle(self):
        import httpx
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from pruner_studio.app import create_asgi_app
        from pruner_studio.desktop import (
            BackgroundUvicornServer,
            GlobalHotkeyDaemon,
            bind_loopback_socket,
            set_win32_click_through,
        )
        from pruner_studio.storage import AudioDiskCache

        # 1. Unit test AudioDiskCache LRU + disable
        limit_holder = {"mb": 1}
        cache = AudioDiskCache(self.data_dir / "cache-unit", limit_mb_provider=lambda: limit_holder["mb"])
        chunk_600k = b"A" * (600 * 1024)
        chunk_700k = b"B" * (700 * 1024)
        self.assertTrue(cache.put_bytes("k1", chunk_600k, "audio/mpeg", format_name="mp3"))
        self.assertIsNotNone(cache.get("k1"))
        # Adding 700KB evicts k1 so total stays <= 1MB
        self.assertTrue(cache.put_bytes("k2", chunk_700k, "audio/mpeg", format_name="mp3"))
        self.assertIsNone(cache.get("k1"))
        self.assertIsNotNone(cache.get("k2"))
        self.assertEqual(1, cache.stats()["entry_count"])
        limit_holder["mb"] = 0
        self.assertIsNone(cache.get("k2"))
        self.assertEqual(0, cache.stats()["entry_count"])

        # 2. Live ASGI cache-through + desktop lyrics & hotkey SSE events
        upstream_hits = {"count": 0}

        class CacheableAudioServer(BaseHTTPRequestHandler):
            def do_GET(self):
                upstream_hits["count"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", "12")
                self.end_headers()
                self.wfile.write(b"ID3CACHEBODY")

            def log_message(self, format, *args):
                return

        remote_srv = HTTPServer(("127.0.0.1", 0), CacheableAudioServer)
        remote_port = remote_srv.server_address[1]
        remote_thread = threading.Thread(target=remote_srv.serve_forever, daemon=True)
        remote_thread.start()

        self.app.user_settings.save({"custom_external_api": f"http://127.0.0.1:{remote_port}/api", "audio_cache_limit_mb": 32})
        target = self.app.audio.register_stream_token(
            session_generation=self.app.session_generation, kind="remote",
            url=f"http://127.0.0.1:{remote_port}/song.mp3",
            headers={},
            content_type="audio/mpeg",
            format_name="mp3",
            provider="custom",
        )

        sock, port = bind_loopback_socket("127.0.0.1", 0)
        server = BackgroundUvicornServer(create_asgi_app(self.app, port), sock)
        server.start()
        try:
            # First fetch misses cache and populates AudioDiskCache
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", f"/api/stream/{target.token}")
            r1 = conn.getresponse()
            self.assertEqual(200, r1.status)
            self.assertEqual(b"ID3CACHEBODY", r1.read())
            conn.close()
            self.assertEqual(1, upstream_hits["count"])

            # Second fetch is served directly from disk cache with 0 upstream hits
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request("GET", f"/api/stream/{target.token}", headers={"Range": "bytes=0-3"})
            r2 = conn.getresponse()
            self.assertEqual(206, r2.status)
            self.assertEqual("HIT", r2.getheader("X-Audio-Cache"))
            self.assertEqual(b"ID3C", r2.read())
            conn.close()
            self.assertEqual(1, upstream_hits["count"])

            # Cache stats & clear endpoints
            stats = self.app.api("GET", "/api/cache/audio/stats").payload["data"]
            self.assertEqual(1, stats["entry_count"])
            self.assertEqual(12, stats["total_bytes"])
            cleared = self.app.api("DELETE", "/api/cache/audio").payload["data"]
            self.assertEqual(1, cleared["cleared_entries"])
            self.assertEqual(0, self.app.api("GET", "/api/cache/audio/stats").payload["data"]["entry_count"])

            # Desktop lyrics window API (headless-safe) & playback state SSE broadcast + hotkey SSE
            win_open = self.app.api("POST", "/api/desktop/lyrics-window", {"open": True, "locked": True})
            self.assertEqual(200, win_open.status)
            self.assertTrue(win_open.payload["data"]["open"])
            self.assertTrue(win_open.payload["data"]["locked"])
            self.assertFalse(set_win32_click_through(0, True))

            self.app.api("POST", "/api/desktop/playback-state", {"title": "Track", "line": "Hello World", "playing": True})
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                with client.stream("GET", f"http://127.0.0.1:{port}/api/events") as stream:
                    lines = stream.iter_lines()
                    self.assertEqual("event: connected", next(lines))
                    next(lines)
                    next(lines)
                    # Latest desktop_lyrics state replayed immediately on SSE connect
                    self.assertEqual("event: desktop_lyrics", next(lines))
                    replay_data = next(lines)
                    self.assertIn("Hello World", replay_data)
                    replay_window = json.loads(replay_data[6:])["window"]
                    self.assertTrue(replay_window["open"] and replay_window["locked"])
                    next(lines)
                    # Hotkey event delivered live
                    self.app.publish_sse("hotkey", {"action": "next"})
                    self.assertEqual("event: hotkey", next(lines))
                    hk_data = next(lines)
                    self.assertIn('"action": "next"', hk_data)

            # GlobalHotkeyDaemon clean start/stop lifecycle
            hk_daemon = GlobalHotkeyDaemon(lambda _act: None)
            hk_daemon.start()
            hk_daemon.stop()
            self.assertFalse(hk_daemon.is_running())
        finally:
            server.stop()
            remote_srv.shutdown()
            remote_srv.server_close()
            remote_thread.join(timeout=2)

    def test_wave35_static_no_store_headers_and_single_instance_window_wake(self):
        import ctypes
        from http.server import ThreadingHTTPServer
        from unittest.mock import MagicMock
        from pruner_studio import __main__ as main_mod
        from pruner_studio.app import create_asgi_app
        from pruner_studio.desktop import (
            BackgroundUvicornServer,
            activate_existing_window,
            bind_loopback_socket,
        )

        # Populate static assets including /js/app.js and /desktop_lyrics.html
        (self.web_dir / "js").mkdir(exist_ok=True)
        (self.web_dir / "js" / "app.js").write_text("export const ok = true;", encoding="utf-8")
        (self.web_dir / "desktop_lyrics.html").write_text("<div>Lyrics</div>", encoding="utf-8")
        fresh_app = build_application(self.settings, self.transport)

        # 1. Verify Starlette ASGI static responses carry no-store + no-cache headers
        sock, port = bind_loopback_socket("127.0.0.1", 0)
        asgi_server = BackgroundUvicornServer(create_asgi_app(fresh_app, port), sock)
        asgi_server.start()
        try:
            for static_path in ("/", "/js/app.js", "/desktop_lyrics.html"):
                with self.subTest(server="asgi", path=static_path):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                    conn.request("GET", static_path)
                    resp = conn.getresponse()
                    self.assertEqual(200, resp.status)
                    self.assertEqual("no-store, must-revalidate", resp.getheader("Cache-Control"))
                    self.assertEqual("no-cache", resp.getheader("Pragma"))
                    resp.read()
                    conn.close()
        finally:
            asgi_server.stop()

        # 2. Verify stdlib make_handler static responses also carry no-store + no-cache headers
        std_server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(fresh_app))
        std_thread = threading.Thread(target=std_server.serve_forever, daemon=True)
        std_thread.start()
        try:
            for static_path in ("/", "/js/app.js", "/desktop_lyrics.html"):
                with self.subTest(server="stdlib", path=static_path):
                    conn = http.client.HTTPConnection("127.0.0.1", std_server.server_port, timeout=3)
                    conn.request("GET", static_path)
                    resp = conn.getresponse()
                    self.assertEqual(200, resp.status)
                    self.assertEqual("no-store, must-revalidate", resp.getheader("Cache-Control"))
                    self.assertEqual("no-cache", resp.getheader("Pragma"))
                    resp.read()
                    conn.close()
        finally:
            std_server.shutdown()
            std_server.server_close()
            std_thread.join(timeout=2)

        # 3. Safe unit coverage of activate_existing_window
        self.assertFalse(activate_existing_window(""))
        self.assertFalse(activate_existing_window("__NonExistent_Pruner_Window_Title_99999__"))

        fake_user32 = MagicMock()
        fake_user32.FindWindowW.return_value = 424242
        with patch.object(ctypes, "WinDLL", return_value=fake_user32):
            self.assertTrue(activate_existing_window("Pruner Studio"))
        fake_user32.FindWindowW.assert_called_once_with(None, "Pruner Studio")
        fake_user32.ShowWindow.assert_any_call(424242, 5)
        fake_user32.ShowWindow.assert_any_call(424242, 9)
        fake_user32.SetForegroundWindow.assert_called_once_with(424242)

        # 4. Single-instance entry wakes existing window and exits cleanly with status 0
        with (
            patch("pruner_studio.app.serve", side_effect=AppError("INSTANCE_ALREADY_RUNNING", "already running", 409)),
            patch("pruner_studio.desktop.wake_running_instance", return_value=False),
            patch("pruner_studio.desktop.activate_existing_window", return_value=True) as mock_wake,
        ):
            with self.assertRaises(SystemExit) as caught_exit:
                main_mod.main()
            self.assertEqual(0, caught_exit.exception.code)
            mock_wake.assert_called_once_with("Pruner Studio")

    def test_wave4_search_multi_pagination_and_desktop_command_allowlist(self):
        import httpx
        from pruner_studio.app import create_asgi_app
        from pruner_studio.desktop import BackgroundUvicornServer, bind_loopback_socket

        # 1. /api/search/multi forwards limit (up to 100) and offset (0..1000) to resolver
        captured_calls: list[dict[str, Any]] = []

        class StubMultiResolver:
            def search_multi_channel(
                self,
                query: str,
                *,
                channels=None,
                limit: int = 30,
                offset: int = 0,
                netease_client=None,
                custom_api: str = "",
            ):
                captured_calls.append(
                    {"query": query, "channels": channels, "limit": limit, "offset": offset, "custom_api": custom_api}
                )
                return {
                    "query": query,
                    "offset": offset,
                    "limit": limit,
                    "has_more": True,
                    "channels_queried": channels or ["netease", "qq"],
                    "tracks": [
                        {"id": 101, "title": "Page Track", "artists": ["Singer"], "provider": "netease"}
                    ],
                    "channel_errors": {},
                }

        orig_resolver = getattr(self.app.audio, "external_resolver", None)
        self.app.audio.external_resolver = StubMultiResolver()
        try:
            res = self.app.api("GET", "/api/search/multi?q=night&channels=netease,qq&limit=100&offset=60")
            self.assertEqual(200, res.status)
            data = res.payload["data"]
            self.assertEqual("night", data["query"])
            self.assertEqual(60, data["offset"])
            self.assertEqual(100, data["limit"])
            self.assertTrue(data["has_more"])
            self.assertEqual(["netease", "qq"], data["channels_queried"])
            self.assertEqual(1, len(data["tracks"]))
            self.assertEqual(100, captured_calls[-1]["limit"])
            self.assertEqual(60, captured_calls[-1]["offset"])

            # Clamping bounds: limit > 100 -> 100, offset > 1000 -> 1000
            clamped = self.app.api("GET", "/api/search/multi?q=night&limit=500&offset=5000")
            self.assertEqual(200, clamped.status)
            self.assertEqual(100, captured_calls[-1]["limit"])
            self.assertEqual(1000, captured_calls[-1]["offset"])

            # Fallback path without multi-channel resolver still returns pagination contract
            self.app.audio.external_resolver = None
            self.transport.handlers["/cloudsearch"] = {
                "code": 200,
                "result": {"songs": [api_song(11, "Fallback Song", "Artist", 180_000)]},
            }
            fb = self.app.api("GET", "/api/search/multi?q=fallback&limit=1&offset=20")
            self.assertEqual(200, fb.status)
            fb_data = fb.payload["data"]
            self.assertEqual("fallback", fb_data["query"])
            self.assertEqual(20, fb_data["offset"])
            self.assertEqual(1, fb_data["limit"])
            self.assertTrue(fb_data["has_more"])
            self.assertEqual(["netease"], fb_data["channels_queried"])
            self.assertEqual(1, len(fb_data["tracks"]))
        finally:
            self.app.audio.external_resolver = orig_resolver

        # 2. POST /api/desktop/command allowlist + CSRF protection + SSE hotkey dispatch
        for bad_body in (
            {},
            {"action": ""},
            {"action": "toggle_desktop_lyrics"},
            {"action": "shutdown"},
            {"action": 123},
            {"action": None},
        ):
            with self.subTest(bad_body=bad_body):
                bad_res = self.app.api("POST", "/api/desktop/command", bad_body)
                self.assertEqual(400, bad_res.status)
                self.assertEqual("DESKTOP_COMMAND_INVALID", bad_res.payload["error"]["code"])

        sock, port = bind_loopback_socket("127.0.0.1", 0)
        server = BackgroundUvicornServer(create_asgi_app(self.app, port), sock)
        server.start()
        try:
            # Cross-origin request to /api/desktop/command MUST be blocked with 403
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            conn.request(
                "POST",
                "/api/desktop/command",
                body=b'{"action":"next"}',
                headers={
                    "Host": f"127.0.0.1:{port}",
                    "Origin": "http://evil.example",
                    "Content-Type": "application/json",
                },
            )
            csrf_resp = conn.getresponse()
            self.assertEqual(403, csrf_resp.status)
            csrf_payload = json.loads(csrf_resp.read())
            self.assertEqual("REQUEST_ORIGIN_REJECTED", csrf_payload["error"]["code"])
            conn.close()

            # Valid commands deliver SSE event: hotkey live
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                with client.stream("GET", f"http://127.0.0.1:{port}/api/events") as stream:
                    lines = stream.iter_lines()
                    self.assertEqual("event: connected", next(lines))
                    next(lines)
                    next(lines)
                    self.assertEqual("event: desktop_lyrics", next(lines))
                    self.assertIn("window", json.loads(next(lines)[6:]))
                    self.assertEqual("", next(lines))
                    for cmd in ("next", "prev", "toggle_play", "toggle_like"):
                        cmd_resp = client.post(
                            f"http://127.0.0.1:{port}/api/desktop/command",
                            json={"action": cmd},
                            headers={
                                "Host": f"127.0.0.1:{port}",
                                "Origin": f"http://127.0.0.1:{port}",
                                "Sec-Fetch-Site": "same-origin",
                            },
                            timeout=3.0,
                        )
                        self.assertEqual(200, cmd_resp.status_code)
                        self.assertEqual(cmd, cmd_resp.json()["data"]["action"])
                        self.assertTrue(cmd_resp.json()["data"]["dispatched"])
                        self.assertEqual("event: hotkey", next(lines))
                        data_line = next(lines)
                        self.assertIn(f'"action": "{cmd}"', data_line)
                        next(lines)
        finally:
            server.stop()

    def test_wave4_settings_appearance_lyrics_style_bounds_and_sse_sync(self):
        import httpx
        from pruner_studio.app import create_asgi_app
        from pruner_studio.desktop import BackgroundUvicornServer, bind_loopback_socket

        # 1. Defaults check
        initial = self.app.api("GET", "/api/settings").payload["data"]
        self.assertEqual(
            {
                "theme": "dark",
                "accent_mode": "preset",
                "accent": "#62a368",
                "density": "comfortable",
                "font_scale": 1.0,
            },
            initial["appearance"],
        )
        self.assertEqual(
            {
                "font_size": 28,
                "played_color": "#62a368",
                "unplayed_color": "#f5f5f7",
                "stroke": False,
                "preset": "cinema_white",
                "shadow_strength": 1.0,
                "font_weight": 700,
                "surface_style": "borderless",
                "bg_opacity": 0.0,
                "align": "center",
                "mode": "double",
                "show_translation": True,
                "wheel_volume": False,
            },
            initial["desktop_lyrics_style"],
        )
        self.assertIsNone(initial["desktop_lyrics_bounds"])

        # 2. Partial nested merges + hex normalization + arbitrary explore ratio (0.37)
        r1 = self.app.api(
            "PUT",
            "/api/settings",
            {
                "appearance": {"theme": "midnight", "accent": "#ABCDEF"},
                "smart_radio_explore_ratio": 0.37,
            },
        )
        self.assertEqual(200, r1.status)
        self.assertEqual("midnight", r1.payload["data"]["appearance"]["theme"])
        self.assertEqual("#abcdef", r1.payload["data"]["appearance"]["accent"])
        self.assertEqual("preset", r1.payload["data"]["appearance"]["accent_mode"])
        self.assertEqual(1.0, r1.payload["data"]["appearance"]["font_scale"])
        self.assertEqual(0.37, r1.payload["data"]["smart_radio_explore_ratio"])

        r2 = self.app.api(
            "PUT",
            "/api/settings",
            {"appearance": {"density": "compact", "font_scale": 1.15}},
        )
        self.assertEqual(200, r2.status)
        self.assertEqual("midnight", r2.payload["data"]["appearance"]["theme"])
        self.assertEqual("#abcdef", r2.payload["data"]["appearance"]["accent"])
        self.assertEqual("compact", r2.payload["data"]["appearance"]["density"])
        self.assertEqual(1.15, r2.payload["data"]["appearance"]["font_scale"])

        # 3. desktop_lyrics_bounds save, partial merge, and reset to None
        b1 = self.app.api(
            "PUT",
            "/api/settings",
            {"desktop_lyrics_bounds": {"x": -200, "y": 820, "width": 900, "height": 140}},
        )
        self.assertEqual(200, b1.status)
        self.assertEqual(
            {"x": -200, "y": 820, "width": 900, "height": 140},
            b1.payload["data"]["desktop_lyrics_bounds"],
        )
        b2 = self.app.api("PUT", "/api/settings", {"desktop_lyrics_bounds": {"width": 1000}})
        self.assertEqual(200, b2.status)
        self.assertEqual(
            {"x": -200, "y": 820, "width": 1000, "height": 140},
            b2.payload["data"]["desktop_lyrics_bounds"],
        )
        b3 = self.app.api("PUT", "/api/settings", {"desktop_lyrics_bounds": None})
        self.assertEqual(200, b3.status)
        self.assertIsNone(b3.payload["data"]["desktop_lyrics_bounds"])

        # 4. Strict validation rejects invalid values with 400 SETTINGS_INVALID
        invalid_payloads = (
            {"appearance": "dark"},
            {"appearance": {"theme": "neon"}},
            {"appearance": {"accent_mode": "rainbow"}},
            {"appearance": {"accent": "#fff"}},
            {"appearance": {"accent": "red; background:url(x)"}},
            {"appearance": {"density": "ultra"}},
            {"appearance": {"font_scale": 0.5}},
            {"appearance": {"font_scale": 1.5}},
            {"appearance": {"font_scale": True}},
            {"desktop_lyrics_style": []},
            {"desktop_lyrics_style": {"font_size": 12}},
            {"desktop_lyrics_style": {"font_size": 60}},
            {"desktop_lyrics_style": {"font_size": 24.5}},
            {"desktop_lyrics_style": {"played_color": "blue"}},
            {"desktop_lyrics_style": {"unplayed_color": "#12345g"}},
            {"desktop_lyrics_style": {"stroke": "yes"}},
            {"desktop_lyrics_style": {"bg_opacity": -0.1}},
            {"desktop_lyrics_style": {"bg_opacity": 0.95}},
            {"desktop_lyrics_style": {"align": "justify"}},
            {"desktop_lyrics_style": {"mode": "triple"}},
            {"desktop_lyrics_style": {"show_translation": 1}},
            {"desktop_lyrics_bounds": "bad"},
            {"desktop_lyrics_bounds": {"x": 10, "y": 20}},
            {"desktop_lyrics_bounds": {"x": 10, "y": 20, "width": 200, "height": 120}},
            {"desktop_lyrics_bounds": {"x": 10, "y": 20, "width": 820, "height": 40}},
            {"desktop_lyrics_bounds": {"x": 10, "y": 20, "width": 1700, "height": 120}},
            {"desktop_lyrics_bounds": {"x": 10, "y": 20, "width": 820, "height": 420}},
            {"smart_radio_explore_ratio": -0.05},
            {"smart_radio_explore_ratio": 1.05},
        )
        for bad_payload in invalid_payloads:
            with self.subTest(bad_payload=bad_payload):
                rejected = self.app.api("PUT", "/api/settings", bad_payload)
                self.assertEqual(400, rejected.status)
                self.assertEqual("SETTINGS_INVALID", rejected.payload["error"]["code"])

        # 5. Live SSE broadcast + replay on reconnect + replay after fresh restart
        sock, port = bind_loopback_socket("127.0.0.1", 0)
        server = BackgroundUvicornServer(create_asgi_app(self.app, port), sock)
        server.start()
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                with client.stream("GET", f"http://127.0.0.1:{port}/api/events") as stream:
                    lines = stream.iter_lines()
                    self.assertEqual("event: connected", next(lines))
                    next(lines)
                    next(lines)
                    self.assertEqual("event: desktop_lyrics", next(lines))
                    self.assertIn("window", json.loads(next(lines)[6:]))
                    self.assertEqual("", next(lines))
                    # Partial desktop_lyrics_style update preserves siblings and emits SSE event
                    put_res = self.app.api(
                        "PUT",
                        "/api/settings",
                        {"desktop_lyrics_style": {"font_size": 36, "align": "left", "bg_opacity": 0.5}},
                    )
                    self.assertEqual(200, put_res.status)
                    saved_style = put_res.payload["data"]["desktop_lyrics_style"]
                    self.assertEqual(36, saved_style["font_size"])
                    self.assertEqual("left", saved_style["align"])
                    self.assertEqual(0.5, saved_style["bg_opacity"])
                    self.assertEqual("#62a368", saved_style["played_color"])
                    self.assertTrue(saved_style["show_translation"])

                    self.assertEqual("event: desktop_lyrics_style", next(lines))
                    ev_data = json.loads(next(lines)[len("data: "):])
                    self.assertEqual(36, ev_data["style"]["font_size"])
                    self.assertEqual("left", ev_data["style"]["align"])
                    next(lines)

                # Second SSE connection immediately replays latest saved style
                with client.stream("GET", f"http://127.0.0.1:{port}/api/events") as stream2:
                    lines2 = stream2.iter_lines()
                    self.assertEqual("event: connected", next(lines2))
                    next(lines2)
                    next(lines2)
                    self.assertEqual("event: desktop_lyrics", next(lines2))
                    self.assertIn("window", json.loads(next(lines2)[6:]))
                    self.assertEqual("", next(lines2))
                    self.assertEqual("event: desktop_lyrics_style", next(lines2))
                    replay_data = json.loads(next(lines2)[len("data: "):])
                    self.assertEqual(36, replay_data["style"]["font_size"])
                    self.assertEqual(0.5, replay_data["style"]["bg_opacity"])
        finally:
            server.stop()

        # Simulate restart: fresh app loads persisted non-default style and replays on connect
        restarted_app = build_application(self.settings, self.transport)
        sock2, port2 = bind_loopback_socket("127.0.0.1", 0)
        server2 = BackgroundUvicornServer(create_asgi_app(restarted_app, port2), sock2)
        server2.start()
        try:
            with httpx.Client(timeout=3.0, trust_env=False) as client:
                with client.stream("GET", f"http://127.0.0.1:{port2}/api/events") as stream3:
                    lines3 = stream3.iter_lines()
                    self.assertEqual("event: connected", next(lines3))
                    next(lines3)
                    next(lines3)
                    self.assertEqual("event: desktop_lyrics", next(lines3))
                    self.assertIn("window", json.loads(next(lines3)[6:]))
                    self.assertEqual("", next(lines3))
                    self.assertEqual("event: desktop_lyrics_style", next(lines3))
                    restarted_data = json.loads(next(lines3)[len("data: "):])
                    self.assertEqual(36, restarted_data["style"]["font_size"])
        finally:
            server2.stop()

    def test_wave4_desktop_lyrics_controller_transparent_fallback_and_bounds_persistence(self):
        import ctypes
        import time
        from unittest.mock import MagicMock
        from pruner_studio.desktop import DesktopLyricsController, set_win32_click_through

        class FakeEventHook:
            def __init__(self):
                self.handlers: list[Any] = []

            def __iadd__(self, fn):
                self.handlers.append(fn)
                return self

            def fire(self, *args):
                for fn in list(self.handlers):
                    fn(*args)

        class FakeWindow:
            def __init__(self, kwargs: dict[str, Any], fail_destroy: bool = False):
                self.kwargs = kwargs
                self.x = kwargs.get("x", 0)
                self.y = kwargs.get("y", 0)
                self.width = kwargs.get("width", 820)
                self.height = kwargs.get("height", 120)
                self.shown_count = 0
                self.destroyed = False
                self._fail_destroy = fail_destroy
                self.events = type(
                    "Events",
                    (),
                    {
                        "closed": FakeEventHook(),
                        "shown": FakeEventHook(),
                        "moved": FakeEventHook(),
                        "resized": FakeEventHook(),
                    },
                )()

            def show(self):
                self.shown_count += 1

            def hide(self):
                pass

            def destroy(self):
                self.destroyed = True
                if self._fail_destroy:
                    raise RuntimeError("destroy boom")

        # 1. Opens transparent=True with saved bounds + debounced move/resize persistence
        saved_bounds_log: list[dict[str, int]] = []
        saved_event = threading.Event()
        created_calls: list[dict[str, Any]] = []
        created_windows: list[FakeWindow] = []

        def _record_bounds(bounds: dict[str, int]) -> None:
            saved_bounds_log.append(bounds)
            saved_event.set()

        class FakeWebview:
            def create_window(self, title, url, **kwargs):
                created_calls.append({"title": title, "url": url, **kwargs})
                win = FakeWindow(kwargs, fail_destroy=True)
                created_windows.append(win)
                return win

        controller = DesktopLyricsController(
            base_url="http://127.0.0.1:8765",
            webview_module=FakeWebview(),
            bounds_provider=lambda: {"x": 120, "y": 760, "width": 940, "height": 136},
            bounds_saver=_record_bounds,
            bounds_debounce_sec=0.05,
        )
        st = controller.set_window(True)
        self.assertTrue(st["open"])
        self.assertTrue(st["transparent"])
        self.assertEqual(1, len(created_calls))
        first_call = created_calls[0]
        self.assertTrue(first_call["transparent"])
        self.assertTrue(first_call["frameless"])
        self.assertTrue(first_call["on_top"])
        self.assertFalse(first_call["easy_drag"])
        self.assertTrue(first_call["resizable"])
        self.assertEqual(120, first_call["x"])
        self.assertEqual(760, first_call["y"])
        self.assertEqual(940, first_call["width"])
        self.assertEqual(136, first_call["height"])

        # Rapid moved + resized events debounce into a single save with latest bounds
        win = created_windows[0]
        win.x, win.y = 200, 700
        win.events.moved.fire(200, 700)
        win.x, win.y = 260, 680
        win.events.moved.fire(260, 680)
        win.width, win.height = 1020, 150
        win.events.resized.fire(1020, 150)
        self.assertTrue(saved_event.wait(timeout=1.0))
        time.sleep(0.05)
        self.assertEqual(1, len(saved_bounds_log))
        self.assertEqual({"x": 260, "y": 680, "width": 1020, "height": 150}, saved_bounds_log[0])

        # Closing handles destroy() exception cleanly and flushes pending move
        win.x, win.y = 310, 650
        win.events.moved.fire(310, 650)
        closed_st = controller.set_window(False)
        self.assertFalse(closed_st["open"])
        self.assertTrue(win.destroyed)
        self.assertEqual({"x": 310, "y": 650, "width": 1020, "height": 150}, saved_bounds_log[-1])

        # 2. Fallback when transparent=True raises: logs warning and retries ONCE with transparent=False
        fallback_calls: list[dict[str, Any]] = []

        class FallbackWebview:
            def create_window(self, title, url, **kwargs):
                fallback_calls.append(dict(kwargs))
                if kwargs.get("transparent"):
                    raise RuntimeError("transparent unsupported")
                return FakeWindow(kwargs)

        fb_controller = DesktopLyricsController(
            base_url="http://127.0.0.1:8765",
            webview_module=FallbackWebview(),
        )
        with self.assertLogs("pruner_studio.desktop", level="WARNING") as logs:
            fb_st = fb_controller.set_window(True)
        self.assertTrue(fb_st["open"])
        self.assertFalse(fb_st["transparent"])
        self.assertEqual(2, len(fallback_calls))
        self.assertTrue(fallback_calls[0]["transparent"])
        self.assertFalse(fallback_calls[1]["transparent"])
        self.assertEqual("#141518", fallback_calls[1]["background_color"])
        self.assertTrue(any("transparent" in msg.lower() for msg in logs.output))
        fb_controller.close()

        # Preserve a valid layered alpha until unlock restores DirectComposition.
        fake_user32 = MagicMock()
        fake_user32.GetWindowLongPtrW.return_value = 0x00080000  # WS_EX_LAYERED already present
        fake_user32.GetWindowLongW.return_value = 0x00080000
        def read_layered(_hwnd, _color, alpha, flags):
            alpha._obj.value = 180
            flags._obj.value = 2
            return 1

        fake_user32.GetLayeredWindowAttributes.side_effect = read_layered
        with patch.object(ctypes, "WinDLL", return_value=fake_user32):
            self.assertTrue(set_win32_click_through(12345, True, preserve_transparency=True))
            self.assertEqual(0, fake_user32.SetLayeredWindowAttributes.call_count)
            self.assertTrue(set_win32_click_through(12345, False, preserve_transparency=True))
            self.assertEqual(0, fake_user32.SetLayeredWindowAttributes.call_count)
            set_style = fake_user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else fake_user32.SetWindowLongW
            self.assertFalse(set_style.call_args.args[2] & 0x00080000)
            fake_user32.GetWindowLongPtrW.return_value = fake_user32.GetWindowLongW.return_value = 0
            self.assertTrue(set_win32_click_through(12345, True, preserve_transparency=True))
            fake_user32.SetLayeredWindowAttributes.assert_called_once_with(12345, 0, 255, 2)
            fake_user32.SetLayeredWindowAttributes.reset_mock()
            # Non-transparent fallback explicitly sets LWA_ALPHA
            self.assertTrue(set_win32_click_through(12345, True, preserve_transparency=False))
            self.assertEqual(1, fake_user32.SetLayeredWindowAttributes.call_count)


if __name__ == "__main__":
    unittest.main()
