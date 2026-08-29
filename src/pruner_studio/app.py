from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import threading
import webbrowser
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .audio_service import AudioService
from .catalog_service import CatalogService, RulesService
from .config import Settings
from .errors import AppError
from .instance_lock import DataDirectoryLock
from .netease_client import NeteaseClient, NeteaseWebTransport, Transport
from .operations import OperationsService
from .operation_journal import OperationJournal
from .storage import JsonStore, SessionStore
from .version import __version__


@dataclass(frozen=True)
class ApiResponse:
    status: int
    payload: dict[str, Any]


def success(data: Any = None, status: int = 200) -> ApiResponse:
    return ApiResponse(status, {"ok": True, "data": data, "error": None})


def failure(error: AppError) -> ApiResponse:
    return ApiResponse(error.status, {"ok": False, "data": None, "error": {"code": error.code, "message": error.message}})


class PrunerApplication:
    def __init__(
        self,
        settings: Settings,
        client: NeteaseClient,
        session: SessionStore,
        catalog: CatalogService,
        rules: RulesService,
        audio: AudioService,
        operations: OperationsService,
    ):
        self.settings = settings
        self.client = client
        self.session = session
        self.catalog = catalog
        self.rules = rules
        self.audio = audio
        self.operations = operations
        self.static_files = _build_static_allowlist(settings.web_dir)
        self._auth_lock = threading.RLock()
        self._active_qr_keys: set[str] = set()

    def api(self, method: str, raw_path: str, body: Any = None) -> ApiResponse:
        parsed = urlsplit(raw_path)
        path = parsed.path
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/api/health":
                return success({"status": "ok", "version": __version__})
            if method == "GET" and path == "/api/status":
                return self._status()
            if method == "POST" and path == "/api/auth/qr":
                with self._auth_lock:
                    result = self.client.create_qr()
                    self._active_qr_keys.clear()
                    self._active_qr_keys.add(result["key"])
                return success(result, 201)
            match = re.fullmatch(r"/api/auth/qr/([^/]+)", path)
            if method == "GET" and match:
                qr_key = unquote(match.group(1))
                with self._auth_lock:
                    if qr_key not in self._active_qr_keys:
                        raise AppError("QR_CANCELLED", "二维码已取消或失效", 409)
                    result = self.client.poll_qr(qr_key)
                    if qr_key not in self._active_qr_keys:
                        raise AppError("QR_CANCELLED", "二维码已取消或失效", 409)
                    if result["state"] in {"authorized", "expired"}:
                        self._active_qr_keys.discard(qr_key)
                    if result["state"] == "authorized":
                        if not self.client.cookie:
                            raise AppError("SESSION_MISSING", "登录成功但会话未保存", 502)
                        self.session.save_cookie(self.client.cookie)
                        self._clear_transport_cache()
                return success(result)
            if method == "POST" and path == "/api/auth/logout":
                with self._auth_lock:
                    self._revoke_session()
                return success({"authenticated": False})
            if method == "GET" and path == "/api/playlists":
                with self._auth_lock:
                    playlists = [item.to_dict() for item in self.client.playlists()]
                return success({"playlists": playlists})
            match = re.fullmatch(r"/api/playlists/(\d+)/sync", path)
            if method == "POST" and match:
                playlist_id = _positive_id(match.group(1), "PLAYLIST_ID_INVALID")
                with self._auth_lock:
                    owned = next((item for item in self.client.playlists() if item.id == playlist_id), None)
                    if owned is None:
                        raise AppError("PLAYLIST_NOT_FOUND", "找不到这个自建歌单", 404)
                    synced = self.catalog.sync(owned)
                return success(synced)
            if method == "POST" and path == "/api/catalog/import":
                payload = _object(body)
                return success(
                    self.catalog.import_content(
                        payload.get("content"),
                        str(payload.get("format") or ""),
                        str(payload.get("playlist_name") or "导入歌曲"),
                    ),
                    201,
                )
            if method == "GET" and path == "/api/catalog":
                return success(self.catalog.view())
            if method == "GET" and path == "/api/lyrics":
                song_id = _query_id(query)
                with self._auth_lock:
                    lyrics = self.client.lyrics(song_id)
                return success(lyrics)
            if method == "GET" and path == "/api/audio":
                song_id = _query_id(query)
                with self._auth_lock:
                    resolved = self.audio.resolve(song_id)
                return success(resolved)
            if method == "GET" and path == "/api/rules":
                return success({"rules": self.rules.get()})
            if method == "PUT" and path == "/api/rules":
                payload = _object(body)
                return success({"rules": self.rules.put(payload.get("rules"))})
            if method == "POST" and path == "/api/operations/backup":
                payload = _object(body, allow_none=True)
                return success(self.operations.backup(payload.get("ids") if payload else None).to_dict(), 201)
            if method == "GET" and path == "/api/operations/pending":
                with self._auth_lock:
                    account = self.client.account()
                    pending = self.operations.pending_for(account.user_id)
                return success({"operations": pending})
            if method == "POST" and path == "/api/operations/create-playlist":
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.create_playlist(
                        payload.get("name"),
                        payload.get("song_ids", []),
                        payload.get("privacy", 0),
                    )
                return success(result.to_dict(), 201)
            if method == "POST" and path == "/api/operations/unlike":
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.unlike(payload.get("song_ids"), payload.get("source_playlist_id"))
                return success(result.to_dict())
            match = re.fullmatch(r"/api/playlists/(\d+)/remove-tracks", path)
            if method == "POST" and match:
                playlist_id = _positive_id(match.group(1), "PLAYLIST_ID_INVALID")
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.remove_tracks(
                        playlist_id,
                        payload.get("song_ids"),
                        payload.get("source_playlist_id"),
                    )
                return success(result.to_dict())
            match = re.fullmatch(r"/api/operations/([0-9a-f]{32})/(reconcile|ignore)", path)
            if method == "POST" and match:
                operation_id, action = match.groups()
                with self._auth_lock:
                    account = self.client.account()
                    if action == "reconcile":
                        result = self.operations.reconcile_pending(account.user_id, operation_id).to_dict()
                    else:
                        result = self.operations.ignore_pending(account.user_id, operation_id)
                return success(result)
            if method == "POST" and path == "/api/operations/reconcile":
                payload = _object(body)
                with self._auth_lock:
                    result = self.operations.reconcile(
                        payload.get("action"),
                        payload.get("playlist_id"),
                        payload.get("song_ids"),
                        payload.get("source_playlist_id"),
                    )
                return success(result.to_dict())
            raise AppError("ROUTE_NOT_FOUND", "接口不存在", 404)
        except AppError as exc:
            return failure(exc)
        except (TypeError, ValueError):
            return failure(AppError("INVALID_INPUT", "输入数据无效", 400))
        except Exception:
            return failure(AppError("INTERNAL_ERROR", "程序内部错误", 500))

    def _clear_transport_cache(self) -> None:
        clear = getattr(self.client.transport, "clear_caches", None)
        if callable(clear):
            clear()

    def _revoke_session(self) -> None:
        self._active_qr_keys.clear()
        self.session.clear()
        self.client.set_cookie(None)
        self._clear_transport_cache()

    def _status(self) -> ApiResponse:
        with self._auth_lock:
            if not self.client.cookie:
                return success({"authenticated": False, "user": None})
            try:
                account = self.client.account()
            except AppError as exc:
                if exc.code == "SESSION_EXPIRED":
                    self._revoke_session()
                    return success({"authenticated": False, "user": None})
                raise
            pending = self.operations.pending_for(account.user_id)
        return success({"authenticated": True, "user": account.to_dict(), "pending_operations": pending})

    def static(self, raw_path: str) -> tuple[int, bytes, str]:
        path = urlsplit(raw_path).path
        relative = "index.html" if path == "/" else unquote(path).lstrip("/")
        target = self.static_files.get(relative)
        if target is None:
            return 404, b"Not found", "text/plain; charset=utf-8"
        try:
            content = target.read_bytes()
        except OSError:
            return 404, b"Not found", "text/plain; charset=utf-8"
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        return 200, content, content_type


def build_application(settings: Settings | None = None, transport: Transport | None = None) -> PrunerApplication:
    selected = settings or Settings.default()
    store = JsonStore(selected.data_dir)
    session = SessionStore(store)
    client = NeteaseClient(transport or NeteaseWebTransport(selected.api_base), session.load_cookie())
    catalog = CatalogService(store, client, selected.max_batch_size)
    journal = OperationJournal(store)
    return PrunerApplication(
        selected,
        client,
        session,
        catalog,
        RulesService(store),
        AudioService(client, catalog, selected.alternative_duration_tolerance_ms),
        OperationsService(store, client, catalog, journal, selected.max_batch_size),
    )


def make_handler(application: PrunerApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"PrunerStudio/{__version__}"

        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_OPTIONS(self) -> None:
            self._json_response(failure(AppError("METHOD_NOT_ALLOWED", "不支持跨域请求", 405)))

        def _dispatch(self) -> None:
            rejected = validate_local_request(self.headers, self.server.server_address[1])
            if rejected is not None:
                self._json_response(failure(rejected))
                return
            if urlsplit(self.path).path.startswith("/api/"):
                body: Any = None
                if self.command in {"POST", "PUT"}:
                    try:
                        body = self._read_json()
                    except AppError as exc:
                        self._json_response(failure(exc))
                        return
                self._json_response(application.api(self.command, self.path, body))
                return
            if self.command != "GET":
                self._json_response(failure(AppError("METHOD_NOT_ALLOWED", "请求方法不支持", 405)))
                return
            status, content, content_type = application.static(self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(content)

        def _read_json(self) -> Any:
            content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if content_type != "application/json":
                raise AppError("CONTENT_TYPE_INVALID", "请求内容类型必须是 application/json", 415)
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return None
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise AppError("CONTENT_LENGTH_INVALID", "请求长度无效", 400) from exc
            if length < 0 or length > application.settings.max_body_bytes:
                raise AppError("BODY_TOO_LARGE", "请求内容过大", 413)
            raw = self.rfile.read(length)
            if not raw:
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AppError("JSON_INVALID", "请求 JSON 格式无效", 400) from exc

        def _json_response(self, response: ApiResponse) -> None:
            raw = json.dumps(response.payload, ensure_ascii=False).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def serve(settings: Settings | None = None, transport: Transport | None = None) -> None:
    selected = settings or Settings.default()
    if selected.host != "127.0.0.1":
        raise ValueError("Pruner Studio may only bind to 127.0.0.1")
    canonical_data_dir = Path(os.path.normcase(os.path.realpath(os.path.expanduser(os.fspath(selected.data_dir)))))
    canonical_data_dir.mkdir(parents=True, exist_ok=True)
    selected = replace(selected, data_dir=canonical_data_dir)
    with DataDirectoryLock(selected.data_dir):
        application = build_application(selected, transport)
        server = None
        for candidate_port in range(selected.port, min(65535, selected.port + 20) + 1):
            try:
                with socket.create_connection((selected.host, candidate_port), timeout=0.15):
                    continue
            except OSError:
                pass
            try:
                server = LoopbackHTTPServer((selected.host, candidate_port), make_handler(application))
                break
            except OSError:
                continue
        if server is None:
            raise OSError("Pruner Studio could not find a free local port")
        if os.environ.get("PRUNER_STUDIO_NO_BROWSER") != "1":
            threading.Timer(0.35, lambda: webbrowser.open(f"http://{selected.host}:{server.server_port}/")).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


class LoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = False
    block_on_close = True

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def validate_local_request(headers: Any, port: int) -> AppError | None:
    """Reject requests that did not come from this application's own page.

    A fixed loopback port is reachable from any web page the user visits:
    browsers let cross-site pages issue blind requests, and DNS rebinding turns
    an attacker domain same-origin. Exact Host and Origin checks plus Fetch
    Metadata reject those channels; JSON-only writes add a separate preflight gate.
    """
    host = (headers.get("Host") or "").strip().lower()
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}:
        return AppError("REQUEST_ORIGIN_REJECTED", "仅接受本机页面发起的请求", 403)
    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        return AppError("REQUEST_ORIGIN_REJECTED", "仅接受本机页面发起的请求", 403)
    origin = headers.get("Origin")
    if origin:
        parsed = urlsplit(origin.strip())
        try:
            origin_port = parsed.port
        except ValueError:
            return AppError("REQUEST_ORIGIN_REJECTED", "仅接受本机页面发起的请求", 403)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "http" or hostname not in {"127.0.0.1", "localhost", "::1"} or origin_port != port:
            return AppError("REQUEST_ORIGIN_REJECTED", "仅接受本机页面发起的请求", 403)
    return None


def _build_static_allowlist(web_dir: Path) -> dict[str, Path]:
    allowed_suffixes = {".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2", ".ttf"}
    if not web_dir.is_dir():
        return {}
    result: dict[str, Path] = {}
    root = web_dir.resolve()
    for target in root.rglob("*"):
        if not target.is_file() or target.suffix.lower() not in allowed_suffixes:
            continue
        resolved = target.resolve()
        if resolved.is_relative_to(root):
            result[resolved.relative_to(root).as_posix()] = resolved
    return result


def _object(value: Any, allow_none: bool = False) -> dict[str, Any]:
    if value is None and allow_none:
        return {}
    if not isinstance(value, dict):
        raise AppError("JSON_OBJECT_REQUIRED", "请求内容必须是 JSON 对象")
    return value


def _positive_id(raw: Any, code: str = "SONG_ID_INVALID") -> int:
    if isinstance(raw, bool):
        raise AppError(code, "ID 必须是正整数")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise AppError(code, "ID 必须是正整数") from exc
    if value <= 0:
        raise AppError(code, "ID 必须是正整数")
    return value


def _query_id(query: dict[str, list[str]]) -> int:
    values = query.get("id")
    if not values or len(values) != 1:
        raise AppError("SONG_ID_REQUIRED", "缺少歌曲 ID")
    return _positive_id(values[0])
