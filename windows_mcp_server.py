"""Start this project's Windows MCP with motion-independent foreground control."""

import ctypes

import win32con
from windows_mcp.desktop import control
from windows_mcp.desktop.control_win32 import _MouseHookData


class ForegroundControl(control.ControlCoordinator):
    def _physical_mouse(self, code, wparam, lparam):
        if code >= 0 and wparam == win32con.WM_MOUSEMOVE:
            # Keep Windows MCP's pointer handling and omit motion from activity tracking.
            point = ctypes.cast(lparam, ctypes.POINTER(_MouseHookData)).contents.pt
            self._last_point = (int(point.x), int(point.y))
        return super()._physical_mouse(code, wparam, lparam)

    def _raw_input(self, lparam):
        # This hook reports relative mouse motion only.
        pass


if __name__ == "__main__":
    from windows_mcp.__main__ import main

    control._controller = ForegroundControl()
    main()
