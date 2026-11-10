"""端到端按键取证：有头浏览器窗口 + 真实服务下发 key.press + 读回浏览器实收。

为什么要「有头」：`--headless=new` 的窗口不是真正可置前的顶层窗口，
`GetForegroundWindow()` 拿不到它，SendInput 的按键就落不进页面，
测出来全是「没收到」——那是测试环境问题，不是产品问题。

本脚本做的事
------------
1. 起一个**有头** Edge，打开遥控页，把 document.title 改成唯一标记，
   页内装 keydown/keyup 监听，记录 key / code / keyCode / which / scan。
2. 用 server.apps.force_foreground 把这个窗口真正切到前台。
3. 连遥控服务 WS，逐个下发 key.press（和手机点按钮同一条
   dispatcher -> _ensure_target_foreground -> input_win.key_press 路径）。
4. 通过 CDP 读回浏览器实收事件，逐键比对。

读结论
------
- right / d 都有记录 → 注入链路没问题，问题在目标应用自身的绑定或前台漂移。
- 某个键没有记录 → 就在注入这一层；再看它的 keyCode / code 和别的键差在哪。

用法
----
    python _e2e_keys.py [PORT]      # 默认 8831，需要该端口上已有服务在跑
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
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

RESULT_PATH = _PROJECT_ROOT / "_e2e_keys_result.txt"
PROBE_TITLE = "E2EKEYPROBE-7f3a"

# 服务默认 HTTPS（自签名）：脚本必须跟着走，否则 TLS 握手直接断连。
SCHEME = os.environ.get("E2E_SCHEME", "https")
HTTP_SCHEME = "https" if SCHEME == "https" else "http"
WS_SCHEME = "wss" if SCHEME == "https" else "ws"


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

KEYS = ["up", "down", "left", "right", "w", "a", "s", "d", "b", "j", "h", "space"]
EXPECT = {
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "space": " ",
}

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


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
                raise RuntimeError(f"{method} 失败: {msg['error']}")
            return msg.get("result", {})


async def wait_devtools(port: int, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    async with aiohttp.ClientSession() as session:
        while time.time() < deadline:
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    return (await resp.json())["webSocketDebuggerUrl"]
            except Exception:
                await asyncio.sleep(0.3)
    raise RuntimeError("Edge 调试端口未就绪")


LISTENER_JS = f"""
(() => {{
  document.title = {json.dumps(PROBE_TITLE)};
  window.__keys = [];
  const push = (kind, ev) => window.__keys.push({{
    kind, key: ev.key, code: ev.code, keyCode: ev.keyCode,
    which: ev.which, t: Math.round(performance.now()),
  }});
  window.addEventListener('keydown', ev => push('down', ev), true);
  window.addEventListener('keyup', ev => push('up', ev), true);
  return {json.dumps(PROBE_TITLE)};
}})()
"""

READ_JS = "JSON.parse(JSON.stringify({keys: window.__keys || [], title: document.title, focus: document.hasFocus()}))"


async def main() -> int:
    port_service = int(sys.argv[1]) if len(sys.argv) > 1 else 8831
    results: list[tuple[bool, str]] = []

    def record(ok: bool, message: str) -> None:
        results.append((ok, message))
        print(f"  [{'PASS' if ok else 'FAIL'}] {message}")

    edge = find_edge()
    if not edge:
        record(False, "找不到 Edge / Chrome")
        return report(results)

    profile = Path(tempfile.mkdtemp(prefix="e2e-keys-"))
    dbg = 9623
    # 关键：不要 --headless；有头窗口才能被真正置前，按键才有落脚点。
    proc = subprocess.Popen(
        [
            edge,
            f"--remote-debugging-port={dbg}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            # 服务现在是 HTTPS + 自签名证书：不跳过校验就会停在「隐私错误」页，
            # 页面里没有我们的应用，按键自然无处落脚（会误判成「按键没收到」）。
            "--ignore-certificate-errors",
            "--allow-insecure-localhost",
            "--window-size=520,760",
            "--window-position=60,60",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        browser_ws_url = await wait_devtools(dbg)
        record(True, f"有头浏览器已启动（调试端口 {dbg}）")

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(browser_ws_url, max_msg_size=0) as bws:
                mid = [0]
                target = await cdp_call(bws, mid, "Target.createTarget",
                                        {"url": f"{HTTP_SCHEME}://127.0.0.1:{port_service}/"})
                attach = await cdp_call(bws, mid, "Target.attachToTarget",
                                        {"targetId": target["targetId"], "flatten": True})
                sid = attach["sessionId"]
                await cdp_call(bws, mid, "Page.enable", {}, sid)
                await cdp_call(bws, mid, "Runtime.enable", {}, sid)
                await asyncio.sleep(2.5)
                title = await cdp_call(bws, mid, "Runtime.evaluate",
                                       {"expression": LISTENER_JS, "returnByValue": True}, sid)
                record(title.get("result", {}).get("value") == PROBE_TITLE,
                       f"页面监听已装好，标题标记={title.get('result', {}).get('value')!r}")

                import apps as apps_module
                # document.title 改完 → 窗口标题栏更新有延迟（实测可达数秒），
                # 必须轮询等到窗口标题真的带上标记再切前台，否则 _try_focus 找不到窗口，
                # 按键会打进别处 —— 这正是上一版 12 个键全「没收到」的原因。
                focused = False
                waited = 0.0
                while waited < 12.0:
                    if apps_module._try_focus(PROBE_TITLE):  # noqa: SLF001 - 取证脚本故意直连
                        focused = True
                        break
                    await asyncio.sleep(0.5)
                    waited += 0.5
                record(focused, f"浏览器窗口已切到前台（按标题 {PROBE_TITLE!r} 匹配，"
                                f"等待 {waited:.1f}s）")
                await asyncio.sleep(0.8)

                async with session.ws_connect(
                    f"{WS_SCHEME}://127.0.0.1:{port_service}/ws",
                    max_msg_size=0,
                    ssl=_ssl_ctx() if WS_SCHEME == "wss" else None,
                ) as sws:
                    hello = json.loads(await sws.receive_str())
                    record(hello.get("type") == "hello",
                           f"遥控服务握手成功：{hello.get('type')}")

                    for key in KEYS:
                        await sws.send_json({
                            "type": "key.press",
                            "payload": {"keys": [key]},
                            "ts": int(time.time() * 1000),
                            "id": f"e2e-{key}",
                        })
                        await asyncio.sleep(0.16)
                        try:
                            while True:
                                msg = json.loads(await asyncio.wait_for(sws.receive_str(), 0.05))
                                if msg.get("payload", {}).get("id") == f"e2e-{key}":
                                    break
                        except asyncio.TimeoutError:
                            pass

                    await asyncio.sleep(0.5)
                    got = await cdp_call(bws, mid, "Runtime.evaluate",
                                         {"expression": READ_JS, "returnByValue": True}, sid)
                    payload = got.get("result", {}).get("value") or {}
                    events = payload.get("keys", [])
                    print(f"        页面 focus={payload.get('focus')} title={payload.get('title')!r}")

                    seen: dict[str, dict[str, Any]] = {}
                    for ev in events:
                        if ev.get("kind") == "down":
                            seen[ev.get("key", "")] = ev
                    print(f"        浏览器实收 keydown {len(seen)} 种：{sorted(seen)}")

                    for key in KEYS:
                        expect = EXPECT.get(key, key)
                        hit = seen.get(expect)
                        if hit:
                            record(True, f"{key:6s} → 收到 key={expect!r} "
                                         f"code={hit.get('code')!r} keyCode={hit.get('keyCode')}")
                        else:
                            record(False, f"{key:6s} → **没收到** {expect!r}")
    except Exception as exc:  # noqa: BLE001
        record(False, f"执行异常：{type(exc).__name__}: {exc}")
    finally:
        # Edge 是多进程的：proc.terminate() 只杀掉启动器，渲染进程/GPU 进程
        # 会活下来并继续持有窗口，下一次运行时多个窗口抢焦点，按键就打偏了
        # （表现为时好时坏的 8/16、4/16）。必须整棵进程树杀掉。
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=6)
        except Exception:
            pass
        await asyncio.sleep(0.8)
        shutil.rmtree(profile, ignore_errors=True)

    return report(results)


def report(results: list[tuple[bool, str]]) -> int:
    passed = sum(1 for ok, _ in results if ok)
    failed = len(results) - passed
    lines = [("PASS" if ok else "FAIL") + "\t" + msg for ok, msg in results]
    lines.append(f"合计：PASS={passed}  FAIL={failed}  TOTAL={len(results)}")
    text = "\n".join(lines)
    print(f"E2E_KEYS_RESULT PASS={passed} FAIL={failed} TOTAL={len(results)}")
    try:
        RESULT_PATH.write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
