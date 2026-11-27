"""Optional isolated low-privilege HTTP Resolver v2 Sidecar bridge manager."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time
from typing import Any

import httpx

from .errors import AppError
from .netease_client import isolate_cookie_jar

_ALLOWED_CHILD_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "PATHEXT",
    "HOME",
    "LANG",
    "LC_ALL",
    "PYTHONUTF8",
)


def build_sanitized_sidecar_env() -> dict[str, str]:
    """Build a strictly stripped environment containing ZERO host Pruner secrets, cookies, or proxy vars."""
    env: dict[str, str] = {"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    for key in _ALLOWED_CHILD_ENV_KEYS:
        val = os.environ.get(key)
        if val:
            env[key] = val
    if getattr(sys, "frozen", False):
        # Share this bootloader extraction directory with the child process.
        env.update({k:v for k,v in os.environ.items() if k.startswith("_PYI_")})
    return env


class SidecarProcessManager:
    """Lifecycle controller for the optional loopback-only HTTP Resolver v2 Sidecar process."""

    def __init__(self, script_path: Path | None = None):
        self._lock = threading.RLock()
        self.frozen = bool(getattr(sys, "frozen", False)) and script_path is None
        self.script_path = script_path or Path(__file__).with_name("sidecar_runner.py")
        self._proc: subprocess.Popen[bytes] | None = None
        self._port: int = 0
        self._token: str = ""
        self._adapter_path: str = ""
        self._manifest: dict[str, Any] | None = None
        self._started_at: float = 0.0

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
            if not running and self._proc is not None:
                self._proc = None
                self._port = 0
                self._token = ""
                self._manifest = None
            return {
                "running": running,
                "url": f"http://127.0.0.1:{self._port}" if running and self._port else "",
                "port": self._port if running else 0,
                "adapter_path": self._adapter_path if running else "",
                "manifest": dict(self._manifest) if running and self._manifest else None,
                "script_available": self.frozen or self.script_path.is_file(),
                "isolation": "loopback_only_stripped_env_per_session_token",
            }

    def active_resolver_config(self) -> dict[str, Any] | None:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None or not self._port or not self._token:
                return None
            return {
                "id": "sidecar",
                "name": str((self._manifest or {}).get("name") or "Isolated Sidecar Resolver"),
                "url": f"http://127.0.0.1:{self._port}",
                "api_key": self._token,
                "enabled": True,
                "is_sidecar": True,
                "capabilities": list((self._manifest or {}).get("capabilities") or ["search", "resolve", "lyric"]),
            }

    def start(self, *, adapter_path: str = "", port: int = 0, timeout_sec: float = 8.0) -> dict[str, Any]:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self.status()
            if not self.frozen and not self.script_path.is_file():
                raise AppError("SIDECAR_SCRIPT_MISSING", "未找到 Sidecar 启动脚本 scripts/pruner_sidecar.py", 404)
            cleaned_adapter = str(adapter_path or "").strip()
            if cleaned_adapter:
                candidate = Path(cleaned_adapter).expanduser().resolve()
                if not candidate.is_file() or candidate.suffix.lower() != ".py":
                    raise AppError("SIDECAR_ADAPTER_INVALID", "Sidecar 适配器必须是已存在的本地 .py 文件", 400)
                cleaned_adapter = str(candidate)

            try:
                requested_port = int(port or 0)
            except (TypeError, ValueError) as exc:
                raise AppError("SIDECAR_PORT_INVALID", "Sidecar 端口必须是整数（0 或 1024-65535）", 400) from exc
            if requested_port != 0 and not (1024 <= requested_port <= 65535):
                raise AppError("SIDECAR_PORT_INVALID", "Sidecar 端口必须在 1024-65535 之间（或 0 自动分配）", 400)

            token = secrets.token_urlsafe(32)
            cmd = [sys.executable, "--sidecar" if self.frozen else str(self.script_path), "--host", "127.0.0.1", "--port", str(requested_port)]
            if cleaned_adapter:
                cmd.extend(["--adapter", cleaned_adapter])

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=build_sanitized_sidecar_env(),
                creationflags=creationflags,
            )
            assert proc.stdin is not None and proc.stdout is not None
            # Pass secret token over stdin (never in process command-line arguments).
            try:
                proc.stdin.write((json.dumps({"token": token}) + "\n").encode("utf-8"))
                proc.stdin.flush()
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

            first_line_queue: queue.Queue[str] = queue.Queue(maxsize=1)
            stderr_tail: list[bytes] = []

            def _drain_stdout(stream: Any) -> None:
                sent = False
                try:
                    for raw_line in iter(stream.readline, b""):
                        if not sent:
                            sent = True
                            try:
                                first_line_queue.put_nowait(raw_line.decode("utf-8", errors="ignore").strip())
                            except queue.Full:
                                pass
                except Exception:
                    pass
                finally:
                    if not sent:
                        try:
                            first_line_queue.put_nowait("")
                        except queue.Full:
                            pass
                    try:
                        stream.close()
                    except Exception:
                        pass

            def _drain_stderr(stream: Any) -> None:
                captured = 0
                try:
                    while True:
                        chunk = stream.read(4096)
                        if not chunk:
                            break
                        if captured < 8192:
                            stderr_tail.append(chunk[: 8192 - captured])
                            captured += len(chunk)
                except Exception:
                    pass
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass

            threading.Thread(target=_drain_stdout, args=(proc.stdout,), name="pruner-sidecar-stdout", daemon=True).start()
            if proc.stderr is not None:
                threading.Thread(target=_drain_stderr, args=(proc.stderr,), name="pruner-sidecar-stderr", daemon=True).start()

            def _cleanup_failed_proc() -> None:
                try:
                    if proc.poll() is None:
                        proc.terminate()
                        proc.wait(timeout=3.0)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass

            effective_timeout = max(0.5, float(timeout_sec))
            try:
                ready_line = first_line_queue.get(timeout=effective_timeout)
            except queue.Empty:
                _cleanup_failed_proc()
                raise AppError("SIDECAR_START_TIMEOUT", "Sidecar 启动超时", 504) from None

            if not ready_line:
                _cleanup_failed_proc()
                err = b"".join(stderr_tail).decode("utf-8", errors="ignore").strip()
                raise AppError("SIDECAR_START_FAILED", f"Sidecar 启动失败：{err or f'exit={proc.returncode}'}", 500)
            try:
                ready = json.loads(ready_line)
                bound_port = int(ready["port"])
            except Exception as exc:
                _cleanup_failed_proc()
                raise AppError("SIDECAR_HANDSHAKE_INVALID", "Sidecar 握手输出格式无效", 500) from exc

            # Probe /manifest using our secret token to confirm health.
            manifest: dict[str, Any] = {}
            try:
                with httpx.Client(trust_env=False, timeout=3.0) as client:
                    resp = isolate_cookie_jar(client).get(
                        f"http://127.0.0.1:{bound_port}/manifest",
                        headers={"X-Resolver-Token": token},
                    )
                    if resp.status_code == 200:
                        manifest = resp.json()
                    if manifest.get("protocol_version") != 2:
                        raise ValueError("invalid resolver manifest")
            except Exception as exc:
                _cleanup_failed_proc()
                raise AppError("SIDECAR_HANDSHAKE_INVALID", "Sidecar 身份验证失败", 502) from exc

            self._proc = proc
            self._port = bound_port
            self._token = token
            self._adapter_path = cleaned_adapter
            self._manifest = manifest or {"protocol_version": 2, "id": "sidecar", "name": "Pruner Sidecar Bridge"}
            self._started_at = time.monotonic()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            proc = self._proc
            self._proc = None
            self._port = 0
            self._token = ""
            self._adapter_path = ""
            self._manifest = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=4.0)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
        return self.status()
