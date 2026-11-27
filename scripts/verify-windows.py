"""Inspect the release PE and exercise its headless lifecycle in an isolated directory."""
import hashlib
import http.client
import json
import marshal
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pefile
from PyInstaller.archive.readers import CArchiveReader
from pruner_studio.db import CatalogDatabase
from pruner_studio.version import __version__


ROOT = Path(__file__).resolve().parents[1]
EXE = Path(os.environ.get("PRUNER_VERIFY_EXE") or (ROOT / "dist" / "Pruner-Studio.exe"))


def stop_headless(process, directory):
    if process.poll() is not None:
        return process.returncode
    try:
        (directory / ".stop-headless").touch()
        return process.wait(timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        raise


def audit():
    with pefile.PE(str(EXE)) as pe:
        versions = pe.FileInfo[0][0].StringTable[0].entries
        for key in (b"FileVersion", b"ProductVersion"):
            assert versions[key].decode() == __version__, (key, versions[key])
        manifest = next(r for r in pe.DIRECTORY_ENTRY_RESOURCE.entries if r.id == 24)
        data = manifest.directory.entries[0].directory.entries[0].data.struct
        assert b"PerMonitorV2" in pe.get_data(data.OffsetToData, data.Size)
        assert all(not s.Name.startswith(b"UPX") for s in pe.sections)
    archive = CArchiveReader(str(EXE))
    contents = {p.replace(chr(92), "/"): p for p in archive.toc}
    web = {f.relative_to(ROOT).as_posix(): f for f in (ROOT / "web").rglob("*") if f.is_file()}
    assert web.keys() == {p for p in contents if p.startswith("web/")}
    for path, file in web.items():
        assert archive.extract(contents[path]) == file.read_bytes(), path
    pyz = archive.open_embedded_archive(next(name for name in archive.toc if name.endswith(".pyz")))
    modules = [name for name in pyz.toc if name == "pruner_studio" or name.startswith("pruner_studio.")]
    assert {"pruner_studio.app", "pruner_studio.sidecar_runner"} <= set(modules)
    assert {"web/bootstrap.js", "web/session-worker.js"} <= web.keys()
    for name in modules:
        source = (ROOT / "src").joinpath(*name.split("."))
        source = source / "__init__.py" if source.is_dir() else source.with_suffix(".py")
        frozen = pyz.extract(name)
        assert compile(source.read_bytes(), frozen.co_filename, "exec", dont_inherit=True) == frozen, name
    entry = marshal.loads(archive.extract("pruner_studio_entry"))
    assert compile((ROOT / "scripts/pruner_studio_entry.py").read_bytes(), entry.co_filename,
                   "exec", dont_inherit=True) == entry, "launcher script"
    print(f"{len(modules)} application modules and launcher match compiled working-tree sources")
    binaries = 0
    for path, entry in archive.toc.items():
        if entry[-1] != "b":
            continue
        binary = archive.extract(path)
        if binary.startswith(b"MZ"):
            with pefile.PE(data=binary, fast_load=True) as embedded:
                assert all(not s.Name.startswith(b"UPX") for s in embedded.sections), path
                binaries += 1
    print(f"FileVersion=ProductVersion={__version__}, PerMonitorV2, no UPX sections "
          f"(launcher + {binaries} embedded PEs), {len(web)} identical web files")
    print("SHA256", hashlib.sha256(EXE.read_bytes()).hexdigest())


def smoke():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="pruner-release-") as folder:
        directory = Path(folder)
        env = dict(os.environ, PRUNER_STUDIO_NO_BROWSER="1", PRUNER_STUDIO_DATA_DIR=folder,
                   PRUNER_STUDIO_PORT="0", PRUNER_STUDIO_MANAGED_HEADLESS="1")
        process = subprocess.Popen([EXE], env=env)
        try:
            deadline = time.monotonic() + 60
            auth_headers = {}
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    instance = json.loads((directory / ".instance.json").read_text("utf-8"))
                    assert instance["mode"] == "headless"
                    base = f"http://127.0.0.1:{instance['port']}"
                    if instance.get("local_token"):
                        auth_headers = {"X-Pruner-Token": instance["local_token"]}
                    req = urllib.request.Request(base + "/api/status", headers=auth_headers)
                    with opener.open(req, timeout=2) as response:
                        status = json.load(response)
                        assert response.status == 200 and status["data"]["authenticated"] is False
                    break
                except (FileNotFoundError, json.JSONDecodeError, PermissionError, urllib.error.URLError, TimeoutError):
                    time.sleep(.15)
            else:
                raise RuntimeError("Packaged application did not become ready")
            assert auth_headers.get("X-Pruner-Token"), "Expected local_token in .instance.json"
            try:
                with opener.open(base + "/api/status", timeout=3) as response:
                    raise AssertionError(f"Unauthenticated /api/status unexpectedly returned {response.status}")
            except urllib.error.HTTPError as exc:
                with exc:
                    assert exc.code == 401, exc.code
                    unauth_body = json.loads(exc.read().decode("utf-8"))
                    assert unauth_body["ok"] is False and unauth_body["error"]["code"] == "LOCAL_AUTH_REQUIRED", unauth_body
            with opener.open(urllib.request.Request(base + "/api/accounts", headers=auth_headers), timeout=3) as response:
                assert response.status == 200 and json.load(response)["data"]["saved_accounts"] == []
            for endpoint in ("/api/settings", "/api/recommend/daily", "/api/lan-sync/status"):
                with opener.open(urllib.request.Request(base + endpoint, headers=auth_headers), timeout=5) as response:
                    assert response.status == 200 and json.load(response)["ok"]
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", 0))
                lan_port = probe.getsockname()[1]

            def post_json(endpoint, payload):
                req = urllib.request.Request(
                    base + endpoint,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", **auth_headers},
                    method="POST",
                )
                with opener.open(req, timeout=10) as response:
                    assert response.status == 200, (endpoint, response.status)
                    body = json.load(response)
                    assert body["ok"] is True, (endpoint, body)
                    return body["data"]

            sidecar = post_json("/api/sidecar/start", {})
            assert sidecar["running"] and sidecar["manifest"]["protocol_version"] == 2
            assert not post_json("/api/sidecar/stop", {})["running"]
            print("Frozen Sidecar start, authenticated /manifest and stop passed")
            with opener.open(base + "/", timeout=5) as page:
                assert "set-cookie" not in {k.lower() for k in page.headers}
                assert instance["local_token"].encode() not in page.read()
            lan_on = post_json("/api/lan-sync/toggle", {"enabled": True, "port": lan_port})
            assert lan_on["running"] is True and lan_on["tls"] is True and lan_on["port"] == lan_port
            fp = lan_on["cert_fingerprint"]
            assert len(fp) == 64 and all(ch in "0123456789abcdef" for ch in fp), fp
            spki = lan_on["spki_fingerprint"]
            assert len(spki) == 64 and all(ch in "0123456789abcdef" for ch in spki), spki
            pair = post_json("/api/lan-sync/pair/start", {})
            assert pair["spki_fingerprint"] == spki, (pair.get("spki_fingerprint"), spki)
            parsed_qr = urllib.parse.urlsplit(pair["qr_uri"])
            assert parsed_qr.scheme == "pruner-lan" and parsed_qr.netloc == "pair", pair["qr_uri"]
            qr_qs = urllib.parse.parse_qs(parsed_qr.query, keep_blank_values=False)
            assert qr_qs.get("v") == ["1"] and qr_qs.get("tls") == ["1"], qr_qs
            assert qr_qs.get("port") == [str(lan_port)] and qr_qs.get("code") == [pair["code"]], qr_qs
            assert qr_qs.get("fp") == [fp] and qr_qs.get("spki") == [spki], qr_qs
            assert len(pair["code"]) == 6 and pair["code"].isdigit()
            tls_ctx = ssl.create_default_context()
            tls_ctx.check_hostname = False
            tls_ctx.verify_mode = ssl.CERT_NONE
            tls_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            conn = http.client.HTTPSConnection("127.0.0.1", lan_port, context=tls_ctx, timeout=5)
            try:
                conn.connect()
                peer_fp = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
                assert peer_fp == fp, (peer_fp, fp)
                conn.request("GET", "/api/lan/v1/info")
                lan_resp = conn.getresponse()
                assert lan_resp.status == 200, lan_resp.status
                info_json = json.loads(lan_resp.read().decode("utf-8"))
                assert info_json["protocol_version"] == 1 and info_json["tls"] is True
                assert info_json["pairing_active"] is True and info_json["cert_fingerprint"] == fp
                conn.request("GET", "/api/lan/v1/info?v=2")
                lan_v2_resp = conn.getresponse()
                assert lan_v2_resp.status == 200, lan_v2_resp.status
                info_v2 = json.loads(lan_v2_resp.read().decode("utf-8"))
                assert info_v2["spki_fingerprint"] == spki and info_v2["pin_type"] == "spki-sha256", info_v2
            finally:
                conn.close()
            lan_off = post_json("/api/lan-sync/toggle", {"enabled": False})
            assert lan_off["running"] is False
            print(f"{base} /api/status=200 /api/accounts=200 /api/settings=200 /api/recommend/daily=200 /api/lan-sync/status=200")
            print(f"LAN TLS smoke ok on 127.0.0.1:{lan_port} (cert fp={fp[:16]}..., /api/lan/v1/info=200, pair/start=200)")
        finally:
            code = stop_headless(process, directory)
        assert code == 0, code
        print("Graceful process exit=0")


def smoke_restored_playback_session():
    """Verify frozen EXE cold-starts cleanly against a pre-populated playback_sessions SQLite DB."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="pruner-release-resume-") as folder:
        directory = Path(folder)
        db = CatalogDatabase(directory)
        restored_track = {
            "id": 1897964061,
            "title": "雨不停。流",
            "artists": ["孙盛希"],
            "album": "《无神之地不下雨》电视原声带",
            "duration": 265935,
            "provider": "netease",
            "source": "netease",
        }
        next_track = {
            "id": 1897964062,
            "title": "Grand Blue",
            "artists": ["湘南乃风"],
            "album": "湘南乃风",
            "duration": 313000,
            "provider": "netease",
            "source": "netease",
        }
        db.save_playback_session(
            "local",
            {
                "current_track": restored_track,
                "position_sec": 221.76,
                "duration_sec": 265.935,
                "queue": [restored_track, next_track],
                "queue_index": 0,
                "play_mode": "order",
                "volume": 0.42,
                "muted": False,
                "playback_rate": 1.0,
                "active_tab": "discover",
            },
        )
        del db

        env = dict(
            os.environ,
            PRUNER_STUDIO_NO_BROWSER="1",
            PRUNER_STUDIO_DATA_DIR=folder,
            PRUNER_STUDIO_PORT="0",
            PRUNER_STUDIO_MANAGED_HEADLESS="1",
        )
        process = subprocess.Popen([EXE], env=env)
        try:
            deadline = time.monotonic() + 60
            auth_headers = {}
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    instance = json.loads((directory / ".instance.json").read_text("utf-8"))
                    assert instance["mode"] == "headless"
                    base = f"http://127.0.0.1:{instance['port']}"
                    if instance.get("local_token"):
                        auth_headers = {"X-Pruner-Token": instance["local_token"]}
                    with opener.open(base + "/api/health", timeout=2) as response:
                        health = json.load(response)
                        assert response.status == 200 and health["ok"] is True
                    break
                except (FileNotFoundError, json.JSONDecodeError, PermissionError, urllib.error.URLError, TimeoutError):
                    time.sleep(0.15)
            else:
                raise RuntimeError("Packaged application did not become ready with pre-populated playback_sessions DB")

            with opener.open(urllib.request.Request(base + "/api/playback/session", headers=auth_headers), timeout=5) as response:
                assert response.status == 200, response.status
                body = json.load(response)
                assert body["ok"] is True, body
                session = body["data"]
                assert isinstance(session, dict), session
                assert session["current_track"]["id"] == 1897964061, session
                assert session["current_track"]["title"] == "雨不停。流", session
                assert abs(float(session["position_sec"]) - 221.76) < 1e-3, session
                assert abs(float(session["duration_sec"]) - 265.935) < 1e-3, session
                assert session["queue_index"] == 0 and len(session["queue"]) == 2, session
                assert abs(float(session["volume"]) - 0.42) < 1e-3, session

            with opener.open(base + "/js/player.js", timeout=5) as response:
                assert response.status == 200
                player_js = response.read().decode("utf-8")
                assert "unlockAudioContextSync" in player_js and "applyPendingResumeSeek" in player_js
                assert ".src = ''" not in player_js and '.src = ""' not in player_js

            with opener.open(base + "/css/base.css", timeout=5) as response:
                assert response.status == 200
                base_css = response.read().decode("utf-8")
                assert "#f3ede2" in base_css

            print(
                f"{base} cold-start playback_session restored ok "
                f"(track={session['current_track']['id']}, pos={session['position_sec']}s, queue={len(session['queue'])})"
            )
        finally:
            code = stop_headless(process, directory)
        assert code == 0, code
        print("Restored-session cold-start graceful exit=0")


if __name__ == "__main__":
    audit()
    smoke()
    smoke_restored_playback_session()
