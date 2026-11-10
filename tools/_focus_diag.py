"""诊断：force_foreground 对「别的进程的窗口」（Edge）为什么失败。

`_focus_check.py` 里 force_foreground 对本进程 tkinter 窗口 9/9 通过；
但真实目标是浏览器这种外进程窗口，需要单独验证。

Windows 的前台锁规则：只有「当前拥有前台的进程」或「最近收到过用户输入的
进程」才能 SetForegroundWindow。我们靠模拟一次 Alt 按键来取得资格，
这一步是否真生效、AttachThreadInput 是否配对成功，都必须实测。

输出每一步的中间状态，最后给出结论。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

SERVER_DIR = _PROJECT_ROOT / "server"
sys.path.insert(0, str(SERVER_DIR))

PROBE_TITLE = "FOCUSDIAG-9b21"
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def enum_windows() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            found.append((int(hwnd), window_title(hwnd)))
        return True

    user32.EnumWindows(_cb, 0)
    return found


def find_edge() -> str | None:
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def main() -> int:
    edge = find_edge()
    if not edge:
        print("找不到 Edge")
        return 1

    profile = Path(tempfile.mkdtemp(prefix="focusdiag-"))
    dbg = 9631
    proc = subprocess.Popen(
        [edge, f"--remote-debugging-port={dbg}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--window-size=520,760", "--window-position=60,60", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    lines: list[str] = []

    def log(text: str) -> None:
        print(text)
        lines.append(text)

    try:
        time.sleep(4.0)

        # 找一个标题含 Edge 的可见窗口
        wins = enum_windows()
        edge_win = [(h, t) for h, t in wins if "edge" in t.casefold() or "about:blank" in t.casefold()]
        log(f"可见窗口总数={len(wins)}")
        log(f"疑似 Edge 窗口={[(h, t[:60]) for h, t in edge_win[:5]]}")
        if not edge_win:
            log("FAIL 没找到 Edge 窗口，无法继续")
            return 1

        hwnd, title = edge_win[0]
        fg = int(user32.GetForegroundWindow() or 0)
        log(f"目标 hwnd={hwnd} title={title[:60]!r}")
        log(f"当前前台 hwnd={fg} title={window_title(fg)[:60]!r}")

        import apps as apps_module
        import win32api
        import win32con
        import win32gui
        import win32process

        # 步骤 1：Alt 骗过前台锁
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        log("步骤1 已模拟 Alt 按下抬起")

        # 步骤 2：AttachThreadInput
        my_tid = win32api.GetCurrentThreadId()
        fg_hwnd = int(win32gui.GetForegroundWindow() or 0)
        fg_tid = win32process.GetWindowThreadProcessId(fg_hwnd)[0] if fg_hwnd else 0
        log(f"步骤2 线程 my={my_tid} fg={fg_tid} 相同={my_tid == fg_tid}")
        attached = False
        try:
            if fg_tid and fg_tid != my_tid:
                attached = bool(win32process.AttachThreadInput(my_tid, fg_tid, True))
                log(f"步骤2 AttachThreadInput={attached}")
            try:
                win32gui.BringWindowToTop(hwnd)
                log("步骤2 BringWindowToTop ok")
            except Exception as exc:  # noqa: BLE001
                log(f"步骤2 BringWindowToTop 异常 {exc}")
            win32gui.SetForegroundWindow(hwnd)
            log("步骤2 SetForegroundWindow 已调用")
            time.sleep(0.25)
            now_fg = int(win32gui.GetForegroundWindow() or 0)
            log(f"步骤2 调用后前台={now_fg} 命中={now_fg == hwnd}")
        finally:
            if fg_tid and fg_tid != my_tid:
                win32process.AttachThreadInput(my_tid, fg_tid, False)

        # 步骤 3：再走一遍封装好的 force_foreground
        ok = apps_module.force_foreground(hwnd)
        now_fg = int(user32.GetForegroundWindow() or 0)
        log(f"步骤3 force_foreground={ok} 实际前台={now_fg} 命中={now_fg == hwnd}")

        # 步骤 4：_try_focus 按标题
        ok2 = apps_module._try_focus("FOCUSDIAG")  # 标题里没有这个，应该 False
        log(f"步骤4 _try_focus('FOCUSDIAG')={ok2}（标题不含该串，预期 False）")
        ok3 = apps_module._try_focus("about:blank")
        now_fg = int(user32.GetForegroundWindow() or 0)
        log(f"步骤4 _try_focus('about:blank')={ok3} 前台={now_fg} 命中={now_fg == hwnd}")

        # 步骤 5：真实验证 —— 注入一个 a 看能不能落进去需要页面监听，
        # 这里只确认前台是否真的移到 Edge。
        final_fg = int(user32.GetForegroundWindow() or 0)
        verdict = final_fg == hwnd
        log(f"结论：前台已移到目标窗口={verdict}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        time.sleep(0.4)
        shutil.rmtree(profile, ignore_errors=True)

    _PROJECT_ROOT.joinpath("_focus_diag_result.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
