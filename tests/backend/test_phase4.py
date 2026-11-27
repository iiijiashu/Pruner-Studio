import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch

import httpx
from pruner_studio.app import build_application
from pruner_studio.config import Settings
from pruner_studio.models import AccountSummary, Song
from pruner_studio.netease_client import NeteaseClient
from pruner_studio.storage import SessionStore
from test_backend import FakeTransport, account_payload, playlist_payload


ROOT = Path(__file__).resolve().parents[2]


class Phase4Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.transport = FakeTransport()
        self.transport.handlers["/user/account"] = account_payload()
        self.settings = Settings(self.directory, ROOT / "web")
        self.app = build_application(self.settings, self.transport)
        self.app.client.pacer.sleep_fn = lambda _: None
        self.vault = self.app.session

    def test_migration_on_identity_verification_and_compatibility(self):
        self.vault.store.write("session.json", {"cookie": "MUSIC_U=one"})
        restarted = build_application(self.settings, self.transport)
        self.assertEqual("MUSIC_U=one", restarted.client.cookie)
        response = restarted.api("GET", "/api/accounts")
        self.assertEqual("42", response.payload["data"]["active_account_id"])
        doc = self.vault.store.read("sessions.json", {})
        self.assertEqual(3, doc["version"])
        if sys.platform == "win32":
            self.assertEqual("dpapi", doc["accounts"]["42"]["encoding"])
            self.assertNotIn("MUSIC_U", json.dumps(doc))
        self.vault.save_account_session(AccountSummary(43, "Two"), "MUSIC_U=two")
        self.vault.set_active_account("42")
        self.assertEqual("MUSIC_U=one", SessionStore(self.vault.store).load_cookie())
        self.vault.clear()
        self.assertIsNone(self.vault.load_cookie())
        self.assertEqual(["43"], [row["account_id"] for row in self.vault.list_saved_accounts()])

    def test_account_routes_expiry_and_active_removal(self):
        api = self.app.api
        self.vault.save_account_session(AccountSummary(42, "One"), "MUSIC_U=a")
        self.vault.save_account_session(AccountSummary(43, "Two"), "MUSIC_U=b")
        switched = api("POST", "/api/accounts/switch", {"account_id": "42"})
        self.assertEqual(200, switched.status)
        gen = switched.payload["data"]["session_generation"]
        self.transport.handlers["/user/account"] = {"code": 301}
        expired = api("POST", "/api/accounts/switch", {"account_id": "43"})
        self.assertEqual((401, "SESSION_EXPIRED"), (expired.status, expired.payload["error"]["code"]))
        data = api("GET", "/api/accounts").payload["data"]
        self.assertTrue(data["authenticated"])
        self.assertEqual("42", data["active_account_id"])
        self.assertNotIn("MUSIC_U", json.dumps(data))
        self.assertEqual(gen, self.app.session_generation)
        self.assertEqual(200, api("DELETE", "/api/accounts/42").status)
        self.assertEqual(gen + 1, self.app.session_generation)
        self.assertIsNone(self.app.client.cookie)
        self.assertEqual("MUSIC_U=b", self.vault.load_account_cookie("43"))

    def login(self):
        self.app.client.set_cookie("MUSIC_U=a")
        self.app.api("GET", "/api/status")

    def test_logout_during_sync_cannot_restore_active_account(self):
        self.login()
        entered, resume = Event(), Event()
        self.transport.handlers["/user/playlist"] = playlist_payload()

        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(resume.wait(5))
            return [Song(71, "First", [])]

        events = self.app._sync_bus.subscribe()
        with patch.object(NeteaseClient, "playlist_tracks", delayed), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.app.api, "POST", "/api/playlists/10/sync", {})
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(200, self.app.api("POST", "/api/auth/logout").status)
            finally:
                resume.set()
            self.assertEqual(409, future.result(timeout=4).status)
        progress = events.get(timeout=1)
        self.app._sync_bus.unsubscribe(events)
        self.assertEqual(("sync_progress", "42", self.app.session_generation - 1),
                         (progress["_event_type"], progress["account_id"], progress["session_generation"]))
        self.assertIsNone(self.app.catalog._active_account_id)
        self.assertIsNotNone(self.app.db.get_song(71, "42"))
        self.assertIsNone(self.app.db.get_song(71, "local"))

    def test_cloud_pages_commit_together_and_failed_refresh_preserves_snapshot(self):
        self.login()
        offsets = []
        def page(params, _method, _cookie):
            start = params["offset"]
            offsets.append(start)
            self.assertEqual(200, params["limit"])
            return {"code": 200, "count": 203, "data": [
                {"songId": i + 1} for i in range(start, min(start + 200, 203))]}
        self.transport.handlers["/user/cloud"] = page
        data = self.app.api("GET", "/api/cloud?full=1").payload["data"]
        self.assertEqual([0, 200], offsets)
        self.assertEqual(203, len(data["songs"]))
        self.transport.handlers["/user/cloud"] = lambda p, *_: (
            {"code": 200, "data": [{"songId": 900}], "hasMore": True}
            if p["offset"] == 0 else {"code": 200, "data": None})
        self.assertGreaterEqual(self.app.api("GET", "/api/cloud").status, 400)
        self.assertEqual(203, len(self.app.db.get_catalog_dict("42")["playlists"]["cloud"]["songs"]))

    def test_search_cursors_resume_partial_and_exhausted_providers(self):
        from pruner_studio.external_sources import ExternalCandidate
        from urllib.parse import urlencode

        resolver = self.app.audio.external_resolver
        totals = {"qq": 9, "migu": 3, "kuwo": 13}
        def provider(ch, keyword, client, *, limit, offset, **kwargs):
            end = min(offset + limit, totals[ch])
            return [ExternalCandidate(f"{ch}track{i}", ["Singer"], "", 1000, ch, str(i))
                    for i in range(offset, end)], end < totals[ch]
        actual = []
        params = {"q": "Singer", "channels": ",".join(totals), "limit": 5}
        with patch.object(resolver, "_search_channel_page", side_effect=provider):
            for _ in range(10):
                result = self.app.api("GET", "/api/search/multi?" + urlencode(params)).payload["data"]
                actual.extend((t["provider"], int(t["external_id"])) for t in result["tracks"])
                if not result["has_more"]:
                    break
                params["cursors"] = json.dumps(result["next_cursors"])
            else:
                self.fail("Provider cursors did not terminate")
        self.assertCountEqual(actual, [(ch, i) for ch, total in totals.items() for i in range(total)])

    def test_probe_verification_expires_on_edit(self):
        vault = self.app.channel_vault
        saved = vault.update_channel("qq", {"cookie": "token"})
        self.assertEqual("configured", saved["connection_state"])
        self.assertFalse(saved["connected"])
        vault.record_probe("qq", saved["updated_at"], True)
        self.assertEqual("reachable", vault.get_channel_public("qq")["connection_state"])
        vault.update_channel("qq", {"clear_credentials": True})
        vault.record_probe("qq", saved["updated_at"], True)
        self.assertEqual("unconfigured", vault.get_channel_public("qq")["connection_state"])

    def test_release_check(self):
        request = httpx.Request("GET", "https://api.github.com/")
        for tag, code, status, newer in [("v0.10.0", 200, "ok", True), ("v0.4.0", 200, "ok", False),
                                          ("v0.3.99", 200, "ok", False), ("bad", 200, "invalid_version", False),
                                          ("", 404, "no_releases", False)]:
            with self.subTest(tag=tag), patch("pruner_studio.app.httpx.get", return_value=httpx.Response(
                    code, request=request, json={"tag_name": tag})) as fetch:
                data = self.app.api("GET", "/api/app/update-check").payload["data"]
                self.assertEqual((status, newer), (data["status"], data["has_update"]))
                self.assertEqual(5, fetch.call_args.kwargs["timeout"])
                self.assertEqual("Pruner-Studio/0.4.0", fetch.call_args.kwargs["headers"]["User-Agent"])
        with patch("pruner_studio.app.httpx.get", side_effect=httpx.ConnectError("offline")):
            self.assertEqual("unreachable", self.app.api("GET", "/api/app/update-check").payload["data"]["status"])

    def test_release_structure(self):
        ci, release, installer, spec = [(ROOT / path).read_text("utf-8") for path in (
            ".github/workflows/ci.yml", ".github/workflows/release.yml", "scripts/Pruner-Studio.iss", "Pruner-Studio.spec")]
        self.assertLess(ci.index("pip install starlette"), ci.index("from pruner_studio.app import serve"))
        for workflow in (ci, release):
            for token in ("--require-hashes", "--no-binary=proxy_tools", "verify-windows.py", "build-windows.ps1"):
                self.assertIn(token, workflow)
        for token in ("workflow_dispatch", "v*.*.*", "contents: write", "SHA256SUMS.txt", "gh release create"):
            self.assertIn(token, release)
        for token in ("PrivilegesRequired=lowest", "{localappdata}", "{userprograms}", "{userdesktop}", "MissingRuntime"):
            self.assertIn(token, installer)
        self.assertIn("upx=False", spec)
        self.assertIn("manifest=str(project_root / 'build' / 'Pruner-Studio.exe.manifest')", spec)
