"""注入保真度实验：带扫描码 vs 不带扫描码，浏览器看到的 code 有何不同。

背景
----
我们注入按键时 `_key_input` 始终把 `wScan` 设为 0、也不带
`KEYEVENTF_SCANCODE`。E2E 实测浏览器收到的 keydown 是
`key='d' keyCode=68 code=''` —— **`code` 是空的**，而物理键盘一定是
`code='KeyD'`。

网页播放器读键的方式各不相同：有的读 `event.key`，有的读 `event.code`。
`code` 为空时，靠 `code` 匹配的那一类快捷键就会「点了没反应」，
而靠 `key` 匹配的照常工作 —— 这正是「a/s/w 能用、d 不能用」的典型形态。

本脚本做对照实验
----------------
同一个浏览器窗口，同一批键，分别用两种方式注入，读回浏览器实收：
  方式 A：现状（wScan=0，无 SCANCODE 标志）
  方式 B：wScan=MapVirtualKey(vk)，同样无 SCANCODE 标志（让系统同时带 VK 和扫描码）
  方式 C：wScan=MapVirtualKey(vk)，带 KEYEVENTF_SCANCODE（完全模拟物理键）

只要 B 或 C 能让 `code` 变成 `KeyD` / `ArrowRight`，就说明修法成立。

用法：python _inject_fidelity.py [PORT]
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

SERVER_DIR = _PROJECT_ROOT / "server"
sys.path.insert(0, str(SERVER_DIR))

RESULT_PATH = _PROJECT_ROOT / "_inject_fidelity_result.txt"
PROBE_TITLE = "FIDELITY-4c8e"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

user32 = ctypes.windll.user32
user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint
MAPVK_VK_TO_VSC = 0


def find_edge() -> str | None:
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


async def cdp_call(ws, mid: list[int], method: str,
                   params: dict | None = None, session_id: str | None = None) -> dict:
    mid[0] += 1
    token = mid[0]
    frame: dict[str, Any] = {"id": token, "method": method, "params": params or {}}
    if session_id:
        frame["sessionId"] = session_id
    await ws.send_json(frame)
    while True:
        msg = json.loads(await ws.receive_str())
        if msg.get("id") == token:
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg.get("result", {})


async def wait_devtools(port: int, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as session:
        while time.time() < deadline:
            try:
                async with session.get(f"http://127.0.0.1:{port}/json/version",
                                       timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    return (await resp.json())["webSocketDebuggerUrl"]
            except Exception:
                await asyncio.sleep(0.3)
    raise RuntimeError("调试端口未就绪")


LISTENER_JS = f"""
(() => {{
  document.title = {json.dumps(PROBE_TITLE)};
  window.__keys = [];
  const push = (kind, ev) => window.__keys.push({{ kind, key: ev.key, code: ev.code, keyCode: ev.keyCode }});
  window.addEventListener('keydown', ev => push('down', ev), true);
  window.addEventListener('keyup', ev => push('up', ev), true);
  return true;
}})()
"""

READ_JS = "JSON.parse(JSON.stringify(window.__keys || []))"
CLEAR_JS = "(() => { window.__keys = []; return true; })()"


def send_key(input_win, key: str, mode: str) -> None:
    """三种注入方式。"""
    vk = input_win.resolve_key(key)
    scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)

    if mode == "A":
        input_win.key_press([key])
        return

    import ctypes as ct
    flags_down = 0
    flags_up = input_win.KEYEVENTF_KEYUP
    if mode == "C":
        flags_down |= input_win.KEYEVENTF_SCANCODE
        flags_up |= input_win.KEYEVENTF_SCANCODE
    if vk in input_win._EXTENDED_VKS:  # noqa: SLF001 - 实验脚本
        flags_down |= input_win.KEYEVENTF_EXTENDEDKEY
        flags_up |= input_win.KEYEVENTF_EXTENDEDKEY

    down = input_win._key_input(vk, flags_down, scan)  # noqa: SLF001
    up = input_win._key_input(vk, flags_up, scan)      # noqa: SLF001
    input_win._send([down])                            # noqa: SLF001
    time.sleep(0.012)
    input_win._send([up])                              # noqa: SLF001


async def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8831
    edge = find_edge()
    if not edge:
        print("找不到 Edge")
        return 1

    import input_win

    profile = Path(tempfile.mkdtemp(prefix="fidelity-"))
    dbg = 9651
    proc = subprocess.Popen(
        [edge, f"--remote-debugging-port={dbg}", f"--user-data-dir={profile}",
         "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--window-size=520,760", "--window-position=60,60", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    lines: list[str] = []
    passed = failed = 0

    def record(ok: bool, text: str) -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
        else:
            failed += 1
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {text}")
        lines.append(f"[{mark}] {text}")

    try:
        ws_url = await wait_devtools(dbg)
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, max_msg_size=0) as bws:
                mid = [0]
                t = await cdp_call(bws, mid, "Target.createTarget",
                                   {"url": f"http://127.0.0.1:{port}/"})
                a = await cdp_call(bws, mid, "Target.attachToTarget",
                                   {"targetId": t["targetId"], "flatten": True})
                sid = a["sessionId"]
                await cdp_call(bws, mid, "Page.enable", {}, sid)
                await cdp_call(bws, mid, "Runtime.enable", {}, sid)
                await asyncio.sleep(2.5)
                await cdp_call(bws, mid, "Runtime.evaluate",
                               {"expression": LISTENER_JS, "returnByValue": True}, sid)

                import apps as apps_module
                ok_focus = False
                waited = 0.0
                while waited < 12.0:
                    if apps_module._try_focus(PROBE_TITLE):  # noqa: SLF001
                        ok_focus = True
                        break
                    await asyncio.sleep(0.5)
                    waited += 0.5
                record(ok_focus, f"窗口已置前（等 {waited:.1f}s）")
                await asyncio.sleep(0.6)

                for mode in ("A", "B", "C"):
                    label = {"A": "现状 wScan=0 无SCANCODE",
                             "B": "带扫描码 无SCANCODE",
                             "C": "带扫描码 + SCANCODE"}[mode]
                    print(f"\n  --- 方式 {mode}：{label} ---")
                    for key in ("d", "right", "w"):
                        await cdp_call(bws, mid, "Runtime.evaluate",
                                       {"expression": CLEAR_JS, "returnByValue": True}, sid)
                        send_key(input_win, key, mode)
                        await asyncio.sleep(0.25)
                        got = await cdp_call(bws, mid, "Runtime.evaluate",
                                             {"expression": READ_JS, "returnByValue": True}, sid)
                        events = got.get("result", {}).get("value") or []
                        downs = [e for e in events if e.get("kind") == "down"]
                        if downs:
                            e = downs[0]
                            has_code = bool(e.get("code"))
                            record(has_code,
                                   f"{mode} {key:6s} key={e.get('key')!r} "
                                   f"code={e.get('code')!r} keyCode={e.get('keyCode')}")
                        else:
                            record(False, f"{mode} {key:6s} 没收到")
    except Exception as exc:  # noqa: BLE001
        record(False, f"异常：{type(exc).__name__}: {exc}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=6)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        await asyncio.sleep(0.4)
        shutil.rmtree(profile, ignore_errors=True)

    summary = f"INJECT_FIDELITY PASS={passed} FAIL={failed} TOTAL={passed + failed}"
    print(f"\n{summary}")
    lines.append(summary)
    RESULT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
