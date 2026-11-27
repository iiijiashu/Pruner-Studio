#!/usr/bin/env python3
"""Standalone isolated low-privilege HTTP Resolver v2 Sidecar Bridge for Pruner Studio.

Security contract:
- Binds strictly to 127.0.0.1 (never 0.0.0.0).
- Reads its per-session authentication token from stdin JSON (`{"token": "..."}`), never CLI argv.
- Requires `X-Resolver-Token` (or `X-API-Key`) on every endpoint using constant-time verification.
- Runs in a stripped environment with ZERO access to Pruner Studio cookies or credential vaults.
- Speaks HTTP Resolver Contract v2: GET /manifest, POST/GET /search, POST /resolve, POST/GET /lyric.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import secrets
import sys
from typing import Any
from urllib.parse import parse_qs, urlsplit

_MAX_BODY_BYTES = 256 * 1024


class BaseSidecarAdapter:
    """Default no-op reference adapter. Community adapters subclass or export these 4 callables."""

    id = "pruner-sidecar-reference"
    name = "Pruner Sidecar Bridge (Reference)"
    version = "2.0.0"
    sources = ["sidecar"]
    capabilities = ["search", "resolve", "lyric"]

    def manifest(self) -> dict[str, Any]:
        return {
            "protocol_version": 2,
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "sources": list(self.sources),
            "rate_limit": {"requests_per_minute": 60},
        }

    def search(self, query: str, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        return {"tracks": [], "has_more": False}

    def resolve(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"candidates": []}

    def lyric(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"lrc": "", "tlyric": "", "romalrc": "", "yrc": ""}


def load_adapter(adapter_path: str | None) -> Any:
    if not adapter_path:
        return BaseSidecarAdapter()
    target = Path(adapter_path).expanduser().resolve()
    spec = importlib.util.spec_from_file_location("pruner_sidecar_user_adapter", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load adapter module: {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, "create_adapter", None)
    if callable(factory):
        return factory()
    adapter_cls = getattr(module, "Adapter", None)
    if isinstance(adapter_cls, type):
        return adapter_cls()
    return module


def make_handler(adapter: Any, token: str):
    class SidecarHandler(BaseHTTPRequestHandler):
        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def _authorized(self) -> bool:
            provided = (self.headers.get("X-Resolver-Token") or self.headers.get("X-API-Key") or "").strip()
            return bool(provided) and secrets.compare_digest(provided, token)

        def _read_json(self) -> dict[str, Any]:
            raw_len = self.headers.get("Content-Length") or "0"
            try:
                length = int(raw_len)
            except ValueError:
                return {}
            if length <= 0 or length > _MAX_BODY_BYTES:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}

        @staticmethod
        def _safe_int(val: Any, default: int, low: int, high: int) -> int:
            try:
                num = int(val)
            except (TypeError, ValueError):
                return default
            return max(low, min(high, num))

        def do_GET(self) -> None:
            if not self._authorized():
                self._send_json(401, {"error": "UNAUTHORIZED"})
                return
            parsed = urlsplit(self.path)
            qs = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
            try:
                if parsed.path == "/manifest":
                    fn = getattr(adapter, "manifest", None)
                    self._send_json(200, fn() if callable(fn) else BaseSidecarAdapter().manifest())
                    return
                if parsed.path == "/search":
                    fn = getattr(adapter, "search", None)
                    res = fn(
                        str(qs.get("q") or qs.get("query") or ""),
                        limit=self._safe_int(qs.get("limit"), 30, 1, 100),
                        offset=self._safe_int(qs.get("offset"), 0, 0, 10_000),
                    ) if callable(fn) else {"tracks": [], "has_more": False}
                    self._send_json(200, res)
                    return
                if parsed.path == "/lyric":
                    fn = getattr(adapter, "lyric", None)
                    self._send_json(200, fn(qs) if callable(fn) else {"lrc": ""})
                    return
                self._send_json(404, {"error": "NOT_FOUND"})
            except Exception:
                self._send_json(500, {"error": "ADAPTER_ERROR"})

        def do_POST(self) -> None:
            if not self._authorized():
                self._send_json(401, {"error": "UNAUTHORIZED"})
                return
            parsed = urlsplit(self.path)
            body = self._read_json()
            try:
                if parsed.path == "/search":
                    fn = getattr(adapter, "search", None)
                    res = fn(
                        str(body.get("query") or body.get("q") or body.get("title") or ""),
                        limit=self._safe_int(body.get("limit"), 30, 1, 100),
                        offset=self._safe_int(body.get("offset"), 0, 0, 10_000),
                    ) if callable(fn) else {"tracks": [], "has_more": False}
                    self._send_json(200, res)
                    return
                if parsed.path == "/resolve":
                    fn = getattr(adapter, "resolve", None)
                    self._send_json(200, fn(body) if callable(fn) else {"candidates": []})
                    return
                if parsed.path == "/lyric":
                    fn = getattr(adapter, "lyric", None)
                    self._send_json(200, fn(body) if callable(fn) else {"lrc": ""})
                    return
                self._send_json(404, {"error": "NOT_FOUND"})
            except Exception:
                self._send_json(500, {"error": "ADAPTER_ERROR"})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return SidecarHandler


def frozen_main():
    # A windowed bootloader has no Python standard streams. Popen supplies Win32 pipe handles.
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        import ctypes
        import msvcrt
        import os
        from ctypes import wintypes
        get_handle = ctypes.windll.kernel32.GetStdHandle
        get_handle.argtypes = [wintypes.DWORD]
        get_handle.restype = wintypes.HANDLE
        for name, index, flags, mode in (("stdin",-10,os.O_RDONLY,"r"), ("stdout",-11,os.O_WRONLY,"w"),
                                         ("stderr",-12,os.O_WRONLY,"w")):
            if getattr(sys, name) is None:
                fd = msvcrt.open_osfhandle(get_handle(index), flags)
                setattr(sys, name, os.fdopen(fd, mode, encoding="utf-8"))
    sys.argv.pop(1)
    return main()


def main() -> int:
    parser = argparse.ArgumentParser(description="Pruner Studio HTTP Resolver v2 Sidecar Bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--adapter", default="")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost"}:
        sys.stderr.write("Sidecar may only bind to 127.0.0.1\n")
        return 2

    token = ""
    if not sys.stdin.isatty():
        line = sys.stdin.readline().strip()
        if line:
            try:
                token = str(json.loads(line).get("token") or "").strip()
            except Exception:
                token = ""
    if not token:
        token = secrets.token_urlsafe(32)

    adapter = load_adapter(args.adapter or None)
    server = ThreadingHTTPServer(("127.0.0.1", int(args.port)), make_handler(adapter, token))
    bound_port = server.server_address[1]
    sys.stdout.write(json.dumps({"ready": True, "host": "127.0.0.1", "port": bound_port}) + "\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
