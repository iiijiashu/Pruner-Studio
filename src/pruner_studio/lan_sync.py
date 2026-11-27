"""Opt-in LAN protocol. The desktop ASGI router is never mounted here."""
import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import threading
import time
from urllib.parse import parse_qs, quote, unquote, urlsplit

from anyio import CancelScope
from Crypto.Cipher import AES
import qrcode
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.concurrency import run_in_threadpool

from .db import VALID_FEEDBACK_ACTIONS
from .errors import AppError
from .lan_tls import (
    certificate_fingerprint,
    certificate_spki_fingerprint,
    certificate_spki_pin,
    load_identity,
)

LAN_PROTOCOL_VERSION = 1
DEFAULT_LAN_SYNC_PORT = 18765
LAN_DISCOVERY_UDP_PORT = 38765
PLAY_MODES = ("order", "loop", "single", "shuffle", "fm", "intelligence", "smart_radio")
PREFIX = "/api/lan/v1/"
BODY_LIMIT = 1024 * 1024
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(v) for v in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def discover_lan_ipv4_addresses():
    try:
        ips = {ipaddress.IPv4Address(info[4][0]) for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)}
    except OSError:
        return []
    return [str(ip) for ip in sorted(ips, key=lambda ip: (not any(ip in n for n in PRIVATE_NETWORKS), ip))
            if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)]


def file_id(path):
    normalized = os.path.normcase(os.path.abspath(os.path.normpath(path)))
    return hashlib.sha256(os.fsencode(normalized)).hexdigest()[:24]


def digest(salt, token):
    return hashlib.sha256((salt + token).encode()).hexdigest()


def fields(row, names):
    """Project only scalar protocol fields, excluding paths and credential-shaped text."""
    result = {}
    for key in names.split():
        value = row.get(key)
        if isinstance(value, str):
            if re.search(r"[A-Za-z]:[/\\]|\\\\|^/|file:|MUSIC_U\s*=|[\x00-\x1f]", value, re.I):
                continue
            result[key] = value[:512]
        elif type(value) in (int, bool) or type(value) is float and math.isfinite(value):
            result[key] = value
    return result


_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NETEASE_IMAGE_PATH_RE = re.compile(r"^/[\w./%-]+$", re.ASCII)
_NETEASE_PARAM_QUERY_RE = re.compile(r"^param=[1-9][0-9]{1,3}y[1-9][0-9]{1,3}$", re.ASCII)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_PROVIDER_RE = re.compile(r"^[a-z0-9_.-]{1,48}(?::[a-z0-9_.-]{1,48})?$")
_COMPOSITE_PROVIDER_PREFIXES = frozenset({"gdstudio", "resolver", "resolver_v2"})
_RESERVED_SINGLE_PROVIDERS = frozenset({"local", "netease"})


def public_image(value):
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 1024 or any(ord(ch) <= 0x20 for ch in cleaned) or _CONTROL_CHAR_RE.search(cleaned):
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        return ""
    try:
        if parsed.port not in {None, 443}:
            return ""
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if not host or len(host) > 253 or host.endswith(".") or ".." in host:
        return ""
    labels = host.split(".")
    if not all(_DNS_LABEL_RE.match(part) for part in labels):
        return ""
    is_direct_netease = (
        (host.endswith(".126.net") and len(labels) >= 3)
        or (host.endswith(".music.163.com") and len(labels) >= 4)
    )
    if not is_direct_netease:
        return ""
    path = parsed.path
    if not _NETEASE_IMAGE_PATH_RE.fullmatch(path):
        return ""
    decoded_path = unquote(path)
    if (
        ".." in path.split("/")
        or ".." in decoded_path.split("/")
        or "\\" in decoded_path
        or _CONTROL_CHAR_RE.search(decoded_path)
    ):
        return ""
    if parsed.query and not _NETEASE_PARAM_QUERY_RE.fullmatch(parsed.query):
        return ""
    return f"https://{host}{path}" + (f"?{parsed.query}" if parsed.query else "")


def _lan_cover_url(raw_cover):
    raw = str(raw_cover or "").strip()
    if not raw:
        return ""
    if raw.startswith("/api/local-cover/"):
        cid = unquote(urlsplit(raw).path[len("/api/local-cover/"):]).strip()
        return f"{PREFIX}cover?local_cover_id={quote(cid, safe='')}" if re.fullmatch(r"[0-9a-f]{24}", cid) else ""
    if raw.startswith("/api/opensubsonic/cover/"):
        aid = unquote(urlsplit(raw).path[len("/api/opensubsonic/cover/"):]).strip()
        if aid and len(aid) <= 256 and not _CONTROL_CHAR_RE.search(aid):
            return f"{PREFIX}cover?opensubsonic_cover_id={quote(aid, safe='')}"
        return ""
    if raw.startswith(f"{PREFIX}cover?"):
        qs = parse_qs(urlsplit(raw).query, keep_blank_values=False)
        if "local_cover_id" in qs:
            cid = qs["local_cover_id"][0].strip()
            return f"{PREFIX}cover?local_cover_id={quote(cid, safe='')}" if re.fullmatch(r"[0-9a-f]{24}", cid) else ""
        if "opensubsonic_cover_id" in qs:
            aid = qs["opensubsonic_cover_id"][0].strip()
            if aid and len(aid) <= 256 and not _CONTROL_CHAR_RE.search(aid):
                return f"{PREFIX}cover?opensubsonic_cover_id={quote(aid, safe='')}"
            return ""
        if "url" in qs:
            inner = qs["url"][0].strip()
            if not inner.lower().startswith("https://"):
                return ""
            raw = inner
        else:
            return ""
    direct = public_image(raw)
    if direct:
        return direct
    from .app import validate_cover_url
    try:
        validated = validate_cover_url(raw, 384, allow_external=True)
    except AppError:
        return ""
    return f"{PREFIX}cover?url={quote(validated, safe='')}"


def track(row):
    data = fields(row, "id song_id title artist album provider external_id track_key duration duration_ms "
                       "format bitrate sample_rate isrc mbid "
                       "source_playlist_id play_count completed_count total_listen_ms last_played_at")
    artists = row.get("artists", [])
    if isinstance(artists, list):
        data["artists"] = [safe["name"] for v in artists[:20] if (safe := fields({"name": v}, "name")) and isinstance(v, str)]
    raw_cover = str(row.get("cover_url") or "").strip()
    if not raw_cover and row.get("cover_id"):
        cid = str(row.get("cover_id") or "").strip()
        if re.fullmatch(r"[0-9a-f]{24}", cid):
            raw_cover = f"/api/local-cover/{cid}"
    data["cover_url"] = _lan_cover_url(raw_cover)
    key = str(row.get("track_key") or "")
    if row.get("local_path") or row.get("provider") == "local" or key.startswith("local:"):
        path = row.get("local_path") or key.removeprefix("local:")
        fid = row.get("file_id") or (key[6:] if key.startswith("local:") else None)
        if not isinstance(fid, str) or not re.fullmatch("[a-f0-9]{24}", fid):
            fid = file_id(path)
        data.update(id=0, song_id=0, provider="local", external_id=fid, track_key="local:"+fid,
                    file_id=fid, stream_url=PREFIX+"local-library/stream/"+fid)
    return data


def checked_track(row):
    if not isinstance(row, dict):
        raise ValueError("Invalid track")
    data = track(row)
    if row.get("provider") not in (None, "") and data.get("provider") != row["provider"]:
        raise ValueError("Invalid track provider")
    for key in ("duration", "duration_ms"):
        if key in row and (type(row[key]) not in (int, float) or not 0 <= row[key] < 2**63):
            raise ValueError("Invalid duration")
    for id_key in ("id", "song_id"):
        if id_key in row and type(row[id_key]) is bool:
            raise ValueError("Invalid track identity")
    raw_key = row.get("track_key")
    raw_ext = row.get("external_id")
    key = str(data.get("track_key") or "")
    provider = data.get("provider")
    if key:
        from .track_identity import parse_track_key
        key_provider, _ = parse_track_key(key)
        if not key_provider and (provider in _COMPOSITE_PROVIDER_PREFIXES or not provider):
            raise ValueError("Invalid track identity")
        if not provider or (provider in _COMPOSITE_PROVIDER_PREFIXES
                            and key_provider.startswith(provider + ":")):
            provider = key_provider
    elif not provider:
        provider = "netease"
    if not isinstance(provider, str) or not _PROVIDER_RE.fullmatch(provider):
        raise ValueError("Invalid track provider")
    if ":" in provider:
        head, tail = provider.split(":", 1)
        if head in _RESERVED_SINGLE_PROVIDERS or head in {".", ".."} or tail in {".", ".."}:
            raise ValueError("Invalid track provider")
    elif provider in {".", ".."}:
        raise ValueError("Invalid track provider")
    if provider == "local":
        return data
    if raw_key not in (None, ""):
        if not isinstance(raw_key, str) or data.get("track_key") != raw_key:
            raise ValueError("Invalid track identity")
    if raw_ext not in (None, ""):
        if (not isinstance(raw_ext, str) and type(raw_ext) is not int) or data.get("external_id") != raw_ext:
            raise ValueError("Invalid track identity")
    prefix = provider + ":"
    key_identity = key[len(prefix):] if key.startswith(prefix) else ""
    fallback_id = ""
    for id_key in ("song_id", "id"):
        val = data.get(id_key)
        if type(val) is int and val > 0:
            fallback_id = str(val)
            break
        if isinstance(val, str) and val:
            fallback_id = val
            break
    identity = str(data.get("external_id") if raw_ext not in (None, "") else (key_identity or fallback_id))
    if (
        not identity
        or identity != identity.strip()
        or _CONTROL_CHAR_RE.search(identity)
        or len(identity) > 256
        or (key and key != prefix + identity)
    ):
        raise ValueError("Invalid track identity")
    if provider == "netease":
        if not re.fullmatch("[0-9]{1,19}", identity) or not 0 < int(identity) < 2**63:
            raise ValueError("Invalid NetEase song ID")
        if any(str(row[k]) != identity for k in ("song_id", "id") if k in row):
            raise ValueError("Inconsistent song identity")
        data.update(id=int(identity), song_id=int(identity))
    else:
        data["song_id"] = 0
    data.update(provider=provider, external_id=identity, track_key=prefix+identity)
    return data


def session(row, project_track=track):
    if row is None:
        return None
    data = fields(row, "position_sec duration_sec is_playing queue_index play_mode volume muted playback_rate active_tab updated_at")
    current = row.get("current_track", row.get("track"))
    data["current_track"] = project_track(current) if current is not None else None
    queue = row.get("queue", [])
    if not isinstance(queue, list) or len(queue) > 2000:
        raise ValueError("Invalid queue")
    data["queue"] = [project_track(t) for t in queue]
    return data


def rule(row):
    return {"action": row.get("action", "prune"), "match_mode": row.get("match_mode", "all"),
            **fields(row, "id name enabled output_playlist_name"),
            "match_clauses": [fields(c, "field operator value") for c in row.get("match_clauses", [])]}


def checked_rule(row):
    required = {"id", "name", "enabled", "action", "match_mode", "match_clauses"}
    if (not required <= row.keys() <= required | {"output_playlist_name"}
            or not all(isinstance(row[k], str) and row[k].strip() for k in ("id", "name"))
            or type(row["enabled"]) is not bool or row["action"] != "prune"
            or row["match_mode"] not in ("all", "any")
            or not isinstance(row.get("output_playlist_name", ""), str)):
        raise ValueError("Invalid or incomplete rule")
    clauses = row["match_clauses"]
    if not isinstance(clauses, list) or not 1 <= len(clauses) <= 20:
        raise ValueError("Invalid clauses")
    normalized = []
    for clause in clauses:
        if not isinstance(clause, dict) or set(clause) not in ({"field", "operator", "value"}, {"field", "op", "value"}):
            raise ValueError("Invalid clause structure")
        normalized.append({"field": clause["field"], "operator": clause.get("op", clause.get("operator")),
                           "value": clause["value"]})
    return {**row, "match_clauses": normalized}


def device_view(row):
    return {key: row.get(key) for key in ("device_id", "device_name", "platform", "paired_at", "last_seen_at", "last_sync_at")}


def label(value):
    if not isinstance(value, str) or not 0 < len(value.strip()) <= 100 or not fields({"label": value}, "label"):
        raise ValueError("Invalid device label")
    return value.strip()


def valid_port(value):
    if type(value) is not int or not 1024 <= value <= 65535:
        raise ValueError("Invalid LAN port")
    return value


class LanDeviceStore:
    def __init__(self, store):
        self.store = store
        defaults = dict(enabled=False, port=DEFAULT_LAN_SYNC_PORT, device_name=socket.gethostname() or
                        "Pruner Studio Desktop", devices={}, latest_handoff=None)
        self.data = {**defaults, **store.read("lan_sync.json", {})}
        valid_port(self.data["port"])

    def update(self, **changes):
        updated = {**self.data, **changes}
        self.store.write("lan_sync.json", updated)
        self.data = updated

    def update_device(self, device):
        self.update(devices={**self.data["devices"], device["device_id"]: device})


class LanSyncManager:
    def __init__(self, application, store, *, enable_udp_discovery=True):
        self.application = application
        self.store = LanDeviceStore(store)
        self.lock = threading.RLock()
        self.lifecycle = threading.RLock()
        self.enable_udp_discovery = enable_udp_discovery
        self.server = self.thread = self.udp_thread = self.bound_port = None
        self.stop_udp = threading.Event()
        self.pair = None
        self.attempts = self.locked_until = 0
        self.listeners = {}
        self.active_streams = {}
        self.proxy_streams = {}
        self.playing_session = None
        try:
            cert_path = application.settings.data_dir / "lan_tls_cert.pem"
            self.fingerprint = certificate_fingerprint(cert_path)
            self.spki_fingerprint = certificate_spki_fingerprint(cert_path)
            self.spki_pin = certificate_spki_pin(cert_path)
        except (OSError, ValueError):
            self.fingerprint = ""
            self.spki_fingerprint = ""
            self.spki_pin = ""

    @property
    def config(self):
        return self.store.data

    def active(self):
        return bool(self.pair and self.pair["expires_at"] > time.time())

    def info(self):
        return dict(protocol_version=1, device_name=self.config["device_name"], pairing_active=self.active(),
                    tls=True, cert_fingerprint=self.fingerprint)

    def status(self):
        with self.lock:
            return {**self.info(),
                    "spki_fingerprint": self.spki_fingerprint,
                    "spki_pin": self.spki_pin,
                    "pin_type": "spki-sha256",
                    "spki_fingerprint_display": ":".join(
                        self.spki_fingerprint[i:i+2].upper() for i in range(0, len(self.spki_fingerprint), 2)),
                    "cert_fingerprint_display": ":".join(
                        self.fingerprint[i:i+2].upper() for i in range(0, len(self.fingerprint), 2)),
                    "pending_desktop_handoff": self.pending_desktop(self.application._active_account_id_best_effort()),
                    "enabled": self.config["enabled"], "running": bool(self.bound_port),
                    "port": self.bound_port or self.config["port"], "addresses": discover_lan_ipv4_addresses(),
                    "devices": [device_view(d) for d in self.config["devices"].values()],
                    "pairing": self.pair if self.active() else None}

    def desktop_change(self, path, body, data):
        session_saved = path in ("/api/playback/session", "/api/playback-session")
        if not (session_saved or path in ("/api/playback/record", "/api/playback/history", "/api/feedback",
                "/api/rules", "/api/catalog/import", "/api/local-library/scan") or path.startswith("/api/playlists/")):
            return
        with self.lock:
            if session_saved and isinstance(data, dict) and isinstance(body, dict):
                self.playing_session = (data.get("account_id"), data.get("updated_at"), body.get("is_playing") is True)
            self.broadcast("sync_available", {"server_time": now()})

    def changed(self):
        self.application.publish_sse("lan_sync_status", {"changed": True})

    def start_if_enabled(self):
        if self.config["enabled"] is True:
            try:
                self.start()
            except OSError:
                # Desktop remains available to choose a different port.
                pass

    def start(self, host="0.0.0.0", port=None, *, enable_udp_discovery=None):
        with self.lifecycle:
            if self.bound_port:
                return self.bound_port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind((host, self.config["port"] if port is None else port))
                cert, key, self.fingerprint, tls_context = load_identity(self.application.settings.data_dir, discover_lan_ipv4_addresses())
                self.spki_fingerprint = certificate_spki_fingerprint(Path(cert))
                self.spki_pin = certificate_spki_pin(Path(cert))
                self.tls_context = tls_context
                # Local history keys contain paths. Seal cursor tie-breakers before sending them to a peer.
                self.cursor_key = hashlib.sha256(b"Pruner LAN cursor v1" + Path(key).read_bytes()).digest()
                sock.listen()
                server = uvicorn.Server(uvicorn.Config(create_lan_asgi_app(self.application, self),
                    lifespan="off", ws="none", access_log=False, proxy_headers=False, log_config=None,
                    timeout_graceful_shutdown=2, ssl_certfile=cert, ssl_keyfile=key,
                    ssl_context_factory=lambda config, default_factory: tls_context))
                worker = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
                worker.start()
                deadline = time.monotonic() + 5
                while not server.started:
                    if not worker.is_alive() or time.monotonic() >= deadline:
                        server.should_exit = True
                        raise OSError("LAN server failed to start")
                    time.sleep(.01)
                self.server, self.thread = server, worker
                self.bound_port = sock.getsockname()[1]
            except BaseException:
                sock.close()
                raise
            discovery = self.enable_udp_discovery if enable_udp_discovery is None else enable_udp_discovery
            if discovery:
                self.stop_udp.clear()
                self.udp_thread = threading.Thread(target=self._discovery, daemon=True)
                self.udp_thread.start()
            return self.bound_port

    def _discovery(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.bind(("0.0.0.0", LAN_DISCOVERY_UDP_PORT))
                udp.settimeout(.25)
                while not self.stop_udp.is_set():
                    try:
                        message, peer = udp.recvfrom(256)
                    except socket.timeout:
                        continue
                    if message == b"PRUNER_DISCOVER_V1":
                        reply = dict(type="pruner_announce", v=1, device_name=self.config["device_name"],
                                     port=self.bound_port, pairing_active=self.active(), tls=True, cert_fingerprint=self.fingerprint)
                        udp.sendto(json.dumps(reply).encode(), peer)
        except OSError:
            return

    def stop(self):
        with self.lifecycle:
            with self.lock:
                self.pair = None
                self.broadcast("closed", {})
                self.cancel_streams(None)
            self.stop_udp.set()
            if self.udp_thread:
                self.udp_thread.join()
            if self.server:
                self.server.should_exit = True
                self.thread.join()
            self.server = self.thread = self.udp_thread = self.bound_port = self.tls_context = None

    def toggle(self, *, enabled=None, port=None, device_name=None):
        with self.lifecycle:
            enabled = self.config["enabled"] if enabled is None else enabled
            if type(enabled) is not bool:
                raise ValueError("Invalid LAN setting")
            port = valid_port(self.config["port"] if port is None else port)
            name = label(self.config["device_name"] if device_name is None else device_name)
            previous = self.bound_port
            if previous and (not enabled or previous != port):
                self.stop()
            try:
                if enabled:
                    self.start(port=port)
                with self.lock:
                    self.store.update(enabled=enabled, port=port, device_name=name)
            except (OSError, AppError) as exc:
                if self.bound_port != previous:
                    self.stop()
                    if previous:
                        self.start(port=previous)
                raise AppError("LAN_SETTINGS_FAILED", "无法启用局域网配置，请检查端口或重试", 409) from exc
            self.changed()
            return self.status()

    def start_pairing(self):
        with self.lock:
            if not self.bound_port:
                raise AppError("LAN_DISABLED", "请先启用局域网同步", 409)
            code = f"{secrets.randbelow(1_000_000):06d}"
            ips = discover_lan_ipv4_addresses()
            cert, key, fp, _ = load_identity(self.application.settings.data_dir, ips)
            if fp != self.fingerprint:
                if getattr(self, "tls_context", None) is not None:
                    self.tls_context.load_cert_chain(cert, key)
                self.fingerprint = fp
            self.spki_fingerprint = certificate_spki_fingerprint(Path(cert))
            self.spki_pin = certificate_spki_pin(Path(cert))
            host = ips[0] if ips else "127.0.0.1"
            uri = (f"pruner-lan://pair?v=1&tls=1&host={host}&port={self.bound_port}&code={code}"
                   f"&fp={self.fingerprint}&spki={self.spki_fingerprint}&name={quote(self.config['device_name'], safe='')}")
            qr = qrcode.QRCode(border=4)
            qr.add_data(uri)
            rows = ["".join("1" if bit else "0" for bit in row) for row in qr.get_matrix()]
            self.pair = dict(
                code=code,
                expires_at=time.time() + 300,
                qr_uri=uri,
                qr_rows=rows,
                spki_fingerprint=self.spki_fingerprint,
                spki_pin=self.spki_pin,
            )
            self.attempts = self.locked_until = 0
            self.changed()
            return self.pair

    def cancel_pairing(self):
        with self.lock:
            self.pair = None
            self.changed()
            return {"pairing_active": False}

    def account(self):
        app = self.application
        with app._auth_lock:
            aid = app._active_account_id_best_effort()
            _, cookie, cached = app._session_snapshot()
            meta = cached.to_dict() if cached else next((r for r in app.db.list_cached_accounts()
                                                         if r["account_id"] == aid), {})
            return dict(account_id=aid, nickname=fields(meta, "display_name").get("display_name", ""),
                        avatar_url=public_image(meta.get("avatar_url")), offline=not cookie)

    def claim_pairing(self, code, device_name, platform):
        with self.lock:
            if time.monotonic() < self.locked_until:
                raise AppError("PAIR_LOCKED", "请稍后重试", 429)
            if not self.pair:
                raise AppError("PAIR_INACTIVE", "配对码无效")
            if not self.active():
                raise AppError("PAIR_EXPIRED", "配对码已过期", 410)
            if not isinstance(code, str) or not secrets.compare_digest(self.pair["code"].encode(), code.encode()):
                self.attempts += 1
                if self.attempts >= 5:
                    self.pair = None
                    self.locked_until = time.monotonic()+180
                    self.changed()
                    raise AppError("PAIR_LOCKED", "请稍后重试", 429)
                raise AppError("PAIR_INVALID", "配对码无效")
            name, platform = label(device_name), label(platform)
            token, salt, did = secrets.token_urlsafe(32), secrets.token_hex(16), secrets.token_hex(16)
            self.store.update_device(dict(device_id=did, device_name=name, platform=platform, token_salt=salt,
                token_hash=digest(salt, token), paired_at=now(), last_seen_at=now(), last_sync_at=None))
            self.pair = None
            self.changed()
            return dict(
                device_id=did,
                token=token,
                server_name=self.config["device_name"],
                protocol_version=1,
                account=self.account(),
                spki_fingerprint=self.spki_fingerprint,
                spki_pin=self.spki_pin,
                cert_fingerprint=self.fingerprint,
                pin_type="spki-sha256",
            )

    def authenticate(self, header, *, touch=True):
        scheme, _, token = header.partition(" ")
        if scheme.lower() == "bearer" and re.fullmatch(r"[\w-]{43}", token, re.ASCII):
            for d in self.config["devices"].values():
                if secrets.compare_digest(d["token_hash"], digest(d["token_salt"], token)):
                    if touch and time.time() - stamp(d["last_seen_at"]).timestamp() >= 60:
                        d = {**d, "last_seen_at": now()}
                        self.store.update_device(d)
                    return d
        raise AppError("LAN_UNAUTHORIZED", "设备尚未授权", 401)

    def register_stream(self, did, cancel_fn):
        token = object()
        with self.lock:
            if did not in self.config["devices"]:
                raise AppError("LAN_UNAUTHORIZED", "设备尚未授权", 401)
            self.active_streams[token] = (did, cancel_fn)

        def unregister():
            with self.lock:
                self.active_streams.pop(token, None)

        return unregister

    def cancel_streams(self, did=None):
        with self.lock:
            targets = [
                (token, cancel_fn)
                for token, (owner, cancel_fn) in list(self.active_streams.items())
                if did is None or owner == did
            ]
            for token, _ in targets:
                self.active_streams.pop(token, None)
        for _, cancel_fn in targets:
            try:
                cancel_fn()
            except Exception:
                pass

    def revoke(self, did):
        with self.lock:
            devices = self.config["devices"].copy()
            if devices.pop(did, None) is None:
                raise AppError("DEVICE_UNKNOWN", "设备不存在", 404)
            self.store.update(devices=devices)
            self.broadcast("device_revoked", {}, did)
            self.cancel_streams(did)
            for sid, entry in list(self.proxy_streams.items()):
                if entry.get("device_id") == did:
                    self.proxy_streams.pop(sid, None)
            self.changed()
            return {"revoked": True}

    def broadcast(self, event, data, did=None):
        for queue, (owner, loop) in list(self.listeners.items()):
            if did is None or did == owner:
                def enqueue(q=queue):
                    if q.full():
                        q.get_nowait()
                    q.put_nowait((event, data))
                loop.call_soon_threadsafe(enqueue)

    def events(self, did, loop):
        q = asyncio.Queue(maxsize=16)
        self.listeners[q] = (did, loop)

        async def stream():
            try:
                yield b"event: connected\ndata: {}\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(q.get(), 15)
                    except asyncio.TimeoutError:
                        yield b": heartbeat\n\n"
                        continue
                    if did not in self.config["devices"]:
                        yield b"event: device_revoked\ndata: {}\n\n"
                        return
                    if event in ("closed", "device_revoked"):
                        return
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
            finally:
                with self.lock:
                    self.listeners.pop(q, None)
        return StreamingResponse(stream(), media_type="text/event-stream")

    def indexed_paths(self, identities):
        wanted = set(identities)
        if not wanted:
            return {}
        found = {}
        if all(re.fullmatch("[0-9a-f]{24}", fid) for fid in wanted):
            with self.application.db._connect() as conn:
                for row in conn.execute("SELECT path FROM local_files"):
                    fid = file_id(row[0])
                    if fid in wanted:
                        found[fid] = row[0]
                        if len(found) == len(wanted):
                            return found
        raise AppError("FILE_UNKNOWN", "文件不存在", 404)

    def proxy_search_multi(self, q, channels="", limit=30, offset=0):
        chan_list = [c.strip() for c in str(channels or "").split(",") if c.strip()] if channels else None
        custom_api = str(self.application.user_settings.load().get("custom_external_api") or "")
        raw = self.application.audio.external_resolver.search_multi_channel(
            str(q or ""),
            channels=chan_list,
            limit=max(1, min(100, int(limit or 30))),
            offset=max(0, min(1000, int(offset or 0))),
            netease_client=self.application.client if self.application.client.cookie else None,
            custom_api=custom_api,
        )
        sanitized_tracks = [track(t) for t in raw.get("tracks", []) if isinstance(t, dict)]
        return {
            "query": raw.get("query", ""),
            "offset": raw.get("offset", 0),
            "limit": raw.get("limit", 30),
            "has_more": bool(raw.get("has_more", False)),
            "channels_queried": list(raw.get("channels_queried") or []),
            "tracks": sanitized_tracks,
            "channel_errors": dict(raw.get("channel_errors") or {}),
        }

    def proxy_resolve_audio(self, did, body):
        t = checked_track(body.get("track") if isinstance(body.get("track"), dict) else body)
        prov = t.get("provider") or "netease"
        ext_id = t.get("external_id") or ""
        if prov == "local":
            fid = t["file_id"]
            path = Path(self.indexed_paths([fid])[fid])
            rec = self.application.db.get_local_file(str(path)) or {}
            return {
                "track_key": f"local:{fid}",
                "provider": "local",
                "stream_url": f"{PREFIX}local-library/stream/{fid}",
                "format": str(rec.get("format") or path.suffix.lower().lstrip(".")),
                "bitrate": int(rec.get("bitrate") or 0),
                "sample_rate": int(rec.get("sample_rate") or 0),
                "duration_ms": int(rec.get("duration_ms") or 0),
            }

        query_parts = []
        if prov != "netease":
            query_parts.append(f"provider={quote(prov, safe='')}")
            query_parts.append(f"external_id={quote(ext_id, safe='')}")
            if t.get("title"):
                query_parts.append(f"title={quote(str(t['title']), safe='')}")
            if t.get("artists"):
                query_parts.append(f"artist={quote(';'.join(t['artists']), safe='')}")
            elif t.get("artist"):
                query_parts.append(f"artist={quote(str(t['artist']), safe='')}")
            if t.get("duration_ms"):
                query_parts.append(f"duration={int(t['duration_ms'])}")
        else:
            query_parts.append(f"id={int(t['id'])}")
        if body.get("quality"):
            query_parts.append(f"quality={quote(str(body['quality']), safe='')}")

        api_resp = self.application.api("GET", "/api/audio?" + "&".join(query_parts))
        if api_resp.status != 200 or not api_resp.payload.get("ok"):
            err = (api_resp.payload.get("error") or {}) if isinstance(api_resp.payload, dict) else {}
            raise AppError(str(err.get("code") or "AUDIO_UNAVAILABLE"), str(err.get("message") or "无法解析音源"), api_resp.status)

        data = dict(api_resp.payload["data"])
        stream_token = str(data.get("stream_token") or "").strip()
        if not stream_token and str(data.get("url") or "").startswith(("https://", "http://")):
            target = self.application.audio.register_stream_token(
                kind="remote",
                url=str(data["url"]),
                content_type="audio/mpeg",
                format_name=str(data.get("format") or ""),
                provider=str(data.get("source_provider") or prov),
            )
            stream_token = target.token
        stream_id = secrets.token_urlsafe(18)
        with self.lock:
            if did not in self.config["devices"]:
                raise AppError("LAN_UNAUTHORIZED", "设备尚未授权", 401)
            now_mono = time.monotonic()
            for k, v in list(self.proxy_streams.items()):
                if now_mono >= v["expires_at"]:
                    self.proxy_streams.pop(k, None)
            self.proxy_streams[stream_id] = {
                "device_id": did,
                "stream_token": stream_token,
                "expires_at": now_mono + 1800.0,
            }
        return {
            "track_key": t.get("track_key") or data.get("track_key") or "",
            "provider": str(data.get("source_provider") or prov),
            "stream_url": f"{PREFIX}audio/stream/{stream_id}",
            "level": str(data.get("level") or ""),
            "format": str(data.get("format") or ""),
            "bitrate": int(data.get("bitrate") or 0),
            "sample_rate": int(data.get("sample_rate") or 0),
            "size": int(data.get("size") or 0),
            "duration_ms": int(data.get("time_ms") or t.get("duration_ms") or 0),
            "loudness": data.get("loudness"),
        }

    def proxy_stream_target(self, did, stream_id):
        with self.lock:
            entry = self.proxy_streams.get(stream_id)
            if not entry or entry["device_id"] != did or time.monotonic() >= entry["expires_at"]:
                raise AppError("STREAM_NOT_FOUND", "音频流已失效", 404)
            tok = entry["stream_token"]
        return self.application.resolve_stream_target(tok)

    def proxy_cover(self, params):
        local_cid = str(params.get("local_cover_id") or "").strip()
        if local_cid:
            if not re.fullmatch(r"[0-9a-f]{24}", local_cid):
                raise AppError("COVER_NOT_FOUND", "找不到本地封面", 404)
            scanner = self.application.audio.external_resolver.local_scanner
            found = scanner.cover_cache.get(local_cid) if hasattr(scanner, "cover_cache") else None
            if not found:
                raise AppError("COVER_NOT_FOUND", "找不到本地封面", 404)
            return found[0], found[1]
        os_cid = str(params.get("opensubsonic_cover_id") or "").strip()
        if os_cid:
            if len(os_cid) > 256 or _CONTROL_CHAR_RE.search(os_cid):
                raise AppError("COVER_URL_REJECTED", "OpenSubsonic 封面标识无效", 400)
            try:
                sz = max(64, min(1024, int(params.get("size") or 384)))
            except ValueError as exc:
                raise AppError("COVER_SIZE_INVALID", "封面尺寸参数无效", 400) from exc
            secret = self.application.channel_vault.get_channel_secret("opensubsonic")
            return self.application.audio.external_resolver.opensubsonic.fetch_cover(
                os_cid, secret, self.application.audio.external_resolver._get_http_client(),
                size=sz,
            )
        raw_url = str(params.get("url") or "").strip()
        if raw_url:
            try:
                sz = max(64, min(1024, int(params.get("size") or 192)))
            except ValueError as exc:
                raise AppError("COVER_SIZE_INVALID", "封面尺寸参数无效", 400) from exc
            _st, data, ct = self.application.fetch_cover(raw_url, sz, allow_external=True)
            return data, ct
        raise AppError("COVER_URL_REQUIRED", "缺少封面参数", 400)

    def proxy_lyrics(self, params):
        parts = []
        for k in ("id", "song_id", "track_key", "provider", "external_id", "title", "artist"):
            val = str(params.get(k) or "").strip()
            if val:
                key_name = "id" if k == "song_id" else k
                parts.append(f"{key_name}={quote(val, safe='')}")
        if not parts:
            raise AppError("LYRIC_QUERY_REQUIRED", "缺少歌词查询参数", 400)
        resp = self.application.api("GET", "/api/lyrics?" + "&".join(parts))
        if resp.status != 200 or not resp.payload.get("ok"):
            err = (resp.payload.get("error") or {}) if isinstance(resp.payload, dict) else {}
            raise AppError(str(err.get("code") or "LYRICS_UNAVAILABLE"), str(err.get("message") or "获取歌词失败"), resp.status)
        return resp.payload["data"]

    def local_page(self, q="", limit=200, offset=0):
        db = self.application.db
        if not 1 <= limit <= 5000 or not 0 <= offset < 2**31:
            raise ValueError("Invalid pagination")
        files = []
        for row in db.list_local_files(q, limit, offset):
            fid = file_id(row["path"])
            cid = str(row.get("cover_id") or "").strip()
            cover_url = (
                f"{PREFIX}cover?local_cover_id={quote(cid, safe='')}"
                if re.fullmatch(r"[0-9a-f]{24}", cid)
                else ""
            )
            files.append({
                **fields(row, "title artist album duration_ms format bitrate mtime"),
                "file_id": fid,
                "stream_url": PREFIX + "local-library/stream/" + fid,
                "cover_url": cover_url,
            })
        total = db.count_local_files(q)
        return dict(files=files, total=total, offset=offset, limit=limit, has_more=offset+len(files)<total)

    def restore_track(self, t, paths=None):
        if not isinstance(t, dict):
            return t
        restored = dict(t)
        raw_cover = str(restored.get("cover_url") or "").strip()
        if raw_cover.startswith(f"{PREFIX}cover?"):
            qs = parse_qs(urlsplit(raw_cover).query, keep_blank_values=False)
            if "local_cover_id" in qs and re.fullmatch(r"[0-9a-f]{24}", qs["local_cover_id"][0].strip()):
                restored["cover_url"] = f"/api/local-cover/{qs['local_cover_id'][0].strip()}"
            elif "opensubsonic_cover_id" in qs:
                aid = qs["opensubsonic_cover_id"][0].strip()
                if aid and len(aid) <= 256 and not _CONTROL_CHAR_RE.search(aid):
                    restored["cover_url"] = f"/api/opensubsonic/cover/{quote(aid, safe='')}"
                else:
                    restored["cover_url"] = ""
            elif "url" in qs:
                from .app import validate_cover_url
                try:
                    restored["cover_url"] = validate_cover_url(qs["url"][0].strip(), 384, allow_external=True)
                except AppError:
                    restored["cover_url"] = ""
            else:
                restored["cover_url"] = ""
        if restored.get("provider") == "local":
            fid = restored["file_id"]
            path = (paths if paths is not None else self.indexed_paths([fid]))[fid]
            restored.update(local_path=path, external_id=path, track_key="local:" + path)
            restored.pop("stream_url", None)
        return restored

    def pending_desktop(self, aid):
        latest = self.config["latest_handoff"]
        if (latest and latest.get("account_id") == aid and latest.get("source_device_id") != "desktop"
                and "desktop" not in latest.get("consumed_by", [])):
            return deepcopy(latest)
        return None

    def acknowledge_desktop(self, handoff_id):
        if not isinstance(handoff_id, str) or not handoff_id:
            raise AppError("INVALID_INPUT", "缺少播放接续标识", 400)
        with self.lock, self.application._auth_lock:
            return self.acknowledge(self.application._active_account_id_best_effort(), handoff_id, "desktop")

    def acknowledge(self, aid, handoff_id, consumer):
        latest = self.config["latest_handoff"]
        if not latest or latest["account_id"] != aid or latest["handoff_id"] != handoff_id:
            raise AppError("HANDOFF_UNKNOWN", "播放接续已失效", 404)
        consumed = latest.get("consumed_by", [])
        if consumer not in consumed:
            self.store.update(latest_handoff={**latest, "consumed_by": [*consumed, consumer]})
            self.changed()
        return {"acknowledged": True}

    def handoff(self, aid, consumer):
        latest = self.config["latest_handoff"]
        current = session(self.application.db.get_playback_session(aid))
        if (latest and latest["account_id"] == aid and consumer not in latest.get("consumed_by", [])
                and (not current or stamp(latest["updated_at"]) >= stamp(current["updated_at"]))):
            return {**deepcopy(latest), "explicit_handoff": True}
        if current:
            return {**current, "explicit_handoff": False, "source": "playback_session", "track": current["current_track"], "account_id": aid, "source_device_id": "desktop",
                    "source_device_name": self.config["device_name"], "source_platform": "desktop",
                    "is_playing": self.playing_session == (aid, current.get("updated_at"), True), "handoff_id": None}
        return None

    def push_handoff(self, body, device, aid):
        current = session(body, checked_track)
        song = current.pop("current_track")
        if not song:
            raise ValueError("Invalid handoff track")
        queue = current["queue"]
        index = body.get("queue_index", -1)
        if type(index) is not int or not -1 <= index < len(queue):
            raise ValueError("Invalid queue index")
        def number(key, default, low, high):
            v = body.get(key, default)
            if type(v) not in (int, float) or not low <= v <= high:
                raise ValueError("Invalid playback number")
            return v
        mode = body.get("play_mode", "order")
        if mode not in PLAY_MODES:
            raise ValueError("Invalid play mode")
        playing = body.get("is_playing", False)
        if type(playing) is not bool:
            raise ValueError("Invalid playback state")
        self.indexed_paths(t["file_id"] for t in [song, *queue] if t.get("provider") == "local")
        result = dict(handoff_id=secrets.token_hex(16), source_device_id=device["device_id"],
            source_device_name=device["device_name"], source_platform=device["platform"], account_id=aid,
            track=song, queue=queue, queue_index=index, play_mode=mode, position_sec=number("position_sec", 0, 0, 1e9),
            duration_sec=number("duration_sec", 0, 0, 1e9), playback_rate=number("playback_rate", 1, .5, 2),
            updated_at=now(), consumed_by=[], explicit_handoff=True, is_playing=playing)
        self.store.update(latest_handoff=result)
        self.broadcast("handoff", result)
        self.application.publish_sse("lan_handoff", result)
        return result

    def pull(self, aid, body):
        db = self.application.db
        since = stamp(body["since"]).isoformat(timespec="microseconds") if body.get("since") else ""
        until = stamp(body["until"]).isoformat(timespec="microseconds") if body.get("until") else now()

        def delta(name, query, key, default):
            cursor_name = name + "_cursor"
            cursor = body.get(cursor_name)
            if cursor_name in body and cursor is None and body.get("until"):
                return {"items": [], "has_more": False, "next_cursor": None}
            cursor_args = {}
            if cursor is not None:
                if not isinstance(cursor, dict) or set(cursor) != {"after_ts", key}:
                    raise ValueError("Invalid cursor")
                tie = cursor[key]
                if key == "after_track_key":
                    if not isinstance(tie, str) or not tie:
                        raise ValueError("Invalid track cursor")
                    if tie.startswith("!local:"):
                        raw = base64.urlsafe_b64decode(tie[7:])
                        cipher = AES.new(self.cursor_key, AES.MODE_SIV)
                        cipher.update(aid.encode())
                        tie = cipher.decrypt_and_verify(raw[16:], raw[:16]).decode()
                elif type(tie) is not int or not 0 < tie < 2**63:
                    raise ValueError("Invalid feedback cursor")
                cursor_args = {"after_ts": stamp(cursor["after_ts"]).isoformat(timespec="microseconds"), key: tie}
            return query(aid, since=since, until=until, limit=body.get(name + "_limit", default), **cursor_args)

        history = delta("history", db.list_play_history_delta, "after_track_key", 100)
        feedback = delta("feedback", db.list_feedback_delta, "after_id", 200)
        cursor = history["next_cursor"]
        if cursor and cursor["after_track_key"].startswith("local:"):
            cipher = AES.new(self.cursor_key, AES.MODE_SIV)
            cipher.update(aid.encode())
            ciphertext, tag = cipher.encrypt_and_digest(cursor["after_track_key"].encode())
            cursor["after_track_key"] = "!local:" + base64.urlsafe_b64encode(tag + ciphertext).decode()
        result = dict(server_time=now(), sync_until=until, account_id=aid,
                      playback_session=session(db.get_playback_session(aid)),
                      play_history=[track(r) for r in history["items"]], history_has_more=history["has_more"],
                      history_next_cursor=cursor, rules=[rule(r) for r in self.application.rules.get()],
                      feedback_has_more=feedback["has_more"], feedback_next_cursor=feedback["next_cursor"],
                      feedback=[{**fields(r, "id song_id action context created_at client_event_id"),
                                 "track_key": track(r).get("track_key", "")} for r in feedback["items"]])
        if body.get("include_catalog") is True:
            playlists = db.get_catalog_dict(aid)["playlists"].values()
            result["playlists"] = [fields(p["summary"], "id name kind track_count") for p in playlists]
            result["songs"] = [track(s) for p in playlists for s in p["songs"]]
        if body.get("include_local") is True:
            result["local_library"] = []
            offset = 0
            while True:
                page = self.local_page(limit=5000, offset=offset)
                result["local_library"].extend(page["files"])
                if not page["has_more"] or not page["files"]:
                    break
                offset += len(page["files"])
        return result

    def push(self, aid, body):
        db = self.application.db
        for name, limit in (("play_events", 1000), ("feedback", 1000), ("rules", 100)):
            items = body.get(name, [])
            if not isinstance(items, list) or len(items) > limit or any(not isinstance(i, dict) for i in items):
                raise ValueError("Invalid sync batch")
        events = []
        event_tracks = {}
        for item in body.get("play_events", []):
            t = self.restore_track(checked_track(item.get("track", item.get("song", item))))
            ms = item.get("listen_ms", item.get("listened_ms", 0))
            completed = item.get("completed", False)
            if type(ms) is not int or not 0 <= ms <= 2**53 or type(completed) is not bool:
                raise ValueError("Invalid play event")
            event_id = label(item["event_id"])
            if event_tracks.setdefault(event_id, t["track_key"]) != t["track_key"]:
                raise ValueError("Play event refers to multiple tracks")
            events.append(dict(song_dict=t, event_id=event_id, listened_ms=ms, completed=completed))

        feedback = []
        for item in body.get("feedback", []):
            if item.get("action") not in VALID_FEEDBACK_ACTIONS:
                raise ValueError("Invalid feedback action")
            t = self.restore_track(checked_track(item))
            context = item.get("context", "")
            if not isinstance(context, str) or len(context) > 200 or (context and not fields(item, "context")):
                raise ValueError("Invalid feedback context")
            row = dict(song_id=t.get("song_id", 0), track_key=t["track_key"], action=item["action"], context=context)
            public_event = {**row, "track_key": track(t)["track_key"]}
            event_id = label(item["event_id"]) if "event_id" in item else hashlib.sha256(
                json.dumps(public_event, sort_keys=True).encode()).hexdigest()
            feedback.append({**row, "client_event_id": event_id})

        incoming = None
        force = body.get("force_session", False)
        if type(force) is not bool:
            raise ValueError("Invalid session flag")
        if "playback_session" in body:
            raw = body["playback_session"]
            allowed = {"current_track", "track", "queue", "position_sec", "duration_sec", "is_playing",
                       "queue_index", "play_mode", "volume", "muted", "playback_rate", "updated_at", "active_tab"}
            if (not isinstance(raw, dict) or not raw or raw.keys() - allowed
                    or type(raw.get("is_playing", False)) is not bool
                    or ("track" in raw and raw["track"] is not None and not isinstance(raw["track"], dict))):
                raise ValueError("Invalid playback session")
            # Validate the unprojected fields before filtering protocol metadata.
            db.playback_session_values(raw)
            if raw.get("play_mode", "order") not in PLAY_MODES:
                raise ValueError("Invalid playback mode")
            incoming = session(raw, checked_track)
            if "updated_at" in raw:
                incoming["updated_at"] = stamp(raw["updated_at"]).isoformat(timespec="microseconds")
            tracks = [incoming["current_track"], *incoming["queue"]]
            paths = self.indexed_paths(t["file_id"] for t in tracks if t and t.get("provider") == "local")
            incoming["queue"] = [self.restore_track(t, paths) for t in incoming["queue"]]
            incoming["current_track"] = self.restore_track(incoming["current_track"], paths)

        rules = self.application.rules
        # Serialize desktop rule saves with validation, the DB commit, and atomic file replacement.
        with rules.store._lock:
            merged = None
            if body.get("rules"):
                try:
                    updates = rules.validate([checked_rule(r) for r in body["rules"]])
                    by_id = {r["id"]: r for r in rules.get()}
                    by_id.update({r["id"]: r for r in updates})
                    merged = rules.validate(list(by_id.values()))
                except AppError as exc:
                    if exc.status >= 500:
                        raise
                    raise ValueError("Invalid rules") from exc
            result, updated_at = db.apply_lan_sync_push(aid, events, feedback, incoming, force)
            if merged is not None:
                try:
                    rules.put(merged)
                except (AppError, OSError) as exc:
                    raise AppError("RULE_SAVE_FAILED_AFTER_DB_COMMIT", "播放数据已提交，规则保存失败，请重试", 500) from exc
        if updated_at:
            self.playing_session = (aid, updated_at, incoming.get("is_playing") is True)
        self.application.publish_sse("history_updated", {"account_id": aid})
        self.broadcast("sync_available", {"account_id": aid})
        return {**result, "server_time": now()}

    def desktop(self, method, route, body):
        if (method, route) == ("POST", "toggle"):
            return self.toggle(**{k: body[k] for k in ("enabled", "port", "device_name") if k in body})
        with self.lock:
            if (method, route) == ("GET", "status"):
                return self.status()
            if (method, route) == ("POST", "pair/start"):
                return self.start_pairing()
            if (method, route) == ("POST", "pair/cancel"):
                return self.cancel_pairing()
            if method == "DELETE" and route.startswith("devices/"):
                return self.revoke(route[8:])
            aid = self.application._active_account_id_best_effort()
            if (method, route) == ("GET", "handoff"):
                data = self.pending_desktop(aid)
                if data:
                    tracks = [data["track"], *data["queue"]]
                    paths = self.indexed_paths(t["file_id"] for t in tracks if t and t.get("provider") == "local")
                    data["track"] = self.restore_track(data["track"], paths)
                    data["queue"] = [self.restore_track(t, paths) for t in data["queue"]]
                return {"pending_desktop_handoff": data}
            if (method, route) == ("POST", "handoff/push"):
                with self.application._auth_lock:
                    aid = self.application._active_account_id_best_effort()
                    if (body.get("expected_account_id", aid) != aid or body.get("session_generation",
                            self.application.session_generation) != self.application.session_generation):
                        raise AppError("ACCOUNT_CHANGED", "播放账户已经切换", 409)
                    return self.push_handoff(body, {"device_id": "desktop", "device_name": self.config["device_name"],
                                                    "platform": "desktop"}, aid)
        raise AppError("NOT_FOUND", "接口不存在", 404)


def stream_indexed_file(
    path,
    mime,
    range_header,
    *,
    head_only,
    register_stream=None,
):
    from .app import _parse_single_byte_range, _STREAM_CHUNK_BYTES

    handle = None
    try:
        if path.resolve() != path.absolute():
            raise OSError("Redirected indexed path")
        expected = path.lstat()
        if not stat.S_ISREG(expected.st_mode):
            raise OSError("Expected regular file")
        def opener(name, flags):
            return os.open(name, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        handle = open(path, "rb", opener=opener)
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(expected, opened):
            raise OSError("Indexed file changed during open")
        size = opened.st_size
        headers = {"Accept-Ranges": "bytes"}
        start, end, status = 0, size - 1, 200
        if range_header is not None:
            bounds = (_parse_single_byte_range(range_header, size)
                      if re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", range_header.strip(), re.I) else None)
            if bounds is None:
                handle.close()
                return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
            start, end = bounds
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            status = 206
        length = end - start + 1
        headers["Content-Length"] = str(length)
        if head_only:
            handle.close()
            return Response(status_code=status, media_type=mime, headers=headers)
        handle.seek(start)
    except (OSError, RuntimeError) as exc:
        if handle is not None:
            handle.close()
        raise AppError("FILE_UNKNOWN", "文件不存在", 404) from exc

    cancelled = threading.Event()

    async def chunks():
        remaining = length
        try:
            while remaining:
                if cancelled.is_set():
                    raise asyncio.CancelledError
                chunk = await run_in_threadpool(handle.read, min(_STREAM_CHUNK_BYTES, remaining))
                if cancelled.is_set():
                    raise asyncio.CancelledError
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            if register_stream is None:
                handle.close()

    class FileResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            unregister = None
            try:
                if register_stream is not None:
                    task = asyncio.current_task()
                    loop = asyncio.get_running_loop()

                    def abort_stream():
                        cancelled.set()
                        loop.call_soon_threadsafe(task.cancel)

                    # Admit the response under the device registry lock. A denied response
                    # is never registered, so it can finish sending its 401.
                    try:
                        unregister = register_stream(abort_stream)
                    except AppError as exc:
                        await JSONResponse({"error": exc.code}, status_code=exc.status)(scope, receive, send)
                        return
                await super().__call__(scope, receive, send)
            finally:
                if unregister is not None:
                    unregister()
                # A canceled request may still have a worker reading the file.
                with CancelScope(shield=True):
                    await run_in_threadpool(handle.close)

    return FileResponse(chunks(), status_code=status, media_type=mime, headers=headers)


async def read_body(request):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise AppError("JSON_REQUIRED", "需要 JSON 请求", 415)
    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > BODY_LIMIT:
            raise AppError("BODY_TOO_LARGE", "请求过大", 413)
        buf.extend(chunk)
    value = json.loads(buf)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def create_lan_asgi_app(application, lan_manager):
    manager = lan_manager

    def dispatch(request, body, loop):
        route = request.url.path[len(PREFIX):]
        with manager.lock:
            if route == "info":
                base_info = manager.info()
                if request.query_params.get("v") == "2":
                    return {
                        **base_info,
                        "spki_fingerprint": manager.spki_fingerprint,
                        "spki_pin": manager.spki_pin,
                        "pin_type": "spki-sha256",
                    }
                return base_info
            if route == "pair/claim":
                return manager.claim_pairing(body.get("code"), body.get("device_name"), body.get("platform"))
            device = manager.authenticate(request.headers.get("authorization", ""), touch=route != "sync/push")
            did = device["device_id"]
            with application._auth_lock:
                aid = application._active_account_id_best_effort()
                if body.get("account_id", aid) != aid:
                    raise AppError("ACCOUNT_CHANGED", "同步账户已经切换", 409)
                if route == "events":
                    if request.method != "GET":
                        raise AppError("METHOD_NOT_ALLOWED", "不支持的请求方式", 405)
                    return manager.events(did, loop)
                if route == "status":
                    handoff = manager.handoff(aid, did)
                    return {**manager.info(), "spki_fingerprint": manager.spki_fingerprint,
                            "spki_pin": manager.spki_pin, "pin_type": "spki-sha256",
                            "server_time": now(), "active_account": manager.account(),
                            "paired_device": device_view(device), "local_library_count": application.db.count_local_files(),
                            "latest_handoff_id": handoff and handoff["handoff_id"]}
                if route in ("sync/pull", "sync/push"):
                    result = manager.pull(aid, body) if route == "sync/pull" else manager.push(aid, body)
                    manager.store.update_device({**device, "last_sync_at": now(), "last_seen_at": now()})
                    manager.changed()
                    return result
                if route == "handoff/ack":
                    return manager.acknowledge(aid, body.get("handoff_id"), did)
                if route == "handoff":
                    return manager.push_handoff(body, device, aid) if request.method == "POST" else manager.handoff(aid, did)
                if route == "local-library":
                    params = request.query_params
                    return manager.local_page(params.get("q", ""), int(params.get("limit", 200)), int(params.get("offset", 0)))
                if route in ("search/multi", "audio/resolve", "cover", "lyrics"):
                    # Release manager.lock and application._auth_lock before executing external network I/O!
                    unlocked_proxy = (route, did, request.query_params, body)
                elif route.startswith("audio/stream/"):
                    sid = request.path_params["stream_id"]
                    target = manager.proxy_stream_target(did, sid)
                    if target.kind == "local" and target.path is not None:
                        return stream_indexed_file(
                            target.path,
                            target.content_type,
                            request.headers.get("range"),
                            head_only=request.method == "HEAD",
                            register_stream=lambda cancel_fn, owner=did: manager.register_stream(owner, cancel_fn),
                        )
                    return ("__async_remote_stream__", target, did)
                else:
                    from .app import _LOCAL_AUDIO_MIME_BY_EXT, _LOCAL_AUDIO_EXTENSIONS
                    fid = request.path_params["file_id"]
                    path = Path(manager.indexed_paths([fid])[fid])
                    if path.suffix.lower() not in _LOCAL_AUDIO_EXTENSIONS:
                        raise AppError("FILE_UNKNOWN", "文件不存在", 404)
                    return stream_indexed_file(
                        path,
                        _LOCAL_AUDIO_MIME_BY_EXT.get(path.suffix.lower(), "audio/mpeg"),
                        request.headers.get("range"),
                        head_only=request.method == "HEAD",
                        register_stream=lambda cancel_fn, owner=did: manager.register_stream(owner, cancel_fn),
                    )
        # Execute slow outbound proxy endpoints outside manager.lock and application._auth_lock.
        route_name, proxy_did, qparams, pbody = unlocked_proxy
        if route_name == "search/multi":
            return manager.proxy_search_multi(
                qparams.get("q", ""),
                qparams.get("channels", ""),
                int(qparams.get("limit", 30)),
                int(qparams.get("offset", 0)),
            )
        if route_name == "audio/resolve":
            return manager.proxy_resolve_audio(proxy_did, pbody)
        if route_name == "cover":
            data, mime = manager.proxy_cover(qparams)
            return Response(
                content=data,
                status_code=200,
                media_type=mime,
                headers={"Cache-Control": "public, max-age=86400", "Content-Security-Policy": "sandbox"},
            )
        return manager.proxy_lyrics(qparams)

    async def endpoint(request):
        try:
            if request.method == "POST" and request.url.path != PREFIX+"pair/claim":
                with manager.lock:
                    manager.authenticate(request.headers.get("authorization", ""), touch=False)
            body = await read_body(request) if request.method == "POST" else {}
            result = await run_in_threadpool(dispatch, request, body, asyncio.get_running_loop())
            if isinstance(result, tuple) and len(result) == 3 and result[0] == "__async_remote_stream__":
                from .app import _stream_remote_audio
                _, target, owner_did = result
                with manager.lock:
                    if owner_did not in manager.config["devices"]:
                        return JSONResponse({"error": "LAN_UNAUTHORIZED"}, status_code=401)
                return await _stream_remote_audio(
                    target,
                    application,
                    request.headers.get("range"),
                    head_only=request.method == "HEAD",
                    register_stream=lambda cancel_fn, owner=owner_did: manager.register_stream(owner, cancel_fn),
                )
            return result if callable(result) else JSONResponse(result)
        except AppError as exc:
            if exc.code == "FILE_UNKNOWN" and request.url.path == PREFIX + "sync/push":
                return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
            return JSONResponse({"error": exc.code}, status_code=exc.status)
        except (ValueError, AttributeError, TypeError, OverflowError, KeyError, RecursionError):
            return JSONResponse({"error": "INVALID_INPUT"}, status_code=400)
        except sqlite3.Error:
            return JSONResponse({"error": "SYNC_DATABASE_FAILED"}, status_code=500)
        except FileNotFoundError:
            return JSONResponse({"error": "FILE_UNKNOWN"}, status_code=404)

    routes = [Route(PREFIX+name, endpoint, methods=methods.split()) for name, methods in (
        ("info", "GET"), ("pair/claim", "POST"), ("status", "GET"), ("sync/pull", "POST"),
        ("sync/push", "POST"), ("handoff", "GET POST"), ("handoff/ack", "POST"), ("local-library", "GET"),
        ("local-library/stream/{file_id}", "GET HEAD"), ("events", "GET"),
        ("search/multi", "GET"), ("audio/resolve", "POST"),
        ("audio/stream/{stream_id}", "GET HEAD"), ("cover", "GET"), ("lyrics", "GET"))]
    app = Starlette(routes=routes)
    app.router.redirect_slashes = False

    async def boundary(request, call_next):
        if request.method == "OPTIONS":
            response = JSONResponse({"error": "METHOD_NOT_ALLOWED"}, status_code=405)
        elif "origin" in request.headers:
            response = JSONResponse({"error": "ORIGIN_REJECTED"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=boundary)
    return app

