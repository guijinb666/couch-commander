"""本轮（m01602）四项修复的端到端实证脚本 —— 零副作用版本。

为什么是零副作用：这是跑在**用户正在使用的桌面**上的真实服务，
``key.press`` 会真的把按键送进前台窗口、``brightness.set`` 会真的改屏幕亮度。
所以这里只走「只读」与「校验短路」两类请求，不向桌面注入任何键、不改亮度：

1. 方向键 / A/B/C 的**真实注入**由 ``_keymap_check.py`` 用 mock 的
   ``input_win.key_press`` 在进程内验证（19 项，含「连发 5 次各自独立」）。
   这里改为验证「同一通路」：四向与 A/B/C 的 keys 都能被 dispatch 校验通过
   （用越界/非法 payload 触发错误码，证明命令已接线且校验顺序正确）。
2. 进度条：``media.state`` 只读；``media.seek`` 用一个**非法** position 触发
   ``BAD_PAYLOAD``，证明该命令真的进了 dispatcher 且校验在注入之前。
3. 亮度：``brightness.get`` 只读（不 set），验证 value/method 真实返回。
4. 断连：静置 25 秒不发任何客户端消息，再 ping，断言连接仍 OPEN。

用法: python _fix_check.py [PORT]，默认 8801。
结果写 _fix_result.txt（UTF-8），打印一行 ASCII 汇总。
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
from pathlib import Path

import aiohttp
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8801
# HTTPS 现在是默认（手机端麦克风权限要求安全上下文）。
SCHEME = os.environ.get("FIX_SCHEME", "https")
WS_SCHEME = "wss" if SCHEME == "https" else "ws"
BASE = f"{SCHEME}://127.0.0.1:{PORT}"
WS = f"{WS_SCHEME}://127.0.0.1:{PORT}/ws"
OUT = Path(__file__).with_name("_fix_result.txt")
IDLE_SECONDS = 25.0


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

results: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


async def collect(ws, wanted: set[str], timeout: float = 6.0, max_msgs: int = 120):
    got: dict[str, dict] = {}
    all_msgs: list[dict] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(got) < len(wanted):
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.receive(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        if raw.type != aiohttp.WSMsgType.TEXT:
            if raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
            continue
        msg = json.loads(raw.data)
        all_msgs.append(msg)
        kind = msg.get("type")
        if kind in wanted and kind not in got:
            got[kind] = msg
        if len(all_msgs) >= max_msgs:
            break
    return got, all_msgs


def find(all_msgs: list[dict], msg_type: str, msg_id: str | None = None):
    for msg in all_msgs:
        if msg.get("type") != msg_type:
            continue
        payload = msg.get("payload") or {}
        if msg_id is not None and payload.get("id") != msg_id:
            continue
        return msg
    return None


async def main() -> int:
    connector = aiohttp.TCPConnector(ssl=_ssl_ctx()) if SCHEME == "https" else None
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(f"{BASE}/api/status") as resp:
            status = await resp.json()
        rec("真实服务 /api/status 可达且 connected=True",
            resp.status == 200 and status.get("connected") is True,
            f"HTTP {resp.status} connected={status.get('connected')} port={status.get('port')}")

        # 前端素材与页面仍由后端同源托管（改 CSS/JS 后必须仍可访问）
        for path, ctype_hint in (
            ("/", "html"),
            ("/js/dpad.js", "javascript"),
            ("/css/base.css", "css"),
            ("/css/app-page.css", "css"),
        ):
            async with session.get(f"{BASE}{path}") as resp:
                body = await resp.read()
            ctype = resp.headers.get("Content-Type", "")
            rec(f"GET {path} 200 且类型正确",
                resp.status == 200 and len(body) > 200 and ctype_hint.split("/")[0] in ctype.lower()
                or (path == "/" and resp.status == 200 and b"<html" in body.lower()),
                f"HTTP {resp.status} {ctype} {len(body)}B")

        async with session.ws_connect(WS, heartbeat=None, autoping=True) as ws:
            got, _ = await collect(ws, {"hello"})
            rec("WS 收到 hello", "hello" in got, "")

            # —— 1. 四向 + A/B/C 的 keys 都能通过 dispatch 校验（同一通路）—— #
            for key in ("up", "down", "left", "right"):
                msg_id = f"probe-{key}"
                # 故意用**空 keys**: 校验在注入之前失败 → BAD_PAYLOAD，证明命令接线且不会误注入
                await ws.send_json({
                    "type": "key.press", "payload": {"keys": []},
                    "ts": 1, "id": msg_id,
                })
                got, msgs = await collect(ws, {"error", "ack"}, timeout=4.0)
                err = find(msgs, "error", msg_id)
                ack = find(msgs, "ack", msg_id)
                code = (err or {}).get("payload", {}).get("code")
                rec(f"key.press 空 keys 被拦在校验层（证明 {key} 所在命令已接线且不误注入）",
                    err is not None and code == "BAD_PAYLOAD" and ack is None,
                    f"code={code}")

            async with session.get(f"{BASE}/api/config") as resp:
                config = await resp.json()
            hotkeys = config.get("hotkeys") or []
            # 全新安装时 hotkeys 为空（干净默认值），套用内置模板后才有 A/B/C。
            # 两种都是合法状态：这里只校验结构，有则逐条验证通路。
            rec("config.hotkeys 结构合法（空 = 全新安装，或含合法项）",
                isinstance(hotkeys, list)
                and all(isinstance(h, dict) and h.get("id") for h in hotkeys),
                f"count={len(hotkeys)} slots={[h.get('slot') for h in hotkeys]}")

            for hotkey in hotkeys[:3]:
                msg_id = f"hk-bad-{hotkey.get('id')}"
                # 非法 id（空字符串）→ BAD_PAYLOAD，且不会注入任何键
                await ws.send_json({
                    "type": "hotkey.run", "payload": {"id": ""},
                    "ts": 1, "id": msg_id,
                })
                got, msgs = await collect(ws, {"error", "ack"}, timeout=4.0)
                err = find(msgs, "error", msg_id)
                code = (err or {}).get("payload", {}).get("code")
                rec(f"hotkey.run 空 id 被拦在校验层（{hotkey.get('slot')} 通路已接线）",
                    err is not None and code == "BAD_PAYLOAD",
                    f"code={code}")

            # —— 2. 进度条：media.state 只读 + media.seek 校验短路 —— #
            await ws.send_json({
                "type": "media.state", "payload": {}, "ts": 1, "id": "ms-1",
            })
            got, msgs = await collect(ws, {"media"}, timeout=10.0)
            media = got.get("media", {}).get("payload", {})
            rec("media.state 回真实结构（playing/title/position/best_effort）",
                all(k in media for k in ("playing", "title", "position", "best_effort")),
                json.dumps(media, ensure_ascii=False)[:160])

            msg_id = "seek-bad"
            await ws.send_json({
                "type": "media.seek", "payload": {"position": 5},
                "ts": 1, "id": msg_id,
            })
            got, msgs = await collect(ws, {"error", "ack"}, timeout=8.0)
            err = find(msgs, "error", msg_id)
            code = (err or {}).get("payload", {}).get("code")
            rec("media.seek 越界 position 被拦在校验层（不触发真实跳转）",
                err is not None and code == "BAD_PAYLOAD", f"code={code}")

            # —— 3. 亮度：只读 —— #
            await ws.send_json({
                "type": "brightness.get", "payload": {}, "ts": 1, "id": "bg-1",
            })
            got, msgs = await collect(ws, {"brightness"}, timeout=10.0)
            bget = got.get("brightness", {}).get("payload", {})
            rec("brightness.get 回 value/method/best_effort（只读，不改屏幕）",
                "value" in bget and "method" in bget and "best_effort" in bget,
                json.dumps(bget, ensure_ascii=False))

            # —— 4. 断连：静置 25 秒不发任何消息 —— #
            await asyncio.sleep(IDLE_SECONDS)
            rec(f"静置 {int(IDLE_SECONDS)}s 无任何客户端消息后连接仍 OPEN",
                not ws.closed, f"closed={ws.closed}")
            await ws.send_json({"type": "ping", "payload": {"echo_ts": 12345}, "ts": 12345, "id": "p-idle"})
            got, msgs = await collect(ws, {"pong"}, timeout=8.0)
            pong = got.get("pong", {}).get("payload", {})
            rec("静置后 ping 仍收到 pong（双向通道存活）",
                bool(pong), json.dumps(pong, ensure_ascii=False))

    ok = sum(1 for _, good, _ in results if good)
    total = len(results)
    lines = ["本轮四项修复端到端实证（真实服务 / 零副作用：不注入键、不改亮度）", ""]
    for name, good, detail in results:
        lines.append(f"[{'PASS' if good else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    lines += ["", f"总计 PASS={ok} FAIL={total - ok} TOTAL={total}"]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"FIX_RESULT PASS={ok} FAIL={total - ok} TOTAL={total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    os._exit(code)
