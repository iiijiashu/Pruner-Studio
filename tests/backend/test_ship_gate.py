"""Release regressions exercised with isolated account stores and in-process transports."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from pruner_studio.app import build_application
from pruner_studio.config import Settings
from pruner_studio.external_sources import _scoped_channel_request, probe_audio_url
from pruner_studio.models import PlaylistSummary, Song
from pruner_studio.netease_client import TransportResponse
from pruner_studio.operations import positive_id
from test_backend import FakeTransport, account_payload, api_song


class ShipGateTests(unittest.TestCase):
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

    def login(self):
        self.transport.handlers["/user/account"] = account_payload()
        self.app.client.set_cookie("MUSIC_U=fixture")
        self.data("GET", "/api/status")
        self.transport.calls.clear()

    def test_offline_binding_partitions_catalog_history_feedback_and_insights(self):
        self.login()
        for aid in ("17", "42"):
            self.app.db.replace_playlist_tracks(aid, PlaylistSummary(1, "cache", 1, "liked", "private"),
                                                [Song(int(aid), aid, ["artist"])])
        for bad in (None, 3, " ", "unclaimed", "a"*65):
            self.assertEqual(400, self.app.api("POST", "/api/accounts/offline", {"account_id": bad}).status)
        binding = self.data("POST", "/api/accounts/offline", {"account_id": "17"})
        scope = {"expected_account_id": "17", "session_generation": binding["session_generation"]}
        self.assertEqual("MUSIC_U=fixture", self.app.session.load_account_cookie("42"))
        self.assertIsNone(self.app._discovery_scope()[-1].cookie)
        self.assertEqual("17", self.data("GET", "/api/accounts")["active_account_id"])
        for route in ("/api/catalog", "/api/recommend/dusty-gems"):
            data = self.data("GET", route)
            self.assertEqual([17], [s["id"] for s in data.get("tracks", data.get("songs"))])
        self.assertEqual("17", self.data("GET", "/api/catalog")["account_id"])
        self.data("POST", "/api/playback/record", {**scope, "song": {"id": 17, "title": "B"}, "completed": True})
        self.data("POST", "/api/feedback", {**scope, "song_id": 17, "action": "not_interested"})
        self.assertEqual(17, self.data("GET", "/api/playback/history")["items"][0]["id"])
        self.assertEqual(17, self.data("GET", "/api/feedback")["items"][0]["song_id"])
        self.assertEqual(self.app.db.get_listening_insights("17"), self.data("GET", "/api/insights?account_id=42"))
        self.assertEqual([], self.app.db.list_play_history("42"))
        self.assertEqual([], self.transport.calls)
        self.assertEqual("17", self.data("GET", "/api/catalog?account_id=42")["account_id"])
        self.assertEqual("17", self.data("GET", "/api/status")["active_account_id"])
        for aid in (17, 42):
            self.app.operations.journal.begin(aid, "like", [f"like:{aid}"], {"song_ids":[aid]})
        self.assertEqual(self.app.operations.pending_for(17), self.data("GET", "/api/operations/pending")["operations"])
        self.assertTrue(self.app.operations.pending_for(17))
        for suffix in ("reconcile", "0"*32+"/reconcile", "0"*32+"/ignore", "like"):
            response = self.app.api("POST", "/api/operations/"+suffix, {})
            self.assertEqual("NOT_AUTHENTICATED", response.payload["error"]["code"])
        self.assertEqual([], self.transport.calls)
        self.assertGreater(self.data("POST", "/api/accounts/switch", {"account_id":"42"})["session_generation"], scope["session_generation"])
        self.assertEqual("42", self.data("GET", "/api/catalog")["account_id"])

    def test_external_requests_pin_each_hop(self):
        hosts = ["one.example", "two.example"]
        addresses = ["8.8.8.8", "1.1.1.1"]
        for probe in (True, False):
            seen = []
            def respond(request):
                i = len(seen)
                seen.append(request.url.host)
                self.assertNotIn("cookie", request.headers)
                self.assertEqual(hosts[i], request.headers["host"])
                self.assertEqual(hosts[i], request.extensions["sni_hostname"])
                if i == 0:
                    return httpx.Response(302, headers={"location": "https://two.example/end"})
                self.assertNotIn("authorization", request.headers)
                self.assertNotIn("x-api-key", request.headers)
                return httpx.Response(206, headers={"content-type": "audio/flac"}, content=b"fLaC")
            with patch("pruner_studio.external_sources.validated_addresses", side_effect=[(ip,) for ip in addresses]) as dns:
                with httpx.Client(transport=httpx.MockTransport(respond), cookies={"unscoped": "fixture"}) as client:
                    if probe:
                        result = probe_audio_url("https://one.example/start", client, headers={"X-Api-Key":"fixture", "Accept":"audio/*"})
                        self.assertEqual({"accept","user-agent"}, {k.lower() for k in result.final_headers})
                        self.assertEqual("https://two.example/end", result.final_url)
                        self.assertTrue(result.ok)
                    else:
                        result = _scoped_channel_request(client, "GET", "https://one.example/start", channel_id="custom",
                                                         validated_api_base="https://one.example",
                                                         secret_headers={"Authorization": "fixture"})
                        self.assertEqual(206, result.status_code)
                        self.assertEqual("two.example", result.url.host)
                self.assertEqual(2, dns.call_count)
            self.assertEqual(addresses, seen)

    def test_import_rejects_foreign_and_non_ascii_ids(self):
        rows = [{"id": 71, "provider": "qq"}, {"id": 71, "track_key": "qq:71"},
                {"id": 71, "source": "local"}, {"id": 71, "local_path":"file"}, {"id": 71.0}, {"id": "７１"}, {"id": 8, "source": "imported"}]
        result = self.app.catalog.import_catalog(json.dumps([{**r, "title": "test"} for r in rows]), "json")
        self.assertEqual(6, result["failed_count"])
        self.assertEqual([8], [s["id"] for s in result["tracks"]])

    def test_invalid_ids_stop_before_upstream_or_journal(self):
        for raw in (True, 42.9, 42.0, "42.9", "４２", "4e2", 2**63, "0"):
            for route, payload in [("/api/operations/like", {"song_id": raw}),
                                   ("/api/operations/unlike", {"song_ids": [42], "source_playlist_id": raw}),
                                   ("/api/operations/create-playlist", {"name": "x", "song_ids": [raw]}),
                                   ("/api/recommend/fm/trash", {"song_id": raw})]:
                with self.subTest(raw=raw, route=route):
                    self.assertEqual(400, self.app.api("POST", route, payload).status)
        self.assertEqual([], self.transport.calls)
        self.app.client.set_cookie("MUSIC_U=cold")
        scoped = {"expected_account_id": "42", "session_generation": self.app.session_generation}
        response = self.app.api("POST", "/api/operations/create-playlist", {**scoped, "name":"x", "song_ids":[42.0]})
        self.assertEqual(400, response.status)
        self.assertEqual([], self.transport.calls)
        for value in (False, 0, "", {}):
            self.assertEqual(400, self.app.api("POST", "/api/operations/create-playlist", {"name":"x","song_ids":value}).status)
        for action, pid, source in (("unlike", 1, 42.0), ("remove_tracks", 42.0, 42), ("remove_tracks", 42, 42.0)):
            self.assertEqual(400, self.app.api("POST", "/api/operations/reconcile", {
                "action": action, "playlist_id":pid, "source_playlist_id":source, "song_ids":[1]}).status)
        self.assertEqual([], self.transport.calls)
        self.assertEqual(2**63-1, positive_id(str(2**63-1)))
        self.assertEqual(42, positive_id(" 042 "))

    def test_local_page_tail_and_comma_exclusions(self):
        files = [{"title": f"name {i:04}", "path": f"C:/music/A,{i}.flac", "duration_ms": 5000}
                 for i in range(2002)]
        self.app.db.replace_local_files(files)
        keys = ["local:" + f["path"] for f in files]
        radio = self.data("POST", "/api/recommend/smart-radio", {"exclude_keys": keys[:-1]})
        self.assertEqual([keys[-1]], [s["track_key"] for s in radio["songs"]])
        result = self.data("GET", "/api/recommend/dusty-gems?" + "&".join("exclude_key=" + k for k in keys[:-1]))
        self.assertEqual([keys[-1]], [s["track_key"] for s in result["songs"]])
        self.assertEqual(5000, result["songs"][0]["duration_ms"])

    def test_active_batch_and_history_survive_long_session(self):
        options = {"radio_session_id": "long", "request_id": "first"}
        first = self.data("POST", "/api/recommend/smart-radio", options)
        for i in range(126):
            self.app.db.commit_radio_batch("local", {**options, "request_id": str(i), "songs": [
                {"id": j, "track_key": f"netease:{j}"} for j in range(6)]})
        self.assertEqual(first, self.data("POST", "/api/recommend/smart-radio", options))
        with self.app.db._connect() as conn:
            self.assertEqual(756, conn.execute("SELECT count(*) FROM radio_served_history").fetchone()[0])
        self.assertEqual([], self.transport.calls)

    def test_explicit_replay_uses_previous_discoveries_without_reads(self):
        self.login()
        self.app.db.commit_radio_batch("42", {"radio_session_id": "s", "request_id": "old", "songs": [
            {**Song(i, str(i), ["artist"], availability="unavailable" if i == 3 else "available").to_dict(),
             "track_key": f"netease:{i}"} for i in (1, 2, 3)]})
        self.app.db.add_feedback("42", 2, "not_interested")
        result = self.data("POST", "/api/recommend/smart-radio", {"radio_session_id": "s", "request_id": "replay",
                           "allow_replay": True, "exclude_ids": [1, 2, 3]})
        self.assertEqual([1], [s["id"] for s in result["songs"]])
        restarted = self.data("POST", "/api/recommend/smart-radio", {"radio_session_id": "new",
                              "request_id": "history", "allow_replay": True})
        self.assertEqual([1], [s["id"] for s in restarted["songs"]])
        self.assertEqual([], self.transport.calls)

    def test_existing_radio_history_migrates_preserving_served_rows(self):
        with self.app.db._connect() as conn:
            conn.execute("DROP TABLE radio_served_history")
            conn.execute("""CREATE TABLE radio_served_history (
                id INTEGER PRIMARY KEY, account_id TEXT, radio_session_id TEXT, request_id TEXT,
                track_key TEXT, song_id INTEGER, status TEXT CHECK(status IN ('served','played','completed')),
                served_at TEXT)""")
            conn.execute("INSERT INTO radio_served_history VALUES (1,'17','s','r','netease:1',1,'served','old')")
        self.app = build_application(self.settings, self.transport)
        self.app.db.mark_radio_requests_superseded("17","s",["r"])
        with self.app.db._connect() as conn:
            self.assertEqual(("superseded","old"),tuple(conn.execute("SELECT status,served_at FROM radio_served_history").fetchone()))

    def test_daily_snapshot_survives_empty_check_restart_and_offline_history(self):
        self.login()
        route = "/api/recommend/daily"
        self.transport.handlers["/recommend/songs"] = {"code": 200, "data": {"dailySongs": [api_song(7)]}}
        initial = self.data("GET", route)
        self.transport.handlers["/recommend/songs"]["data"]["dailySongs"] = []
        later = self.data("GET", route+"?force_check=1")
        self.assertTrue(later["degraded"])
        self.assertGreaterEqual(later["checked_at"], initial["checked_at"])
        self.assertEqual([7], later["raw_song_ids"])
        self.app = build_application(self.settings, self.transport)
        self.data("POST", "/api/accounts/offline", {"account_id": "42"})
        self.transport.calls.clear()
        self.assertEqual([7], self.data("GET", route)["raw_song_ids"])
        day = initial["recommend_day"]
        self.assertIn(day, self.data("GET", route+"/history")["dates"])
        history = self.data("GET", route+"/history?date="+day)
        self.assertEqual(("ok", 7), (history["status"], history["songs"][0]["id"]))
        self.assertEqual([], self.transport.calls)

    def test_playback_alias_rebases_center_and_guards_generation(self):
        queue = [{"id": i} for i in range(600)]
        for idx in (-1, 0, 318, 599):
            res = self.data("POST", "/api/playback-session", {"queue": queue, "queue_index": idx})
            self.assertEqual(221, len(res["queue"]))
            self.assertEqual(res, self.data("GET", "/api/playback/session"))
            if idx >= 0:
                self.assertEqual(queue[idx], res["queue"][res["queue_index"]])
        self.assertEqual(409, self.app.api("POST", "/api/playback-session", {"session_generation": -1}).status)

    def test_fm_trash_auth_failure_preserves_block(self):
        self.login()
        self.data("PUT", "/api/settings", {"sync_fm_trash_to_netease": True})
        self.data("POST", "/api/feedback", {"song_id": 9, "action": "not_interested"})
        for response in (TransportResponse(401, {}), {"code": 401}, {"code": 301, "message":"risk"}):
            self.transport.handlers["/fm_trash"] = response
            self.assertEqual("auth_required", self.data("POST", "/api/recommend/fm/trash", {"song_id": 9})["status"])
        self.assertEqual(0, self.app.client.cooldown_seconds())
        self.assertIn(9, self.app.db.blocked_song_ids("42"))

    def test_rule_validation_preserves_previous_rules(self):
        route = "/api/rules"
        rule = {"id": "x", "name": "rule", "match_clauses": [{"field":"duration", "operator":"lt", "value":"9"}]}
        original = self.data("PUT", route, {"rules": [rule]})
        for field, op, value in [("duration","contains",90), ("duration","lt","nan"), ("liked","equals",1),
                                 ("title","regex","["), ("file","equals",42), ("artist","equals",[])]:
            rule["match_clauses"] = [{"field":field, "operator":op, "value":value}]
            self.assertEqual(400, self.app.api("PUT", route, {"rules": [rule]}).status)
        self.assertEqual(original, self.data("GET", route))

    def test_stream_generation_on_mint_switch_and_late_response(self):
        self.login()
        gen = self.app.session_generation
        minted = []
        def mint(*args, **kwargs):
            target = self.app.audio.register_stream_token(kind="remote", url="https://one.example/song")
            minted.append(target.token)
            self.assertEqual(gen, target.session_generation)
            return {"stream_token": target.token}

        with patch.object(self.app.audio, "resolve", side_effect=mint):
            token = self.data("GET", "/api/audio?id=1")["stream_token"]
        self.assertEqual(token, self.app.resolve_stream_target(token).token)
        def mint_late(*args, **kwargs):
            self.data("POST", "/api/accounts/offline", {"account_id":"17"})
            return mint()
        with patch.object(self.app.audio, "resolve_external_track", side_effect=mint_late):
            self.assertEqual(409, self.app.api("GET", "/api/audio?provider=custom&external_id=1").status)
        self.assertTrue(all(self.app.audio.resolve_stream_token(t) is None for t in minted))
        stale = self.app.audio.register_stream_token(kind="remote", url="https://one.example/song", session_generation=gen)
        self.assertIsNone(self.app.audio.resolve_stream_token(stale.token, expected_generation=self.app.session_generation))
        self.assertIsNone(self.app.audio.resolve_stream_token(stale.token))

    def test_radio_supersession_reclaims_last_track_in_both_commit_orders(self):
        self.app.db.replace_local_files([{"path":"C:/song.flac","title":"song","duration_ms":3000}])
        path = "/api/recommend/smart-radio"
        for late in (False, True):
            session = str(late)
            refill = {"radio_session_id":session,"request_id":"refill","mode":"refill"}
            if not late:
                self.assertEqual(1, len(self.data("POST", path, refill)["songs"]))
            manual = self.data("POST", path, {**refill, "mode":"rearrange","request_id":"manual",
                                              "superseded_request_ids":["refill"]})
            self.assertEqual(["local:C:/song.flac"], [s["track_key"] for s in manual["songs"]])
            if late:
                stale = {**manual, **refill}
                self.app.db.commit_radio_batch("local", stale)
                self.assertEqual(stale, self.data("POST", path, refill))
            with self.app.db._connect() as conn:
                rows = conn.execute("SELECT request_id,status FROM radio_served_history WHERE radio_session_id=?", (session,))
                self.assertEqual({"manual":"served","refill":"superseded"}, dict(rows))
            self.assertEqual({"local:C:/song.flac"}, self.app.db.radio_served_keys("local", session))
        for bad in ("x", [" "], [1], ["x"*201], ["id"]*51):
            self.assertEqual(400, self.app.api("POST", path, {"superseded_request_ids":bad}).status)

    def test_history_index_survives_restart_without_detail(self):
        self.login()
        path = "/api/recommend/daily/history"
        for day in ("2026-06-01","2026-06-02"):
            self.transport.handlers["/history/recommend/songs"] = {"code":200,"data":{"dates":[day]}}
            self.data("GET", path)
        self.app = build_application(self.settings, self.transport)
        self.data("POST", "/api/accounts/offline", {"account_id":"42"})
        self.transport.calls.clear()
        history = self.data("GET", path)
        self.assertEqual((["2026-06-02","2026-06-01"], [], "ok"), (history["dates"],history["cached_dates"],history["status"]))
        detail = self.data("GET", path+"?date=2026-06-01")
        self.assertEqual("auth_required", detail["status"])
        self.assertIn("详情尚未缓存", detail["reason"])
        self.data("POST", "/api/accounts/offline", {"account_id":"17"})
        self.assertEqual([], self.data("GET", path)["dates"])
        self.assertEqual([], self.transport.calls)

    def test_portable_rule_fixture(self):
        from pruner_studio.catalog_service import _valid_clause
        fixture = Path(__file__).resolve().parents[1] / "rule_validation.json"
        for field, op, value, valid in json.loads(fixture.read_text("utf-8")):
            with self.subTest(value=value):
                self.assertEqual(valid, _valid_clause({"field": field, "operator": op, "value": value}))
