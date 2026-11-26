"""Check foreground routing and physical mouse motion handling."""

import ctypes
import time
from pathlib import Path
from runpy import run_path
from types import SimpleNamespace
from unittest.mock import patch

import win32con
from windows_mcp.desktop import control_hooks
from windows_mcp.desktop.control_win32 import _MouseHookData

can_fall_back = run_path(str(Path(__file__).with_name("server.py")))["can_fall_back"]


def result(code, delivery="not_delivered", retry="safe"):
    return SimpleNamespace(structured_content={
        "ok": False,
        "error": {"code": code, "disposition": {"delivery": delivery, "retry": retry}},
    })


assert can_fall_back(result("ACTION_NOT_SUPPORTED"))
assert not can_fall_back(result("ACTION_NOT_SUPPORTED", "unknown", "unsafe"))
assert not can_fall_back(result("ACTION_NOT_SUPPORTED", retry="unsafe"))
assert not can_fall_back(result("PERM_DENIED"))
assert not can_fall_back(result("STALE_REF"))
assert not can_fall_back(SimpleNamespace(structured_content={"ok": True}))
print("Foreground fallback checks passed.")

foreground_control = run_path(str(Path(__file__).with_name("windows_mcp_server.py")))["ForegroundControl"]()
event = _MouseHookData()
event.pt.x, event.pt.y = 100, 200
with patch.object(control_hooks._user32, "CallNextHookEx", return_value=0):
    assert foreground_control._physical_mouse(0, win32con.WM_MOUSEMOVE, ctypes.addressof(event)) == 0
    foreground_control._state = "ai"
    foreground_control._suppress = True
    foreground_control._deadline = time.monotonic() + 2
    event.pt.x = 300
    assert foreground_control._physical_mouse(0, win32con.WM_MOUSEMOVE, ctypes.addressof(event)) == 1

    with patch.object(control_hooks, "_read_raw_mouse", return_value=(1, 2000, 2000)):
        foreground_control._raw_input(0)
    assert foreground_control._last_physical_event == 0 and foreground_control._events.empty()

    event.flags = 1
    assert foreground_control._physical_mouse(0, win32con.WM_MOUSEMOVE, ctypes.addressof(event)) == 0
    event.flags = 0
    assert foreground_control._physical_mouse(0, win32con.WM_LBUTTONDOWN, ctypes.addressof(event)) == 1
    assert foreground_control._mouse_down and foreground_control._last_physical_event > 0
print("Physical mouse movement checks passed.")
