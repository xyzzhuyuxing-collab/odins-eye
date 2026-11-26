"""Check live browser routing and preserve the existing Edge tab and session."""

import asyncio
import json
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import perf_counter
from urllib.request import urlopen

import win32con
import win32gui
from fastmcp import Client
from fastmcp.client.transports import StdioTransport


ROOT = Path(__file__).resolve().parent
OPEN_EDGE = """
const { readFile } = require('node:fs/promises');
const { join } = require('node:path');
const { chromium } = require('playwright');
const [profile, url] = process.argv.slice(1);
(async () => {
    const context = await chromium.launchPersistentContext(profile, {
        channel: 'msedge', headless: false, args: ['--remote-debugging-port=0'],
    });
    try {
        await context.pages()[0].goto(url + 'sign-in');
        const port = (await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\\n')[0];
        console.log(Number(port));
        process.stdin.resume();
        await new Promise(resolve => process.stdin.once('end', resolve));
    } finally {
        await context.close();
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


class SearchPage(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/json/version":
            if self.server.debug_endpoint is None:
                self.send_error(503)
                return
            with urlopen(f"{self.server.debug_endpoint}/json/version", timeout=5) as response:
                version = response.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(version)
            return
        if self.path == "/sign-in":
            self.send_response(303)
            self.send_header("Set-Cookie", "fixture=logged-in; Path=/; HttpOnly")
            self.send_header("Location", "/")
            self.end_headers()
            return
        pages = {
            "/": '<title>Desktop Control Route Test</title>'
                 '<form><label>Search <input name="q"></label><button>Search</button></form>',
            "/?q=Elon+Musk": '<a href="/parody">Elon Musk @notelonmusk</a>'
                            '<a href="/elonmusk">Elon Musk @elonmusk</a>',
            "/elonmusk": "<h1>Verified profile @elonmusk</h1>",
        }
        page = pages.get(self.path) if "fixture=logged-in" in self.headers.get("Cookie", "") else None
        if page is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(page.encode("utf-8"))

    def log_message(self, *_):
        pass


def open_tabs(endpoint):
    with urlopen(f"{endpoint}/json/list", timeout=5) as response:
        return {tab["id"]: tab["url"] for tab in json.load(response) if tab["type"] == "page"}


def service(endpoint):
    transport = StdioTransport(sys.executable, ["-I", "-B", str(ROOT / "server.py")],
                               env={"PLAYWRIGHT_MCP_CDP_ENDPOINT": endpoint}, keep_alive=False)
    return Client(transport, timeout=40)


async def check_browser(url, endpoint, site):
    original_tabs = open_tabs(endpoint)
    assert list(original_tabs.values()) == [url], original_tabs
    async with service(url.rstrip("/")) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {"desktop_observe", "desktop_run", "browser_run", "browser_disconnect", "browser_click",
                "browser_verify_text_visible"} <= names, names
        assert "browser_close" not in names

        async def route(tool, **arguments):
            result = await client.call_tool_mcp("browser_run", {"tool": tool, "arguments": arguments})
            content = "\n".join(block.text for block in result.content if block.type == "text")
            assert not result.is_error, content
            assert result.structured_content["backend"] == "playwright", result.structured_content["backend"]
            return content

        windows = await client.call_tool_mcp("browser_run", {})
        assert not windows.is_error and windows.structured_content["backend"] == "desktop"
        fixture = [window for window in windows.structured_content["result"]["data"]
                   if "Desktop Control Route Test" in window["title"]]
        assert len(fixture) == 1, fixture
        hwnd = int(fixture[0]["id"].removeprefix("w-"))
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        assert win32gui.IsIconic(hwnd)
        alternate = [{"background": {"operation": "restore", "arguments": {"window_id": fixture[0]["id"]}}}]
        restored = await client.call_tool_mcp("browser_run", {"desktop_steps": alternate})
        assert not restored.is_error and restored.structured_content["backend"] == "desktop"
        assert restored.structured_content["result"]["steps"][0]["backend"] == "agent-desktop"
        assert not win32gui.IsIconic(hwnd)

        site.debug_endpoint = endpoint
        started = perf_counter()
        try:
            snapshot = await route("browser_snapshot")
            assert 'textbox "Search"' in snapshot, snapshot
            await route("browser_type", target='role=textbox[name="Search"]', text="Elon Musk", submit=True)
            snapshot = await route("browser_snapshot")
            account = re.search(r'link "Elon Musk @elonmusk" \[ref=([^\]\s]+)\]', snapshot)
            assert account is not None, snapshot
            profile = await route("browser_click", target=account.group(1))
            assert f"Page URL: {url}elonmusk" in profile, profile
            await route("browser_verify_text_visible", text="Verified profile @elonmusk")
            rejected = await client.call_tool_mcp("browser_run", {
                "tool": "browser_click", "arguments": {}, "desktop_steps": alternate,
            })
            assert rejected.is_error, "An invalid Playwright action must not run the desktop alternative"
        finally:
            released = await client.call_tool_mcp("browser_disconnect", {})
            assert not released.is_error, released.content
    assert open_tabs(endpoint) == {next(iter(original_tabs)): f"{url}elonmusk"}
    print(f"Switched from desktop to Playwright in one service session. Kept the existing Edge tab open. "
          f"Attached flow: {perf_counter() - started:.2f}s.")


async def run_check(site):
    url = f"http://127.0.0.1:{site.server_port}/"
    with TemporaryDirectory(prefix="desktop-control-edge-test-") as profile:
        edge = await asyncio.create_subprocess_exec(
            "node.exe", "-e", OPEN_EDGE, profile, url, cwd=str(ROOT),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            port = int(await asyncio.wait_for(edge.stdout.readline(), timeout=25))
            await check_browser(url, f"http://127.0.0.1:{port}", site)
        finally:
            edge.stdin.close()
            try:
                await asyncio.wait_for(edge.wait(), timeout=15)
            except TimeoutError:
                edge.kill()
                await edge.wait()
        assert edge.returncode == 0, edge.returncode


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", 0), SearchPage) as site:
        site.debug_endpoint = None
        thread = Thread(target=site.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(run_check(site))
        finally:
            site.shutdown()
            thread.join()
