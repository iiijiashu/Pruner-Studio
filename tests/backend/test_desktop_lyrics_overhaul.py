"""Regression tests for Stage 1-4 Desktop Lyrics WinForms/Win32 overhaul and HTTP endpoints."""
from __future__ import annotations

import asyncio
import ctypes
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from pytest import MonkeyPatch

import pruner_studio.desktop as desktop_mod
from pruner_studio.app import _ALLOWED_DESKTOP_COMMANDS, build_application, create_asgi_app
from pruner_studio.config import Settings
from pruner_studio.desktop import (
    _LYRICS_MAX_HEIGHT,
    _LYRICS_MIN_HEIGHT,
    _SWP_NOACTIVATE,
    _VALID_RESIZE_EDGES,
    _VK_LBUTTON,
    _WS_EX_NOACTIVATE,
    DesktopLyricsController,
    compute_min_lyrics_height,
    launch_desktop_or_server,
    set_win32_click_through,
)
from pruner_studio.storage import DEFAULT_DESKTOP_LYRICS_STYLE, DEFAULT_USER_SETTINGS, SettingsStore
try:
    from test_backend import FakeTransport
except ImportError:
    from tests.backend.test_backend import FakeTransport


class _FakeEvent:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self, *args) -> None:
        for fn in list(self.handlers):
            fn(*args)


class _FakeEvents:
    def __init__(self) -> None:
        self.closed = _FakeEvent()
        self.closing = _FakeEvent()
        self.shown = _FakeEvent()
        self.moved = _FakeEvent()
        self.resized = _FakeEvent()
        self.maximized = _FakeEvent()
        self.minimized = _FakeEvent()
        self.restored = _FakeEvent()


class DesktopLyricsOverhaulTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(self.root, self.root)
        self.transport = FakeTransport()
        self.app = build_application(self.settings, self.transport)
        self.app.client.pacer.sleep_fn = lambda _: None

    def test_compute_min_lyrics_height_monotonic_and_bounded(self) -> None:
        single_no_trans = [
            compute_min_lyrics_height({"font_size": fs, "mode": "single", "show_translation": False})
            for fs in (18, 24, 28, 36, 48)
        ]
        double_with_trans = [
            compute_min_lyrics_height({"font_size": fs, "mode": "double", "show_translation": True})
            for fs in (18, 24, 28, 36, 48)
        ]
        self.assertEqual(single_no_trans, sorted(single_no_trans))
        self.assertEqual(double_with_trans, sorted(double_with_trans))
        for val in single_no_trans + double_with_trans:
            self.assertGreaterEqual(val, _LYRICS_MIN_HEIGHT)
            self.assertLessEqual(val, _LYRICS_MAX_HEIGHT)
        self.assertGreater(double_with_trans[-1], single_no_trans[0])

    def test_settings_store_and_api_validation_for_surface_wheel_and_height(self) -> None:
        normalized = self.app.user_settings._normalize(
            {
                **DEFAULT_USER_SETTINGS,
                "desktop_lyrics_style": {
                    **DEFAULT_DESKTOP_LYRICS_STYLE,
                    "surface_style": "pill",
                    "wheel_volume": True,
                },
                "desktop_lyrics_bounds": {"x": 10, "y": 20, "width": 820, "height": 300},
            },
            strict=False,
        )
        self.assertEqual("pill", normalized["desktop_lyrics_style"]["surface_style"])
        self.assertIs(True, normalized["desktop_lyrics_style"]["wheel_volume"])
        self.assertEqual(300, normalized["desktop_lyrics_bounds"]["height"])

        for surface in ("borderless", "pill", "acrylic"):
            ok = self.app.api("PUT", "/api/settings", {"desktop_lyrics_style": {"surface_style": surface}})
            self.assertEqual(200, ok.status)
            self.assertEqual(surface, ok.payload["data"]["desktop_lyrics_style"]["surface_style"])

        for wv in (True, False):
            ok = self.app.api("PUT", "/api/settings", {"desktop_lyrics_style": {"wheel_volume": wv}})
            self.assertEqual(200, ok.status)
            self.assertIs(wv, ok.payload["data"]["desktop_lyrics_style"]["wheel_volume"])

        for valid_h in (300, 360):
            ok = self.app.api(
                "PUT",
                "/api/settings",
                {"desktop_lyrics_bounds": {"x": 10, "y": 20, "width": 820, "height": valid_h}},
            )
            self.assertEqual(200, ok.status)
            self.assertEqual(valid_h, ok.payload["data"]["desktop_lyrics_bounds"]["height"])

        for bad in (
            {"desktop_lyrics_style": {"surface_style": "frosted"}},
            {"desktop_lyrics_style": {"wheel_volume": "yes"}},
            {"desktop_lyrics_style": {"wheel_volume": 1}},
            {"desktop_lyrics_bounds": {"x": 10, "y": 20, "width": 820, "height": 361}},
            {"desktop_lyrics_bounds": {"x": 10, "y": 20, "width": 820, "height": 420}},
        ):
            rejected = self.app.api("PUT", "/api/settings", bad)
            self.assertEqual(400, rejected.status, bad)
            self.assertEqual("SETTINGS_INVALID", rejected.payload["error"]["code"])

    def test_controller_stale_bounds_healing_and_stale_closed_guard(self) -> None:
        created_windows: list[Any] = []
        created_kwargs: list[dict[str, Any]] = []
        saved_bounds: list[dict[str, int]] = []

        class _StubWebview:
            def create_window(self, title, url, **kwargs):
                created_kwargs.append({"title": title, "url": url, **kwargs})
                win = SimpleNamespace(
                    title=title,
                    x=kwargs["x"],
                    y=kwargs["y"],
                    width=kwargs["width"],
                    height=kwargs["height"],
                    events=_FakeEvents(),
                    destroyed=False,
                    hide=lambda: None,
                    show=lambda: None,
                    destroy=lambda: setattr(win, "destroyed", True),
                )
                created_windows.append(win)
                return win

        ctrl = DesktopLyricsController(
            base_url="http://127.0.0.1:8765",
            webview_module=_StubWebview(),
            bounds_provider=lambda: {"x": 100, "y": 200, "width": 772, "height": 64},
            bounds_saver=lambda b: saved_bounds.append(dict(b)),
            style_provider=lambda: {"font_size": 28, "mode": "double", "show_translation": True},
        )
        ctrl.set_window(True)
        self.assertEqual(1, len(created_windows))
        self.assertIs(False, created_kwargs[0]["focus"])
        expected_min_h = compute_min_lyrics_height({"font_size": 28, "mode": "double", "show_translation": True})
        self.assertGreaterEqual(created_kwargs[0]["height"], expected_min_h)
        win1 = created_windows[0]

        # Calibrating gate suppresses spurious WinForms resize/move persistence.
        ctrl._calibrating = True
        win1.events.moved.fire(10, 20)
        win1.events.resized.fire(500, 90)
        ctrl._calibrating = False
        self.assertEqual([], saved_bounds)

        # Simulate close + quick reopen, then stale win1 closed firing after win2 opened.
        ctrl.set_window(False)
        ctrl.set_window(True)
        self.assertEqual(2, len(created_windows))
        win2 = created_windows[1]
        win1.events.closed.fire()
        self.assertIs(ctrl._window, win2)
        self.assertIs(True, ctrl.state()["open"])
        ctrl.set_window(False)

    @unittest.skipUnless(sys.platform == "win32", "Windows-only HWND reuse check")
    def test_open_replaces_a_disposed_or_invalid_native_window(self) -> None:
        for disposed in (True, False):
            with self.subTest(disposed=disposed):
                fresh = SimpleNamespace(native=None, events=_FakeEvents())
                webview = MagicMock()
                webview.create_window.return_value = fresh
                ctrl = DesktopLyricsController(base_url="http://127.0.0.1:8765", webview_module=webview)
                ctrl._window = SimpleNamespace(native=SimpleNamespace(IsDisposed=disposed))
                with patch.object(desktop_mod, "_lookup_window_hwnd", return_value=0):
                    ctrl.set_window(True)
                self.assertIs(ctrl._window, fresh)
                webview.create_window.assert_called_once()

    def test_fullscreen_api_reports_native_support_and_state(self) -> None:
        endpoint = "/api/desktop/window/fullscreen"
        def request():
            result = self.app.api("POST", endpoint, {})
            self.assertEqual(200, result.status)
            return result.payload["data"]

        self.assertEqual({"supported": False, "fullscreen": False}, request())
        self.app.toggle_fullscreen = MagicMock(side_effect=[True, False])
        for active in (True, False):
            self.assertEqual({"supported": True, "fullscreen": active}, request())

    def test_set_win32_click_through_preserves_swp_noactivate(self) -> None:
        fake_user32 = MagicMock()
        fake_user32.GetWindowLongPtrW.return_value = 0
        fake_user32.GetWindowLongW.return_value = 0
        fake_user32.SetLayeredWindowAttributes.return_value = 1
        fake_user32.SetWindowPos.return_value = 1
        with (
            patch.object(desktop_mod.sys, "platform", "win32"),
            patch.object(desktop_mod.ctypes, "WinDLL", return_value=fake_user32),
        ):
            for locked in (True, False):
                for preserve in (True, False):
                    fake_user32.reset_mock()
                    self.assertTrue(set_win32_click_through(12345, locked, preserve_transparency=preserve))
                    fake_user32.SetWindowPos.assert_called_once()
                    flags = fake_user32.SetWindowPos.call_args[0][6]
                    self.assertTrue(flags & _SWP_NOACTIVATE)
                    set_fn = (
                        fake_user32.SetWindowLongPtrW
                        if fake_user32.SetWindowLongPtrW.called
                        else fake_user32.SetWindowLongW
                    )
                    ex_val = set_fn.call_args[0][2]
                    self.assertTrue(ex_val & _WS_EX_NOACTIVATE)
                    if preserve:
                        self.assertEqual(bool(ex_val & desktop_mod._WS_EX_LAYERED), locked)
                        if locked:
                            fake_user32.SetLayeredWindowAttributes.assert_called_once_with(
                                12345, 0, 255, desktop_mod._LWA_ALPHA
                            )
                        else:
                            fake_user32.SetLayeredWindowAttributes.assert_not_called()

    def test_layered_window_repairs_only_missing_or_invisible_alpha(self) -> None:
        user32 = MagicMock()
        user32.GetWindowLongPtrW.return_value = user32.GetWindowLongW.return_value = desktop_mod._WS_EX_LAYERED
        with patch.object(desktop_mod.sys, "platform", "win32"), patch.object(ctypes, "WinDLL", return_value=user32, create=True):
            for result, flags, alpha, repair in ((0, 2, 255, True), (1, 0, 0, True),
                                                 (1, 2, 0, True), (1, 2, 127, False),
                                                 (1, 1, 0, False)):
                with self.subTest(result=result, flags=flags, alpha=alpha):
                    def get_attributes(hwnd, color_ptr, alpha_ptr, flags_ptr):
                        alpha_ptr._obj.value = alpha
                        flags_ptr._obj.value = flags
                        return result
                    user32.GetLayeredWindowAttributes.side_effect = get_attributes
                    user32.SetLayeredWindowAttributes.reset_mock()
                    self.assertTrue(set_win32_click_through(12345, True))
                    if repair:
                        user32.SetLayeredWindowAttributes.assert_called_once_with(12345, 0, 255, 2)
                    else:
                        user32.SetLayeredWindowAttributes.assert_not_called()

    def test_start_interactive_geometry_lbutton_guard_and_all_8_edges(self) -> None:
        ctrl = DesktopLyricsController(base_url="http://127.0.0.1:8765")
        win = SimpleNamespace(title="T", x=100, y=100, width=820, height=120, native=None)
        ctrl._window = win
        ctrl._open = True

        fake_user32 = MagicMock()
        with (
            patch.object(desktop_mod.sys, "platform", "win32"),
            patch.object(desktop_mod, "_lookup_window_hwnd", return_value=999),
            patch.object(desktop_mod.ctypes, "WinDLL", return_value=fake_user32),
        ):
            # LBUTTON not pressed -> False immediately
            fake_user32.GetAsyncKeyState.return_value = 0
            self.assertFalse(ctrl.start_interactive_geometry("move"))
            self.assertEqual(0, fake_user32.SendMessageW.call_count)

            # LBUTTON pressed -> move and all 8 edges succeed
            fake_user32.GetAsyncKeyState.return_value = -32768
            for edge in sorted(_VALID_RESIZE_EDGES):
                self.assertTrue(ctrl.start_interactive_geometry(edge), edge)
            fake_user32.GetAsyncKeyState.assert_any_call(_VK_LBUTTON)
            self.assertEqual(len(_VALID_RESIZE_EDGES), fake_user32.SendMessageW.call_count)
            self.assertFalse(ctrl.start_interactive_geometry("diagonal"))

    def test_http_endpoints_resize_start_fit_center_and_allowlist(self) -> None:
        self.assertNotIn("toggle_desktop_lyrics", _ALLOWED_DESKTOP_COMMANDS)
        blocked = self.app.api("POST", "/api/desktop/command", {"action": "toggle_desktop_lyrics"})
        self.assertEqual(400, blocked.status)

        with patch.object(self.app.desktop_lyrics, "start_interactive_geometry", return_value=True) as mock_geo:
            for edge in sorted(_VALID_RESIZE_EDGES - {"move"}):
                res = self.app.api("POST", "/api/desktop/lyrics-window/resize-start", {"edge": edge})
                self.assertEqual(200, res.status, edge)
                self.assertEqual(edge, res.payload["data"]["edge"])
                self.assertIs(True, res.payload["data"]["started"])
            self.assertEqual(8, mock_geo.call_count)

        for bad_edge in ("move", "middle", "", 123):
            bad = self.app.api("POST", "/api/desktop/lyrics-window/resize-start", {"edge": bad_edge})
            self.assertEqual(400, bad.status)
            self.assertEqual("INVALID_INPUT", bad.payload["error"]["code"])

        fit_ok = self.app.api("POST", "/api/desktop/lyrics-window/fit", {"min_height": 150})
        self.assertEqual(200, fit_ok.status)
        for bad_h in (40, 400, "150", True):
            bad_fit = self.app.api("POST", "/api/desktop/lyrics-window/fit", {"min_height": bad_h})
            self.assertEqual(400, bad_fit.status)

        center = self.app.api("POST", "/api/desktop/lyrics-window/center", {})
        self.assertEqual(200, center.status)
        self.assertIn("x", center.payload["data"]["bounds"])

    @unittest.skipUnless(sys.platform == "win32", "Windows-only live WinForms test")
    def test_winforms_real_10_cycle_calibration_zero_drift_no_focus(self) -> None:
        import clr

        clr.AddReference("System.Drawing")
        clr.AddReference("System.Windows.Forms")
        from System.Drawing import Point, Size
        import System.Windows.Forms as WF

        owner = WF.Form()
        owner.StartPosition = WF.FormStartPosition.Manual
        owner.ShowInTaskbar = False
        owner.Location = Point(-32000, -32000)
        owner.Size = Size(300, 200)
        owner.Show()
        WF.Application.DoEvents()

        form = WF.Form()
        form.StartPosition = WF.FormStartPosition.Manual
        form.ShowInTaskbar = False
        form.TopMost = True
        form.FormBorderStyle = WF.FormBorderStyle.Sizable
        form.Location = Point(-32000, -32000)
        form.Size = Size(820, 120)
        ctrl = DesktopLyricsController(
            base_url="http://127.0.0.1:8765",
            style_provider=lambda: {"font_size": 28, "mode": "double", "show_translation": True, "surface_style": "borderless"},
        )
        fake_win = SimpleNamespace(native=form, title="PrunerWinFormsTest")
        try:
            hwnd = int(form.Handle.ToInt64())
            target = {"x": -32000, "y": -32000, "width": 820, "height": 120}
            ctrl._calibrate_winforms_window(fake_win, target, transparent_flag=True, locked_flag=False)
            owner.Activate()
            owner_hwnd = int(owner.Handle.ToInt64())
            ctypes.windll.user32.SetActiveWindow(ctypes.c_void_p(owner_hwnd))
            ctypes.windll.user32.SetFocus(ctypes.c_void_p(owner_hwnd))
            WF.Application.DoEvents()
            self.assertIs(True, bool(owner.ContainsFocus))
            self.assertIs(False, bool(form.ContainsFocus))
            for _ in range(10):
                ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), 0)  # SW_HIDE
                ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), 4)  # SW_SHOWNOACTIVATE
                ctrl._calibrate_winforms_window(fake_win, target, transparent_flag=True, locked_flag=False)
                WF.Application.DoEvents()
                self.assertEqual((820, 120), (int(form.Size.Width), int(form.Size.Height)))
                self.assertEqual((820, 120), (int(form.ClientSize.Width), int(form.ClientSize.Height)))
                self.assertIs(False, bool(form.ContainsFocus))
                self.assertIs(True, bool(owner.ContainsFocus))
        finally:
            for win_obj in (form, owner):
                try:
                    win_obj.Close()
                except Exception:
                    pass
                win_obj.Dispose()

    def test_set_win32_click_through_fails_when_set_window_long_sets_last_error(self) -> None:
        user32 = MagicMock()
        user32.GetWindowLongPtrW.return_value = user32.GetWindowLongW.return_value = _WS_EX_NOACTIVATE
        user32.SetWindowLongPtrW.return_value = user32.SetWindowLongW.return_value = 0
        with (
            patch.object(desktop_mod.sys, "platform", "win32"),
            patch.object(ctypes, "WinDLL", return_value=user32, create=True),
            patch.object(ctypes, "set_last_error", create=True),
        ):
            with self.subTest(last_error=5), patch.object(ctypes, "get_last_error", return_value=5, create=True):
                self.assertFalse(set_win32_click_through(12345, True))
                user32.SetWindowPos.assert_not_called()
            with self.subTest(last_error=0), patch.object(ctypes, "get_last_error", return_value=0, create=True):
                user32.SetWindowPos.return_value = 1
                self.assertTrue(set_win32_click_through(12345, True))
                user32.SetWindowPos.assert_called_once()

    def test_hide_clears_want_visible_and_hides_webview_and_hwnd(self) -> None:
        ctrl = DesktopLyricsController(base_url="http://127.0.0.1:8765")
        fake_wv = SimpleNamespace(Visible=True)
        native = SimpleNamespace(IsDisposed=False, webview=fake_wv, Hide=MagicMock())
        win = SimpleNamespace(native=native, title="PrunerLyricsHideTest")
        ctrl._window = win
        ctrl._open = True
        ctrl._want_visible = True
        fake_user32 = MagicMock()
        with (
            patch.object(desktop_mod.sys, "platform", "win32"),
            patch.object(desktop_mod, "_lookup_window_hwnd", return_value=54321),
            patch.object(desktop_mod, "_set_webview_controller_visible", return_value=True) as mock_wv_vis,
            patch.object(ctrl, "_run_on_ui_thread", side_effect=lambda _w, fn, wait=False: fn()),
            patch.object(ctypes, "windll", SimpleNamespace(user32=fake_user32), create=True),
        ):
            ctrl.hide()
        self.assertFalse(ctrl._want_visible)
        mock_wv_vis.assert_called_once_with(fake_wv, False)
        self.assertFalse(fake_wv.Visible)
        self.assertEqual(1, fake_user32.ShowWindow.call_count)
        show_arg, cmd_arg = fake_user32.ShowWindow.call_args[0]
        self.assertEqual(54321, show_arg.value)
        self.assertEqual(desktop_mod._SW_HIDE, cmd_arg)
        native.Hide.assert_called_once()

    def test_sse_initial_stream_replays_window_fullscreen(self) -> None:
        asgi = create_asgi_app(self.app, 8765)

        async def read_fs_event() -> dict[str, object]:
            scope = {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "method": "GET",
                "path": "/api/events",
                "scheme": "http",
                "http_version": "1.1",
                "query_string": b"",
                "headers": [(b"host", b"127.0.0.1:8765")],
            }
            done = asyncio.Event()
            collected = bytearray()

            async def receive() -> dict[str, object]:
                await done.wait()
                return {"type": "http.disconnect"}

            async def send(message: dict[str, object]) -> None:
                body = message.get("body", b"")
                if body:
                    collected.extend(body)
                    if b"event: window_fullscreen\ndata: " in collected:
                        done.set()

            await asyncio.wait_for(asgi(scope, receive, send), 3.0)
            marker = b"event: window_fullscreen\ndata: "
            raw = bytes(collected).split(marker, 1)[1].split(b"\n\n", 1)[0]
            return json.loads(raw.decode("utf-8"))

        for expected in (True, False):
            with self.subTest(fullscreen=expected):
                self.app.get_fullscreen = lambda val=expected: val
                self.assertEqual({"fullscreen": expected}, asyncio.run(read_fs_event()))

    def test_toggle_fullscreen_restores_maximized_and_recovers_stale_flag(self) -> None:
        fake_wf = SimpleNamespace(
            FormWindowState=SimpleNamespace(Normal="Normal", Maximized="Maximized", Minimized="Minimized"),
            FormBorderStyle=SimpleNamespace(**{"None": "None", "Sizable": "Sizable"}),
        )

        class _Rect:
            def __init__(self, x: int, y: int, w: int, h: int) -> None:
                self.X, self.Y, self.Width, self.Height = x, y, w, h

        class _Point:
            def __init__(self, x: int, y: int) -> None:
                self.X, self.Y = x, y

        class _Size:
            def __init__(self, w: int, h: int) -> None:
                self.Width, self.Height = w, h

        fake_drawing = SimpleNamespace(Rectangle=_Rect, Point=_Point, Size=_Size)
        fake_sys = SimpleNamespace(
            Windows=SimpleNamespace(Forms=fake_wf),
            Drawing=fake_drawing,
            Action=lambda fn: fn,
        )

        class _FakeNativeForm:
            def __init__(self) -> None:
                self._normal_bounds = _Rect(83, 62, 880, 660)
                self._window_state = fake_wf.FormWindowState.Maximized
                self.FormBorderStyle = fake_wf.FormBorderStyle.Sizable
                self.is_fullscreen = False
                self.old_style = fake_wf.FormBorderStyle.Sizable
                self.old_location = _Point(83, 62)
                self.old_size = _Size(880, 660)
                self.InvokeRequired = False

            @property
            def WindowState(self) -> str:
                return self._window_state

            @WindowState.setter
            def WindowState(self, val: str) -> None:
                self._window_state = val

            @property
            def RestoreBounds(self) -> _Rect:
                return _Rect(self._normal_bounds.X, self._normal_bounds.Y, self._normal_bounds.Width, self._normal_bounds.Height)

            @property
            def Location(self) -> _Point:
                if self._window_state == fake_wf.FormWindowState.Maximized:
                    return _Point(-8, -8)
                return _Point(self._normal_bounds.X, self._normal_bounds.Y)

            @Location.setter
            def Location(self, pt: _Point) -> None:
                if self._window_state == fake_wf.FormWindowState.Normal:
                    self._normal_bounds = _Rect(pt.X, pt.Y, self._normal_bounds.Width, self._normal_bounds.Height)

            @property
            def Size(self) -> _Size:
                if self._window_state == fake_wf.FormWindowState.Maximized:
                    return _Size(2576, 1456)
                return _Size(self._normal_bounds.Width, self._normal_bounds.Height)

            @Size.setter
            def Size(self, sz: _Size) -> None:
                if self._window_state == fake_wf.FormWindowState.Normal:
                    self._normal_bounds = _Rect(self._normal_bounds.X, self._normal_bounds.Y, sz.Width, sz.Height)

            @property
            def Bounds(self) -> _Rect:
                if self._window_state == fake_wf.FormWindowState.Maximized:
                    return _Rect(-8, -8, 2576, 1456)
                return self.RestoreBounds

            @Bounds.setter
            def Bounds(self, rect: _Rect) -> None:
                if self._window_state == fake_wf.FormWindowState.Normal:
                    self._normal_bounds = _Rect(rect.X, rect.Y, rect.Width, rect.Height)

            def toggle_fullscreen(self) -> None:
                if not self.is_fullscreen:
                    self.old_size = self.Size
                    self.old_location = self.Location
                    self.old_style = self.FormBorderStyle
                    self.FormBorderStyle = getattr(fake_wf.FormBorderStyle, "None")
                    self.WindowState = fake_wf.FormWindowState.Maximized
                    self.is_fullscreen = True
                else:
                    self.WindowState = fake_wf.FormWindowState.Normal
                    self.FormBorderStyle = self.old_style
                    self.is_fullscreen = False
                    self.Location = self.old_location
                    self.Size = self.old_size

        native = _FakeNativeForm()
        events = _FakeEvents()
        fake_win = SimpleNamespace(
            title="Pruner Studio",
            x=83,
            y=62,
            width=880,
            height=660,
            events=events,
            native=native,
            destroy=MagicMock(),
            show=MagicMock(),
            restore=MagicMock(),
            hide=MagicMock(),
            maximize=MagicMock(),
        )

        ran_start = [False]

        def _fake_start(**_kwargs: object) -> None:
            ran_start[0] = True
            # 1. Maximized with RestoreBounds(83,62,880,660) -> F11 enter -> F11 exit restores Maximized & RestoreBounds
            self.assertFalse(self.app.get_fullscreen())
            self.assertTrue(self.app.toggle_fullscreen())
            self.assertTrue(self.app.get_fullscreen())
            self.assertEqual(getattr(fake_wf.FormBorderStyle, "None"), native.FormBorderStyle)
            self.assertFalse(self.app.toggle_fullscreen())
            self.assertFalse(self.app.get_fullscreen())
            self.assertEqual(fake_wf.FormBorderStyle.Sizable, native.FormBorderStyle)
            self.assertEqual(fake_wf.FormWindowState.Maximized, native.WindowState)
            rb = native.RestoreBounds
            self.assertEqual((83, 62, 880, 660), (rb.X, rb.Y, rb.Width, rb.Height))

            # 2. Minimizing while in fullscreen keeps fullscreen intact and does not restore border
            self.assertTrue(self.app.toggle_fullscreen())
            self.assertTrue(self.app.get_fullscreen())
            native.WindowState = fake_wf.FormWindowState.Minimized
            events.resized.fire(0, 0)
            self.assertTrue(native.is_fullscreen)
            self.assertEqual(getattr(fake_wf.FormBorderStyle, "None"), native.FormBorderStyle)
            self.assertTrue(self.app.get_fullscreen())
            # Restoring from minimize back to Maximized keeps fullscreen intact
            native.WindowState = fake_wf.FormWindowState.Maximized
            events.restored.fire()
            self.assertTrue(native.is_fullscreen)
            self.assertEqual(getattr(fake_wf.FormBorderStyle, "None"), native.FormBorderStyle)
            self.assertTrue(self.app.get_fullscreen())
            self.assertFalse(self.app.toggle_fullscreen())

            # 3. Stale native.is_fullscreen=True with Normal WindowState -> first press enters cleanly
            native.WindowState = fake_wf.FormWindowState.Normal
            native.FormBorderStyle = getattr(fake_wf.FormBorderStyle, "None")
            native.old_style = fake_wf.FormBorderStyle.Sizable
            native.is_fullscreen = True
            self.assertFalse(self.app.get_fullscreen())
            self.assertTrue(self.app.toggle_fullscreen())
            self.assertTrue(self.app.get_fullscreen())
            self.assertEqual(fake_wf.FormBorderStyle.Sizable, native.old_style)
            self.assertFalse(self.app.toggle_fullscreen())
            self.assertEqual(fake_wf.FormWindowState.Normal, native.WindowState)

            # 4. External OS Restore triggering save_bounds clears stale is_fullscreen & FormBorderStyle
            native.is_fullscreen = True
            native.WindowState = fake_wf.FormWindowState.Normal
            native.FormBorderStyle = getattr(fake_wf.FormBorderStyle, "None")
            events.resized.fire(800, 600)
            self.assertFalse(native.is_fullscreen)
            self.assertEqual(fake_wf.FormBorderStyle.Sizable, native.FormBorderStyle)

        fake_webview = SimpleNamespace(
            FileDialog=SimpleNamespace(FOLDER=1),
            create_window=MagicMock(return_value=fake_win),
            start=MagicMock(side_effect=_fake_start),
        )
        fake_server = SimpleNamespace(_thread=None, start=MagicMock(), stop=MagicMock())
        fake_sock = MagicMock()
        fake_sock.getsockname.return_value = ("127.0.0.1", 8765)
        fake_hotkeys = SimpleNamespace(start=MagicMock(), stop=MagicMock(), set_enabled=MagicMock(), registration_status=MagicMock(return_value={}))
        with (
            MonkeyPatch.context() as launch_env,
            patch.dict(sys.modules, {
                "webview": fake_webview,
                "pystray": None,
                "System": fake_sys,
                "System.Drawing": fake_drawing,
                "System.Windows": fake_sys.Windows,
                "System.Windows.Forms": fake_wf,
            }),
            patch.object(desktop_mod.sys, "platform", "win32"),
            patch.object(desktop_mod, "is_webview2_installed", return_value=True),
            patch.object(desktop_mod.webbrowser, "open") as mock_browser_open,
            patch.object(desktop_mod, "BackgroundUvicornServer", return_value=fake_server),
            patch.object(desktop_mod, "GlobalHotkeyDaemon", return_value=fake_hotkeys),
            patch.object(desktop_mod, "resolve_brand_icon_paths", return_value=(None, None)),
            patch.object(desktop_mod, "set_windows_app_user_model_id"),
            patch.object(desktop_mod, "_lookup_window_hwnd", return_value=0),
        ):
            for name in ("PRUNER_STUDIO_NO_BROWSER", "PRUNER_STUDIO_NO_GUI",
                         "PRUNER_STUDIO_BROWSER", "PRUNER_STUDIO_MANAGED_HEADLESS"):
                launch_env.delenv(name, raising=False)
            launch_desktop_or_server(
                MagicMock(),
                fake_sock,
                self.root,
                application=self.app,
                web_dir=self.root,
            )
            self.assertTrue(ran_start[0], "webview.start test callback did not execute")
            fake_webview.start.assert_called_once()
            mock_browser_open.assert_not_called()
