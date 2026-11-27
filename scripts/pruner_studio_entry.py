from __future__ import annotations

import sys

def _configure_dpi():
    if sys.platform != "win32":
        return
    import ctypes

    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def _notify_already_running() -> None:
    from pruner_studio.config import Settings
    from pruner_studio.desktop import activate_existing_window, wake_running_instance

    if wake_running_instance(Settings.default().data_dir) or activate_existing_window("Pruner Studio"):
        raise SystemExit(0) from None
    message = "Pruner Studio is already running for this data directory."
    if sys.stderr is not None:
        raise SystemExit(message) from None
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                "同一数据目录已有 Pruner Studio 实例正在运行。",
                "Pruner Studio",
                0x40,
            )
        except Exception:
            pass
    raise SystemExit(1) from None


def main() -> None:
    if sys.argv[1:2] == ["--sidecar"]:
        from pruner_studio.sidecar_runner import frozen_main
        raise SystemExit(frozen_main())
    _configure_dpi()
    from pruner_studio.app import serve
    from pruner_studio.errors import AppError

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
            _notify_already_running()
        raise


if __name__ == "__main__":
    main()
