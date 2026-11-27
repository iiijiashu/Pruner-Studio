"""Tests for enhanced RecommendService.dusty_gems() and GET /api/recommend/dusty-gems."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

from pruner_studio.app import build_application, create_asgi_app
from pruner_studio.config import Settings
from pruner_studio.db import CatalogDatabase
from pruner_studio.models import PlaylistSummary, Song
from pruner_studio.netease_client import NeteaseClient
from pruner_studio.recommend_service import (
    RecommendService,
    _dusty_memory_reason,
    _enrich_dusty_track,
)
from test_backend import FakeTransport


def _iso_days_ago(now_ts: float, days: float) -> str:
    dt = datetime.fromtimestamp(now_ts - days * 86_400.0, tz=timezone.utc)
    return dt.isoformat()


def _seed_play(
    db: CatalogDatabase,
    account_id: str,
    song_dict: dict,
    *,
    last_played_at: str,
    play_count: int = 1,
    completed_count: int = 0,
) -> None:
    db.record_play(account_id, song_dict, listened_ms=180_000, completed=completed_count > 0)
    tkey = song_dict.get("track_key") or f"netease:{song_dict['id']}"
    with db._connect() as conn:
        conn.execute(
            """
            UPDATE play_history
            SET play_count = ?, completed_count = ?, last_played_at = ?
            WHERE account_id = ? AND track_key = ?
            """,
            (play_count, completed_count, last_played_at, account_id, tkey),
        )


@pytest.fixture()
def dusty_env():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        transport = FakeTransport()
        app = build_application(Settings(root, root), transport)
        asgi = create_asgi_app(app, 8765)
        with TestClient(asgi, base_url="http://127.0.0.1:8765") as http:
            yield app, app.db, http, root


def test_memory_reason_templates_and_enrichment_edge_cases():
    now_ts = 1_800_000_000.0

    # Forgotten reason variants
    assert _dusty_memory_reason("forgotten", 186, 9, "夜晚歌单") == "186 天未听 · 曾播 9 次"
    assert _dusty_memory_reason("forgotten", 45, 0, "夜晚歌单") == "45 天未听"
    assert _dusty_memory_reason("forgotten", None, 4, "夜晚歌单") == "超过 30 天未听"

    # Unplayed reason variants
    assert _dusty_memory_reason("unplayed", None, 0, "我喜欢的音乐") == "尚未播放 · 来自「我喜欢的音乐」"
    assert _dusty_memory_reason("unplayed", None, 0, "") == "收藏后尚未播放"

    # Future timestamp clamps days_since_played >= 0
    future_iso = datetime.fromtimestamp(now_ts + 3 * 86_400.0, tz=timezone.utc).isoformat()
    entry, _ts = _enrich_dusty_track(
        {"id": 1, "title": "Future", "provider": "netease"},
        bucket="dusty",
        history_row={"play_count": 2, "completed_count": 1, "last_played_at": future_iso},
        now_ts=now_ts,
    )
    assert entry["days_since_played"] == 0
    assert entry["play_count"] == 2
    assert entry["completed_count"] == 1
    assert entry["last_played_at"] == future_iso
    assert entry["recommend_reason"] == "0 天未听 · 曾播 2 次"

    # Unparseable timestamp -> days_since_played is None
    bad_entry, bad_ts = _enrich_dusty_track(
        {"id": 2, "title": "BadTs", "provider": "netease"},
        bucket="dusty",
        history_row={"play_count": 3, "completed_count": 0, "last_played_at": "not-a-date"},
        now_ts=now_ts,
    )
    assert bad_ts is None
    assert bad_entry["days_since_played"] is None
    assert bad_entry["recommend_reason"] == "超过 30 天未听"

    # source_playlist_name fallbacks: local -> 本地曲库, cloud -> 网易云云盘, else ""
    local_entry, _ = _enrich_dusty_track(
        {"id": 0, "title": "LocalTrack", "provider": "local", "track_key": "local:/a.flac"},
        bucket="unplayed",
        history_row=None,
        now_ts=now_ts,
    )
    assert local_entry["source_playlist_name"] == "本地曲库"
    assert local_entry["recommend_reason"] == "尚未播放 · 来自「本地曲库」"

    cloud_entry, _ = _enrich_dusty_track(
        {"id": 3, "title": "CloudTrack", "provider": "netease", "is_cloud": True},
        bucket="unplayed",
        history_row=None,
        now_ts=now_ts,
    )
    assert cloud_entry["source_playlist_name"] == "网易云云盘"
    assert cloud_entry["recommend_reason"] == "尚未播放 · 来自「网易云云盘」"

    bare_entry, _ = _enrich_dusty_track(
        {"id": 4, "title": "BareTrack", "provider": "netease", "is_cloud": False},
        bucket="unplayed",
        history_row=None,
        now_ts=now_ts,
    )
    assert bare_entry["source_playlist_name"] == ""
    assert bare_entry["recommend_reason"] == "收藏后尚未播放"


def test_service_memory_fields_playlist_fallbacks_and_counts():
    now_ts = 1_795_200_000.0
    with tempfile.TemporaryDirectory() as tmp:
        db = CatalogDatabase(Path(tmp))
        liked_songs = [
            Song(1, "Old Classic", ["ArtistA", "GuestB"], album="Golden Era", duration=200_000, availability="available"),
            Song(2, "Deep Cut", ["ArtistC"], album="Late Night", duration=210_000, availability="available"),
            Song(3, "Fresh Track", ["ArtistD"], album="Recent", duration=190_000, availability="available"),
            Song(4, "Cooldown Track", ["ArtistE"], album="Today", duration=180_000, availability="available"),
            Song(5, "Blocked Track", ["ArtistF"], album="Nope", duration=180_000, availability="available"),
            Song(6, "Unavailable Grey", ["ArtistG"], album="Grey", duration=180_000, availability="unavailable"),
        ]
        cloud_songs = [
            Song(7, "Cloud Gem", ["CloudArtist"], album="CloudAlb", duration=220_000, availability="unavailable", is_cloud=True),
        ]
        db.replace_playlist_tracks("local", PlaylistSummary(10, "我喜欢的音乐", len(liked_songs), "liked", "public"), liked_songs)
        db.replace_playlist_tracks("local", PlaylistSummary(20, "", len(cloud_songs), "cloud", "private"), cloud_songs)
        db.replace_local_files(
            [
                {"path": "C:/music/forgotten_local.flac", "title": "Local Forgotten", "artist": "LocalArt", "album": "Tape", "duration_ms": 240_000},
                {"path": "C:/music/unplayed_local.flac", "title": "Local Unplayed", "artist": "LocalArt2", "album": "Tape2", "duration_ms": 230_000},
            ]
        )

        # Song 1: forgotten 120.4 days ago, played 9 times, completed 6
        ts_120 = _iso_days_ago(now_ts, 120.4)
        _seed_play(db, "local", liked_songs[0].to_dict(), last_played_at=ts_120, play_count=9, completed_count=6)
        # Song 3: fresh (15 days ago) -> excluded from dusty pool
        _seed_play(db, "local", liked_songs[2].to_dict(), last_played_at=_iso_days_ago(now_ts, 15.0), play_count=4, completed_count=4)
        # Song 4: cooldown (2 days ago) -> excluded from dusty pool
        _seed_play(db, "local", liked_songs[3].to_dict(), last_played_at=_iso_days_ago(now_ts, 2.0), play_count=10, completed_count=10)
        # Local forgotten: 65.2 days ago, played 3 times, completed 2
        ts_65 = _iso_days_ago(now_ts, 65.2)
        _seed_play(
            db,
            "local",
            {
                "id": 0,
                "track_key": "local:C:/music/forgotten_local.flac",
                "provider": "local",
                "local_path": "C:/music/forgotten_local.flac",
                "title": "Local Forgotten",
                "artists": ["LocalArt"],
            },
            last_played_at=ts_65,
            play_count=3,
            completed_count=2,
        )
        db.add_feedback("local", 5, "not_interested", "")

        service = RecommendService(NeteaseClient(FakeTransport()), db=db)
        res = service.dusty_gems("local", limit=30, now_ts=now_ts, seed=42)

        # Eligible dusty pool: Song 1 (forgotten), Song 2 (unplayed), Song 7 (cloud unplayed),
        # Local Forgotten (forgotten), Local Unplayed (unplayed) -> total 5
        assert res["source"] == "dusty_gems"
        assert res["degraded"] is False
        assert res["reason"] == ""
        assert res["total_candidates"] == 5
        assert res["filtered_total"] == 5
        assert res["offset"] == 0
        assert res["limit"] == 30
        assert res["remaining_count"] == 0
        assert res["has_more"] is False
        assert res["exhausted"] is True
        assert res["counts"] == {"all": 5, "forgotten": 2, "unplayed": 3}
        assert res["playlists"] == [
            {"name": "我喜欢的音乐", "count": 2},
            {"name": "本地曲库", "count": 2},
            {"name": "网易云云盘", "count": 1},
        ]

        by_key = {s["track_key"]: s for s in res["songs"]}
        assert set(by_key.keys()) == {
            "netease:1",
            "netease:2",
            "netease:7",
            "local:C:/music/forgotten_local.flac",
            "local:C:/music/unplayed_local.flac",
        }

        s1 = by_key["netease:1"]
        assert s1["dusty_kind"] == "forgotten"
        assert s1["days_since_played"] == 120
        assert s1["play_count"] == 9
        assert s1["completed_count"] == 6
        assert s1["last_played_at"] == ts_120
        assert s1["source_playlist_name"] == "我喜欢的音乐"
        assert s1["recommend_source"] == "dusty_gems"
        assert s1["recommend_reason"] == "120 天未听 · 曾播 9 次"

        s2 = by_key["netease:2"]
        assert s2["dusty_kind"] == "unplayed"
        assert s2["days_since_played"] is None
        assert s2["play_count"] == 0
        assert s2["completed_count"] == 0
        assert s2["last_played_at"] is None
        assert s2["source_playlist_name"] == "我喜欢的音乐"
        assert s2["recommend_reason"] == "尚未播放 · 来自「我喜欢的音乐」"

        s7 = by_key["netease:7"]
        assert s7["dusty_kind"] == "unplayed"
        assert s7["source_playlist_name"] == "网易云云盘"
        assert s7["recommend_reason"] == "尚未播放 · 来自「网易云云盘」"

        lf = by_key["local:C:/music/forgotten_local.flac"]
        assert lf["dusty_kind"] == "forgotten"
        assert lf["days_since_played"] == 65
        assert lf["play_count"] == 3
        assert lf["completed_count"] == 2
        assert lf["last_played_at"] == ts_65
        assert lf["source_playlist_name"] == "本地曲库"
        assert lf["recommend_reason"] == "65 天未听 · 曾播 3 次"

        lu = by_key["local:C:/music/unplayed_local.flac"]
        assert lu["dusty_kind"] == "unplayed"
        assert lu["source_playlist_name"] == "本地曲库"
        assert lu["recommend_reason"] == "尚未播放 · 来自「本地曲库」"


def test_http_filters_sorting_deterministic_pagination_and_validation(dusty_env):
    _app, db, http, _root = dusty_env
    now_ts = time.time()

    pl_a = [
        Song(101, "Midnight Rain", ["Taylor Swift", "Lana"], album="Midnights", duration=180_000, availability="available"),
        Song(102, "Sunset Drive", ["Kavinsky"], album="OutRun", duration=200_000, availability="available"),
        Song(103, "Starlight Echo", ["Muse"], album="Black Holes", duration=210_000, availability="available"),
        Song(104, "Quiet Harbor", ["Nils Frahm"], album="Spaces", duration=220_000, availability="available"),
    ]
    pl_b = [
        Song(201, "City Pop 1984", ["Tatsuro Yamashita"], album="Big Wave", duration=230_000, availability="available"),
        Song(202, "Rainy Tokyo", ["Mariya Takeuchi"], album="Variety", duration=240_000, availability="available"),
        Song(203, "Neon Avenue", ["Anri"], album="Timely", duration=215_000, availability="available"),
    ]
    db.replace_playlist_tracks("local", PlaylistSummary(1, "深夜驾驶", len(pl_a), "liked", "public"), pl_a)
    db.replace_playlist_tracks("local", PlaylistSummary(2, "日系都市", len(pl_b), "self_created", "public"), pl_b)

    # Seed forgotten plays:
    # 101: 45d ago, play=12, completed=5
    # 102: 200d ago, play=12, completed=9  (same play_count as 101, higher completed_count)
    # 103: 150d ago, play=12, completed=5  (same play_count & completed_count as 101, older days_since_played)
    # 201: 90d ago, play=20, completed=15
    # 202: 350d ago, play=4, completed=4
    # Unplayed: 104, 203
    _seed_play(db, "local", pl_a[0].to_dict(), last_played_at=_iso_days_ago(now_ts, 45.2), play_count=12, completed_count=5)
    _seed_play(db, "local", pl_a[1].to_dict(), last_played_at=_iso_days_ago(now_ts, 200.2), play_count=12, completed_count=9)
    _seed_play(db, "local", pl_a[2].to_dict(), last_played_at=_iso_days_ago(now_ts, 150.2), play_count=12, completed_count=5)
    _seed_play(db, "local", pl_b[0].to_dict(), last_played_at=_iso_days_ago(now_ts, 90.2), play_count=20, completed_count=15)
    _seed_play(db, "local", pl_b[1].to_dict(), last_played_at=_iso_days_ago(now_ts, 350.2), play_count=4, completed_count=4)

    # 1. kind filter keeps full-pool counts & playlists intact
    r_forgotten = http.get("/api/recommend/dusty-gems?kind=forgotten&sort=oldest").json()["data"]
    assert r_forgotten["total_candidates"] == 7
    assert r_forgotten["counts"] == {"all": 7, "forgotten": 5, "unplayed": 2}
    assert r_forgotten["playlists"] == [{"name": "深夜驾驶", "count": 4}, {"name": "日系都市", "count": 3}]
    assert r_forgotten["filtered_total"] == 5
    assert [s["id"] for s in r_forgotten["songs"]] == [202, 102, 103, 201, 101]
    assert all(s["dusty_kind"] == "forgotten" for s in r_forgotten["songs"])

    r_unplayed = http.get("/api/recommend/dusty-gems?kind=unplayed").json()["data"]
    assert r_unplayed["filtered_total"] == 2
    assert {s["id"] for s in r_unplayed["songs"]} == {104, 203}
    assert all(s["dusty_kind"] == "unplayed" for s in r_unplayed["songs"])
    assert r_unplayed["counts"] == {"all": 7, "forgotten": 5, "unplayed": 2}

    # 2. sort=oldest puts forgotten (days desc) before unplayed
    r_oldest = http.get("/api/recommend/dusty-gems?sort=oldest").json()["data"]
    oldest_ids = [s["id"] for s in r_oldest["songs"]]
    assert oldest_ids[:5] == [202, 102, 103, 201, 101]
    assert set(oldest_ids[5:]) == {104, 203}

    # 3. sort=most_played orders by play_count desc, completed_count desc, days_since_played desc
    r_most = http.get("/api/recommend/dusty-gems?sort=most_played").json()["data"]
    most_ids = [s["id"] for s in r_most["songs"]]
    assert most_ids[:5] == [201, 102, 103, 101, 202]
    assert set(most_ids[5:]) == {104, 203}

    # 4. Exact playlist filter (trimmed; partial name does NOT match)
    qs_exact = urlencode({"playlist": "  日系都市  ", "sort": "oldest"})
    r_pl = http.get(f"/api/recommend/dusty-gems?{qs_exact}").json()["data"]
    assert r_pl["filtered_total"] == 3
    assert [s["id"] for s in r_pl["songs"]] == [202, 201, 203]

    qs_partial = urlencode({"playlist": "日系"})
    r_pl_partial = http.get(f"/api/recommend/dusty-gems?{qs_partial}").json()["data"]
    assert r_pl_partial["filtered_total"] == 0
    assert r_pl_partial["songs"] == []
    assert r_pl_partial["exhausted"] is True
    assert r_pl_partial["has_more"] is False

    # 5. Case-insensitive q substring filter across title, joined artists, album, source_playlist_name
    assert {s["id"] for s in http.get("/api/recommend/dusty-gems?q=RAIN").json()["data"]["songs"]} == {101, 202}
    assert [s["id"] for s in http.get("/api/recommend/dusty-gems?q=taylor%20swift%20lana").json()["data"]["songs"]] == [101]
    assert [s["id"] for s in http.get("/api/recommend/dusty-gems?q=big%20wave").json()["data"]["songs"]] == [201]
    qs_q_pl = urlencode({"q": "深夜"})
    assert {s["id"] for s in http.get(f"/api/recommend/dusty-gems?{qs_q_pl}").json()["data"]["songs"]} == {101, 102, 103, 104}

    # 6. Deterministic seeded pagination with sort=balanced (including large JS Date.now() seed & seed=0)
    for test_seed in (0, 1764221063123):
        p1 = http.get(f"/api/recommend/dusty-gems?sort=balanced&seed={test_seed}&limit=3&offset=0").json()["data"]
        p2 = http.get(f"/api/recommend/dusty-gems?sort=balanced&seed={test_seed}&limit=3&offset=3").json()["data"]
        p3 = http.get(f"/api/recommend/dusty-gems?sort=balanced&seed={test_seed}&limit=3&offset=6").json()["data"]
        p4 = http.get(f"/api/recommend/dusty-gems?sort=balanced&seed={test_seed}&limit=3&offset=20").json()["data"]

        assert (p1["offset"], p1["limit"], p1["filtered_total"], p1["remaining_count"], p1["has_more"], p1["exhausted"]) == (
            0, 3, 7, 4, True, False
        )
        assert (p2["offset"], p2["limit"], p2["remaining_count"], p2["has_more"], p2["exhausted"]) == (
            3, 3, 1, True, False
        )
        assert (p3["offset"], p3["limit"], p3["remaining_count"], p3["has_more"], p3["exhausted"]) == (
            6, 3, 0, False, True
        )
        assert (p4["songs"], p4["remaining_count"], p4["has_more"], p4["exhausted"]) == ([], 0, False, True)

        combined_ids = [s["id"] for s in (*p1["songs"], *p2["songs"], *p3["songs"])]
        assert len(combined_ids) == 7
        assert set(combined_ids) == {101, 102, 103, 104, 201, 202, 203}

        # Re-requesting page 1 with the same seed yields the exact same order
        p1_again = http.get(f"/api/recommend/dusty-gems?sort=balanced&seed={test_seed}&limit=3&offset=0").json()["data"]
        assert [s["id"] for s in p1_again["songs"]] == [s["id"] for s in p1["songs"]]

    # 7. Exclude ids & exclude_key combined with filters
    r_ex = http.get("/api/recommend/dusty-gems?kind=forgotten&exclude_ids=101&exclude_key=netease:202&sort=oldest").json()["data"]
    assert r_ex["total_candidates"] == 7
    assert r_ex["filtered_total"] == 3
    assert [s["id"] for s in r_ex["songs"]] == [102, 103, 201]

    # 8. Invalid non-integer offset/seed returns 400; negative offset clamps to 0
    assert http.get("/api/recommend/dusty-gems?offset=abc").status_code == 400
    assert http.get("/api/recommend/dusty-gems?seed=notint").status_code == 400
    r_neg = http.get("/api/recommend/dusty-gems?offset=-10&limit=2").json()["data"]
    assert r_neg["offset"] == 0
    assert len(r_neg["songs"]) == 2


def test_playlists_capped_at_24_sorted_by_count_desc_then_name_asc(dusty_env):
    _app, db, http, _root = dusty_env
    # Create 28 playlists with varying counts and ties to verify tie-breaking & cap at 24
    song_id = 1
    for idx in range(28):
        name = f"PL-{idx:02d}"
        track_count = 3 if idx < 5 else (2 if idx < 15 else 1)
        songs = [
            Song(song_id + j, f"Song {song_id + j}", [f"Art {idx}"], duration=180_000, availability="available")
            for j in range(track_count)
        ]
        song_id += track_count
        db.replace_playlist_tracks("local", PlaylistSummary(100 + idx, name, track_count, "self_created", "public"), songs)

    data = http.get("/api/recommend/dusty-gems?limit=10").json()["data"]
    playlists = data["playlists"]
    assert len(playlists) == 24
    assert playlists[:5] == [{"name": f"PL-{i:02d}", "count": 3} for i in range(5)]
    assert playlists[5:15] == [{"name": f"PL-{i:02d}", "count": 2} for i in range(5, 15)]
    assert playlists[15:] == [{"name": f"PL-{i:02d}", "count": 1} for i in range(15, 24)]


def test_dusty_gems_performance_and_paging_on_2200_tracks():
    now_ts = 1_800_000_000.0
    with tempfile.TemporaryDirectory() as tmp:
        db = CatalogDatabase(Path(tmp))
        # 2,000 Netease catalog tracks across 4 playlists + 200 local files = 2,200 tracks
        playlist_specs = [
            (1, "红心精选", "liked", 800),
            (2, "夜间工作", "self_created", 500),
            (3, "通勤轻音乐", "self_created", 400),
            (4, "周末发呆", "self_created", 300),
        ]
        sid = 1
        play_rows = []
        for pid, pname, pkind, count in playlist_specs:
            songs = []
            for _ in range(count):
                songs.append(
                    Song(
                        sid,
                        f"Melody {sid}",
                        [f"Artist {sid % 120}"],
                        album=f"Album {sid % 200}",
                        duration=200_000,
                        availability="available",
                    )
                )
                if sid % 3 == 0:
                    days = 35 + (sid % 400)
                    played_at = (
                        datetime.fromtimestamp(now_ts, tz=timezone.utc) - timedelta(days=days)
                    ).isoformat()
                    play_rows.append(
                        (
                            "local",
                            f"netease:{sid}",
                            "netease",
                            str(sid),
                            "",
                            sid,
                            f"Melody {sid}",
                            f'["Artist {sid % 120}"]',
                            f"Album {sid % 200}",
                            200_000,
                            "",
                            1 + (sid % 25),
                            sid % 10,
                            200_000 * (1 + (sid % 25)),
                            played_at,
                        )
                    )
                sid += 1
            db.replace_playlist_tracks("local", PlaylistSummary(pid, pname, count, pkind, "public"), songs)

        with db._connect() as conn:
            conn.executemany(
                """
                INSERT INTO play_history (
                    account_id, track_key, provider, external_id, local_path,
                    song_id, title, artists_json, album, duration, cover_url,
                    play_count, completed_count, total_listen_ms, last_played_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                play_rows,
            )

        db.replace_local_files(
            [
                {"path": f"D:/local/track_{i:04d}.flac", "title": f"Local {i}", "artist": f"LocArt {i % 30}", "duration_ms": 180_000}
                for i in range(200)
            ]
        )

        service = RecommendService(NeteaseClient(FakeTransport()), db=db)

        t0 = time.perf_counter()
        p1 = service.dusty_gems("local", limit=48, offset=0, sort="balanced", seed=2026, now_ts=now_ts)
        p2 = service.dusty_gems("local", limit=48, offset=48, sort="balanced", seed=2026, now_ts=now_ts)
        oldest = service.dusty_gems("local", limit=48, offset=0, kind="forgotten", sort="oldest", now_ts=now_ts)
        most = service.dusty_gems("local", limit=48, offset=0, playlist="红心精选", q="Melody 1", sort="most_played", now_ts=now_ts)
        elapsed = time.perf_counter() - t0

        assert elapsed < 3.0, f"dusty_gems on 2,200 tracks too slow: {elapsed:.3f}s"
        assert p1["total_candidates"] == 2200
        assert p1["counts"]["all"] == 2200
        assert p1["counts"]["forgotten"] == len(play_rows)
        assert p1["counts"]["unplayed"] == 2200 - len(play_rows)
        assert len(p1["songs"]) == 48
        assert len(p2["songs"]) == 48
        assert {s["track_key"] for s in p1["songs"]}.isdisjoint({s["track_key"] for s in p2["songs"]})
        assert p1["remaining_count"] == 2200 - 48
        assert p1["has_more"] is True
        first_8_kinds = {s["dusty_kind"] for s in p1["songs"][:8]}
        assert first_8_kinds == {"forgotten", "unplayed"}

        # Oldest ordering strictly non-increasing by days_since_played
        days_seq = [s["days_since_played"] for s in oldest["songs"]]
        assert all(d is not None for d in days_seq)
        assert days_seq == sorted(days_seq, reverse=True)

        # Most played ordering strictly non-increasing by (play_count, completed_count, days_since_played)
        most_keys = [
            (s["play_count"], s["completed_count"], s["days_since_played"] if s["days_since_played"] is not None else -1)
            for s in most["songs"]
        ]
        assert most_keys == sorted(most_keys, reverse=True)


def test_balanced_sort_interleaves_forgotten_and_unplayed_with_artist_spread():
    now_ts = 1_800_000_000.0
    with tempfile.TemporaryDirectory() as tmp:
        db = CatalogDatabase(Path(tmp))
        # 560 forgotten + 548 unplayed = 1,108 tracks sharing the same 40 artists
        # (reproduces the exact multi-track-per-artist pool where tier-0 unplayed used to starve tier-1 forgotten).
        total_tracks = 1108
        forgotten_total = 560
        songs = [
            Song(
                sid,
                f"Track {sid}",
                [f"SharedArtist {sid % 40}"],
                album=f"Alb {sid % 80}",
                duration=200_000,
                availability="available",
            )
            for sid in range(1, total_tracks + 1)
        ]
        db.replace_playlist_tracks("local", PlaylistSummary(1, "红心歌单", total_tracks, "liked", "public"), songs)

        play_rows = []
        for sid in range(1, forgotten_total + 1):
            played_at = _iso_days_ago(now_ts, 45 + (sid % 375))
            play_rows.append(
                (
                    "local",
                    f"netease:{sid}",
                    "netease",
                    str(sid),
                    "",
                    sid,
                    f"Track {sid}",
                    f'["SharedArtist {sid % 40}"]',
                    f"Alb {sid % 80}",
                    200_000,
                    "",
                    2 + (sid % 20),
                    1 + (sid % 10),
                    200_000 * (2 + (sid % 20)),
                    played_at,
                )
            )
        with db._connect() as conn:
            conn.executemany(
                """
                INSERT INTO play_history (
                    account_id, track_key, provider, external_id, local_path,
                    song_id, title, artists_json, album, duration, cover_url,
                    play_count, completed_count, total_listen_ms, last_played_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                play_rows,
            )

        service = RecommendService(NeteaseClient(FakeTransport()), db=db)

        # Discover showcase slice (limit=8, unseeded & seeded): both kinds must appear in first 8
        showcase_unseeded = service.dusty_gems("local", limit=8, sort="balanced", now_ts=now_ts)
        unseeded_kinds = [s["dusty_kind"] for s in showcase_unseeded["songs"]]
        assert "forgotten" in unseeded_kinds and "unplayed" in unseeded_kinds
        assert unseeded_kinds.count("forgotten") >= 2
        assert unseeded_kinds.count("unplayed") >= 2

        # Explorer page 1 & page 2 (limit=48, deterministic seed): proportional mix + artist spread + zero overlap
        p1 = service.dusty_gems("local", limit=48, offset=0, sort="balanced", seed=202605, now_ts=now_ts)
        p2 = service.dusty_gems("local", limit=48, offset=48, sort="balanced", seed=202605, now_ts=now_ts)
        p1_again = service.dusty_gems("local", limit=48, offset=0, sort="balanced", seed=202605, now_ts=now_ts)

        assert [s["id"] for s in p1["songs"]] == [s["id"] for s in p1_again["songs"]]
        assert {s["id"] for s in p1["songs"]}.isdisjoint({s["id"] for s in p2["songs"]})

        p1_first_8_kinds = [s["dusty_kind"] for s in p1["songs"][:8]]
        assert p1_first_8_kinds.count("forgotten") >= 3
        assert p1_first_8_kinds.count("unplayed") >= 3

        p1_forgotten_count = sum(1 for s in p1["songs"] if s["dusty_kind"] == "forgotten")
        assert 18 <= p1_forgotten_count <= 30

        # Zero consecutive same primary artist across the combined 96-track span
        span = [*p1["songs"], *p2["songs"]]
        for prev_song, cur_song in zip(span, span[1:]):
            assert prev_song["artists"][0] != cur_song["artists"][0]

