import hashlib
import json
import ssl
import sqlite3
from urllib.parse import parse_qs, urlsplit
import socket
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from pruner_studio.app import build_application
from pruner_studio.config import Settings
from pruner_studio import lan_sync
from pruner_studio.models import PlaylistSummary, Song
from test_backend import FakeTransport

BASE = "/api/lan/v1/"


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=tmp_path, web_dir=Path("web"), api_base="http://fake.invalid", port=0)
    with patch.object(socket.socket, "bind", side_effect=AssertionError("build bound a socket")):
        application = build_application(settings, FakeTransport())
    yield application
    application.lan_sync.stop()


def trust(app):
    return ssl.create_default_context(cafile=str(app.settings.data_dir / "lan_tls_cert.pem"))


@pytest.fixture
def client(app):
    port = app.lan_sync.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
    with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=trust(app), trust_env=False) as client:
        yield client


def claim(app, client):
    pair = app.lan_sync.start_pairing()
    response = client.post(BASE+"pair/claim", json=dict(code=pair["code"], device_name="Phone", platform="android"))
    assert response.status_code == 200
    return response.json()


def auth(device):
    return {"Authorization": "Bearer "+device["token"]}


def test_pairing_lifecycle(app, client, tmp_path):
    mgr = app.lan_sync
    response = client.get(BASE+"info")
    info = response.json()
    peer = response.extensions["network_stream"].get_extra_info("ssl_object")
    peer_pin = hashlib.sha256(peer.getpeercert(True)).hexdigest()
    assert set(info) == {"protocol_version", "device_name", "pairing_active", "tls", "cert_fingerprint"}
    pair = mgr.start_pairing()
    assert len(pair["code"]) == 6 and pair["code"].isdigit()
    params = parse_qs(urlsplit(pair["qr_uri"]).query)
    with socket.create_connection(("127.0.0.1", mgr.bound_port)) as tcp:
        with trust(app).wrap_socket(tcp, server_hostname="127.0.0.1") as sock:
            fingerprint = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
    assert peer_pin == fingerprint and params["tls"] == ["1"] and params["fp"] == [fingerprint]
    assert mgr.server.config.ssl.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert info["tls"] and info["cert_fingerprint"] == mgr.status()["cert_fingerprint"] == fingerprint
    with httpx.Client(trust_env=False, timeout=2) as plain, pytest.raises(httpx.TransportError):
        plain.get(f"http://127.0.0.1:{mgr.bound_port}" + BASE + "info")
    assert "code="+pair["code"] in pair["qr_uri"]
    assert len(pair["qr_rows"]) >= 21
    assert all(len(row) == len(pair["qr_rows"]) and set(row) <= {"0", "1"} for row in pair["qr_rows"])
    args = dict(code="invalid", device_name="Phone", platform="ios")
    for expected in (400, 400, 400, 400, 429):
        assert client.post(BASE+"pair/claim", json=args).status_code == expected
    args["code"] = pair["code"]
    assert client.post(BASE+"pair/claim", json=args).status_code == 429
    mgr.start_pairing()["expires_at"] = time.time()-1
    assert client.post(BASE+"pair/claim", json=args).status_code == 410
    device = claim(app, client)
    assert client.post(BASE+"pair/claim", json=args).status_code == 400
    saved = (app.settings.data_dir/"lan_sync.json").read_text(encoding="utf-8")
    assert device["token"] not in saved and "token_salt" in saved and "token_hash" in saved
    header = auth(device)
    audio = tmp_path/"tls.mp3"
    audio.write_bytes(b"012345")
    app.db.replace_local_files([{"path": str(audio)}])
    pushed = client.post(BASE+"sync/push", headers=header, json={"feedback": [{"song_id": 42, "action": "replay"}]})
    assert pushed.json()["accepted_feedback"] == 1
    pulled = client.post(BASE+"sync/pull", headers=header, json={"include_local": True}).json()
    assert pulled["feedback"][0]["song_id"] == 42
    ranged = client.get(pulled["local_library"][0]["stream_url"], headers={**header, "Range": "bytes=2-4"})
    assert (ranged.status_code, ranged.content) == (206, b"234")
    for headers in ({}, {"Authorization": "Bearer "+"A"*43}):
        assert client.get(BASE+"status", headers=headers).status_code == 401
    assert client.get(BASE+"status", headers={**header, "Origin": "https://example.org"}).status_code == 403
    assert client.options(BASE+"info").status_code == 405
    assert client.post(BASE+"sync/pull", content=b"x"*1048577,
                       headers={**header, "Content-Type": "application/json"}).status_code == 413
    status = client.get(BASE+"status", headers=header)
    assert status.headers["cache-control"] == "no-store"
    assert status.headers["x-content-type-options"] == "nosniff"
    assert "token" not in status.text
    for path in ("/api/operations/pending", "/api/desktop/lyrics", "/api/lan-sync/status"):
        assert client.get(path, headers=header).status_code == 404
    app.lan_sync.revoke(device["device_id"])
    assert client.get(BASE+"status", headers=header).status_code == 401


def test_sync_and_handoff(app, client, tmp_path):
    header = auth(claim(app, client))
    app.client.set_cookie("MUSIC_U=private_fixture")
    path = str(tmp_path/"private"/"album.flac")
    app.db.replace_local_files([dict(path=path, title="Song")])
    app.db.replace_playlist_tracks("local", PlaylistSummary(3, "Music", 1, "imported", ""), [Song(9, "Song", [])])
    event = dict(song_id=9, event_id="phone-1", listened_ms=30000)
    payload = dict(current_track={"id": 9, "credential": "sensitive_fixture"}, updated_at="2026-05-01T00:00:00Z")
    def push(**body):
        response = client.post(BASE+"sync/push", headers=header, json=body)
        assert response.status_code == 200, response.text
        return response.json()
    assert push(play_events=[event], playback_session=payload,
                feedback=[{"action": "not_interested", "song_id": 9}])["session_updated"]
    assert push(play_events=[event])["accepted_events"] == 0
    assert not push(playback_session={**payload, "updated_at": "2025-01-01T00:00:00Z"})["session_updated"]
    assert push(playback_session={**payload, "position_sec": 35, "updated_at": "2026-06-01T00:00:00Z"})["session_updated"]
    assert push(playback_session=payload, force_session=True)["session_updated"]
    local = dict(provider="local", track_key="local:"+path, local_path=path, external_id=path, title="Indexed")
    app.db.save_playback_session("local", {"current_track": local, "queue": [local]})
    result = client.post(BASE+"sync/pull", headers=header, json=dict(include_catalog=True, include_local=True)).json()
    assert result["play_history"][0]["play_count"] == 1
    assert result["feedback"][0]["song_id"] == 9
    rule = {"id": "a", "name": "Rule", "enabled": True, "action": "prune", "match_mode": "all", "match_clauses": [
        {"field": "title", "operator": "equals", "value": "Song"}]}
    push(rules=[rule])
    synced_rules = client.post(BASE+"sync/pull", headers=header, json={}).json()["rules"]
    assert synced_rules[0]["id"] == "a" and "fixture_secret" not in json.dumps(synced_rules)
    invalid = {"rules": [{"id": "x"}], "feedback": [{"song_id": 123, "action": "replay"}],
               "play_events": [{**event, "song_id": 123, "event_id": "rolled-back"}]}
    assert client.post(BASE+"sync/push", headers=header, json=invalid).status_code == 400
    assert all(r["song_id"] != 123 for r in app.db.list_feedback("local") + app.db.list_play_history("local"))
    assert client.post(BASE+"handoff", headers=header, json={"track": {"id": -7}}).status_code == 400
    assert client.post(BASE+"sync/push", headers=header, json={"account_id": "other"}).status_code == 409
    assert result["playlists"][0]["id"] == 3 and result["songs"][0]["id"] == 9
    fid = result["local_library"][0]["file_id"]
    assert result["playback_session"]["current_track"]["file_id"] == fid
    for secret in (str(tmp_path), "private_fixture", "sensitive_fixture", "local_path", "album.flac"):
        assert secret not in json.dumps(result)
    with patch.object(app, "publish_sse", wraps=app.publish_sse) as publish:
        response = client.post(BASE+"handoff", headers=header, json=dict(track=result["playback_session"]["current_track"],
                                                                         position_sec=34, is_playing=True))
        assert response.status_code == 200, response.text
        handoff = response.json()
        assert client.get(BASE+"handoff", headers=header).json() == handoff
        publish.assert_any_call("lan_handoff", handoff)
    assert app.api("GET", "/api/lan-sync/handoff").payload["data"]["pending_desktop_handoff"]["track"]["local_path"] == path


def test_library_ranges(app, client, tmp_path):
    header = auth(claim(app, client))
    path = tmp_path/"sample.mp3"
    path.write_bytes(b"0123456789")
    app.db.replace_local_files([dict(path=str(path), title="sample")])
    files = client.get(BASE+"local-library?q=amp&limit=1", headers=header).json()["files"]
    assert len(files) == 1 and str(path) not in json.dumps(files)
    url = files[0]["stream_url"]
    assert client.get(url, headers=header).content == b"0123456789"
    r = client.get(url, headers={**header, "Range": "bytes=2-5"})
    assert (r.status_code, r.content, r.headers["accept-ranges"]) == (206, b"2345", "bytes")
    assert client.get(url, headers={**header, "Range": "bytes=22-"}).status_code == 416
    head = client.head(url, headers=header)
    assert (head.status_code, head.content, head.headers["content-length"]) == (200, b"", "10")
    assert client.get(BASE+"local-library/stream/%2e%2e%2fprivate", headers=header).status_code == 404
    path.unlink()
    missing = client.get(url, headers=header)
    assert missing.status_code == 404 and missing.json()["error"] == "FILE_UNKNOWN"
    app.db.replace_local_files([])
    assert client.get(url, headers=header).status_code == 404


def test_live_listener_and_revocation(app):
    mgr = app.lan_sync
    assert mgr.bound_port is None
    port = mgr.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
    with httpx.Client(base_url=f"https://127.0.0.1:{port}", verify=trust(app), trust_env=False, timeout=4) as client:
        device = claim(app, client)
        with client.stream("GET", BASE+"events", headers=auth(device)) as stream:
            lines = stream.iter_lines()
            assert next(lines) == "event: connected"
            assert app.api("PUT", "/api/playback/session", {"current_track": {"id": 11}, "is_playing": True}).status == 200
            assert next(line for line in lines if line.startswith("event:")) == "event: sync_available"
            fallback = client.get(BASE+"handoff", headers=auth(device)).json()
            assert fallback["track"]["id"] == 11 and fallback["is_playing"]
            response = app.api("POST", "/api/lan-sync/handoff/push", {"track": {"id": 12}, "position_sec": 7})
            assert response.status == 200
            assert next(line for line in lines if line.startswith("event:")) == "event: handoff"
            delivered = json.loads(next(lines).removeprefix("data: "))
            assert delivered["handoff_id"] == response.payload["data"]["handoff_id"]
            mgr.revoke(device["device_id"])
            assert "event: device_revoked" in list(lines)
        assert client.get(BASE+"status", headers=auth(device)).status_code == 401
    mgr.stop()
    assert mgr.bound_port is None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_discovery_and_busy_port(app, monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    monkeypatch.setattr(lan_sync, "LAN_DISCOVERY_UDP_PORT", port)
    manager = app.lan_sync
    tcp = manager.start(host="127.0.0.1", port=0)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as peer:
        peer.settimeout(.2)
        for _ in range(10):
            peer.sendto(b"PRUNER_DISCOVER_V1", ("127.0.0.1", port))
            try:
                message, _ = peer.recvfrom(1024)
                break
            except socket.timeout:
                pass
        else:
            pytest.fail("Discovery responder did not answer")
    assert json.loads(message) == {"type": "pruner_announce", "v": 1, "device_name": manager.config["device_name"],
                                   "port": tcp, "pairing_active": False, "tls": True,
                                   "cert_fingerprint": manager.status()["cert_fingerprint"]}
    manager.stop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
        busy.bind(("0.0.0.0", port))
        tcp = manager.start(host="127.0.0.1", port=0)
        with httpx.Client(verify=trust(app), trust_env=False) as client:
            assert client.get(f"https://127.0.0.1:{tcp}"+BASE+"info").status_code == 200


def test_desktop_settings_lifecycle(app):
    manager = app.lan_sync
    manager.enable_udp_discovery = False
    assert app.api("GET", "/api/lan-sync/status").payload["data"]["enabled"] is False
    assert app.api("POST", "/api/lan-sync/toggle", {"port": 65536}).status == 400
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    settings = {"enabled": True, "port": port, "device_name": "Desktop"}
    assert app.api("POST", "/api/lan-sync/toggle", settings).status == 200
    assert manager.bound_port == port
    assert app.api("POST", "/api/lan-sync/pair/start", {}).status == 200
    assert app.api("POST", "/api/lan-sync/pair/cancel", {}).status == 200
    pin = manager.status()["cert_fingerprint"]
    manager.stop()
    with patch.object(socket.socket, "bind", side_effect=AssertionError("unexpected bind")):
        reopened = build_application(app.settings, FakeTransport())
    try:
        assert reopened.lan_sync.config["enabled"]
        reopened.lan_sync.enable_udp_discovery = False
        reopened.lan_sync.start_if_enabled()
        assert reopened.lan_sync.bound_port == port
        assert reopened.lan_sync.status()["cert_fingerprint"] == pin
        reopened.lan_sync.toggle(enabled=False)
        assert reopened.lan_sync.bound_port is None
    finally:
        reopened.lan_sync.stop()



def test_incremental_pages(app, client):
    header = auth(claim(app, client))
    for sid in range(1, 102):
        app.db.record_play("local", {"id": sid}, completed=True)
    with app.db.write_batch() as conn:
        conn.execute("UPDATE play_history SET last_played_at=(SELECT MIN(last_played_at) FROM play_history)")
    def pull(**params):
        response = client.post(BASE+"sync/pull", headers=header, json=params)
        assert response.status_code == 200
        return response.json()
    first = pull(history_limit=100)
    assert len(first["play_history"]) == 100 and first["history_has_more"]
    assert pull(since=first["sync_until"])["play_history"] == []
    # An intervening write belongs to the next sync window.
    app.db.record_play("local", {"id": 102}, completed=True)
    second = pull(until=first["sync_until"], history_cursor=first["history_next_cursor"])
    assert len(second["play_history"]) == 1 and not second["history_has_more"]
    assert len({r["track_key"] for r in first["play_history"] + second["play_history"]}) == 101
    next_window = pull(since=first["sync_until"])
    assert [r["song_id"] for r in next_window["play_history"]] == [102]
    assert pull(since=next_window["sync_until"])["play_history"] == []


def test_push_validation_and_commit_order(app, client):
    header = auth(claim(app, client))
    rule = dict(id="r", name="original", enabled=False, action="prune", match_mode="any",
                match_clauses=[dict(field="title", operator="equals", value="Song")])
    original = app.rules.put([rule])
    event = dict(song_id=7, event_id="p", completed=True)
    fb = dict(song_id=7, action="replay")
    def push(**body):
        return client.post(BASE+"sync/push", headers=header, json=body)
    for invalid in ({"id": "r", "name": "overwrite"}, {**rule, "match_clauses": []},
                    {**rule, "enabled": 1}, {**rule, "action": "invalid"},
                    {**rule, "match_clauses": [dict(field="duration", op="contains", value="5")]}):
        with patch.object(app.db, "write_batch", side_effect=AssertionError("premature transaction")):
            r = push(play_events=[event], feedback=[fb], rules=[invalid])
        assert (r.status_code, r.json()["error"]) == (400, "INVALID_INPUT")
        assert app.rules.get() == original
    update = {**rule, "name": "changed"}
    with patch.object(app.db, "write_batch", side_effect=AssertionError("premature transaction")):
        r = push(play_events=[event], feedback=[fb, {**fb, "action": "invalid"}], rules=[update])
        assert r.status_code == 400
        assert push(play_events=[event], playback_session={"volume": 2}).status_code == 400
    with patch.object(app.db, "add_feedback", side_effect=sqlite3.OperationalError("failed")):
        assert push(play_events=[event], feedback=[fb], rules=[update]).status_code == 500
    assert app.rules.get() == original and not app.db.list_play_history("local") and not app.db.list_feedback("local")
    with app.db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM play_events").fetchone()[0] == 0
    with patch.object(app.rules, "put", side_effect=OSError("disk full")):
        failure = push(feedback=[fb], rules=[update])
        assert failure.status_code == 500 and failure.json()["error"] == "RULE_SAVE_FAILED_AFTER_DB_COMMIT"
    assert app.rules.get() == original and len(app.db.list_feedback("local")) == 1
    assert push(feedback=[fb], rules=[update]).json()["accepted_feedback"] == 0
    assert app.rules.get()[0]["name"] == "changed"
    assert len(app.db.list_feedback("local")) == 1
    assert push(feedback=[{**fb, "event_id": "unique"}]).json()["accepted_feedback"] == 1
    assert push(feedback=[{**fb, "event_id": "unique"}]).json()["accepted_feedback"] == 0
    first = client.post(BASE+"sync/pull", headers=header, json={"feedback_limit": 1}).json()
    assert first["feedback_has_more"] and first["feedback"][0]["client_event_id"]
    second = client.post(BASE+"sync/pull", headers=header, json={
        "until": first["sync_until"], "feedback_cursor": first["feedback_next_cursor"]}).json()
    assert not second["feedback_has_more"] and second["feedback"][0]["client_event_id"] == "unique"


def test_handoff_consumption_and_freshness(app, client):
    header = auth(claim(app, client))
    explicit = client.post(BASE+"handoff", headers=header, json={"track": {"id": 5}}).json()
    endpoint = "/api/lan-sync/"
    assert app.api("GET", endpoint+"status").payload["data"]["pending_desktop_handoff"] == explicit
    assert app.api("POST", endpoint+"handoff/ack", {"handoff_id": explicit["handoff_id"]}).status == 200
    for route in ("status", "handoff"):
        assert app.api("GET", endpoint+route).payload["data"]["pending_desktop_handoff"] is None
    stored = json.loads((app.settings.data_dir/"lan_sync.json").read_text("utf8"))
    assert stored["latest_handoff"]["consumed_by"] == ["desktop"]
    assert client.get(BASE+"handoff", headers=header).json()["explicit_handoff"]
    assert client.post(BASE+"handoff/ack", headers=header, json={"handoff_id": explicit["handoff_id"]}).status_code == 200
    assert client.get(BASE+"handoff", headers=header).json() is None
    client.post(BASE+"handoff", headers=header, json={"track": {"id": 5}})
    app.db.save_playback_session("local", {"current_track": {"id": 9}}, updated_at="2090-01-01T01:00:00+08:00")
    live = client.get(BASE+"handoff", headers=header).json()
    assert not live["explicit_handoff"] and live["source"] == "playback_session" and live["track"]["id"] == 9


def test_single_fd_before_headers(tmp_path):
    import asyncio
    path = tmp_path/"sample.mp3"
    path.write_bytes(b"abcdef")
    opened = []
    def capture(*args, **kwargs):
        h = open(*args, **kwargs)
        opened.append(h)
        return h
    with patch.object(lan_sync, "open", capture, create=True):
        response = lan_sync.stream_indexed_file(path, "audio/mpeg", "bytes=1-3", head_only=False)
        assert len(opened) == 1 and not opened[0].closed
        async def read():
            return b"".join([chunk async for chunk in response.body_iterator])
        assert asyncio.run(read()) == b"bcd"
        assert len(opened) == 1 and opened[0].closed


def test_local_history_cursor_is_private(app, client, tmp_path):
    header = auth(claim(app, client))
    private = str(tmp_path/"private-track.mp3")
    app.db.record_play("local", {"provider": "local", "local_path": private}, completed=True)
    app.db.record_play("local", {"id": 10}, completed=True)
    first = client.post(BASE+"sync/pull", headers=header, json={"history_limit": 1}).json()
    assert first["history_has_more"] and "private-track" not in json.dumps(first)
    second = client.post(BASE+"sync/pull", headers=header, json={
        "history_cursor": first["history_next_cursor"], "until": first["sync_until"]}).json()
    assert [row["song_id"] for row in second["play_history"]] == [10]


def test_adjacent_sync_windows_disjoint_on_exact_boundary(app, client):
    from datetime import datetime, timedelta, timezone

    header = auth(claim(app, client))
    base = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
    boundary = base.isoformat(timespec="microseconds")
    for sid in (41, 42):
        app.db.record_play("local", {"id": sid}, completed=True)
        app.db.add_feedback("local", sid, "replay", client_event_id=f"fb-{sid}")
    with app.db.write_batch() as conn:
        conn.execute("UPDATE play_history SET last_played_at=? WHERE song_id IN (41, 42)", (boundary,))
        conn.execute("UPDATE explicit_feedback SET created_at=? WHERE song_id IN (41, 42)", (boundary,))
    # Whole-second cursor pagination must advance cleanly from 41 -> 42 -> done (no infinite loop on .000000).
    p1 = client.post(BASE+"sync/pull", headers=header, json={
        "until": boundary, "history_limit": 1, "feedback_limit": 1}).json()
    assert p1["sync_until"] == boundary
    assert [r["song_id"] for r in p1["play_history"]] == [41] and p1["history_has_more"]
    assert [r["song_id"] for r in p1["feedback"]] == [41] and p1["feedback_has_more"]
    p2 = client.post(BASE+"sync/pull", headers=header, json={
        "until": p1["sync_until"],
        "history_limit": 1,
        "feedback_limit": 1,
        "history_cursor": p1["history_next_cursor"],
        "feedback_cursor": p1["feedback_next_cursor"],
    }).json()
    assert [r["song_id"] for r in p2["play_history"]] == [42] and not p2["history_has_more"]
    assert [r["song_id"] for r in p2["feedback"]] == [42] and not p2["feedback_has_more"]
    # Adjacent window starting at exact sync_until returns neither boundary item.
    w2 = client.post(BASE+"sync/pull", headers=header, json={"since": p1["sync_until"]}).json()
    assert w2["play_history"] == []
    assert w2["feedback"] == []
    # Strictly newer records still arrive in the subsequent window.
    later = (base + timedelta(microseconds=1)).isoformat(timespec="microseconds")
    app.db.record_play("local", {"id": 43}, completed=True)
    app.db.add_feedback("local", 43, "replay", client_event_id="fb-43")
    with app.db.write_batch() as conn:
        conn.execute("UPDATE play_history SET last_played_at=? WHERE song_id=43", (later,))
        conn.execute("UPDATE explicit_feedback SET created_at=? WHERE song_id=43", (later,))
    w3 = client.post(BASE+"sync/pull", headers=header, json={"since": p1["sync_until"]}).json()
    assert [r["song_id"] for r in w3["play_history"]] == [43]
    assert [r["song_id"] for r in w3["feedback"]] == [43]


def test_tls_identity_updates_on_ip_change_across_restart_and_live_pairing(app):
    import hashlib
    import ssl
    from urllib.parse import parse_qs, urlsplit

    cert_file = app.settings.data_dir / "lan_tls_cert.pem"

    def verify_tls_handshake(connect_host, port, server_hostname, expected_fp):
        ctx = ssl.create_default_context(cafile=str(cert_file))
        ctx.check_hostname = True
        with socket.create_connection((connect_host, port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname=server_hostname) as tls_sock:
                der = tls_sock.getpeercert(binary_form=True)
                assert hashlib.sha256(der).hexdigest() == expected_fp

    mgr = app.lan_sync
    try:
        # 1. Initial start on 127.0.0.1
        with patch.object(lan_sync, "discover_lan_ipv4_addresses", return_value=["127.0.0.1"]):
            port1 = mgr.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
            pair1 = mgr.start_pairing()
        q1 = parse_qs(urlsplit(pair1["qr_uri"]).query)
        assert q1["host"] == ["127.0.0.1"]
        verify_tls_handshake("127.0.0.1", port1, "127.0.0.1", q1["fp"][0])
        mgr.stop()

        # 2. Restart with same data_dir after desktop IP changed to 127.0.0.2
        with patch.object(lan_sync, "discover_lan_ipv4_addresses", return_value=["127.0.0.2"]):
            port2 = mgr.start(host="127.0.0.2", port=0, enable_udp_discovery=False)
            pair2 = mgr.start_pairing()
        q2 = parse_qs(urlsplit(pair2["qr_uri"]).query)
        assert q2["host"] == ["127.0.0.2"]
        assert q2["fp"][0] == mgr.status()["cert_fingerprint"] != q1["fp"][0]
        verify_tls_handshake("127.0.0.2", port2, "127.0.0.2", q2["fp"][0])

        # 3. Live IP change while server is still running -> start_pairing refreshes live TLS context
        with patch.object(lan_sync, "discover_lan_ipv4_addresses", return_value=["127.0.0.3"]):
            pair3 = mgr.start_pairing()
        q3 = parse_qs(urlsplit(pair3["qr_uri"]).query)
        assert q3["host"] == ["127.0.0.3"]
        assert q3["fp"][0] == mgr.status()["cert_fingerprint"] != q2["fp"][0]
        verify_tls_handshake("127.0.0.2", port2, "127.0.0.3", q3["fp"][0])
    finally:
        mgr.stop()


def test_revoked_at_stream_admission_gets_complete_401(app, client, tmp_path):
    import asyncio

    device = claim(app, client)
    manager = app.lan_sync
    audio = tmp_path / "admission.mp3"
    audio.write_bytes(b"ID3fixture")

    def register(cancel):
        manager.revoke(device["device_id"])
        return manager.register_stream(device["device_id"], cancel)

    response = lan_sync.stream_indexed_file(audio, "audio/mpeg", None,
                                            head_only=False, register_stream=register)
    sent = []

    async def send(message):
        await asyncio.sleep(0)
        sent.append(message)

    async def receive():
        return {"type": "http.disconnect"}

    asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    assert sent[0]["status"] == 401
    assert json.loads(sent[1]["body"]) == {"error": "LAN_UNAUTHORIZED"}
    assert not sent[1].get("more_body")
    assert manager.active_streams == {}
    audio.unlink()


def test_revoke_aborts_in_flight_audio_stream_without_affecting_other_device(app, tmp_path):
    import http.client
    import ssl

    audio = tmp_path / "long-stream.mp3"
    total_bytes = 8 * 1024 * 1024
    audio.write_bytes(b"ID3" + (b"\x55" * (total_bytes - 3)))
    app.db.replace_local_files([dict(path=str(audio), title="Long Stream", artist="Tester", duration_ms=240000)])
    fid = lan_sync.file_id(str(audio))

    mgr = app.lan_sync
    conn_a = conn_b = conn_retry = None
    try:
        port = mgr.start(host="127.0.0.1", port=0, enable_udp_discovery=False)
        pair_a = mgr.start_pairing()
        dev_a = mgr.claim_pairing(pair_a["code"], "Phone A", "android")
        pair_b = mgr.start_pairing()
        dev_b = mgr.claim_pairing(pair_b["code"], "Phone B", "android")

        ctx = ssl.create_default_context(cafile=str(app.settings.data_dir / "lan_tls_cert.pem"))
        ctx.check_hostname = True

        conn_a = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
        conn_a.request("GET", f"{BASE}local-library/stream/{fid}", headers={"Authorization": f"Bearer {dev_a['token']}"})
        resp_a = conn_a.getresponse()
        assert resp_a.status == 200
        assert int(resp_a.getheader("Content-Length")) == total_bytes
        first_a = resp_a.read(4096)
        assert len(first_a) == 4096 and first_a.startswith(b"ID3")

        conn_b = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
        conn_b.request("GET", f"{BASE}local-library/stream/{fid}", headers={"Authorization": f"Bearer {dev_b['token']}"})
        resp_b = conn_b.getresponse()
        assert resp_b.status == 200
        first_b = resp_b.read(4096)
        assert len(first_b) == 4096

        assert len(mgr.active_streams) == 2

        # Revoke Device A while its audio stream is actively open in-flight.
        mgr.revoke(dev_a["device_id"])

        received_a = len(first_a)
        try:
            while True:
                chunk = resp_a.read(65536)
                if not chunk:
                    break
                received_a += len(chunk)
        except http.client.IncompleteRead as exc:
            received_a += len(exc.partial)
        except (ConnectionError, ssl.SSLEOFError):
            pass
        assert received_a < total_bytes, f"Revoked stream delivered full {received_a}/{total_bytes} bytes"

        # Device B remains authorized and completes its full 8 MiB download cleanly.
        received_b = len(first_b)
        while True:
            chunk = resp_b.read(65536)
            if not chunk:
                break
            received_b += len(chunk)
        assert received_b == total_bytes

        # Subsequent request from revoked Device A is rejected with 401.
        conn_retry = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5)
        conn_retry.request("GET", f"{BASE}local-library/stream/{fid}", headers={"Authorization": f"Bearer {dev_a['token']}"})
        assert conn_retry.getresponse().status == 401

        deadline = time.monotonic() + 3.0
        while mgr.active_streams and time.monotonic() < deadline:
            time.sleep(0.02)
        assert mgr.active_streams == {}
        # Verify server closed the file handle cleanly (unlink succeeds on Windows).
        audio.unlink()
        assert not audio.exists()
    finally:
        for c in (conn_a, conn_b, conn_retry):
            if c is not None:
                c.close()
        mgr.stop()
