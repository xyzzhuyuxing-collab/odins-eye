"""Operate existing browser and desktop windows through their available controls."""

import asyncio
import os
import platform
import sys
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import win32con
import win32event
import win32gui
import win32process
from anyio import CancelScope
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ClientError, McpError, ToolError
from fastmcp.server.providers.proxy import ProxyClient, ProxyProvider
from fastmcp.tools.base import ToolResult
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


ROOT = Path(__file__).resolve().parent
ARCH = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64"}[platform.machine().lower()]
AGENT_DESKTOP = ROOT / "node_modules" / "agent-desktop" / "bin" / f"agent-desktop-win32-{ARCH}.exe"
PLAYWRIGHT_CLI = ROOT / "node_modules" / "@playwright" / "mcp" / "cli.js"
WINDOWS_MCP_TOOLS = "ControlStatus,Wait,App,Click,Type,Move,Scroll,Shortcut"
BROWSER_CONNECTION_ERRORS = (ClientError, McpError, OSError, RuntimeError, TimeoutError)
READ_OPERATIONS = Literal[
    "status", "list_apps", "list_windows", "list_displays", "snapshot", "find", "get", "is", "screenshot"
]
BACKGROUND_OPERATIONS = Literal[
    "click", "set_value", "clear", "select", "toggle", "check", "uncheck", "expand", "collapse", "scroll", "scroll_to",
    "restore",
]
Point = tuple[StrictInt, StrictInt]


class BackgroundAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: BACKGROUND_OPERATIONS
    arguments: dict[str, Any] = Field(description="agent-desktop MCP arguments, including the current ref_id.")


class WindowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_id: str = Field(pattern=r"^w-[1-9][0-9]*$", description="Window ID from desktop_observe(list_windows).")


class Click(WindowAction):
    tool: Literal["Click"]
    loc: Point
    button: Literal["left", "right", "middle"] = "left"
    clicks: int = Field(default=1, ge=0, le=3)


class Type(WindowAction):
    tool: Literal["Type"]
    loc: Point
    text: str
    clear: bool = False
    press_enter: bool = False
    caret_position: Literal["start", "idle", "end"] = "idle"


class Move(WindowAction):
    tool: Literal["Move"]
    loc: Point
    from_loc: Point | None = Field(default=None, description="Set both points for a drag. Omit this for hover.")


class Scroll(WindowAction):
    tool: Literal["Scroll"]
    loc: Point
    direction: Literal["up", "down", "left", "right"]
    wheel_times: int = Field(default=1, ge=1)


class Shortcut(WindowAction):
    tool: Literal["Shortcut"]
    shortcut: str = Field(min_length=1)


ForegroundAction = Annotated[Click | Type | Move | Scroll | Shortcut, Field(discriminator="tool")]


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    background: BackgroundAction | None = None
    foreground: ForegroundAction | None = None

    @model_validator(mode="after")
    def needs_action(self):
        if self.background is None and self.foreground is None:
            raise ValueError("Provide a background or foreground action.")
        return self


def subprocess_client(command, args, env=None):
    return Client(StdioTransport(str(command), args, env=env, keep_alive=False), timeout=30)


def result_data(result):
    if result.structured_content is not None:
        return result.structured_content
    return {"messages": [block.text for block in result.content if block.type == "text"]}


def can_fall_back(result):
    data = result.structured_content
    if not isinstance(data, dict) or data.get("ok") is not False:
        return False
    error = data.get("error") or {}
    disposition = error.get("disposition") or {}
    return (
        error.get("code") in {"ACTION_NOT_SUPPORTED", "PLATFORM_NOT_SUPPORTED"}
        and disposition.get("delivery") == "not_delivered"
        and disposition.get("retry") == "safe"
    )


@contextmanager
def foreground_mutex():
    mutex = win32event.CreateMutex(None, False, r"Local\AgentDesktopWindowsMcpForeground")
    try:
        status = win32event.WaitForSingleObject(mutex, 0)
        if status not in (win32con.WAIT_OBJECT_0, win32con.WAIT_ABANDONED):
            raise ToolError("Another Odin's Eye call is using the foreground.")
        try:
            yield
        finally:
            win32event.ReleaseMutex(mutex)
    finally:
        mutex.Close()


@asynccontextmanager
async def take_foreground():
    args = ["-I", "-B", str(ROOT / "windows_mcp_server.py"), "serve", "--transport", "stdio",
            "--config", str(ROOT / "windows-mcp.toml"), "--control", "--control-policy", "strict",
            "--tools", WINDOWS_MCP_TOOLS]
    with foreground_mutex():
        async with subprocess_client(sys.executable, args, {"ANONYMIZED_TELEMETRY": "false"}) as windows:
            try:
                # Strict policy lets Wait claim the foreground without sending input.
                acquired = await windows.call_tool_mcp("Wait", {"duration": 0}, timeout=20)
                if acquired.is_error:
                    raise ToolError(f"Windows MCP did not acquire control: {result_data(acquired)}")
                yield windows
            finally:
                with CancelScope(shield=True):
                    released = await windows.call_tool_mcp("ControlStatus", {"release": True}, timeout=5)
                    if released.is_error or result_data(released).get("state") == "ai":
                        raise ToolError("Windows MCP did not confirm release. The foreground server is closing.")


def window_identity(hwnd):
    if not win32gui.IsWindow(hwnd):
        raise ToolError("The target window is gone. Observe it again before using foreground input.")
    return win32process.GetWindowThreadProcessId(hwnd)


async def run_foreground(windows, action, identity, rect):
    hwnd = int(action.window_id[2:])
    status = await windows.call_tool_mcp("ControlStatus", {}, timeout=5)
    if status.is_error or result_data(status).get("state") != "ai":
        raise ToolError("Windows MCP no longer has control. Observe again before acting.")
    if window_identity(hwnd) != identity or win32gui.GetWindowRect(hwnd) != rect:
        raise ToolError("The target window moved or changed before foreground control started. Observe it again.")
    if win32gui.GetForegroundWindow() != hwnd:
        title = win32gui.GetWindowText(hwnd)
        if not title:
            raise ToolError("The target window has no title for Windows MCP to activate.")
        focused = await windows.call_tool_mcp("App", {"mode": "switch", "name": title}, timeout=8)
        if focused.is_error or win32gui.GetForegroundWindow() != hwnd:
            raise ToolError("The target window did not come to the foreground. Observe it again.")

    arguments = action.model_dump(mode="json", exclude={"tool", "window_id"}, exclude_none=True)
    if isinstance(action, Move):
        arguments["drag"] = action.from_loc is not None
    if isinstance(action, Scroll):
        arguments["type"] = "horizontal" if action.direction in {"left", "right"} else "vertical"

    # Verify the observed coordinates after acquiring foreground control.
    if window_identity(hwnd) != identity or win32gui.GetWindowRect(hwnd) != rect:
        raise ToolError("The target window moved or changed. Refresh the coordinates.")
    if win32gui.GetForegroundWindow() != hwnd:
        raise ToolError("Focus left the target window before input. Observe it again.")
    for key in ("loc", "from_loc"):
        if key in arguments:
            target = win32gui.WindowFromPoint(tuple(arguments[key]))
            if win32gui.GetAncestor(target, win32con.GA_ROOT) != hwnd:
                raise ToolError("The coordinate is outside the active target window. Refresh the coordinates.")
    return await windows.call_tool_mcp(action.tool, arguments, timeout=10)


def step_result(results, backend, result):
    results.append({"backend": backend, "ok": not result.is_error, "result": result_data(result)})


def run_result(results, error=None):
    data = {"ok": error is None, "steps": results}
    if error is not None:
        data["error"] = error
    return ToolResult(structured_content=data, is_error=error is not None)


def browser_result(backend, result):
    return ToolResult(content=result.content, is_error=result.is_error,
                      structured_content={"backend": backend, "result": result.structured_content})


def edge_endpoint():
    configured = os.environ.get("PLAYWRIGHT_MCP_CDP_ENDPOINT", "").strip()
    if configured == "msedge":
        return configured
    if configured:
        try:
            address = urlsplit(configured)
            if (address.scheme in {"http", "https", "ws", "wss"}
                    and address.hostname in {"localhost", "127.0.0.1", "::1"}
                    and address.username is None and address.port):
                return configured
        except ValueError:
            pass
        return None

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        active_port = Path(local_app_data) / "Microsoft" / "Edge" / "User Data" / "DevToolsActivePort"
        if active_port.is_file():
            return "msedge"
    return None


def build_server(state):
    endpoint = edge_endpoint() or "msedge"
    browser = None
    node = which("node.exe")
    if node and PLAYWRIGHT_CLI.is_file():
        browser = ProxyClient(StdioTransport(node, [
            str(PLAYWRIGHT_CLI), "--browser=msedge", "--cdp-endpoint", endpoint, "--cdp-timeout=5000",
            "--caps=testing", "--no-webmcp", "--output-dir", str(Path(state).with_name("playwright")),
        ], cwd=str(ROOT), keep_alive=True), roots=[ROOT.as_uri()], timeout=90, forward_incoming_headers=False)
    browser_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield
        finally:
            if browser is not None:
                await browser.close()

    server = FastMCP("Odin's Eye", instructions=(
        "Start Edge tasks with browser_run. It checks the live connection and returns the existing tabs "
        "or Edge windows. Select the tab by title or URL. Use browser_run for actions with equivalent "
        "desktop_steps when the native targets have already been observed. If it returns Edge windows, "
        "continue the task with desktop_observe and desktop_run. Restore a minimized window by observed window_id, "
        "then observe again. Use browser_disconnect after Playwright work. Verify browser actions by checking "
        "the resulting URL or visible content. Edge security settings and connection prompts are handled by the user. "
        "For desktop actions, use desktop_observe to find windows and current accessibility refs and desktop_run to act. "
        "Prefer a background action, with a foreground alternative only when its desktop coordinates and "
        "target window have been observed. A foreground-only step is for a known pointer or keyboard action "
        "without a UI Automation equivalent. Windows MCP takes foreground control when needed. Group only "
        "consecutive actions whose targets are known. Observe again after the interface changes."
    ), lifespan=lifespan)
    if browser is not None:
        provider = ProxyProvider(lambda: browser)
        provider.disable(names={"browser_close"}, components={"tool"})
        server.add_provider(provider)

        @server.tool
        async def browser_disconnect() -> str:
            """Disconnect Playwright from Edge and leave its tabs open."""
            async with browser_lock:
                await browser.close()
            return "Disconnected from Edge."

    lock = asyncio.Lock()

    def primary_client():
        return subprocess_client(AGENT_DESKTOP, ["mcp"], {"AGENT_DESKTOP_HOME": state})

    @server.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def desktop_observe(operation: READ_OPERATIONS, arguments: dict[str, Any] | None = None) -> ToolResult:
        """Read app state. Use snapshot with window_id and include_bounds for UIA, or screenshot with screen for desktop coordinates."""
        arguments = arguments or {}
        if operation == "screenshot" and arguments.get("output_path") is not None:
            raise ToolError("Request a screenshot without output_path to return the image.")
        async with lock, primary_client() as primary:
            result = await primary.call_tool_mcp(f"desktop_{operation}", arguments)
        return ToolResult.from_mcp_result(result)

    @server.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                              "openWorldHint": False})
    async def desktop_run(steps: Annotated[list[Step], Field(min_length=1)]) -> ToolResult:
        """Run planned steps in order. Windows MCP takes over only for supported foreground alternatives."""
        results = []
        try:
            async with lock, primary_client() as primary, AsyncExitStack() as foreground:
                windows = None
                for step in steps:
                    if step.background is not None:
                        active_window = win32gui.GetForegroundWindow()
                        result = await primary.call_tool_mcp(f"desktop_{step.background.operation}",
                                                             step.background.arguments)
                        step_result(results, "agent-desktop", result)
                        if win32gui.GetForegroundWindow() != active_window:
                            results[-1]["foreground_window_changed"] = True
                        if not result.is_error:
                            await foreground.aclose()
                            windows = None
                            continue
                        if step.foreground is None or not can_fall_back(result):
                            return run_result(results, "Background action stopped. Observe the result before retrying.")

                    action = step.foreground
                    hwnd = int(action.window_id[2:])
                    identity = window_identity(hwnd)
                    rect = win32gui.GetWindowRect(hwnd)
                    if windows is None:
                        windows = await foreground.enter_async_context(take_foreground())
                    result = await run_foreground(windows, action, identity, rect)
                    step_result(results, "windows-mcp", result)
                    if result.is_error:
                        return run_result(results, "Foreground action stopped. Observe the result before retrying.")
        except ToolError as error:
            return run_result(results, str(error))
        return run_result(results)

    @server.tool
    async def browser_run(
        tool: Annotated[str, Field(pattern=r"^browser_[a-z_]+$")] = "browser_tabs",
        arguments: dict[str, Any] | None = None,
        desktop_steps: Annotated[list[Step] | None, Field(min_length=1)] = None,
    ) -> ToolResult:
        """Run a Playwright tool or its observed desktop alternative. With no arguments, locate existing Edge tabs or windows."""
        if tool in {"browser_close", "browser_disconnect"}:
            raise ToolError("Use browser_disconnect to release the browser connection.")
        arguments = arguments or ({"action": "list"} if tool == "browser_tabs" else {})
        if browser is not None and edge_endpoint():
            async with browser_lock:
                async with AsyncExitStack() as connection:
                    try:
                        await connection.enter_async_context(browser)
                        tabs = await browser.call_tool_mcp("browser_tabs", {"action": "list"}, timeout=8)
                    except BROWSER_CONNECTION_ERRORS:
                        tabs = None
                    if tabs is not None and not tabs.is_error:
                        result = tabs if tool == "browser_tabs" and arguments == {"action": "list"} else (
                            await browser.call_tool_mcp(tool, arguments)
                        )
                        return browser_result("playwright", ToolResult.from_mcp_result(result))
                # Only the read-only connection check can trigger the desktop route.
                with suppress(*BROWSER_CONNECTION_ERRORS):
                    await browser.close()

        if desktop_steps is not None:
            return browser_result("desktop", await desktop_run(desktop_steps))
        windows = await desktop_observe("list_windows", {"app": "msedge.exe"})
        return browser_result("desktop", windows)

    return server


if __name__ == "__main__":
    if not AGENT_DESKTOP.is_file():
        sys.exit("Run npm ci --ignore-scripts in the Odin's Eye project directory first.")
    # Use physical desktop coordinates even with Windows display scaling.
    import ctypes

    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    with TemporaryDirectory(prefix="desktop-control-") as state:
        # Let agent-desktop create its own access-controlled state directory.
        build_server(str(Path(state) / "agent-desktop")).run(transport="stdio", show_banner=False)
