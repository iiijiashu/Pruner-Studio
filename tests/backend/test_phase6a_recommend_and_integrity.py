"""Phase 6A regression coverage using temporary SQLite databases and isolated transports."""
import asyncio
import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from pruner_studio.app import build_application, create_asgi_app
from pruner_studio.config import Settings
from pruner_studio.db import CatalogDatabase
from pruner_studio.errors import AppError
from pruner_studio.models import PlaylistSummary, Song
from pruner_studio.netease_client import (
    NeteaseClient, NeteaseWebTransport, TransportResponse,
    UrllibTransport, recommendation_reads,
)
from pruner_studio.recommend_service import beijing_recommend_day
from test_backend import FakeTransport, account_payload, api_song


class Phase6ATests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.transport = FakeTransport()
        self.app = build_application(Settings(self.root, self.root), self.transport)
        self.db = self.app.db
        self.asgi = create_asgi_app(self.app, 8765)
        self.http = self.enterContext(TestClient(self.asgi, base_url="http://127.0.0.1:8765"))

    def login(self):
        self.transport.handlers["/user/account"] = account_payload()
        self.app.client.set_cookie("MUSIC_U=test")
        self.http.get("/api/status").raise_for_status()
        self.transport.calls.clear()

    def library(self, size, aid="local", kind="imported", songs=None):
        songs = songs or [Song(i, f"Track {i}", [f"Artist {i%11}"], duration=180000) for i in range(1, size+1)]
        self.db.replace_playlist_tracks(aid, PlaylistSummary(10, "Music", size, kind, "private"), songs)

    def radio(self, request, **options):
        response = self.http.post("/api/recommend/smart-radio", json={
            "radio_session_id": "session", "request_id": str(request), **options})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["data"]

    def test_overlap_matrix_through_exhaustion_and_replay(self):
        for size in (15, 40, 200, 2000):
            with self.subTest(size=size):
                self.library(size)
                seen = set()
                number = 0
                while len(seen) < size:
                    batch = self.radio(number, radio_session_id=str(size), mode="refill", limit=100)
                    tracks = [t["track_key"] for t in batch["songs"]]
                    self.assertTrue(tracks)
                    self.assertTrue(seen.isdisjoint(tracks))
                    self.assertEqual(len(tracks), len(set(tracks)))
                    self.assertEqual(len(tracks), batch["new_count"])
                    seen.update(tracks)
                    number += 1
                exhausted = self.radio(number, radio_session_id=str(size))
                self.assertEqual(("exhausted", 0, [], True, True), tuple(exhausted[k] for k in
                                 ("status", "new_count", "songs", "exhausted", "allow_replay_available")))
                self.assertTrue(self.radio(number+1, radio_session_id=str(size), allow_replay=True)["songs"])
        self.assertFalse(self.transport.calls)
        with self.db._connect() as conn:
            self.assertLessEqual(conn.execute("SELECT count(*) FROM radio_served_history WHERE radio_session_id != '2000'").fetchone()[0], 600)

    def test_offline_local_files_keys_and_version_dedupe(self):
        self.db.replace_local_files([{"path": str(self.root/f"{i}.flac"), "title": title, "artist":"V",
                                      "duration_ms":180000} for i, title in enumerate(
                                          ["Song", "Song (2024 Remaster)", "Song Live", "Song Cover"])])
        batch = self.radio("a")
        self.assertEqual(3, batch["breakdown"]["local_files"])
        for t in batch["songs"]:
            self.assertEqual(("local", 0, "local:"+t["local_path"]), (t["provider"], t["id"], t["track_key"]))
        self.assertEqual(batch, self.radio("a"))
        with self.db._connect() as conn:
            self.assertEqual(3, conn.execute("SELECT count(*) FROM radio_served_history").fetchone()[0])

    def test_online_expansion_served_seeds_cache_replay_and_refill_budget(self):
        self.login()
        self.library(15, "42", "liked")
        self.transport.handlers["/playmode/intelligence/list"] = lambda params, *_: {
            "code":200, "data":[{"songInfo":api_song(i, str(i))} for i in range(100, 180)]}
        first = self.radio("a", exclude_ids=list(range(1, 16)), limit=20)
        self.assertEqual(20, first["new_count"])
        self.assertTrue(set(range(1, 16)).isdisjoint(t["id"] for t in first["songs"]))
        self.assertEqual(["/playmode/intelligence/list"], [r[0] for r in self.transport.calls])
        self.assertIn(self.transport.calls[0][1]["id"], range(1, 16))
        self.transport.calls.clear()
        self.assertEqual(first, self.radio("a"))
        refill = self.radio("b", mode="refill", limit=10, exclude_ids=list(range(1,16)))
        self.assertTrue({t["id"] for t in first["songs"]}.isdisjoint(t["id"] for t in refill["songs"]))
        self.assertEqual(10, refill["new_count"])
        self.assertEqual([], self.transport.calls)

    def test_cache_miss_read_caps_and_cooldown_stops_remaining_sources(self):
        self.login()
        self.library(15, "42", "liked")
        for path in ("/playmode/intelligence/list", "/simi/playlist", "/simi/song", "/personal_fm", "/recommend/resource"):
            self.transport.handlers[path] = {"code":200}
        for i, (mode, cap) in enumerate([("start", 3), ("refill", 2), ("rearrange", 3)]):
            self.app.recommend._cache.clear()
            self.transport.calls.clear()
            self.radio(i, mode=mode)
            self.assertLessEqual(len(self.transport.calls), cap)
            self.assertGreater(len(self.transport.calls), 0)
        for path in self.transport.handlers:
            self.transport.handlers[path] = TransportResponse(429, {})
        self.transport.calls.clear()
        self.app.recommend._cache.clear()
        batch = self.radio("risk", radio_session_id="risk")
        self.assertEqual(1, len(self.transport.calls))
        self.assertEqual("cooldown", batch["status"])
        self.assertTrue(batch["songs"])
        self.assertGreater(batch["cooldown_sec"], 0)
        self.assertTrue(batch["reason"])
        self.radio("cool", radio_session_id="cool")
        self.assertEqual(1, len(self.transport.calls))

    def test_hard_blocks_replays_and_recent_tracks(self):
        self.library(3, songs=[Song(1, "Blocked", ["A"]), Song(2, "Recent", ["B"]),
                               Song(3, "Unavailable", ["C"], availability="unavailable")])
        self.db.add_feedback("local", 1, "not_interested")
        self.db.record_play("local", {"id":2}, listened_ms=30000)
        self.assertEqual([], self.radio(1)["songs"])
        self.assertEqual([2], [t["id"] for t in self.radio(2, allow_replay=True)["songs"]])

    def test_same_request_concurrency_and_session_switch_during_fetch(self):
        self.login()
        self.library(3, "42", "liked")
        started, release = threading.Event(), threading.Event()
        def heartbeat(*args):
            started.set()
            self.assertTrue(release.wait(4))
            return {"code":200, "data":[api_song(200, "new")]}
        self.transport.handlers["/playmode/intelligence/list"] = heartbeat
        body = {"radio_session_id":"s", "request_id":"r", "limit":2, "expected_account_id":"42"}
        post = lambda: self.app.api("POST", "/api/recommend/smart-radio", body)
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(post)
            self.assertTrue(started.wait(4))
            b = pool.submit(post)
            release.set()
            self.assertEqual(a.result().payload, b.result().payload)
        self.assertEqual(1, len(self.transport.calls))
        started.clear()
        release.clear()
        body.update(request_id="stale", radio_session_id="switched", exclude_ids=[200])
        self.app.recommend._cache.clear()
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(post)
            self.assertTrue(started.wait(4))
            self.app.api("POST", "/api/auth/logout", {})
            release.set()
            response = future.result()
        self.assertEqual(409, response.status)
        self.assertEqual("SESSION_GENERATION_MISMATCH", response.payload["error"]["code"])
        self.assertIsNone(self.db.radio_batch("42", "switched", "stale"))

    def test_daily_boundary_raw_ids_and_independent_explore(self):
        for wall, day in [("2026-02-01T05:59:00+08:00","2026-01-31"), ("2026-02-01T06:01:00+08:00","2026-02-01")]:
            self.assertEqual(day, beijing_recommend_day(datetime.fromisoformat(wall).timestamp()))
        self.login()
        self.transport.handlers["/recommend/songs"] = {"code":200, "data":{"dailySongs":[api_song(1), api_song(2)]}}
        self.db.add_feedback("42", 1, "not_interested")
        daily = "/api/recommend/daily"
        first = self.http.get(daily).json()["data"]
        self.assertEqual([1, 2], first["raw_song_ids"])
        self.assertEqual([2], [s["id"] for s in first["songs"]])
        self.assertFalse(self.http.get(daily+"?known_raw_ids=2,1").json()["data"]["changed"])
        checked = self.http.get(daily+"?force_check=1&known_raw_ids=2").json()["data"]
        self.assertEqual((True, 1), (checked["changed"], checked["added_count"]))
        self.assertEqual(2, len(self.transport.calls))
        self.transport.handlers["/recommend/resource"] = {"code":200,"recommend":[{"id":10}]}
        self.transport.handlers["/playlist/sample"] = {"code":200, "playlist":{"tracks":[api_song(i,str(i)) for i in range(3,10)]}}
        self.transport.calls.clear()
        a = self.http.post("/api/recommend/explore-batch", json={"exclude_ids":[3], "limit":2}).json()["data"]
        self.assertEqual(2, a["new_count"])
        self.assertLessEqual(len(self.transport.calls), 2)
        self.transport.calls.clear()
        b = self.http.post("/api/recommend/explore-batch", json={"exclude_ids":[3], "limit":2}).json()["data"]
        self.assertTrue({3, *(t["id"] for t in a["songs"])}.isdisjoint(t["id"] for t in b["songs"]))
        self.assertEqual(2, b["new_count"])
        self.assertEqual(first["raw_song_ids"], self.http.get(daily).json()["data"]["raw_song_ids"])
        self.assertFalse(self.transport.calls)

    def test_history_dates_detail_validation_and_status(self):
        route = "/api/recommend/daily/history"
        self.assertEqual("auth_required", self.http.get(route).json()["data"]["status"])
        self.login()
        for code, status in [(403,"vip_required"), (301,"auth_required"), (500,"error"), (200,"empty")]:
            self.transport.handlers["/history/recommend/songs"] = {"code":code}
            self.assertEqual(status, self.http.get(route).json()["data"]["status"])
        self.transport.handlers["/history/recommend/songs"] = {"code":200,"data":{"dates":["2026-01-01"]}}
        self.assertEqual(["2026-01-01"], self.http.get(route).json()["data"]["dates"])
        self.transport.handlers["/history/recommend/songs/detail"] = {"code":200, "data":{"songs":[api_song(9)]}}
        self.transport.calls.clear()
        for day in ("2026-02-31", "2026-1-1", "2026-11-02"):
            self.assertEqual(400, self.http.get(route+"?date="+day).status_code)
        self.assertEqual(9, self.http.get(route+"?date=2026-01-01").json()["data"]["songs"][0]["id"])
        self.assertEqual(1, len(self.transport.calls))
        self.transport.handlers["/history/recommend/songs"] = TransportResponse(503,{})
        self.assertEqual("cooldown", self.http.get(route).json()["data"]["status"])

    def test_optional_guards_and_catalog_counts(self):
        self.login()
        cases = [("POST", "/api/feedback", {"song_id":1, "action":"replay"}),
                 ("DELETE", "/api/feedback", {}), ("POST","/api/catalog/import", {}),
                 ("POST","/api/recommend/smart-radio", {}), ("POST","/api/recommend/fm/trash", {"song_id":1})]
        for method, route, body in cases:
            for guard in [{"expected_account_id":"wrong"}, {"session_generation":-1}]:
                response = self.http.request(method, route, json={**body, **guard})
                self.assertEqual((409,"SESSION_GENERATION_MISMATCH"), (response.status_code, response.json()["error"]["code"]))
        self.assertFalse(self.transport.calls)
        for rows, counts in [([{"id":1,"title":"x"}], (1,0,0)), ([{"id":1,"title":"x"},{"id":2,"title":"y"},{}], (1,1,1))]:
            payload = {"format":"json","content":json.dumps(rows)}
            data = self.http.post("/api/catalog/import", json=payload).json()["data"]
            self.assertEqual(counts, tuple(data[k] for k in ("added_count","existing_count","failed_count")))
            self.assertEqual(sum(counts[:2]), data["imported_count"])
        self.assertEqual(201, self.http.post("/api/feedback",json={"song_id":1,"action":"replay"}).status_code)

    def test_fm_trash_opt_in_serialization_and_cooldown(self):
        self.login()
        route = "/api/recommend/fm/trash"
        self.assertFalse(self.http.post(route,json={"song_id":1}).json()["data"]["synced"])
        self.assertFalse(self.transport.calls)
        self.http.put("/api/settings",json={"sync_fm_trash_to_netease":True}).raise_for_status()
        active = peak = 0
        lock = threading.Lock()
        def handler(*args):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak,active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"code":200}
        self.transport.handlers["/fm_trash"] = handler
        self.app.client.pacer.sleep_fn = lambda seconds: None
        with ThreadPoolExecutor(3) as pool:
            list(pool.map(self.app.client.fm_trash, (1,2,3)))
        self.assertEqual(1, peak)
        self.transport.handlers["/fm_trash"] = {"code":-447}
        result = self.http.post(route,json={"song_id":1}).json()["data"]
        self.assertEqual(("cooldown",False), (result["status"],result["synced"]))
        self.assertGreater(result["cooldown_sec"], 59)
        self.http.post(route,json={"song_id":1})
        with self.assertRaises(AppError):
            self.app.client.playlist_tracks_sample(10)
        self.assertEqual(4,len(self.transport.calls))

    def test_bounded_transport_sample_and_artist_id_migration(self):
        transport = NeteaseWebTransport("https://music.163.com")
        self.addCleanup(transport.close)
        client = NeteaseClient(transport)
        song = api_song(1)
        song["ar"] = [{"id":7, "name":"a"},{"id":0},{"id":True}]
        detail = TransportResponse(200,{"code":200,"playlist":{"tracks":[song],"trackIds":[{"id":i} for i in range(1,150)]}})
        fill = TransportResponse(200,{"code":200,"songs":[api_song(2)],"privileges":[{"id":2,"st":-1}]})
        with patch.object(UrllibTransport,"request",side_effect=[detail,fill]) as call, recommendation_reads(2):
            tracks = client.playlist_tracks_sample(10)
            self.assertEqual(["/api/v6/playlist/detail","/api/v3/song/detail"],[c.args[0] for c in call.call_args_list])
            self.assertEqual(59,len(json.loads(call.call_args_list[1].args[1]["c"])))
        self.assertEqual([7], tracks[0].artist_ids)
        self.assertEqual("unavailable", tracks[1].availability)
        with patch.object(UrllibTransport,"request",return_value=detail) as call, recommendation_reads(1):
            self.assertEqual([1],[s.id for s in client.playlist_tracks_sample(10)])
            self.assertEqual(1, call.call_count)
        with self.db._connect() as conn:
            conn.execute("ALTER TABLE account_songs DROP COLUMN artist_ids_json")
        db = CatalogDatabase(self.root)
        db.upsert_songs("42", tracks)
        self.assertEqual([7], CatalogDatabase(self.root).get_song(1,"42").artist_ids)

    def test_local_scan_safe_refresh_and_stable_pagination(self):
        readable, missing = self.root/"music", self.root/"offline"
        readable.mkdir()
        self.db.replace_local_files([{"path":str(folder/name),"title":name} for folder,name in
                                     [(readable,"stale"), (missing,"offline1"), (missing,"offline2")]])
        route = "/api/local-library"
        for dirs, status, count in [([], "empty_dirs", 3), ([str(missing)], "unreadable_dirs",3),
                                   ([str(readable),str(missing)],"partial_failure",2)]:
            data = self.http.post(route+"/scan",json={"dirs":dirs}).json()["data"]
            self.assertEqual((status,0,count), (data["status"],data["scanned_count"],data["total_indexed"]))
            self.assertIn("revision",data)
        page = self.http.get(route+"?q=offline&limit=1").json()["data"]
        self.assertEqual((True,2), (page["has_more"],page["total"]))
        tail = self.http.get(route+"?q=offline&limit=1&offset=1").json()["data"]
        self.assertEqual((False,1,"offline2"),(tail["has_more"],tail["offset"],tail["files"][0]["title"]))

    def test_durable_feedback_insights_and_dusty_exclusion(self):
        self.library(3)
        self.db.add_feedback("local",1,"not_interested")
        with self.db._connect() as conn:
            conn.execute("UPDATE explicit_feedback SET created_at='2000-01-01'")
        self.db.add_feedback("local",2,"replay")
        self.assertEqual([1], [r["song_id"] for r in self.db.list_feedback("local",action="not_interested")])
        gems = self.http.get("/api/recommend/dusty-gems?exclude_keys=netease:2").json()["data"]
        self.assertEqual([3],[s["id"] for s in gems["songs"]])
        self.assertEqual((2,0,True),(gems["total_candidates"],gems["remaining_count"],gems["exhausted"]))
        self.assertEqual([],self.http.get("/api/recommend/dusty-gems?exclude_ids=2,3").json()["data"]["songs"])
        self.db.record_play("local",{"id":2},listened_ms=30000)
        self.db.record_play("local",{"id":2},listened_ms=30000,completed=True)
        insights = self.http.get("/api/insights").json()["data"]
        self.assertEqual((2,1),(insights["total_plays"],insights["total_completed"]))


    def test_preview_sse_lifetime_without_native_window(self):
        route = "/api/desktop/lyrics-window"
        initial = self.http.get(route).json()["data"]

        async def connect_and_close(query, expected):
            scope = {"type":"http", "asgi":{"version":"3.0","spec_version":"2.3"},
                     "method":"GET", "path":"/api/events", "scheme":"http", "http_version":"1.1",
                     "query_string":query, "headers":[(b"host", b"127.0.0.1:8765")]}
            done = asyncio.Event()
            async def receive():
                await done.wait()
                return {"type":"http.disconnect"}
            async def send(message):
                if b"event: connected" in message.get("body", b""):
                    state = self.app.api("GET", route).payload["data"]
                    self.assertEqual(expected, state["preview_subscribers"])
                    self.assertEqual(initial.get("open"), state.get("open"))
                    done.set()
            await asyncio.wait_for(self.asgi(scope, receive, send), 3)
        observer = self.app._sync_bus.subscribe()
        try:
            asyncio.run(connect_and_close(b"lyrics_preview=1", 1))
            for expected in (1, 0):
                event = observer.get_nowait()
                self.assertEqual("desktop_lyrics", event["_event_type"])
                self.assertEqual(expected, event["preview_subscribers"])
                self.assertEqual(initial["open"], event["window"]["open"])
        finally:
            self.app._sync_bus.unsubscribe(observer)
        asyncio.run(connect_and_close(b"", 0))
        self.assertEqual(0,self.http.get(route).json()["data"]["preview_subscribers"])

    def test_hotkey_registration_uses_native_results_and_is_read_only(self):
        from pruner_studio.desktop import GlobalHotkeyDaemon, HOTKEY_BINDINGS
        from unittest.mock import Mock
        daemon = GlobalHotkeyDaemon(lambda _: None)
        user32 = Mock()
        user32.RegisterHotKey.side_effect = lambda _, ident, *args: ident == HOTKEY_BINDINGS[0][0]
        daemon._sync_bindings(user32)
        self.assertFalse(self.http.get("/api/settings").json()["data"]["hotkeys_supported"])
        self.app.user_settings.hotkey_status = daemon.registration_status
        with patch.object(daemon, "is_running", return_value=True):
            initial = self.http.get("/api/settings").json()["data"]
            self.assertTrue(initial["hotkeys_supported"])
            self.assertFalse(initial["sync_fm_trash_to_netease"])
            self.http.put("/api/settings", json={"sync_fm_trash_to_netease": True})
            self.assertTrue(self.http.get("/api/settings").json()["data"]["sync_fm_trash_to_netease"])
            saved = self.http.put("/api/settings", json={"hotkeys_supported":False,"hotkey_registration":{}}).json()["data"]
            self.assertTrue(saved["hotkeys_supported"])
            self.assertEqual({action: i==0 for i, (_, _, action) in enumerate(HOTKEY_BINDINGS)}, saved["hotkey_registration"])

    def test_native_discovery_routes_use_post_and_intelligence_forwards_start(self):
        transport = NeteaseWebTransport("https://music.163.com")
        self.addCleanup(transport.close)
        client = NeteaseClient(transport)
        paths = [("similar_playlists",(7,),"/api/discovery/simiPlaylist"),
                 ("recommend_playlists",(),"/api/v1/discovery/recommend/resource"),
                 ("artist_top_songs",(9,),"/api/artist/top/song"),
                 ("daily_history_dates",(),"/api/discovery/recommend/songs/history/recent"),
                 ("daily_history_songs",("2026-01-01",),"/api/discovery/recommend/songs/history/detail"),
                 ("fm_trash",(8,),"/api/radio/trash/add")]
        for method,args,path in paths:
            with patch.object(UrllibTransport,"request",return_value=TransportResponse(200,{"code":200})) as call:
                getattr(client, method)(*args)
                self.assertEqual(path,call.call_args.args[0])
                self.assertEqual("POST",call.call_args.kwargs["method"])
                if method == "fm_trash":
                    self.assertEqual({"songId":8}, call.call_args.args[1])
        self.login()
        self.transport.handlers["/playmode/intelligence/list"] = {"code":200}
        for name in ("sid", "start_music_id"):
            self.http.get(f"/api/recommend/intelligence?id=1&pid=2&count=7&{name}=3").raise_for_status()
            self.assertEqual({"id":1,"pid":2,"count":7,"sid":3},self.transport.calls[-1][1])

    def test_restart_replay_and_confirmed_offline_make_no_reads(self):
        self.login()
        self.library(8, "42", "liked")
        self.transport.handlers["/playmode/intelligence/list"] = {"code":200,"data":[api_song(20,"new")]}
        initial = self.radio("resume", limit=2)
        restored = build_application(Settings(self.root, self.root), self.transport)
        self.transport.calls.clear()
        route = "/api/recommend/smart-radio"
        options = {"radio_session_id":"session","request_id":"resume","expected_account_id":"42","limit":2}
        self.assertEqual(initial, restored.api("POST",route,options).payload["data"])
        self.assertFalse(self.transport.calls)
        def unavailable(*args):
            raise AppError("UPSTREAM_UNAVAILABLE", "offline", 502)
        self.transport.handlers["/user/account"] = unavailable
        self.assertTrue(restored.api("GET","/api/status").payload["data"]["offline"])
        self.transport.calls.clear()
        batch = restored.api("POST",route,{**options,"request_id":"offline"}).payload["data"]
        self.assertEqual(2,batch["new_count"])
        self.assertTrue({s["id"] for s in initial["songs"]}.isdisjoint(s["id"] for s in batch["songs"]))
        self.assertEqual([],self.transport.calls)

    def test_artist_top_songs_from_rotated_eligible_seed(self):
        self.login()
        self.library(3, "42", songs=[Song(i, str(i), [str(i)], artist_ids=[100+i]) for i in range(1,4)])
        self.db.add_feedback("42", 1, "not_interested")
        self.transport.handlers["/artist/top/song"] = {"code":200, "songs":[api_song(i,str(i)) for i in range(20,31)]}
        batch = self.radio("artist", exclude_ids=[1,2,3], limit=5)
        self.assertEqual(5, batch["breakdown"]["novel_count"])
        self.assertEqual(1, len(self.transport.calls))
        path, params, _, _ = self.transport.calls[0]
        self.assertEqual("/artist/top/song",path)
        self.assertIn(params["id"], (102,103))

    def test_local_block_by_key_is_durable_and_clear_is_scoped(self):
        files = [{"path": str(self.root/f"{t}.flac"), "title": t} for t in ("one","two")]
        self.db.replace_local_files(files)
        keys = ["local:"+item["path"] for item in files]
        for key in keys:
            self.http.post("/api/feedback",json={"song_id":0,"track_key":key,"action":"not_interested"}).raise_for_status()
        with self.db._connect() as conn:
            conn.execute("UPDATE explicit_feedback SET created_at='2000-01-01'")
        self.assertFalse(self.radio(1,allow_replay=True)["songs"])
        self.assertFalse(self.http.get("/api/recommend/dusty-gems").json()["data"]["songs"])
        self.http.request("DELETE","/api/feedback",json={"track_key":keys[0]}).raise_for_status()
        self.assertEqual([keys[0]],[t["track_key"] for t in self.radio(2)["songs"]])
        self.assertEqual(400,self.http.request("DELETE","/api/feedback",json={"track_key":"unknown"}).status_code)
        self.assertEqual({keys[1]},self.db.blocked_track_keys("local"))

    def test_discovery_continues_when_heartbeat_only_echoes_the_library(self):
        self.login()
        self.library(15, "42", "liked", [Song(i, str(i), ["A"], artist_ids=[7]) for i in range(1,16)])
        self.transport.handlers["/playmode/intelligence/list"] = {"code":200,"data":[api_song(i,str(i),"A") for i in range(1,16)]}
        self.transport.handlers["/artist/top/song"] = {"code":200,"songs":[api_song(i,str(i)) for i in range(20,40)]}
        data = self.radio("expand",limit=20)
        self.assertEqual(20, data["new_count"])
        self.assertGreaterEqual(data["breakdown"]["novel_count"], 8)
        self.assertEqual(["/playmode/intelligence/list","/artist/top/song"],[c[0] for c in self.transport.calls])
