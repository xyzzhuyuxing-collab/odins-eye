# 奥丁之眼 · Odin's Eye

奥丁之眼是一个本地 MCP 服务，把网页和 Windows 桌面操作放在同一个入口。网页任务操作用户已经打开的 Edge 窗口，沿用该窗口的登录状态。Edge 有可用的本机调试连接时，Playwright 可接入已有标签页。桌面任务用 `agent-desktop` 读取界面和调用控件。需要前台输入时，Windows MCP 直接开始操作，并在本轮操作结束、出错或恢复后台调用后交还桌面。

Odin's Eye is a local MCP server for existing Edge tabs and Windows apps. It uses Playwright when the browser connection is available, then falls back to agent-desktop and Windows MCP for desktop input.

## 在 Codex 中使用

需要 64 位 Windows 10 或 11、Python 3.14+ 和 Node.js 20+。操作桌面窗口需要 Windows 保持登录并解锁。网页工具使用本机安装的 Edge。本项目的组合流程已在 Windows 11 x64 上验收。

在项目目录的 PowerShell 中安装依赖。

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd ci --ignore-scripts
Copy-Item .codex/config.example.toml .codex/config.toml
```

把 `.codex/config.toml` 中的两个示例路径改成实际的项目路径。在 Codex 中打开并信任该项目，新会话会把奥丁之眼加载为 `odins_eye`。

## 工具调用

### 网页

- 每个 Edge 任务先调用 `browser_run`。它当场检查连接，返回已有标签页或 Edge 窗口列表。同一服务进程中，后续调用可以发现后来开放的连接。远程调试和连接许可在 Edge 内由用户确认。
- 给 `browser_run` 传入 Playwright 的 `tool` 和 `arguments`。如果已经观测到对应的桌面动作，可以一并提供 `desktop_steps`。连接检查失败时会执行这组桌面动作，或返回现有 Edge 窗口供接续操作。已经提交给 Playwright 的动作需要先核对结果，才能换工具继续。
- Playwright 使用 Edge 自己开放的调试接口，也可以通过环境变量 `PLAYWRIGHT_MCP_CDP_ENDPOINT` 指定本机回环端点。用 `browser_disconnect` 结束连接，已有标签页继续留在 Edge 中。
- 桌面操作用 `desktop_observe` 读取原 Edge 窗口。窗口已最小化时，先用 `desktop_run` 的 `restore` 和该窗口的 `window_id` 恢复，再读取页面控件。

### 桌面

- `desktop_observe` 通过 `list_windows` 找窗口，通过 `snapshot` 读取当前控件编号和可用动作。传 `include_bounds: true` 可以看控件位置。纯画布可以用 `screenshot` 获取画面。
- `desktop_run` 按顺序执行 `steps`。每步可以给 `background` 动作及对等的 `foreground` 动作。后台明确返回动作不受支持且未执行时，才采用对应的前台动作。如果只能用实际指针拖拽等方式，可提交单独的 `foreground` 动作。
- 前台步骤使用最近一次观测中的窗口 ID 和绝对桌面坐标。已定位的连续动作可以放在同一次调用中。界面布局变化后重新观测目标与坐标。

```json
{
  "steps": [
    {
      "background": {
        "operation": "click",
        "arguments": {"ref_id": "@s8f3k2p9:e8"}
      },
      "foreground": {"tool": "Click", "window_id": "w-12345", "loc": [420, 320]}
    }
  ]
}
```

示例中的编号与坐标来自一次界面观测。可用的前台操作包括点击、键入、拖拽、滚动和快捷键。前台操作期间，系统指针由工具驱动，移动鼠标时当前动作会继续。按 `Ctrl+Alt+Shift+Backspace` 可以收回控制。

`foreground_window_changed: true` 表示后台调用前后检测到前台窗口变化。有些应用响应 UI Automation 时会自行把窗口前置。坐标输入前，工具会检查焦点和目标窗口。执行结果应通过重新读取界面确认。

## 本机验收

2026 年 11 月 26 日，Playwright 用两个独立的 Chromium 窗口验收了后台点击、前台切换、连续点击和拖拽，再回到 UIA 点击。前台阶段启动了一次 Windows MCP，倒计时窗口数为 0，恢复后台后前台进程退出。

`test_routing.py` 验证了动作切换条件，并用模拟的物理鼠标事件检查普通移动和原始设备位移都不会触发中断。

`test_browser.py` 验收同一 MCP 会话内的桌面操作、连接恢复后接入现有标签页、复用测试登录状态和断开后保留标签页。用运行 `server.py` 的 Python 环境执行该检查。

```powershell
.\.venv\Scripts\python.exe -B test_routing.py
.\.venv\Scripts\python.exe -B test_browser.py
```

## 许可证

项目源码使用 MIT 许可证。通过依赖安装的项目各自使用其原有许可证。[MIT 许可证](LICENSE)
