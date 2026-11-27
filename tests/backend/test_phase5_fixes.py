"""Release regressions using isolated accounts, a real SQLite store and mocked upstreams."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.testclient import TestClient

from pruner_studio.app import build_application, create_asgi_app
from pruner_studio.config import Settings
from pruner_studio.errors import AppError
from pruner_studio.external_sources import ExternalCandidate
from pruner_studio.models import AccountSummary, PlaylistSummary, Song
from pruner_studio.netease_client import NeteaseWebTransport, TransportResponse, UrllibTransport
from pruner_studio.snapshots import download_snapshot
from test_backend import FakeTransport, account_payload, mock_liked_detail


class Phase5Tests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.transport = FakeTransport()
        self.app = build_application(Settings(root, root), self.transport)
        self.app.client.pacer.sleep_fn = lambda _: None
        self.http = self.enterContext(TestClient(create_asgi_app(self.app, 8765), base_url="http://127.0.0.1:8765"))

    def login(self):
        self.transport.handlers["/user/account"] = account_payload()
        self.app.client.set_cookie("MUSIC_U=fixture")
        self.app.api("GET", "/api/status")

    def test_contradictory_like_list_preserves_catalog_and_journal(self):
        self.login()
        ids = [70, 71]
        mock_liked_detail(self.transport, lambda: ids)
        self.app.db.replace_playlist_tracks("42", PlaylistSummary(10, "Liked", 2, "liked", "public"),
                                            [Song(i, str(i), []) for i in ids])
        self.transport.handlers["/like"] = {"code": 200}
        for single, target in [(True, 70), (False, 71)]:
            reads = iter([[]] if single else [ids, []])
            self.transport.handlers["/likelist"] = lambda *_: {"code": 200, "ids": next(reads, [])}
            ops = self.app.operations
            result = ops.like_track(target, False) if single else ops.unlike([target], 10)
            for outcome in [result, ops.reconcile("unlike", None, [target], 10, confirm_not_applied=True),
                            ops.reconcile_pending(42, result.operation_id, confirm_not_applied=True)]:
                self.assertEqual([], outcome.successful_ids)
                self.assertEqual([target], [i.id for i in outcome.unknown_items])
                self.assertIn(result.operation_id, outcome.pending_operation_ids)
            self.assertEqual(ids, [row["id"] for row in self.app.catalog.get("42")["playlists"]["10"]["songs"]])
            self.assertEqual([result.operation_id], [entry["operation_id"] for entry in ops.pending_for(42)])
            # Start the next independent write scenario with a fresh journal.
            ops.journal.resolve(result.operation_id)

    def test_old_account_writes_rejected_at_http_boundary(self):
        for uid in (42, 43):
            self.app.session.save_account_session(AccountSummary(uid, str(uid)), f"MUSIC_U={uid}")
            self.transport.handlers["/user/account"] = {"code": 200, "profile": {"userId": uid, "nickname": str(uid)}}
            switched = self.http.post("/api/accounts/switch", json={"account_id": str(uid)})
            self.assertEqual(uid - 40, switched.json()["data"]["session_generation"])
        self.app.db.save_playback_session("43", {"position_sec": 12})
        self.app.db.record_play("43", {"id": 99}, completed=True)
        baseline = len(self.transport.calls)
        routes = [("POST", "/api/operations/create-playlist"), ("PUT", "/api/playback/session"),
                  ("DELETE", "/api/playback/history"), ("POST", "/api/playback/record"),
                  ("POST", "/api/operations/like"), ("POST", "/api/operations/unlike"),
                  ("POST", "/api/playlists/10/add-tracks"), ("POST", "/api/playlists/10/remove-tracks")]
        for method, route in routes:
            for scope in [{}, {"expected_account_id": "42", "session_generation": 2},
                          {"expected_account_id": "42", "session_generation": 3}]:
                with self.subTest(route=route, scope=scope):
                    res = self.http.request(method, route, json={**scope, "name": "old", "song_ids": [99]})
                    self.assertEqual((409, "SESSION_GENERATION_MISMATCH"), (res.status_code, res.json()["error"]["code"]))
        self.assertEqual(baseline, len(self.transport.calls))
        self.assertEqual(12, self.app.db.get_playback_session("43")["position_sec"])
        self.assertEqual([99], [r["song_id"] for r in self.app.db.list_play_history("43")])

    def test_large_snapshot_http_roundtrip_checksum_and_account_rotation(self):
        count = 4096
        self.app.db.replace_playlist_tracks("local", PlaylistSummary(4, "Saved", count, "self_created", "private"),
            [Song(i, "t" * 128, ["singer"]) for i in range(1, count + 1)])
        snapshot = self.http.post("/api/operations/backup", json={}).json()["data"]
        route = "/api/operations/backup/" + snapshot["backup_id"] + "/download"
        raw = self.http.get(route).content
        self.assertGreater(len(raw), 1024**2)
        restored = self.http.post("/api/catalog/import", json={"content": raw.decode(), "format": "json", "mode": "replace"})
        self.assertEqual(201, restored.status_code, restored.text[:200])
        self.assertEqual(count, restored.json()["data"]["imported_count"])
        previous = restored.json()["data"]["backup_id"]
        stored = json.loads(raw)
        for digest in [None, "", "0" * 64]:
            invalid = stored.copy()
            if digest is None:
                invalid.pop("checksum")
            else:
                invalid["checksum"] = digest
            res = self.http.post("/api/catalog/import", json={"format": "json", "content": json.dumps(invalid)})
            self.assertEqual((400, "SNAPSHOT_CHECKSUM_INVALID"), (res.status_code, res.json()["error"]["code"]))
        self.app.catalog.import_catalog('[{"id":1,"title":"a"}]', "json", account_id="other")
        receipts = [self.app.operations.backup(account_id="other") for _ in range(11)]
        self.assertFalse(Path(receipts[0].snapshot_path).exists())
        for receipt in receipts[1:]:
            self.assertTrue(Path(receipt.snapshot_path).is_file())
        store = self.app.operations.store
        self.assertEqual(raw, download_snapshot(store, snapshot["backup_id"], "local"))
        self.assertEqual(count, len(json.loads(download_snapshot(store, previous, "local"))["tracks"]))

    def test_global_read_budget_across_clients(self):
        active = peak = 0
        lock = threading.Lock()
        start = threading.Barrier(4)
        def search(*_):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(active, peak)
            time.sleep(.06)
            with lock:
                active -= 1
            return {"code": 200, "result": {"songs": []}}
        self.transport.handlers["/cloudsearch"] = search
        def worker(_):
            start.wait(5)
            return self.app.client.with_cookie("same").search_songs("song")
        with ThreadPoolExecutor(4) as pool:
            self.assertEqual([[]] * 4, list(pool.map(worker, range(4))))
        self.assertIn(peak, (1, 2))

    def test_risk_stops_audio_recommendations_and_detail_waves(self):
        self.login()
        for fn in [lambda: self.app.audio._resolve_netease_track(self.app.client, 7, "hires"),
                   lambda: self.app.recommend.daily("42")]:
            self.app.client.pacer.risk_cooldown_until.clear()
            self.transport.calls.clear()
            for path in ["/song/url/v1", "/recommend/songs"]:
                self.transport.handlers[path] = TransportResponse(429, {})
            with self.assertRaises(AppError) as caught:
                fn()
            self.assertEqual("UPSTREAM_RISK_CONTROL", caught.exception.code)
            self.assertEqual(1, len(self.transport.calls))
        ids = list(range(1, 2501))
        detail = TransportResponse(200, {"code": 200, "playlist": {
            "trackCount": len(ids), "trackIds": [{"id": i} for i in ids], "tracks": []}})
        with patch.object(UrllibTransport, "request", side_effect=[detail, TransportResponse(429, {})]) as request:
            with self.assertRaises(AppError) as caught:
                NeteaseWebTransport("https://music.163.com").request("/playlist/track/all", {"id": 10})
            self.assertEqual("UPSTREAM_RISK_CONTROL", caught.exception.code)
            self.assertEqual(2, request.call_count)

    def test_truncated_cloud_pages_preserve_cache(self):
        self.login()
        self.transport.handlers["/user/cloud"] = {"code": 200, "count": 1, "data": [{"songId": 99}]}
        self.assertEqual(200, self.http.get("/api/cloud").status_code)
        for pages in [[{"count": 31, "hasMore": False, "data": [{"songId": 7}]}],
                      [{"count": 2, "hasMore": True, "data": [{"songId": 7}]},
                       {"hasMore": False, "data": [{"songId": 7}]}]]:
            it = iter(pages)
            self.transport.handlers["/user/cloud"] = lambda *_: {"code": 200, **next(it)}
            res = self.http.get("/api/cloud")
            self.assertEqual((502, "CLOUD_SYNC_INCOMPLETE"), (res.status_code, res.json()["error"]["code"]))
            self.assertEqual([99], [r["id"] for r in self.app.catalog.get("42")["playlists"]["cloud"]["songs"]])

    def test_untyped_audio_stream_and_dns_pinning_on_redirect(self):
        resolver = self.enterContext(patch("pruner_studio.external_sources._resolve_addresses",
            side_effect=lambda host, port, **kw: [(2, 1, 6, "", ("8.8.8.8" if host == "cdn.test" else "8.8.4.4", port))]))
        factory = httpx.AsyncClient
        for body, mime, status in [(b"<html>failure", None, 502), (b'{"error":1}', "application/octet-stream", 502),
                                   (b"unknown", "", 502), (b"fLaCtest", "application/octet-stream", 200)]:
            target = self.app.audio.register_stream_token(session_generation=self.app.session_generation, kind="remote", url="https://cdn.test/start", headers={"X-Api-Key":"test"}, cache_key="stable")
            hits = []
            def upstream(req):
                self.assertEqual(not hits, "x-api-key" in req.headers)
                hits.append((req.url.host, req.headers["host"], req.extensions.get("sni_hostname")))
                if len(hits) == 1:
                    return httpx.Response(302, headers={"location": "https://media.test/final"})
                return httpx.Response(200, headers={"content-type": mime} if mime is not None else {}, content=body)
            with patch("pruner_studio.app.httpx.AsyncClient", side_effect=lambda **kw: factory(
                    transport=httpx.MockTransport(upstream), **kw)):
                res = self.http.get("/api/stream/" + target.token)
            self.assertEqual(status, res.status_code)
            self.assertEqual([("8.8.8.8", "cdn.test", "cdn.test"), ("8.8.4.4", "media.test", "media.test")], hits)
            self.assertIs(status == 200, self.app.audio_cache.get("stable") is not None)
            if status == 200:
                self.assertEqual(body, res.content)
            else:
                self.assertEqual("STREAM_UPSTREAM_INVALID", res.json()["error"]["code"])
        self.assertEqual(8, resolver.call_count)

    def test_rotating_external_urls_share_track_quality_cache_key(self):
        keys = []
        for sig in ["one", "two"]:
            track = ExternalCandidate("T", [], "", 1500, "qq", "abc", url="https://cdn.test/" + sig, format="flac")
            resolved = self.app.audio._build_external_payload(Song(0, "t", []), track, force_proxy=True)
            keys.append(self.app.audio.resolve_stream_token(resolved["stream_token"]).cache_key)
        self.assertEqual(keys[0], keys[1])
        self.assertIn("qq:abc:flac", keys[0])

    def test_cover_pins_ip_and_rejects_scriptable_content(self):
        factory = httpx.Client
        self.enterContext(patch("pruner_studio.external_sources._resolve_addresses",
                                return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
        for mime, status in [("image/svg+xml", 502), ("image/png", 200)]:
            def upstream(req):
                self.assertEqual("8.8.8.8", req.url.host)
                self.assertEqual("p1.music.126.net", req.headers["host"])
                self.assertEqual(req.headers["host"], req.extensions["sni_hostname"])
                return httpx.Response(200, headers={"content-type": mime}, content=b"image")
            with patch("pruner_studio.app.httpx.Client", side_effect=lambda **kw: factory(
                    transport=httpx.MockTransport(upstream), **kw)):
                res = self.http.get("/api/cover", params={"url": "https://p1.music.126.net/test"})
            self.assertEqual(status, res.status_code)
            if status == 200:
                self.assertEqual("sandbox", res.headers["Content-Security-Policy"])
                self.assertEqual("nosniff", res.headers["X-Content-Type-Options"])

    def test_transient_channel_failure_preserves_cursor(self):
        resolver = self.app.audio.external_resolver
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as http:
            with patch.object(resolver, "_get_http_client", return_value=http):
                result = resolver.search_multi_channel("song", channels=["qq"], cursors={"qq": [10, 2]})
        self.assertIn("qq", result["channel_errors"])
        self.assertTrue(result["has_more"])
        self.assertEqual([10, 2], result["next_cursors"]["qq"])

    def test_probe_reports_search_without_unverified_media_quality(self):
        resolver = self.app.audio.external_resolver
        with patch.object(resolver, "_search_single_provider", return_value=[object()]):
            result = resolver.test_channel("qq")
        self.assertTrue(result["ok"])
        self.assertIn("搜索可用", result["message"])
        self.assertEqual("", result["max_quality_hint"])

    @unittest.skipUnless(sys.platform == "win32", "Windows build tool")
    def test_python_discovery_falls_back_after_unregistered_launcher(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        scripts = root / "scripts"
        scripts.mkdir()
        original = Path(__file__).resolve().parents[2] / "scripts/build-windows.ps1"
        bootstrap = original.read_text(encoding="utf-8").split("if (-not $SkipLockInstall)")[0]
        # Interpreter discovery only needs the venv interpreter, without bootstrapping pip.
        bootstrap = bootstrap.replace("-m venv $venvDir", "-m venv --without-pip $venvDir")
        probe = scripts / "probe.ps1"
        probe.write_text(bootstrap, encoding="utf-8-sig")
        (scripts / "py.cmd").write_text('@echo tried > "%~dp0attempted.txt"\n@exit /b 103\n')
        search_path = os.pathsep.join((str(scripts), str(Path(sys.executable).parent), os.environ["PATH"]))
        env = dict(os.environ, PATH=search_path)
        run = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
                             env=env, capture_output=True, timeout=90)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertTrue((scripts / "attempted.txt").is_file())
        self.assertTrue((root / ".venv/Scripts/python.exe").is_file())

    def test_runtime_files_are_ignored(self):
        root = Path(__file__).resolve().parents[2]
        paths = ["fixture/" + name for name in ("backups/abc.json", "test.sqlite3", "test.sqlite3-wal",
                                                "test.sqlite3-shm", ".instance.lock", ".instance.json")]
        result = subprocess.run(["git", "check-ignore", "--no-index", *paths], cwd=root, capture_output=True, text=True)
        self.assertCountEqual(paths, result.stdout.splitlines())

    def test_minimum_listen_applied_once(self):
        def record(ms, completed=False, event="one"):
            return self.app.db.record_play("42", {"id": 7}, event_id=event, listen_ms=ms, completed=completed)
        self.assertFalse(record(14999)["recorded"])
        self.assertFalse(self.app.db.list_play_history("42"))
        for ms, completed in [(15000, False), (21000, True), (21000, True), (18000, False)]:
            result = record(ms, completed)
            self.assertEqual(1, result["play_count"])
        self.assertEqual((21000, 1), (result["total_listen_ms"], result["completed_count"]))
        self.assertEqual(2, record(100, True, "two")["play_count"])
        updated = record(500, False, "two")
        self.assertEqual((2, 21500), (updated["play_count"], updated["total_listen_ms"]))
