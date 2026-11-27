from __future__ import annotations

import ctypes
import json
import logging
import math
import subprocess
import tempfile

import httpx
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

import uvicorn

from .channel_auth import CHANNEL_AUTH_CONFIG, extract_channel_auth_cookies
from .config import bundled_web_dir

_LOGGER = logging.getLogger(__name__)
APP_USER_MODEL_ID = "PrunerStudio.Desktop"

_IMAGE_ICON = 1
_LR_LOADFROMFILE = 0x0010
_WM_SETICON = 0x0080
_ICON_SMALL = 0
_ICON_BIG = 1
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1
_SM_CXICON = 11
_SM_CYICON = 12
_SM_CXSMICON = 49
_SM_CYSMICON = 50
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SPI_GETWORKAREA = 0x0030
_MONITOR_DEFAULTTONEAREST = 2

_LYRICS_DEFAULT_WIDTH = 820
_LYRICS_DEFAULT_HEIGHT = 120
_LYRICS_MIN_WIDTH = 360
_LYRICS_MAX_WIDTH = 1600
_LYRICS_MIN_HEIGHT = 64
_LYRICS_MAX_HEIGHT = 360

# Win32 ShowWindow constants
_SW_SHOWNOACTIVATE = 4
_SW_SHOW = 5
_SW_RESTORE = 9

# Win32 window styles for click-through & non-activating floating desktop lyrics
_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_WS_SYSMENU = 0x00080000
_WS_EX_TRANSPARENT = 0x00000020
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_WS_EX_LAYERED = 0x00080000
_WS_EX_NOACTIVATE = 0x08000000
_LWA_ALPHA = 0x00000002
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_SWP_SHOWWINDOW = 0x0040

_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_DONOTROUND = 1
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMSBT_NONE = 1
_DWMSBT_TRANSIENTWINDOW = 3
_VK_LBUTTON = 0x01

_VALID_RESIZE_EDGES = frozenset({
    "move",
    "left",
    "right",
    "top",
    "bottom",
    "top-left",
    "top-right",
    "bottom-left",
    "bottom-right",
})

# Win32 RegisterHotKey constants (Ctrl+Alt+Right/Left/P/L/D)
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012

HOTKEY_BINDINGS: tuple[tuple[int, int, str], ...] = (
    (1, 0x27, "next"),                          # Ctrl+Alt+Right
    (2, 0x25, "prev"),                          # Ctrl+Alt+Left
    (3, ord("P"), "toggle_play"),               # Ctrl+Alt+P
    (4, ord("L"), "toggle_like"),               # Ctrl+Alt+L
    (6, ord("U"), "unlock_desktop_lyrics"),
    (5, ord("D"), "toggle_desktop_lyrics"),     # Ctrl+Alt+D
    (7, 0x26, "volume_up"),                     # Ctrl+Alt+Up
    (8, 0x28, "volume_down"),                   # Ctrl+Alt+Down
    (9, ord("M"), "toggle_mute"),               # Ctrl+Alt+M
    (10, ord("K"), "toggle_desktop_lyrics_lock"),  # Ctrl+Alt+K
    (11, ord("G"), "toggle_game_mode"),         # Ctrl+Alt+G
)
HOTKEY_ACTION_BY_ID: dict[int, str] = {hk_id: action for hk_id, _vk, action in HOTKEY_BINDINGS}


def trim_process_working_set() -> bool:
    """Ask Windows to trim unused pages from the current process working set when idle/minimized."""
    if sys.platform != "win32":
        return False
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessWorkingSetSizeEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_size_t,
            ctypes.c_size_t,
            wintypes.DWORD,
        ]
        kernel32.SetProcessWorkingSetSizeEx.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        return bool(kernel32.SetProcessWorkingSetSizeEx(handle, ctypes.c_size_t(-1), ctypes.c_size_t(-1), 0))
    except Exception:
        return False


def resolve_brand_icon_paths(web_dir: Path | None = None) -> tuple[Path | None, Path | None]:
    """Resolve bundled brand icon.ico and icon.png paths (works in source & PyInstaller _MEIPASS)."""
    base = web_dir or bundled_web_dir()
    assets_dir = base / "assets"
    ico_candidate = assets_dir / "icon.ico"
    png_candidate = assets_dir / "icon.png"
    ico_path = ico_candidate if ico_candidate.is_file() else None
    png_path = png_candidate if png_candidate.is_file() else None
    return ico_path, png_path


def set_windows_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Assign an explicit Windows AppUserModelID so taskbar pins/icons detach from python.exe."""
    if sys.platform != "win32" or not app_id:
        return False
    try:
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        hr = shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return hr == 0
    except Exception:
        return False


def activate_existing_window(title: str = "Pruner Studio") -> bool:
    """Restore and focus an already-running Pruner Studio window (including when hidden to tray)."""
    if sys.platform != "win32" or not isinstance(title, str) or not title.strip():
        return False
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        hwnd = user32.FindWindowW(None, title.strip())
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, _SW_SHOW)
        user32.ShowWindow(hwnd, _SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _apply_winforms_native_icon(window: Any, ico_path: Path | None) -> bool:
    """Apply System.Drawing.Icon directly to a pywebview WinForms Form when available."""
    if sys.platform != "win32" or ico_path is None or not ico_path.is_file():
        return False
    native = getattr(window, "native", None)
    if native is None:
        return False
    try:
        from System.Drawing import Icon

        icon_obj = Icon(str(ico_path))
        if getattr(native, "InvokeRequired", False) and hasattr(native, "BeginInvoke"):
            from System import Action

            native.BeginInvoke(Action(lambda: setattr(native, "Icon", icon_obj)))
        else:
            native.Icon = icon_obj
        return True
    except Exception:
        return False


def _lookup_window_hwnd(window: Any) -> int:
    """Safely extract Win32 HWND from a pywebview WinForms window (with FindWindowW fallback)."""
    native = getattr(window, "native", None)
    handle = getattr(native, "Handle", None)
    if handle is not None:
        for attr in ("ToInt64", "ToInt32"):
            getter = getattr(handle, attr, None)
            if callable(getter):
                try:
                    value = int(getter())
                    if value:
                        return value
                except Exception:
                    continue
        try:
            value = int(handle)
            if value:
                return value
        except Exception:
            pass

    if sys.platform == "win32":
        title = str(getattr(window, "title", "") or "")
        if not title:
            return 0
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.FindWindowW.restype = wintypes.HWND
            hwnd = user32.FindWindowW(None, title)
            return int(hwnd) if hwnd else 0
        except Exception:
            return 0
    return 0


def set_win32_click_through(hwnd: int, locked: bool, *, preserve_transparency: bool = True) -> bool:
    """Toggle WS_EX_TRANSPARENT click-through on a Win32 floating window without stealing focus or clobbering alpha."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        is_64 = ctypes.sizeof(ctypes.c_void_p) == 8
        get_wl = user32.GetWindowLongPtrW if is_64 and hasattr(user32, "GetWindowLongPtrW") else user32.GetWindowLongW
        set_wl = user32.SetWindowLongPtrW if is_64 and hasattr(user32, "SetWindowLongPtrW") else user32.SetWindowLongW
        long_ptr = ctypes.c_ssize_t if is_64 else ctypes.c_long

        get_wl.argtypes = [wintypes.HWND, ctypes.c_int]
        get_wl.restype = long_ptr
        set_wl.argtypes = [wintypes.HWND, ctypes.c_int, long_ptr]
        set_wl.restype = long_ptr

        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL

        get_layer = user32.GetLayeredWindowAttributes
        get_layer.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.COLORREF),
                              ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(wintypes.DWORD)]
        get_layer.restype = wintypes.BOOL
        set_layer = user32.SetLayeredWindowAttributes
        set_layer.argtypes = [wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD]
        set_layer.restype = wintypes.BOOL

        ex_style = int(get_wl(hwnd, _GWL_EXSTYLE))
        had_layered = bool(ex_style & _WS_EX_LAYERED)
        new_style = ex_style | _WS_EX_NOACTIVATE
        if locked:
            new_style |= _WS_EX_LAYERED | _WS_EX_TRANSPARENT
        elif preserve_transparency:
            new_style &= ~(_WS_EX_TRANSPARENT | _WS_EX_LAYERED)
        else:
            new_style = (new_style | _WS_EX_LAYERED) & ~_WS_EX_TRANSPARENT
        set_last_err = getattr(ctypes, "set_last_error", None)
        get_last_err = getattr(ctypes, "get_last_error", None)
        if callable(set_last_err):
            set_last_err(0)
        prev_style = set_wl(hwnd, _GWL_EXSTYLE, new_style)
        if prev_style == 0 and callable(get_last_err) and get_last_err() != 0:
            return False
        if preserve_transparency and locked:
            color, alpha, flags = wintypes.COLORREF(), ctypes.c_ubyte(), wintypes.DWORD()
            valid = get_layer(hwnd, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags))
            if not had_layered or not valid or not flags.value or (flags.value & _LWA_ALPHA and not alpha.value):
                if not set_layer(hwnd, 0, 255, _LWA_ALPHA):
                    return False
        elif not preserve_transparency and not set_layer(hwnd, 0, 236, _LWA_ALPHA):
            return False
        ok = user32.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED | _SWP_NOACTIVATE,
        )
        return bool(ok)
    except Exception:
        return False


def compute_min_lyrics_height(style: dict[str, Any] | None = None) -> int:
    """Compute the minimum client height (DIPs) needed so rendered lyrics never clip."""
    st = style if isinstance(style, dict) else {}
    raw_fs = st.get("font_size", 28)
    try:
        font_size = max(18, min(48, int(raw_fs))) if not isinstance(raw_fs, bool) else 28
    except (TypeError, ValueError):
        font_size = 28
    mode = str(st.get("mode") or "double").strip().lower()
    if mode not in {"single", "double"}:
        mode = "double"
    show_trans = st.get("show_translation")
    if not isinstance(show_trans, bool):
        show_trans = True

    primary_h = math.ceil(font_size * 1.35)
    if mode == "single" and not show_trans:
        needed = max(72, primary_h + 34)
    elif show_trans:
        secondary_h = math.ceil(max(14.0, font_size * 0.65) * 1.35)
        needed = max(108, primary_h + secondary_h + 38)
    else:
        needed = max(116, primary_h * 2 + 38)
    return max(_LYRICS_MIN_HEIGHT, min(_LYRICS_MAX_HEIGHT, int(needed)))


def _clamp_lyrics_bounds(
    raw: dict[str, Any] | None,
    *,
    min_height: int = _LYRICS_MIN_HEIGHT,
) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    if any(k not in raw for k in ("x", "y", "width", "height")):
        return None
    try:
        x = int(raw["x"])
        y = int(raw["y"])
        width = int(raw["width"])
        height = int(raw["height"])
    except (TypeError, ValueError):
        return None
    if any(isinstance(raw[k], bool) for k in ("x", "y", "width", "height")):
        return None
    eff_min_h = max(_LYRICS_MIN_HEIGHT, min(_LYRICS_MAX_HEIGHT, int(min_height)))
    return {
        "x": max(-16384, min(16384, x)),
        "y": max(-16384, min(16384, y)),
        "width": max(_LYRICS_MIN_WIDTH, min(_LYRICS_MAX_WIDTH, width)),
        "height": max(eff_min_h, min(_LYRICS_MAX_HEIGHT, height)),
    }


def _primary_work_area():
    left, top, right, bottom = 0, 0, 1280, 800
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            rect = wintypes.RECT()
            user32.SystemParametersInfoW.argtypes = [
                wintypes.UINT,
                wintypes.UINT,
                ctypes.c_void_p,
                wintypes.UINT,
            ]
            user32.SystemParametersInfoW.restype = wintypes.BOOL
            if user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                if rect.right > rect.left and rect.bottom > rect.top:
                    left, top, right, bottom = int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
            else:
                user32.GetSystemMetrics.argtypes = [ctypes.c_int]
                user32.GetSystemMetrics.restype = ctypes.c_int
                sw = int(user32.GetSystemMetrics(_SM_CXSCREEN) or 0)
                sh = int(user32.GetSystemMetrics(_SM_CYSCREEN) or 0)
                if sw > 0 and sh > 0:
                    right, bottom = sw, sh
        except Exception:
            pass
    return left, top, right, bottom


def _virtual_screen_bounds() -> tuple[int, int, int, int]:
    if sys.platform == "win32":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetSystemMetrics.argtypes = [ctypes.c_int]
            user32.GetSystemMetrics.restype = ctypes.c_int
            vx = int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN))
            vy = int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN))
            vw = int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN))
            vh = int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN))
            if vw > 0 and vh > 0:
                return vx, vy, vx + vw, vy + vh
        except Exception:
            pass
    return _primary_work_area()


def _window_dpi_scale(hwnd: int = 0) -> float:
    if sys.platform != "win32":
        return 1.0
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dpi = 0
        if hwnd and hasattr(user32, "GetDpiForWindow"):
            dpi = int(user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) or 0)
        elif hasattr(user32, "GetDpiForSystem"):
            dpi = int(user32.GetDpiForSystem() or 0)
        if dpi >= 96:
            return max(1.0, float(dpi) / 96.0)
    except Exception:
        pass
    return 1.0


def _monitor_work_area_for_hwnd(hwnd: int) -> tuple[int, int, int, int]:
    """Return physical-pixel rcWork (left, top, right, bottom) for the monitor containing hwnd."""
    if sys.platform == "win32" and hwnd:
        try:
            from ctypes import wintypes

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
            user32.MonitorFromWindow.restype = ctypes.c_void_p
            user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            hmon = user32.MonitorFromWindow(wintypes.HWND(hwnd), _MONITOR_DEFAULTTONEAREST)
            if hmon:
                mi = _MONITORINFO()
                mi.cbSize = ctypes.sizeof(_MONITORINFO)
                if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    rc = mi.rcWork
                    if rc.right > rc.left and rc.bottom > rc.top:
                        return int(rc.left), int(rc.top), int(rc.right), int(rc.bottom)
        except Exception:
            pass
    return _primary_work_area()


def _clamp_bounds_to_visible(
    bounds: dict[str, int],
    *,
    min_height: int = _LYRICS_MIN_HEIGHT,
) -> dict[str, int]:
    """Preserve valid multi-monitor coordinates while pulling completely off-screen bars back."""
    clamped = _clamp_lyrics_bounds(bounds, min_height=min_height) or _default_lyrics_bounds(height=min_height)
    if sys.platform != "win32":
        return clamped
    vx, vy, vr, vb = _virtual_screen_bounds()
    scale = _window_dpi_scale(0)
    lvx = int(round(vx / scale))
    lvy = int(round(vy / scale))
    lvr = int(round(vr / scale))
    lvb = int(round(vb / scale))
    w, h = clamped["width"], clamped["height"]
    x, y = clamped["x"], clamped["y"]
    if x + w < lvx + 80 or x > lvr - 80 or y + h < lvy + 40 or y > lvb - 40:
        fallback = _default_lyrics_bounds(width=w, height=h)
        clamped["x"] = fallback["x"]
        clamped["y"] = fallback["y"]
    return clamped


def _default_lyrics_bounds(
    width: int = _LYRICS_DEFAULT_WIDTH,
    height: int = _LYRICS_DEFAULT_HEIGHT,
) -> dict[str, int]:
    """Return bottom-center primary monitor work-area bounds (logical DIPs) for desktop lyrics."""
    w = max(_LYRICS_MIN_WIDTH, min(_LYRICS_MAX_WIDTH, int(width)))
    h = max(_LYRICS_MIN_HEIGHT, min(_LYRICS_MAX_HEIGHT, int(height)))
    left, top, right, bottom = _primary_work_area()
    scale = _window_dpi_scale(0)
    ll = int(round(left / scale))
    lt = int(round(top / scale))
    lr = int(round(right / scale))
    lb = int(round(bottom / scale))
    work_w = max(w, lr - ll)
    x = ll + (work_w - w) // 2
    y = max(lt, lb - h - 28)
    return {"x": int(x), "y": int(y), "width": w, "height": h}


def apply_win32_window_icons(hwnd: int, ico_path: Path | None) -> list[int]:
    """Apply ICON_SMALL and ICON_BIG via Win32 LoadImageW + WM_SETICON; returns HICON handles."""
    if sys.platform != "win32" or not hwnd or ico_path is None or not ico_path.is_file():
        return []
    handles: list[int] = []
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.LoadImageW.argtypes = [
            wintypes.HINSTANCE,
            wintypes.LPCWSTR,
            wintypes.UINT,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.SendMessageW.restype = ctypes.c_ssize_t

        small_cx = user32.GetSystemMetrics(_SM_CXSMICON) or 16
        small_cy = user32.GetSystemMetrics(_SM_CYSMICON) or 16
        big_cx = user32.GetSystemMetrics(_SM_CXICON) or 32
        big_cy = user32.GetSystemMetrics(_SM_CYICON) or 32

        ico_str = str(ico_path)
        small_icon = user32.LoadImageW(None, ico_str, _IMAGE_ICON, small_cx, small_cy, _LR_LOADFROMFILE)
        if small_icon:
            user32.SendMessageW(hwnd, _WM_SETICON, _ICON_SMALL, small_icon)
            handles.append(int(small_icon))

        big_icon = user32.LoadImageW(None, ico_str, _IMAGE_ICON, big_cx, big_cy, _LR_LOADFROMFILE)
        if big_icon:
            user32.SendMessageW(hwnd, _WM_SETICON, _ICON_BIG, big_icon)
            handles.append(int(big_icon))
    except Exception:
        pass
    return handles


def destroy_win32_icons(handles: list[int]) -> None:
    if sys.platform != "win32" or not handles:
        return
    try:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.DestroyIcon.argtypes = [wintypes.HICON]
        user32.DestroyIcon.restype = wintypes.BOOL
        for handle in handles:
            if handle:
                user32.DestroyIcon(handle)
    except Exception:
        pass


def apply_win32_titlebar_theme(hwnd: int, dark: bool) -> bool:
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        apply = ctypes.windll.dwmapi.DwmSetWindowAttribute
        value = ctypes.c_int(bool(dark))
        return any(apply(ctypes.c_void_p(hwnd), attr, ctypes.byref(value), 4) == 0 for attr in (20, 19))
    except (AttributeError, OSError):
        return False


def apply_win32_lyrics_backdrop(
    hwnd: int,
    *,
    surface_style: str = "borderless",
    game_mode: bool = False,
) -> bool:
    """Clear WinForms Mica (or enable Win11 Acrylic on 22621+ when explicitly requested); returns acrylic active flag."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        dwm = ctypes.windll.dwmapi
        want_acrylic = surface_style == "acrylic" and not game_mode and sys.getwindowsversion().build >= 22621
        if want_acrylic:
            hr = dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                _DWMWA_SYSTEMBACKDROP_TYPE,
                ctypes.byref(ctypes.c_int(_DWMSBT_TRANSIENTWINDOW)),
                4,
            )
            if hr == 0:
                return True
        dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            _DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(ctypes.c_int(_DWMSBT_NONE)),
            4,
        )
    except Exception:
        pass
    return False


def _configure_win32_seamless_overlay(
    hwnd: int,
    *,
    surface_style: str = "borderless",
    game_mode: bool = False,
) -> dict[str, Any]:
    if sys.platform != "win32" or not hwnd:
        return {"acrylic_active": False}
    # WinForms ShowInTaskbar recreates the HWND during WebView2 initialization.
    # Update the existing frameless tool window in place to keep its controller alive,
    # apply WS_EX_NOACTIVATE so opening/showing/interacting never steals focus from games,
    # and strip WinForms' default Mica backdrop on transparent desktop lyrics.
    handle = ctypes.c_void_p(hwnd)
    user32 = ctypes.windll.user32
    style = user32.GetWindowLongW(handle, _GWL_EXSTYLE)
    user32.SetWindowLongW(
        handle,
        _GWL_EXSTYLE,
        (style | _WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE) & ~_WS_EX_APPWINDOW,
    )
    window_style = user32.GetWindowLongW(handle, _GWL_STYLE)
    user32.SetWindowLongW(handle, _GWL_STYLE, window_style & ~_WS_SYSMENU)
    dwm = ctypes.windll.dwmapi
    dwm.DwmExtendFrameIntoClientArea(handle, (ctypes.c_int * 4)(-1, -1, -1, -1))
    dwm.DwmSetWindowAttribute(handle, _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(ctypes.c_int(_DWMWCP_DONOTROUND)), 4)
    acrylic_active = apply_win32_lyrics_backdrop(hwnd, surface_style=surface_style, game_mode=game_mode)
    return {"acrylic_active": acrylic_active}


_SWP_SHOWWINDOW = 0x0040
_SW_HIDE = 0

def _set_webview_controller_visible(webview_ctrl: Any, visible: bool) -> bool:
    """Safely synchronize WebView2 CoreWebView2Controller.IsVisible via WinForms reflection."""
    if webview_ctrl is None:
        return False
    try:
        from System.Reflection import BindingFlags

        field = webview_ctrl.GetType().GetField(
            "_coreWebView2Controller", BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public
        )
        controller = field.GetValue(webview_ctrl) if field is not None else None
        if controller is not None:
            controller.IsVisible = bool(visible)
            if visible and hasattr(controller, "NotifyParentWindowPositionChanged"):
                controller.NotifyParentWindowPositionChanged()
            return True
    except Exception:
        _LOGGER.debug("WebView2 controller visibility update skipped", exc_info=True)
    return False


class _LyricsJsApi:
    """Thin pywebview JS-API bridge for zero-latency drag, 8-edge resize, fit & snap."""

    def __init__(self, controller: DesktopLyricsController):
        self._controller = controller

    def start_drag(self) -> bool:
        return self._controller.start_interactive_geometry("move")

    def start_resize(self, edge: str) -> bool:
        return self._controller.start_interactive_geometry(str(edge or ""))

    def ensure_min_height(self, height_px: int) -> dict[str, Any]:
        return self._controller.ensure_min_height(int(height_px))

    def snap_center(self) -> dict[str, int] | None:
        return self._controller.snap_center()


class DesktopLyricsController:
    """Manages the on-demand frameless always-on-top transparent mini-lyrics window."""

    WINDOW_TITLE = "Pruner Studio 桌面歌词"

    def __init__(
        self,
        base_url: str = "",
        webview_module: Any = None,
        on_state_change: Callable[[dict[str, Any]], None] | None = None,
        bounds_provider: Callable[[], dict[str, Any] | None] | None = None,
        bounds_saver: Callable[[dict[str, int]], None] | None = None,
        bounds_debounce_sec: float = 0.5,
        style_provider: Callable[[], dict[str, Any] | None] | None = None,
        game_mode_provider: Callable[[], bool] | None = None,
    ):
        self.window_url = lambda url: url
        self.base_url = base_url.rstrip("/")
        self._webview = webview_module
        self._on_state_change = on_state_change
        self._bounds_provider = bounds_provider
        self._bounds_saver = bounds_saver
        self._style_provider = style_provider
        self._game_mode_provider = game_mode_provider
        self._bounds_debounce_sec = max(0.01, float(bounds_debounce_sec))
        self._lock = threading.RLock()
        self._window: Any = None
        self._open = False
        self._want_visible = False
        self._locked = False
        self._is_transparent = True
        self._acrylic_active = False
        self._calibrating = False
        self._interactive_drag_active = False
        self._interaction_timer: Any = None
        self._last_bounds: dict[str, int] | None = None
        self._intended_bounds: dict[str, int] | None = None
        self._pending_bounds: dict[str, int] | None = None
        self._bounds_timer: threading.Timer | None = None

    def attach_runtime(self, base_url: str, webview_module: Any) -> None:
        with self._lock:
            self.base_url = base_url.rstrip("/")
            self._webview = webview_module

    def _current_style(self) -> dict[str, Any]:
        if callable(self._style_provider):
            try:
                loaded = self._style_provider()
                if isinstance(loaded, dict):
                    return dict(loaded)
            except Exception:
                pass
        return {}

    def _current_game_mode(self) -> bool:
        if callable(self._game_mode_provider):
            try:
                return bool(self._game_mode_provider())
            except Exception:
                pass
        return False

    def _min_lyrics_height(self) -> int:
        return compute_min_lyrics_height(self._current_style())

    def state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "open": self._open,
                "locked": self._locked,
                "supported": self._webview is not None,
                "transparent": self._is_transparent,
                "acrylic_active": self._acrylic_active,
            }

    def _resolve_initial_bounds(self) -> dict[str, int]:
        min_h = self._min_lyrics_height()
        if callable(self._bounds_provider):
            try:
                raw = self._bounds_provider()
                loaded = _clamp_bounds_to_visible(raw, min_height=min_h) if isinstance(raw, dict) else None
                if loaded is not None:
                    return loaded
            except Exception:
                pass
        return _default_lyrics_bounds(_LYRICS_DEFAULT_WIDTH, max(_LYRICS_DEFAULT_HEIGHT, min_h))

    def _extract_window_bounds(
        self,
        win: Any,
        *,
        override_xy: tuple[Any, Any] | None = None,
        override_wh: tuple[Any, Any] | None = None,
    ) -> dict[str, int] | None:
        min_h = self._min_lyrics_height()
        with self._lock:
            base = dict(self._last_bounds or _default_lyrics_bounds(height=min_h))
        for attr in ("x", "y", "width", "height"):
            val = getattr(win, attr, None)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                base[attr] = int(val)
        if override_xy is not None:
            ox, oy = override_xy
            if isinstance(ox, (int, float)) and not isinstance(ox, bool):
                base["x"] = int(ox)
            if isinstance(oy, (int, float)) and not isinstance(oy, bool):
                base["y"] = int(oy)
        if override_wh is not None:
            ow, oh = override_wh
            if isinstance(ow, (int, float)) and not isinstance(ow, bool):
                base["width"] = int(ow)
            if isinstance(oh, (int, float)) and not isinstance(oh, bool):
                base["height"] = int(oh)
        return _clamp_lyrics_bounds(base, min_height=min_h)

    def _schedule_bounds_save(self, bounds: dict[str, int] | None) -> None:
        if bounds is None:
            return
        with self._lock:
            self._last_bounds = dict(bounds)
            self._intended_bounds = dict(bounds)
            if not callable(self._bounds_saver):
                return
            self._pending_bounds = dict(bounds)
            if self._bounds_timer is not None:
                self._bounds_timer.cancel()
            timer = threading.Timer(self._bounds_debounce_sec, self._flush_bounds_save)
            timer.daemon = True
            self._bounds_timer = timer
            timer.start()

    def _flush_bounds_save(self) -> None:
        with self._lock:
            timer = self._bounds_timer
            self._bounds_timer = None
            pending = self._pending_bounds
            self._pending_bounds = None
            saver = self._bounds_saver
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        if pending is not None and callable(saver):
            try:
                saver(dict(pending))
            except Exception:
                pass

    def _run_on_ui_thread(self, win: Any, fn: Callable[[], None], *, wait: bool = False) -> bool:
        native = getattr(win, "native", None)
        if native is None:
            fn()
            return True
        if getattr(native, "InvokeRequired", False):
            from System import Action

            if wait:
                done = threading.Event()

                def _wrapped() -> None:
                    try:
                        fn()
                    finally:
                        done.set()

                native.BeginInvoke(Action(_wrapped))
                return done.wait(timeout=2.0)
            native.BeginInvoke(Action(fn))
            return True
        fn()
        return True

    @staticmethod
    def _logical_bounds_from_hwnd(hwnd: int) -> dict[str, int] | None:
        if sys.platform != "win32" or not hwnd:
            return None
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            rect = wintypes.RECT()
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.GetWindowRect.restype = wintypes.BOOL
            if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                return None
            scale = _window_dpi_scale(hwnd)
            return {
                "x": int(round(rect.left / scale)),
                "y": int(round(rect.top / scale)),
                "width": int(round((rect.right - rect.left) / scale)),
                "height": int(round((rect.bottom - rect.top) / scale)),
            }
        except Exception:
            return None

    def _stop_interaction_timer(self, *, on_ui_thread: bool = True) -> None:
        with self._lock:
            timer = self._interaction_timer
            self._interaction_timer = None
            self._interactive_drag_active = False
            win = self._window
        if timer is None:
            return

        def _dispose() -> None:
            try:
                timer.Stop()
                timer.Dispose()
            except Exception:
                pass

        if on_ui_thread or win is None:
            _dispose()
        else:
            try:
                self._run_on_ui_thread(win, _dispose, wait=False)
            except Exception:
                pass

    def _calibrate_winforms_window(
        self,
        win: Any,
        bounds: dict[str, int],
        *,
        transparent_flag: bool,
        locked_flag: bool = False,
    ) -> None:
        native = getattr(win, "native", None)
        if native is None or sys.platform != "win32":
            return

        style = self._current_style()
        game_mode = self._current_game_mode()
        min_h = compute_min_lyrics_height(style)
        acrylic_holder = [False]

        def _calibrate() -> None:
            self._calibrating = True
            try:
                hwnd = _lookup_window_hwnd(win)
                scale = _window_dpi_scale(hwnd)

                # Unsubscribe pywebview's SystemEvents listener so OS theme switches cannot re-inject Mica.
                try:
                    from Microsoft.Win32 import SystemEvents

                    if hasattr(native, "on_system_theme_changed"):
                        SystemEvents.UserPreferenceChanged -= native.on_system_theme_changed
                except Exception:
                    pass

                # Replace pywebview's Shown and NavigationStarting handlers so transparent overlay never steals focus.
                try:
                    from System import EventHandler

                    if not getattr(win, "_lyrics_reveal_installed", False) and hasattr(native, "on_shown") and hasattr(native, "Shown"):
                        native.Shown -= native.on_shown
                        native.Shown += EventHandler(lambda *_a: native.shown.set() if hasattr(native, "shown") else None)
                except Exception:
                    pass
                # Fix WinForms FormBorderStyle=None border-shrinkage bug + black DWM backing.
                try:
                    from System.Drawing import Color, Size
                    import System.Windows.Forms as WF

                    none_style = getattr(WF.FormBorderStyle, "None")
                    if native.FormBorderStyle != none_style:
                        native.FormBorderStyle = none_style
                    if transparent_flag:
                        native.BackColor = Color.Black
                    native.MinimumSize = Size(max(1, int(round(_LYRICS_MIN_WIDTH * scale))), max(1, int(round(min_h * scale))))
                    native.Size = Size(max(1, int(round(bounds["width"] * scale))), max(1, int(round(bounds["height"] * scale))))
                except Exception:
                    pass

                hwnd = _lookup_window_hwnd(win)
                if hwnd:
                    overlay_res = _configure_win32_seamless_overlay(
                        hwnd,
                        surface_style=str(style.get("surface_style") or "borderless"),
                        game_mode=game_mode,
                    )
                    acrylic_holder[0] = bool(overlay_res.get("acrylic_active"))
                    ctypes.windll.user32.SetWindowPos(
                        ctypes.c_void_p(hwnd),
                        None,
                        int(round(bounds["x"] * scale)),
                        int(round(bounds["y"] * scale)),
                        max(1, int(round(bounds["width"] * scale))),
                        max(1, int(round(bounds["height"] * scale))),
                        _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
                    )
                    set_win32_click_through(hwnd, locked_flag, preserve_transparency=transparent_flag)

                browser = getattr(native, "browser", None)
                webview_ctrl = getattr(native, "webview", None) or getattr(browser, "webview", None)
                if webview_ctrl is None or getattr(win, "_lyrics_reveal_installed", False):
                    return

                def _configure_webview(*_args):
                    if transparent_flag:
                        webview_ctrl.DefaultBackgroundColor = Color.FromArgb(0, 0, 0, 0)
                    core = webview_ctrl.CoreWebView2
                    if core is not None:
                        core.Settings.IsZoomControlEnabled = False

                def _reveal_noactivate(*_args):
                    with self._lock:
                        want = self._window is win and self._want_visible
                    if not want or getattr(native, "IsDisposed", False):
                        return
                    live_hwnd = _lookup_window_hwnd(win)
                    if not live_hwnd:
                        return
                    handle = ctypes.c_void_p(live_hwnd)
                    user32 = ctypes.windll.user32
                    native.TopMost = True
                    user32.SetWindowPos(handle, ctypes.c_void_p(-1), 0, 0, 0, 0,
                                        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW)
                    user32.ShowWindow(handle, _SW_SHOWNOACTIVATE)
                    webview_ctrl.Visible = True
                    _set_webview_controller_visible(webview_ctrl, True)
                    set_win32_click_through(live_hwnd, self._locked, preserve_transparency=transparent_flag)

                if hasattr(browser, "on_navigation_start"):
                    webview_ctrl.NavigationStarting -= browser.on_navigation_start
                webview_ctrl.NavigationStarting += _reveal_noactivate
                webview_ctrl.NavigationCompleted += _reveal_noactivate
                webview_ctrl.CoreWebView2InitializationCompleted += _configure_webview
                win._lyrics_reveal_installed = True
                timer = WF.Timer()
                timer.Interval = 250
                timer_stopped = [False]

                def _stop_reveal_timer(*_args):
                    if timer_stopped[0]:
                        return
                    timer_stopped[0] = True
                    try:
                        timer.Stop()
                    except Exception:
                        pass
                    try:
                        native.FormClosed -= _stop_reveal_timer
                    except Exception:
                        pass
                    try:
                        timer.Dispose()
                    except Exception:
                        pass

                def _on_tick(*_args):
                    _stop_reveal_timer()
                    with self._lock:
                        want = self._window is win and self._want_visible
                    if want and not getattr(native, "IsDisposed", False):
                        live_hwnd = _lookup_window_hwnd(win)
                        if live_hwnd and not ctypes.windll.user32.IsWindowVisible(ctypes.c_void_p(live_hwnd)):
                            _reveal_noactivate()

                timer.Tick += _on_tick
                native.FormClosed += _stop_reveal_timer
                timer.Start()

                _configure_webview()
                with self._lock:
                    want_now = self._window is win and self._want_visible
                if want_now and webview_ctrl.CoreWebView2 is not None:
                    _reveal_noactivate()
                elif not want_now:
                    _stop_reveal_timer()
                    if not getattr(native, "IsDisposed", False):
                        _set_webview_controller_visible(webview_ctrl, False)
                        webview_ctrl.Visible = False
                        live_hwnd = _lookup_window_hwnd(win)
                        if live_hwnd:
                            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(live_hwnd), _SW_HIDE)
                        native.Hide()
            finally:
                self._calibrating = False

        try:
            self._run_on_ui_thread(win, _calibrate, wait=True)
            with self._lock:
                if self._window is win:
                    self._acrylic_active = acrylic_holder[0]
        except Exception:
            self._calibrating = False

    def _create_lyrics_window(self, bounds: dict[str, int]) -> tuple[Any, bool]:
        url = self.window_url(f"{self.base_url}/desktop_lyrics.html")
        min_h = self._min_lyrics_height()
        common_kwargs: dict[str, Any] = {
            "x": bounds["x"],
            "y": bounds["y"],
            "width": bounds["width"],
            "height": bounds["height"],
            "min_size": (_LYRICS_MIN_WIDTH, min_h),
            "resizable": True,
            "frameless": True,
            "on_top": True,
            "easy_drag": False,
            "focus": False,
            "js_api": _LyricsJsApi(self),
        }
        try:
            win = self._webview.create_window(
                self.WINDOW_TITLE,
                url,
                shadow=False,
                transparent=True,
                **common_kwargs,
            )
            return win, True
        except Exception as exc:
            _LOGGER.warning(
                "Desktop lyrics transparent window creation failed (%s); retrying with opaque fallback.",
                exc,
            )
            win = self._webview.create_window(
                self.WINDOW_TITLE,
                url,
                transparent=False,
                background_color="#141518",
                **common_kwargs,
            )
            return win, False

    def start_native_drag(self) -> bool:
        return self.start_interactive_geometry("move")

    def start_interactive_geometry(self, edge: str = "move") -> bool:
        clean_edge = str(edge or "move").strip().lower()
        if clean_edge not in _VALID_RESIZE_EDGES:
            return False
        with self._lock:
            win = self._window
            if self._locked or not win or sys.platform != "win32":
                return False
        hwnd = _lookup_window_hwnd(win)
        if not hwnd:
            return False
        try:
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            user32.GetAsyncKeyState.restype = ctypes.c_short
            # Guard against fast click-release race before JS->host bridge arrives.
            if (user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000) == 0:
                return False

            native = getattr(win, "native", None)
            if native is None:
                # Fallback for fake/non-WinForms stubs that supply only HWND.
                def _legacy_drag() -> None:
                    ht = {"move": 2, "left": 10, "right": 11, "top": 12, "top-left": 13,
                          "top-right": 14, "bottom": 15, "bottom-left": 16, "bottom-right": 17}[clean_edge]
                    user32.ReleaseCapture()
                    user32.SendMessageW(ctypes.c_void_p(hwnd), 0x00A1, ht, 0)

                _legacy_drag()
                return True

            def _begin_timer_drag() -> None:
                import System.Windows.Forms as WF
                from System.Drawing import Size

                self._stop_interaction_timer()
                if (user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000) == 0:
                    return
                start_pt = wintypes.POINT()
                start_rect = wintypes.RECT()
                if not user32.GetCursorPos(ctypes.byref(start_pt)):
                    return
                if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(start_rect)):
                    return

                scale = _window_dpi_scale(hwnd)
                min_w_px = max(1, int(round(_LYRICS_MIN_WIDTH * scale)))
                max_w_px = max(min_w_px, int(round(_LYRICS_MAX_WIDTH * scale)))
                min_h_px = max(1, int(round(self._min_lyrics_height() * scale)))
                max_h_px = max(min_h_px, int(round(_LYRICS_MAX_HEIGHT * scale)))

                try:
                    native.MinimumSize = Size(min_w_px, min_h_px)
                except Exception:
                    pass

                start_l, start_t = int(start_rect.left), int(start_rect.top)
                start_r, start_b = int(start_rect.right), int(start_rect.bottom)
                start_w = max(min_w_px, start_r - start_l)
                start_h = max(min_h_px, start_b - start_t)
                sx, sy = int(start_pt.x), int(start_pt.y)
                self._interactive_drag_active = True

                timer = WF.Timer()
                timer.Interval = 16
                self._interaction_timer = timer

                def _on_tick(_sender=None, _args=None) -> None:
                    with self._lock:
                        still_open = self._window is win and not self._locked
                    if not still_open or (user32.GetAsyncKeyState(_VK_LBUTTON) & 0x8000) == 0:
                        self._stop_interaction_timer()
                        final_bounds = self._logical_bounds_from_hwnd(hwnd)
                        if final_bounds is not None:
                            min_h_now = self._min_lyrics_height()
                            self._schedule_bounds_save(_clamp_bounds_to_visible(final_bounds, min_height=min_h_now))
                        return

                    cur_pt = wintypes.POINT()
                    if not user32.GetCursorPos(ctypes.byref(cur_pt)):
                        return
                    dx = int(cur_pt.x) - sx
                    dy = int(cur_pt.y) - sy

                    if clean_edge == "move":
                        nx = start_l + dx
                        ny = start_t + dy
                        cur_work = _monitor_work_area_for_hwnd(hwnd)
                        wl, wt, wr, wb = cur_work
                        snap_px = int(round(12 * scale))
                        center_x = wl + ((wr - wl) - start_w) // 2
                        if abs(nx - center_x) <= snap_px:
                            nx = center_x
                        elif abs(nx - (wl + snap_px)) <= snap_px:
                            nx = wl + snap_px
                        elif abs((nx + start_w) - (wr - snap_px)) <= snap_px:
                            nx = wr - snap_px - start_w
                        if abs(ny - (wt + snap_px)) <= snap_px:
                            ny = wt + snap_px
                        elif abs((ny + start_h) - (wb - snap_px)) <= snap_px:
                            ny = wb - snap_px - start_h
                        nw, nh = start_w, start_h
                    else:
                        nl, nt, nr, nb = start_l, start_t, start_r, start_b
                        if "left" in clean_edge:
                            nl = max(start_r - max_w_px, min(start_r - min_w_px, start_l + dx))
                        if "right" in clean_edge:
                            nr = max(start_l + min_w_px, min(start_l + max_w_px, start_r + dx))
                        if "top" in clean_edge:
                            nt = max(start_b - max_h_px, min(start_b - min_h_px, start_t + dy))
                        if "bottom" in clean_edge:
                            nb = max(start_t + min_h_px, min(start_t + max_h_px, start_b + dy))
                        nx, ny = nl, nt
                        nw = max(min_w_px, min(max_w_px, nr - nl))
                        nh = max(min_h_px, min(max_h_px, nb - nt))

                    user32.SetWindowPos(
                        wintypes.HWND(hwnd),
                        None,
                        int(nx),
                        int(ny),
                        int(nw),
                        int(nh),
                        _SWP_NOZORDER | _SWP_NOACTIVATE,
                    )

                timer.Tick += _on_tick
                timer.Start()

            self._run_on_ui_thread(win, _begin_timer_drag, wait=False)
            return True
        except Exception:
            self._stop_interaction_timer()
            return False

    def ensure_min_height(self, required_logical_height: int) -> dict[str, Any]:
        with self._lock:
            win = self._window
            if not self._open or win is None or self._interactive_drag_active:
                return {"resized": False, "bounds": dict(self._last_bounds or {})}
        min_h = max(self._min_lyrics_height(), min(_LYRICS_MAX_HEIGHT, int(required_logical_height)))
        hwnd = _lookup_window_hwnd(win)
        if not hwnd or sys.platform != "win32":
            cur = dict(self._last_bounds or _default_lyrics_bounds(height=min_h))
            if cur["height"] < min_h:
                cur["y"] = max(-16384, cur["y"] - (min_h - cur["height"]))
                cur["height"] = min_h
                self._schedule_bounds_save(cur)
                return {"resized": True, "bounds": cur}
            return {"resized": False, "bounds": cur}

        resized_holder: dict[str, Any] = {"resized": False, "bounds": dict(self._last_bounds or {})}

        def _apply_min_h() -> None:
            from ctypes import wintypes

            cur_rect = wintypes.RECT()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(cur_rect)):
                return
            scale = _window_dpi_scale(hwnd)
            cur_w_px = int(cur_rect.right - cur_rect.left)
            cur_h_px = int(cur_rect.bottom - cur_rect.top)
            target_h_px = max(1, int(round(min_h * scale)))
            native = getattr(win, "native", None)
            if native is not None:
                try:
                    from System.Drawing import Size

                    native.MinimumSize = Size(max(1, int(round(_LYRICS_MIN_WIDTH * scale))), target_h_px)
                except Exception:
                    pass
            if cur_h_px < target_h_px:
                delta_px = target_h_px - cur_h_px
                wl, wt, _wr, wb = _monitor_work_area_for_hwnd(hwnd)
                # Anchor bottom edge when bar is in the lower half of the monitor so it never pushes behind the taskbar.
                in_lower_half = (cur_rect.top + cur_rect.bottom) // 2 >= (wt + wb) // 2
                ny = max(wt, int(cur_rect.top) - delta_px) if in_lower_half else int(cur_rect.top)
                self._calibrating = True
                try:
                    if native is not None:
                        try:
                            from System.Drawing import Size

                            native.Size = Size(cur_w_px, target_h_px)
                        except Exception:
                            pass
                    user32.SetWindowPos(
                        wintypes.HWND(hwnd),
                        None,
                        int(cur_rect.left),
                        ny,
                        cur_w_px,
                        target_h_px,
                        _SWP_NOZORDER | _SWP_NOACTIVATE,
                    )
                finally:
                    self._calibrating = False
                new_bounds = self._logical_bounds_from_hwnd(hwnd)
                if new_bounds:
                    self._schedule_bounds_save(new_bounds)
                    resized_holder["resized"] = True
                    resized_holder["bounds"] = new_bounds

        self._run_on_ui_thread(win, _apply_min_h, wait=True)
        return resized_holder

    def snap_center(self) -> dict[str, int] | None:
        with self._lock:
            win = self._window
            cur_bounds = dict(self._last_bounds or self._resolve_initial_bounds())
        hwnd = _lookup_window_hwnd(win) if win is not None else 0
        if not hwnd or sys.platform != "win32":
            wl, wt, wr, _wb = _primary_work_area()
            scale = _window_dpi_scale(0)
            l_wl = int(round(wl / scale))
            l_wr = int(round(wr / scale))
            cur_bounds["x"] = l_wl + max(0, (l_wr - l_wl - cur_bounds["width"]) // 2)
            self._schedule_bounds_save(cur_bounds)
            return cur_bounds

        result_holder: dict[str, int] | None = None

        def _center() -> None:
            nonlocal result_holder
            from ctypes import wintypes

            rect = wintypes.RECT()
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
                return
            w_px = int(rect.right - rect.left)
            h_px = int(rect.bottom - rect.top)
            wl, wt, wr, _wb = _monitor_work_area_for_hwnd(hwnd)
            nx = wl + ((wr - wl) - w_px) // 2
            ny = max(wt, int(rect.top))
            self._calibrating = True
            try:
                user32.SetWindowPos(
                    wintypes.HWND(hwnd),
                    None,
                    nx,
                    ny,
                    w_px,
                    h_px,
                    _SWP_NOZORDER | _SWP_NOACTIVATE,
                )
            finally:
                self._calibrating = False
            result_holder = self._logical_bounds_from_hwnd(hwnd)
            if result_holder:
                self._schedule_bounds_save(result_holder)

        self._run_on_ui_thread(win, _center, wait=True)
        return result_holder

    def apply_style_runtime(self, style: dict[str, Any] | None = None, game_mode: bool = False) -> None:
        with self._lock:
            win = self._window
            if not self._open or win is None:
                return
        eff_style = style if isinstance(style, dict) else self._current_style()
        hwnd = _lookup_window_hwnd(win)
        if hwnd and sys.platform == "win32":
            def _update_backdrop() -> None:
                acrylic_active = apply_win32_lyrics_backdrop(
                    hwnd,
                    surface_style=str(eff_style.get("surface_style") or "borderless"),
                    game_mode=bool(game_mode),
                )
                changed = False
                with self._lock:
                    if self._acrylic_active != acrylic_active:
                        self._acrylic_active = acrylic_active
                        changed = True
                if changed:
                    self._emit_state()

            self._run_on_ui_thread(win, _update_backdrop, wait=False)
        self.ensure_min_height(compute_min_lyrics_height(eff_style))

    def set_window(self, open_flag: bool, locked: bool | None = None) -> dict[str, Any]:
        flush_on_close = False
        post_open_callback: Callable[[], None] | None = None
        post_open_needs_ui = False
        close_timer_win: Any = None
        close_timer_obj: Any = None
        with self._lock:
            want_open = bool(open_flag)
            if want_open:
                self._want_visible = True
                if locked is not None:
                    self._locked = bool(locked)
                if self._webview is not None and self.base_url:
                    native = getattr(self._window, "native", None)
                    if sys.platform == "win32" and native is not None:
                        hwnd = 0 if getattr(native, "IsDisposed", False) else _lookup_window_hwnd(self._window)
                        if not hwnd or not ctypes.windll.user32.IsWindow(ctypes.c_void_p(hwnd)):
                            self._window = None
                    if self._window is None:
                        initial_bounds = self._resolve_initial_bounds()
                        self._last_bounds = dict(initial_bounds)
                        self._intended_bounds = dict(initial_bounds)
                        self._acrylic_active = False
                        try:
                            win, is_transparent = self._create_lyrics_window(initial_bounds)
                        except Exception as exc:
                            _LOGGER.warning("Desktop lyrics window fallback creation failed: %s", exc)
                            self._window = None
                            self._open = False
                            self._want_visible = False
                            snapshot = self.state()
                            self._emit_state(snapshot)
                            return snapshot
                        self._window = win
                        self._is_transparent = is_transparent

                        def _on_closed() -> None:
                            with self._lock:
                                if self._window is not win:
                                    return
                                self._interactive_drag_active = False
                                timer_to_stop = self._interaction_timer
                                self._interaction_timer = None
                                self._window = None
                                self._open = False
                                self._want_visible = False
                                self._locked = False
                                self._acrylic_active = False
                            if timer_to_stop is not None:
                                try:
                                    self._run_on_ui_thread(
                                        win,
                                        lambda: (timer_to_stop.Stop(), timer_to_stop.Dispose()),
                                        wait=False,
                                    )
                                except Exception:
                                    pass
                            self._flush_bounds_save()
                            self._emit_state()

                        def _on_shown() -> None:
                            with self._lock:
                                if self._window is not win:
                                    return
                                current_locked = self._locked
                                transparent_flag = self._is_transparent
                                target_bounds = dict(self._intended_bounds or initial_bounds)
                            try:
                                if getattr(win, "native", None) is not None:
                                    self._calibrate_winforms_window(
                                        win,
                                        target_bounds,
                                        transparent_flag=transparent_flag,
                                        locked_flag=current_locked,
                                    )
                                else:
                                    hwnd = _lookup_window_hwnd(win)
                                    if hwnd:
                                        overlay_res = _configure_win32_seamless_overlay(
                                            hwnd,
                                            surface_style=str(self._current_style().get("surface_style") or "borderless"),
                                            game_mode=self._current_game_mode(),
                                        )
                                        with self._lock:
                                            self._acrylic_active = bool(overlay_res.get("acrylic_active"))
                                        set_win32_click_through(
                                            hwnd,
                                            current_locked,
                                            preserve_transparency=transparent_flag,
                                        )
                            except Exception:
                                pass

                        def _on_moved(*args: Any) -> None:
                            with self._lock:
                                if self._window is not win or self._calibrating or self._interactive_drag_active:
                                    return
                            override_xy = (args[0], args[1]) if len(args) >= 2 else None
                            bounds = self._extract_window_bounds(win, override_xy=override_xy)
                            self._schedule_bounds_save(bounds)

                        def _on_resized(*args: Any) -> None:
                            with self._lock:
                                if self._window is not win or self._calibrating or self._interactive_drag_active:
                                    return
                            override_wh = (args[0], args[1]) if len(args) >= 2 else None
                            bounds = self._extract_window_bounds(win, override_wh=override_wh)
                            self._schedule_bounds_save(bounds)

                        events = getattr(win, "events", None)
                        if events is not None:
                            for ev_name, handler in (
                                ("closed", _on_closed),
                                ("shown", _on_shown),
                                ("moved", _on_moved),
                                ("resized", _on_resized),
                            ):
                                try:
                                    ev = getattr(events, ev_name)
                                    ev += handler
                                except Exception:
                                    pass
                            # Immediately calibrate real WinForms handle outside self._lock before navigation reveals it.
                            if getattr(win, "native", None) is not None or getattr(getattr(events, "shown", None), "is_set", lambda: False)():
                                post_open_callback = _on_shown
                    else:
                        existing_win = self._window
                        existing_locked = self._locked
                        existing_transparent = self._is_transparent
                        hwnd = _lookup_window_hwnd(existing_win)
                        if sys.platform == "win32" and getattr(existing_win, "native", None) is not None:
                            def _show_existing() -> None:
                                with self._lock:
                                    if self._window is not existing_win or not self._want_visible:
                                        return
                                nat = getattr(existing_win, "native", None)
                                if nat is None or getattr(nat, "IsDisposed", False):
                                    return
                                live_hwnd = _lookup_window_hwnd(existing_win)
                                if live_hwnd:
                                    handle = ctypes.c_void_p(live_hwnd)
                                    user32 = ctypes.windll.user32
                                    nat.TopMost = True
                                    user32.SetWindowPos(
                                        handle, ctypes.c_void_p(-1), 0, 0, 0, 0,
                                        _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
                                    )
                                    user32.ShowWindow(handle, _SW_SHOWNOACTIVATE)
                                browser = getattr(nat, "browser", None)
                                wv = getattr(nat, "webview", None) or getattr(browser, "webview", None)
                                if wv is not None:
                                    wv.Visible = True
                                    _set_webview_controller_visible(wv, True)
                                if live_hwnd:
                                    set_win32_click_through(
                                        live_hwnd,
                                        existing_locked,
                                        preserve_transparency=existing_transparent,
                                    )

                            post_open_callback = _show_existing
                            post_open_needs_ui = True
                        elif hwnd and sys.platform == "win32":
                            try:
                                ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), _SW_SHOWNOACTIVATE)
                            except Exception:
                                pass
                            try:
                                set_win32_click_through(
                                    hwnd,
                                    existing_locked,
                                    preserve_transparency=existing_transparent,
                                )
                            except Exception:
                                pass
                        else:
                            try:
                                existing_win.show()
                            except Exception:
                                pass
                self._open = True
            else:
                self._want_visible = False
                self._interactive_drag_active = False
                close_timer_obj = self._interaction_timer
                self._interaction_timer = None
                self._open = False
                self._locked = False
                self._acrylic_active = False
                win = self._window
                close_timer_win = win
                self._window = None
                flush_on_close = True
                if win is not None:
                    try:
                        hwnd = _lookup_window_hwnd(win)
                        if hwnd:
                            set_win32_click_through(
                                hwnd,
                                False,
                                preserve_transparency=self._is_transparent,
                            )
                    except Exception:
                        pass
        if post_open_callback is not None:
            if post_open_needs_ui and self._window is not None:
                self._run_on_ui_thread(self._window, post_open_callback, wait=True)
            else:
                post_open_callback()
        if close_timer_obj is not None and close_timer_win is not None:
            try:
                self._run_on_ui_thread(
                    close_timer_win,
                    lambda: (close_timer_obj.Stop(), close_timer_obj.Dispose()),
                    wait=True,
                )
            except Exception:
                pass
        snapshot = self.state()
        if flush_on_close:
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    # pywebview can raise after disposing its Form. If still live,
                    # use its own native handle (never a title-based lookup).
                    native = getattr(win, "native", None)
                    if native is not None and not native.IsDisposed:
                        hwnd = native.Handle.ToInt64()
                        ctypes.windll.user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)
            self._flush_bounds_save()
        self._emit_state(snapshot)
        return snapshot

    def hide(self) -> None:
        with self._lock:
            self._want_visible = False
            win = self._window
        if win is not None:
            native = getattr(win, "native", None)
            if native is not None and sys.platform == "win32":
                def _hide_ui() -> None:
                    if getattr(native, "IsDisposed", False):
                        return
                    browser = getattr(native, "browser", None)
                    wv = getattr(native, "webview", None) or getattr(browser, "webview", None)
                    _set_webview_controller_visible(wv, False)
                    if wv is not None:
                        wv.Visible = False
                    live_hwnd = _lookup_window_hwnd(win)
                    if live_hwnd:
                        ctypes.windll.user32.ShowWindow(ctypes.c_void_p(live_hwnd), _SW_HIDE)
                    native.Hide()

                try:
                    self._run_on_ui_thread(win, _hide_ui, wait=True)
                except Exception:
                    pass
                return
            try:
                win.hide()
            except Exception:
                pass

    def set_locked(self, locked: bool) -> dict[str, Any]:
        with self._lock:
            want_locked = bool(locked)
            if self._window is not None:
                hwnd = _lookup_window_hwnd(self._window)
                if hwnd and sys.platform == "win32":
                    ok = set_win32_click_through(
                        hwnd,
                        want_locked,
                        preserve_transparency=self._is_transparent,
                    )
                    if not ok and want_locked:
                        _LOGGER.warning("Failed to apply WS_EX_TRANSPARENT lock to desktop lyrics window.")
                        snapshot = self.state()
                        self._emit_state(snapshot)
                        return snapshot
            self._locked = want_locked
            snapshot = self.state()
        self._emit_state(snapshot)
        return snapshot

    def toggle_locked(self) -> dict[str, Any]:
        with self._lock:
            currently_locked = self._locked
        return self.set_locked(not currently_locked)

    def toggle_open(self) -> dict[str, Any]:
        with self._lock:
            currently_open = self._open
        return self.set_window(not currently_open)

    def close(self) -> None:
        self.set_window(False)

    def _emit_state(self, snapshot: dict[str, Any] | None = None) -> None:
        if callable(self._on_state_change):
            try:
                self._on_state_change(snapshot or self.state())
            except Exception:
                pass


class GlobalHotkeyDaemon:
    """Pure-ctypes Windows RegisterHotKey background daemon with clean WM_QUIT teardown."""

    def __init__(
        self,
        on_hotkey: Callable[[str], None],
        enabled_provider: Callable[[], bool] | None = None,
    ):
        self._on_hotkey = on_hotkey
        self._enabled_provider = enabled_provider or (lambda: True)
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._registered_ids: list[int] = []
        self._enabled = bool(self._enabled_provider())

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self.is_running():
            return True
        self._stop_requested.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_loop, name="pruner-hotkeys", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout=2.0)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if self._thread_id and sys.platform == "win32":
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x8001, 0, 0)

    def registration_status(self):
        registered = set(self._registered_ids)
        return {"hotkeys_supported": self.is_running(),
                "hotkey_registration": {action: key in registered for key, _, action in HOTKEY_BINDINGS}}

    def _sync_bindings(self, user32):
        mods = _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT
        for hk_id, vk, _action in HOTKEY_BINDINGS:
            if self._enabled and hk_id not in self._registered_ids:
                if user32.RegisterHotKey(None, hk_id, mods, vk):
                    self._registered_ids.append(hk_id)
            elif not self._enabled and hk_id in self._registered_ids:
                if user32.UnregisterHotKey(None, hk_id):
                    self._registered_ids.remove(hk_id)

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_requested.set()
        if sys.platform == "win32" and self._thread_id:
            try:
                from ctypes import wintypes

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostThreadMessageW.argtypes = [
                    wintypes.DWORD,
                    wintypes.UINT,
                    wintypes.WPARAM,
                    wintypes.LPARAM,
                ]
                user32.PostThreadMessageW.restype = wintypes.BOOL
                for _ in range(5):
                    if user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0):
                        break
                    time.sleep(0.02)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        self._thread_id = 0

    def _run_loop(self) -> None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.PeekMessageW.restype = wintypes.BOOL

        self._thread_id = int(kernel32.GetCurrentThreadId())
        # Ensure thread message queue exists before signalling ready
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        self._sync_bindings(user32)
        self._ready.set()

        try:
            while not self._stop_requested.is_set():
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break
                if msg.message == 0x8001:
                    self._sync_bindings(user32)
                if msg.message == _WM_HOTKEY:
                    action = HOTKEY_ACTION_BY_ID.get(int(msg.wParam))
                    if action:
                        try:
                            if self._enabled:
                                self._on_hotkey(action)
                        except Exception:
                            pass
        finally:
            for hk_id in self._registered_ids:
                try:
                    user32.UnregisterHotKey(None, hk_id)
                except Exception:
                    pass
            self._registered_ids = []


def is_webview2_installed() -> bool:
    """Check Windows Registry for the Microsoft Edge WebView2 Evergreen Runtime."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:
        return False

    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    candidates = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
    ]
    for hive, subkey in candidates:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if isinstance(version, str) and version.strip() and version.strip() != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def bind_loopback_socket(host: str, preferred_port: int, max_attempts: int = 21) -> tuple[socket.socket, int]:
    """Bind an exclusive loopback socket without TOCTOU port races."""
    if host != "127.0.0.1":
        raise ValueError("Pruner Studio may only bind to 127.0.0.1")

    ports = [0] if preferred_port == 0 else list(range(preferred_port, min(65535, preferred_port + max_attempts)))
    for candidate_port in ports:
        if candidate_port != 0:
            try:
                with socket.create_connection((host, candidate_port), timeout=0.12):
                    continue
            except OSError:
                pass
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((host, candidate_port))
            sock.listen(128)
            sock.setblocking(False)
            bound_port = int(sock.getsockname()[1])
            return sock, bound_port
        except OSError:
            sock.close()
            continue
    raise OSError("Pruner Studio could not find a free local port")


class BackgroundUvicornServer:
    """Single-worker Uvicorn server running on a pre-bound loopback socket."""

    def __init__(self, asgi_app: Any, sock: socket.socket):
        self.sock = sock
        self.host, self.port = sock.getsockname()[0], int(sock.getsockname()[1])
        config = uvicorn.Config(
            asgi_app,
            host=self.host,
            port=self.port,
            workers=1,
            loop="asyncio",
            http="h11",
            lifespan="on",
            log_config=None,
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        # Prevent uvicorn from trying to register signal handlers on non-main threads.
        self.server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self, startup_timeout: float = 5.0) -> None:
        def _run() -> None:
            try:
                self.server.run(sockets=[self.sock])
            except BaseException as exc:  # pragma: no cover - defensive capture
                self._error = exc

        self._thread = threading.Thread(target=_run, name="pruner-uvicorn", daemon=True)
        self._thread.start()
        deadline = time.monotonic() + startup_timeout
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if self._error is not None:
                raise RuntimeError(f"Uvicorn failed to start: {self._error}") from self._error
            if not self._thread.is_alive():
                raise RuntimeError("Uvicorn server thread exited prematurely")
            time.sleep(0.02)
        if not self.server.started:
            raise TimeoutError("Timed out waiting for local HTTP server startup")

    def run_foreground(self) -> None:
        try:
            self.server.run(sockets=[self.sock])
        except KeyboardInterrupt:
            pass
        finally:
            self.close_socket()

    def stop(self, timeout: float = 5.0) -> None:
        self.server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.close_socket()

    def close_socket(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _create_tray_icon_image() -> Any:
    from PIL import Image, ImageDraw

    size = 64
    image = Image.new("RGBA", (size, size), (18, 22, 26, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 5, size - 5), radius=14, fill=(24, 30, 36, 255), outline=(98, 163, 104, 255), width=3)
    draw.ellipse((18, 18, 46, 46), fill=(98, 163, 104, 255))
    draw.ellipse((26, 26, 38, 38), fill=(18, 22, 26, 255))
    return image


def load_tray_icon_image(web_dir: Path | None = None) -> Any:
    """Load bundled web/assets/icon.png for pystray tray icon with safe procedural fallback."""
    ico_path, png_path = resolve_brand_icon_paths(web_dir)
    for candidate in (png_path, ico_path):
        if candidate is None:
            continue
        try:
            from PIL import Image

            with Image.open(candidate) as loaded:
                rgba = loaded.convert("RGBA").copy()
            if rgba.size[0] > 64 or rgba.size[1] > 64:
                rgba = rgba.resize((64, 64), Image.Resampling.LANCZOS)
            return rgba
        except Exception:
            continue
    return _create_tray_icon_image()


def write_instance_info(data_dir, port, token, mode, *, local_token=""):
    fd, name = tempfile.mkstemp(dir=data_dir, prefix=".instance-")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            if sys.platform == "win32":
                # OW grants full access to the owner and removes inherited access.
                subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", "*S-1-3-4:(F)"],
                               check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.chmod(path, 0o600)
            payload = {"pid": os.getpid(), "port": port, "wake_token": token, "mode": mode}
            if local_token:
                payload["local_token"] = local_token
            json.dump(payload, file)
        os.replace(path, data_dir / ".instance.json")
    finally:
        path.unlink(missing_ok=True)


def wake_running_instance(data_dir):
    try:
        record = json.loads((data_dir / ".instance.json").read_text("utf-8"))
        port = int(record["port"])
        if not 0 < port < 65536:
            return False
        with httpx.Client(trust_env=False, timeout=1, follow_redirects=False) as client:
            response = client.post(f"http://127.0.0.1:{port}/api/desktop/window/activate",
                                   headers={"X-Pruner-Wake-Token": record["wake_token"]}, json={})
        return response.is_success and response.json()["data"]["activated"] is True
    except (ValueError, KeyError, TypeError, OSError, httpx.HTTPError):
        return False


class ChannelAuthWindowController:
    """Manage an isolated child WebView2 login popup strictly inside Pruner Studio's container."""

    def __init__(
        self,
        *,
        on_authorized: Callable[[str, dict[str, Any]], Any] | None = None,
        on_status: Callable[[str, str, str], Any] | None = None,
        window_factory: Callable[[str, str], Any] | None = None,
        cookie_reader: Callable[[Any, tuple[str, ...]], list[dict[str, Any]]] | None = None,
        poll_interval_sec: float = 1.0,
        timeout_sec: float = 180.0,
    ) -> None:
        self._on_authorized = on_authorized
        self._on_status = on_status
        self._window_factory = window_factory
        self._cookie_reader = cookie_reader
        self._poll_interval_sec = max(0.02, float(poll_interval_sec))
        self._timeout_sec = max(0.1, float(timeout_sec))
        self._lock = threading.RLock()
        self._active_window: Any = None
        self._active_channel: str | None = None
        self._stop_event: threading.Event | None = None
        self._finished = False

    def open_auth_window(self, channel_id: str) -> bool:
        ch = str(channel_id or "").strip().lower()
        cfg = CHANNEL_AUTH_CONFIG.get(ch)
        if cfg is None or not callable(self._window_factory):
            return False

        self.close_all(emit_cancelled=False)

        stop_event = threading.Event()
        popup = self._window_factory(cfg["title"], cfg["login_url"])
        if popup is None:
            return False

        with self._lock:
            self._active_window = popup
            self._active_channel = ch
            self._stop_event = stop_event
            self._finished = False

        def _on_closed(*_args: Any) -> None:
            should_notify = False
            with self._lock:
                if self._active_window is popup:
                    should_notify = not self._finished
                    self._finished = True
                    self._active_window = None
                    self._active_channel = None
                    stop_event.set()
            if should_notify and callable(self._on_status):
                try:
                    self._on_status(ch, "cancelled", "已关闭官方登录窗口")
                except Exception:
                    pass

        events = getattr(popup, "events", None)
        if events is not None:
            for ev_name in ("closed", "closing"):
                ev = getattr(events, ev_name, None)
                if ev is not None:
                    try:
                        ev += _on_closed
                        break
                    except Exception:
                        pass

        watcher = threading.Thread(
            target=self._watch_cookies,
            args=(ch, cfg["cookie_query_urls"], popup, stop_event),
            name=f"pruner-auth-{ch}",
            daemon=True,
        )
        watcher.start()
        return True

    def _watch_cookies(
        self,
        channel_id: str,
        query_urls: tuple[str, ...],
        popup: Any,
        stop_event: threading.Event,
    ) -> None:
        deadline = time.monotonic() + self._timeout_sec
        while not stop_event.is_set() and time.monotonic() < deadline:
            if callable(self._cookie_reader):
                try:
                    raw_cookies = self._cookie_reader(popup, query_urls) or []
                    payload = extract_channel_auth_cookies(channel_id, raw_cookies)
                except Exception:
                    payload = None
                if payload is not None:
                    should_commit = False
                    with self._lock:
                        if self._active_window is popup and not self._finished:
                            self._finished = True
                            self._active_window = None
                            self._active_channel = None
                            stop_event.set()
                            should_commit = True
                    if should_commit:
                        if callable(self._on_authorized):
                            try:
                                self._on_authorized(channel_id, payload)
                            except Exception:
                                pass
                        self._destroy_popup(popup)
                    return
            stop_event.wait(self._poll_interval_sec)

        timed_out = False
        with self._lock:
            if self._active_window is popup and not self._finished:
                self._finished = True
                self._active_window = None
                self._active_channel = None
                stop_event.set()
                timed_out = True
        if timed_out:
            self._destroy_popup(popup)
            if callable(self._on_status):
                try:
                    self._on_status(channel_id, "timeout", "官方登录等待超时，请重试")
                except Exception:
                    pass

    @staticmethod
    def _destroy_popup(popup: Any) -> None:
        destroy = getattr(popup, "destroy", None)
        if callable(destroy):
            try:
                destroy()
            except Exception:
                pass

    def close_all(self, *, emit_cancelled: bool = False) -> None:
        with self._lock:
            popup = self._active_window
            ch = self._active_channel
            stop_event = self._stop_event
            was_unfinished = not self._finished
            self._finished = True
            self._active_window = None
            self._active_channel = None
            self._stop_event = None
        if stop_event is not None:
            stop_event.set()
        if popup is not None:
            self._destroy_popup(popup)
        if emit_cancelled and was_unfinished and ch and callable(self._on_status):
            try:
                self._on_status(ch, "cancelled", "已关闭官方登录窗口")
            except Exception:
                pass


def launch_desktop_or_server(
    asgi_app: Any,
    sock: socket.socket,
    data_dir: Path,
    *,
    web_dir: Path | None = None,
    application=None,
    minimize_to_tray_provider: Any = None,
    hotkeys_enabled_provider: Callable[[], bool] | None = None,
    on_hotkey: Callable[[str], None] | None = None,
    desktop_lyrics_controller: DesktopLyricsController | None = None,
) -> None:
    """Run Pruner Studio in headless, browser, or native WebView2 desktop window mode."""
    host = sock.getsockname()[0]
    port = int(sock.getsockname()[1])
    base_url = f"http://{host}:{port}"
    def launch_url(path="/"):
        url = base_url + path
        launcher = getattr(application, "launcher_url", None)
        return launcher(url) if launcher else url

    no_browser = os.environ.get("PRUNER_STUDIO_NO_BROWSER") == "1"
    no_gui = os.environ.get("PRUNER_STUDIO_NO_GUI") == "1"
    force_browser = os.environ.get("PRUNER_STUDIO_BROWSER") == "1"

    server = BackgroundUvicornServer(asgi_app, sock)

    def set_mode(mode):
        if application is not None:
            application.folder_picker = None
            application.apply_theme = None
            application.toggle_fullscreen = None
            application.channel_auth_window = None
            application.wake_window = (lambda: webbrowser.open(launch_url())) if mode == "browser" else None
            write_instance_info(
                data_dir,
                port,
                application.wake_token,
                mode,
                local_token=getattr(application, "local_api_token", ""),
            )

    if no_browser:
        set_mode("headless")
        # A supervisor can request graceful shutdown using its private data directory.
        if os.environ.get("PRUNER_STUDIO_MANAGED_HEADLESS") == "1":
            stop_file = data_dir / ".stop-headless"
            def await_shutdown():
                while not stop_file.exists():
                    time.sleep(0.2)
                stop_file.unlink(missing_ok=True)
                server.server.should_exit = True
            threading.Thread(target=await_shutdown, daemon=True).start()
        # Headless loopback server mode (tests / CLI daemon)
        server.run_foreground()
        return

    if no_gui or force_browser or sys.platform != "win32" or not is_webview2_installed():
        set_mode("browser")
        threading.Timer(0.35, lambda: webbrowser.open(launch_url())).start()
        server.run_foreground()
        return

    try:
        import webview
    except Exception:
        set_mode("browser")
        threading.Timer(0.35, lambda: webbrowser.open(launch_url())).start()
        server.run_foreground()
        return

    set_windows_app_user_model_id(APP_USER_MODEL_ID)
    ico_path, _png_path = resolve_brand_icon_paths(web_dir)

    lyrics_ctrl = desktop_lyrics_controller or DesktopLyricsController(base_url, webview)
    lyrics_ctrl.window_url = getattr(application, "launcher_url", lambda url: url)
    lyrics_ctrl.attach_runtime(base_url, webview)

    def _dispatch_hotkey(action: str) -> None:
        if action == "toggle_desktop_lyrics":
            lyrics_ctrl.toggle_open()
        elif action == "unlock_desktop_lyrics":
            lyrics_ctrl.set_locked(False)
        elif action == "toggle_desktop_lyrics_lock":
            lyrics_ctrl.toggle_locked()
        if callable(on_hotkey):
            on_hotkey(action)

    hotkey_daemon: GlobalHotkeyDaemon | None = None
    hotkeys_check = hotkeys_enabled_provider or (lambda: True)
    if sys.platform == "win32":
        try:
            hotkey_daemon = GlobalHotkeyDaemon(_dispatch_hotkey, enabled_provider=hotkeys_check)
            hotkey_daemon.start()
            if application is not None:
                application.set_hotkeys_enabled = hotkey_daemon.set_enabled
                application.user_settings.hotkey_status = hotkey_daemon.registration_status
        except Exception:
            hotkey_daemon = None

    server.start()
    storage_dir = data_dir / "webview2"
    storage_dir.mkdir(parents=True, exist_ok=True)

    tray_icon: Any = None
    auth_window_ctrl: ChannelAuthWindowController | None = None
    loaded_hicons: list[int] = []
    quitting = threading.Event()

    timer = None
    trim_timer = None
    bounds_lock = threading.Lock()
    pending_bounds = {}
    def flush_bounds():
        with bounds_lock:
            if pending_bounds and application:
                application.user_settings.save({"window_bounds": dict(pending_bounds)})
                pending_bounds.clear()

    def on_visibility_change(visible, minimized):
        nonlocal trim_timer
        with bounds_lock:
            if trim_timer:
                trim_timer.cancel()
                trim_timer = None
            if not visible or minimized:
                trim_timer = threading.Timer(1.5, trim_process_working_set)
                trim_timer.daemon = True
                trim_timer.start()
        if application:
            application.publish_sse("window_visibility", {"visible": visible, "minimized": minimized})

    try:
        prefs = application.user_settings.load() if application else {}
        saved = prefs.get("window_bounds")
        geometry = {"width": 1280, "height": 820}
        if saved:
            left, top, right, bottom = _primary_work_area()
            w = min(max(800, saved["width"]), right-left)
            h = min(max(600, saved["height"]), bottom-top)
            geometry = dict(width=w, height=h, x=max(left, min(saved["x"], right-w)),
                            y=max(top, min(saved["y"], bottom-h)), maximized=saved["maximized"])
        window = webview.create_window(
            "Pruner Studio",
            launch_url(),
            **geometry,
            min_size=(800, 600),
            background_color="#121417",
        )

        fullscreen_lock = threading.Lock()
        fullscreen_transitioning = [False]
        pre_fullscreen_maximized = [False]
        pre_fullscreen_restore_bounds: list[tuple[int, int, int, int] | None] = [None]
        cached_fullscreen = [False]

        def _is_window_maximized() -> bool:
            native = getattr(window, "native", None)
            if native is not None:
                try:
                    import System.Windows.Forms as WF

                    return native.WindowState == WF.FormWindowState.Maximized
                except Exception:
                    pass
            hwnd = _lookup_window_hwnd(window)
            return bool(hwnd and sys.platform == "win32" and ctypes.windll.user32.IsZoomed(ctypes.c_void_p(hwnd)))

        def _is_window_minimized() -> bool:
            native = getattr(window, "native", None)
            if native is not None:
                try:
                    import System.Windows.Forms as WF

                    if native.WindowState == WF.FormWindowState.Minimized:
                        return True
                except Exception:
                    pass
            hwnd = _lookup_window_hwnd(window)
            return bool(hwnd and sys.platform == "win32" and ctypes.windll.user32.IsIconic(ctypes.c_void_p(hwnd)))

        def get_fullscreen_state() -> bool:
            native = getattr(window, "native", None)
            if native is None:
                return cached_fullscreen[0]
            try:
                if not bool(getattr(native, "is_fullscreen", False)):
                    return False
                import System.Windows.Forms as WF

                borderless = getattr(WF.FormBorderStyle, "None")
                if getattr(native, "FormBorderStyle", None) != borderless:
                    return False
                if getattr(native, "WindowState", None) not in (
                    WF.FormWindowState.Maximized,
                    WF.FormWindowState.Minimized,
                ):
                    return False
                return True
            except Exception:
                return bool(getattr(native, "is_fullscreen", cached_fullscreen[0]))

        def toggle_fullscreen():
            with fullscreen_lock:
                was_fs = get_fullscreen_state()
                native = getattr(window, "native", None)
                if not was_fs and native is None:
                    pre_fullscreen_maximized[0] = _is_window_maximized()
                restore_maximized = was_fs and pre_fullscreen_maximized[0]
                fullscreen_transitioning[0] = True
                try:
                    if native is not None and sys.platform == "win32":
                        done = threading.Event()
                        err_holder: list[BaseException] = []

                        def _do_toggle() -> None:
                            try:
                                import System.Windows.Forms as WF

                                if not was_fs:
                                    was_max = native.WindowState == WF.FormWindowState.Maximized
                                    pre_fullscreen_maximized[0] = was_max
                                    saved_rb: tuple[int, int, int, int] | None = None
                                    if was_max:
                                        rb = getattr(native, "RestoreBounds", None)
                                        if (
                                            rb is not None
                                            and int(getattr(rb, "Width", 0)) > 0
                                            and int(getattr(rb, "Height", 0)) > 0
                                        ):
                                            saved_rb = (int(rb.X), int(rb.Y), int(rb.Width), int(rb.Height))
                                    pre_fullscreen_restore_bounds[0] = saved_rb
                                restore_max = was_fs and pre_fullscreen_maximized[0]
                                saved_restore_bounds = pre_fullscreen_restore_bounds[0] if was_fs else None

                                if getattr(native, "is_fullscreen", False) and not was_fs:
                                    # Clean up stale native.is_fullscreen after external Restore
                                    native.WindowState = WF.FormWindowState.Normal
                                    if hasattr(native, "old_style"):
                                        native.FormBorderStyle = native.old_style
                                    native.is_fullscreen = False
                                native.toggle_fullscreen()
                                if (
                                    not was_fs
                                    and pre_fullscreen_restore_bounds[0] is not None
                                    and getattr(native, "is_fullscreen", False)
                                ):
                                    try:
                                        from System.Drawing import Point, Size

                                        rx, ry, rw, rh = pre_fullscreen_restore_bounds[0]
                                        native.old_location = Point(rx, ry)
                                        native.old_size = Size(rw, rh)
                                    except Exception:
                                        pass
                                if not getattr(native, "is_fullscreen", False):
                                    if saved_restore_bounds is not None:
                                        rx, ry, rw, rh = saved_restore_bounds
                                        try:
                                            from System.Drawing import Rectangle

                                            native.Bounds = Rectangle(rx, ry, rw, rh)
                                        except Exception:
                                            native.Bounds = saved_restore_bounds
                                    if restore_max:
                                        native.WindowState = WF.FormWindowState.Maximized
                            except BaseException as exc:
                                err_holder.append(exc)
                            finally:
                                done.set()

                        if getattr(native, "InvokeRequired", False):
                            from System import Action

                            native.BeginInvoke(Action(_do_toggle))
                            done.wait(timeout=3.0)
                        else:
                            _do_toggle()
                        if err_holder:
                            raise err_holder[0]
                    else:
                        window.toggle_fullscreen()
                        if restore_maximized:
                            window.maximize()
                    if was_fs:
                        pre_fullscreen_maximized[0] = False
                        pre_fullscreen_restore_bounds[0] = None
                    actual_fs = get_fullscreen_state() if native is not None else (not was_fs)
                    cached_fullscreen[0] = actual_fs
                finally:
                    fullscreen_transitioning[0] = False
                if application:
                    application.publish_sse("window_fullscreen", {"fullscreen": actual_fs})
                return actual_fs

        def save_bounds(*args):
            nonlocal timer
            if fullscreen_transitioning[0] or _is_window_minimized():
                return
            native = getattr(window, "native", None)
            if native is not None and getattr(native, "is_fullscreen", False) and not get_fullscreen_state():
                # User restored out of native fullscreen via OS controls; sync border & SSE cleanly.
                def _finish_exit() -> None:
                    try:
                        import System.Windows.Forms as WF

                        if getattr(native, "is_fullscreen", False):
                            native.is_fullscreen = False
                            if hasattr(native, "old_style"):
                                native.FormBorderStyle = native.old_style
                    except Exception:
                        pass

                try:
                    if getattr(native, "InvokeRequired", False):
                        from System import Action

                        native.BeginInvoke(Action(_finish_exit))
                    else:
                        _finish_exit()
                except Exception:
                    pass
                cached_fullscreen[0] = False
                pre_fullscreen_maximized[0] = False
                pre_fullscreen_restore_bounds[0] = None
                if application:
                    application.publish_sse("window_fullscreen", {"fullscreen": False})
            if get_fullscreen_state():
                return
            hwnd = ctypes.c_void_p(_lookup_window_hwnd(window))
            if not hwnd or ctypes.windll.user32.IsIconic(hwnd):
                return
            maximized = bool(ctypes.windll.user32.IsZoomed(hwnd))
            with bounds_lock:
                if maximized:
                    last = pending_bounds or (application.user_settings.load().get("window_bounds") if application else None)
                    if not last:
                        last = dict(x=0, y=0, width=1280, height=820)
                    bounds = dict(last)
                else:
                    bounds = {key: int(getattr(window, key)) for key in ("x","y","width","height")}
                bounds["maximized"] = maximized
                pending_bounds.update(bounds)
                if timer:
                    timer.cancel()
                timer = threading.Timer(0.5, flush_bounds)
                timer.daemon = True
                timer.start()

        def _restore_window(_icon=None, _item=None):
            window.show()
            native = getattr(window, "native", None)
            if native is not None and get_fullscreen_state():
                def _restore_fs() -> None:
                    try:
                        import System.Windows.Forms as WF

                        native.WindowState = WF.FormWindowState.Maximized
                    except Exception:
                        pass

                try:
                    if getattr(native, "InvokeRequired", False):
                        from System import Action

                        native.BeginInvoke(Action(_restore_fs))
                    else:
                        _restore_fs()
                except Exception:
                    pass
            else:
                window.restore()
            on_visibility_change(True, False)
            return True

        def pick_folder():
            result = window.create_file_dialog(webview.FileDialog.FOLDER, allow_multiple=False)
            return str(result[0]) if result else None

        def apply_theme(theme):
            dark = theme not in {"light", "celadon", "parchment"}
            if theme == "auto":
                import winreg
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                        dark = not winreg.QueryValueEx(key, "AppsUseLightTheme")[0]
                except OSError:
                    pass
            return apply_win32_titlebar_theme(_lookup_window_hwnd(window), dark)

        def _read_webview2_cookies(popup_window: Any, query_urls: tuple[str, ...]) -> list[dict[str, str]]:
            """Read cookies ONLY from Pruner Studio's isolated child WebView2 window."""
            native = getattr(popup_window, "native", None)
            if native is None:
                return []

            def _find_wv2(ctrl: Any) -> Any:
                browser = getattr(ctrl, "browser", None)
                if browser is not None:
                    wv = getattr(browser, "webview", None)
                    if wv is not None and getattr(wv, "CoreWebView2", None) is not None:
                        return wv
                if getattr(ctrl, "CoreWebView2", None) is not None:
                    return ctrl
                children = getattr(ctrl, "Controls", None)
                if children is not None:
                    try:
                        for child in children:
                            found = _find_wv2(child)
                            if found is not None:
                                return found
                    except Exception:
                        pass
                return None

            collected: list[dict[str, str]] = []
            for qurl in query_urls:
                done = threading.Event()
                entries: list[dict[str, str]] = []

                def _invoke(target_url: str = qurl, ev: threading.Event = done, out: list[dict[str, str]] = entries) -> None:
                    try:
                        wv2 = _find_wv2(native)
                        core = getattr(wv2, "CoreWebView2", None) if wv2 is not None else None
                        cookie_mgr = getattr(core, "CookieManager", None) if core is not None else None
                        if cookie_mgr is None:
                            ev.set()
                            return
                        task = cookie_mgr.GetCookiesAsync(target_url)
                        awaiter = task.GetAwaiter()

                        def _on_complete() -> None:
                            try:
                                result = awaiter.GetResult()
                                if result is not None:
                                    for item in result:
                                        out.append(
                                            {
                                                "name": str(getattr(item, "Name", "") or ""),
                                                "value": str(getattr(item, "Value", "") or ""),
                                                "domain": str(getattr(item, "Domain", "") or ""),
                                            }
                                        )
                            except Exception:
                                pass
                            finally:
                                ev.set()

                        from System import Action

                        awaiter.OnCompleted(Action(_on_complete))
                    except Exception:
                        ev.set()

                try:
                    if getattr(native, "InvokeRequired", False):
                        from System import Action

                        native.BeginInvoke(Action(_invoke))
                    else:
                        _invoke()
                    done.wait(timeout=2.0)
                    collected.extend(entries)
                except Exception:
                    pass
            return collected

        auth_window_ctrl = ChannelAuthWindowController(
            on_authorized=lambda ch, payload: application.complete_channel_auth(ch, payload) if application else None,
            on_status=lambda ch, status, msg: application.broadcast_channel_auth_status(ch, status, msg) if application else None,
            window_factory=lambda title, target_url: webview.create_window(
                title=title,
                url=target_url,
                width=980,
                height=720,
                resizable=True,
                confirm_close=False,
            ),
            cookie_reader=_read_webview2_cookies,
        )

        set_mode("desktop")
        if application:
            application.toggle_fullscreen = toggle_fullscreen
            application.get_fullscreen = get_fullscreen_state
            application.wake_window = _restore_window
            application.folder_picker = pick_folder
            application.apply_theme = apply_theme
            application.channel_auth_window = auth_window_ctrl
        window.events.moved += save_bounds
        window.events.resized += save_bounds
        window.events.maximized += save_bounds
        window.events.minimized += lambda: on_visibility_change(False, True)
        window.events.restored += lambda: on_visibility_change(True, False)
        window.events.restored += save_bounds

        def _on_shown() -> None:
            on_visibility_change(True, False)
            apply_theme(application.user_settings.load()["appearance"]["theme"] if application else "dark")
            if ico_path is None:
                return
            try:
                _apply_winforms_native_icon(window, ico_path)
            except Exception:
                pass
            try:
                hwnd = _lookup_window_hwnd(window)
                if hwnd:
                    loaded_hicons.extend(apply_win32_window_icons(hwnd, ico_path))
            except Exception:
                pass

        try:
            window.events.shown += _on_shown
        except Exception:
            pass

        def _should_minimize_to_tray() -> bool:
            if callable(minimize_to_tray_provider):
                try:
                    return bool(minimize_to_tray_provider())
                except Exception:
                    return False
            return False

        try:
            import pystray

            def _tray_action(action: str) -> Any:
                def _handler(_icon: Any = None, _item: Any = None) -> None:
                    try:
                        _dispatch_hotkey(action)
                    except Exception:
                        pass
                return _handler

            def _toggle_desktop_lyrics(_icon: Any = None, _item: Any = None) -> None:
                try:
                    lyrics_ctrl.toggle_open()
                except Exception:
                    pass

            def _unlock_desktop_lyrics(_icon: Any = None, _item: Any = None) -> None:
                try:
                    lyrics_ctrl.set_locked(False)
                except Exception:
                    pass

            def _quit_app(_icon: Any = None, _item: Any = None) -> None:
                quitting.set()
                try:
                    lyrics_ctrl.close()
                except Exception:
                    pass
                try:
                    if tray_icon is not None:
                        tray_icon.stop()
                except Exception:
                    pass
                try:
                    window.destroy()
                except Exception:
                    pass

            def _on_closing() -> bool:
                if quitting.is_set() or not _should_minimize_to_tray():
                    quitting.set()
                    return True
                try:
                    window.hide()
                    on_visibility_change(False, False)
                    return False
                except Exception:
                    return True

            window.events.closing += _on_closing
            tray_icon = pystray.Icon(
                "pruner_studio",
                load_tray_icon_image(web_dir),
                "Pruner Studio",
                menu=pystray.Menu(
                    pystray.MenuItem("显示 Pruner Studio", _restore_window, default=True),
                    pystray.MenuItem("播放 / 暂停", _tray_action("toggle_play")),
                    pystray.MenuItem("上一首", _tray_action("prev")),
                    pystray.MenuItem("下一首", _tray_action("next")),
                    pystray.MenuItem("音量 +5%", _tray_action("volume_up")),
                    pystray.MenuItem("音量 -5%", _tray_action("volume_down")),
                    pystray.MenuItem("静音 / 取消静音", _tray_action("toggle_mute")),
                    pystray.MenuItem("桌面歌词", _toggle_desktop_lyrics),
                    pystray.MenuItem("锁定 / 解锁桌面歌词", _tray_action("toggle_desktop_lyrics_lock")),
                    pystray.MenuItem("解锁桌面歌词", _unlock_desktop_lyrics),
                    pystray.MenuItem("游戏护航模式", _tray_action("toggle_game_mode")),
                    pystray.MenuItem("退出", _quit_app),
                ),
            )
            tray_icon.run_detached()
        except Exception:
            tray_icon = None

        start_kwargs: dict[str, Any] = {
            "gui": "edgechromium",
            "storage_path": str(storage_dir),
            "private_mode": False,
        }
        if ico_path is not None:
            start_kwargs["icon"] = str(ico_path)
        webview.start(**start_kwargs)
    except Exception:
        # Fallback cleanly to system browser if WebView2 initialization fails at runtime.
        set_mode("browser")
        webbrowser.open(launch_url())
        try:
            while server._thread is not None and server._thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    finally:
        if timer:
            timer.cancel()
        if trim_timer:
            trim_timer.cancel()
        flush_bounds()
        if hotkey_daemon is not None:
            try:
                hotkey_daemon.stop()
            except Exception:
                pass
        try:
            lyrics_ctrl.close()
        except Exception:
            pass
        if auth_window_ctrl is not None:
            try:
                auth_window_ctrl.close_all()
            except Exception:
                pass
        destroy_win32_icons(loaded_hicons)
        if tray_icon is not None:
            try:
                tray_icon.stop()
            except Exception:
                pass
        server.stop()
