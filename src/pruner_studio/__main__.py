from __future__ import annotations

import sys

def main() -> None:
    if sys.argv[1:2] == ["--sidecar"]:
        from .sidecar_runner import frozen_main
        raise SystemExit(frozen_main())
    from .app import serve
    from .config import Settings
    from .desktop import activate_existing_window, wake_running_instance
    from .errors import AppError

    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                try:
                    stream.reconfigure(encoding="utf-8")
                except Exception:
                    pass
    try:
        serve()
    except AppError as exc:
        if exc.code == "INSTANCE_ALREADY_RUNNING":
            if wake_running_instance(Settings.default().data_dir) or activate_existing_window("Pruner Studio"):
                raise SystemExit(0) from None
            raise SystemExit("Pruner Studio is already running for this data directory.") from None
        raise


if __name__ == "__main__":
    main()
