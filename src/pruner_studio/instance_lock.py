from __future__ import annotations

import errno
import os
from pathlib import Path
from types import TracebackType

from .errors import AppError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class DataDirectoryLock:
    """Process-wide ownership gate for one canonical runtime data directory."""

    FILE_NAME = ".instance.lock"

    def __init__(self, data_dir: Path):
        canonical = os.path.normcase(os.path.realpath(os.path.expanduser(os.fspath(data_dir))))
        self.data_dir = Path(canonical)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise RuntimeError("data directory lock is already acquired")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / self.FILE_NAME
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        fd = os.open(target, flags, 0o600)
        try:
            if os.fstat(fd).st_size < 1:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                if os.name == "nt":
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} and getattr(exc, "winerror", None) not in {33, 36}:
                    raise
                raise AppError(
                    "INSTANCE_ALREADY_RUNNING",
                    "同一数据目录已有 Pruner Studio 实例正在运行",
                    409,
                ) from exc
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        unlock_error: OSError | None = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            unlock_error = exc
        finally:
            os.close(fd)
        if unlock_error is not None:
            raise unlock_error

    def __enter__(self) -> "DataDirectoryLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
