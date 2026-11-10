"""V2.2 语音链路专项自检（零副作用：不弹浏览器、不改屏幕）。

muse 那条「把手机 PCM 喂进它自己的麦克风」的链路已整体下线（实测 muse.ai 是
Meta 落地页、没有可用的语音输入按钮）。这里验证下线后的真实状态：

1. ``GET /voice-ws`` 路由仍注册，但返回 410（保留路径避免旧手机页面困惑）。
2. ``voice.start`` 仍能把页面打开/记录状态。
3. ``voice.text{final:false}`` 不回任何消息（高频预览）。
4. ``voice.text{final:true}`` 回 ack 并真的尝试注入（用 mock 记录，不碰真键盘）。
5. ``voice.stop`` 回 ack，且不清空 / 不关闭页面记录。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

ROOT = _PROJECT_ROOT
sys.path.insert(0, str(ROOT / "server"))

import http_api  # noqa: E402
import input_win  # noqa: E402
import main as srv_main  # noqa: E402
import voice  # noqa: E402

RESULT = ROOT / "_voicews_result.txt"
LINES: list[str] = []
PASS = 0
FAIL = 0

TYPO: list[str] = []
KEYS: list[list[str]] = []


def rec(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        LINES.append(f"[PASS] {name} — {detail}")
    else:
        FAIL += 1
        LINES.append(f"[FAIL] {name} — {detail}")


def _fake_type(text: str) -> None:
    TYPO.append(text)


def _fake_press(keys) -> None:
    KEYS.append(list(keys))


async def collect(ws, wanted: set[str], timeout: float = 6.0, max_msgs: int = 40) -> dict:
    got: dict[str, dict] = {}
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    for _ in range(max_msgs):
        left = deadline - loop.time()
        if left <= 0:
            break
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=left)
        except asyncio.TimeoutError:
            break
        if msg.type != web.WSMsgType.TEXT:
            continue
        import json as _json

        data = _json.loads(msg.data)
        kind = data.get("type")
        if kind in wanted and kind not in got:
            got[kind] = data
            wanted = wanted - {kind}
            if not wanted:
                break
    return got


async def main() -> int:
    # 注入点全部 mock：绝不真的敲键盘、绝不真的开浏览器。
    input_win.key_type = _fake_type          # type: ignore[assignment]
    input_win.key_press = _fake_press        # type: ignore[assignment]
    voice._focus_window = lambda *a, **k: True  # noqa: SLF001
    voice.load_point = lambda: None             # noqa: SLF001
    voice.activate_mic = lambda: {"focused": True, "mic_clicked": False, "mic_point_configured": False}  # noqa: SLF001
    import webbrowser

    opened_urls: list[str] = []
    webbrowser.open = lambda url, *a, **k: (opened_urls.append(url), True)[1]  # type: ignore[assignment]

    ctx = srv_main.build_context("127.0.0.1", 8899, b"", "http://127.0.0.1:8899/")
    app = http_api.create_app(ctx)

    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        # 1) /voice-ws 已下线但路径仍在
        rec("GET /voice-ws 仍在路由表（兼容旧页面）",
            "GET /voice-ws" in http_api.rest_endpoints(), "")
        gone = await client.get("/voice-ws?role=phone")
        rec("GET /voice-ws 返回 410（已下线）", gone.status == 410, f"status={gone.status}")
        gone.close()

        ws = await client.ws_connect("/ws")
        got = await collect(ws, {"hello"})
        rec("WS 握手成功", "hello" in got, f"hello={bool(got.get('hello'))}")

        # 2) voice.start —— V2.3 起只建立语音会话，**不再打开任何页面**
        #    （用户原话：「长按语音还是会弹出 muse，关掉」）
        await ws.send_json({"type": "voice.start", "payload": {"url": "https://muse.ai/"}, "ts": 1, "id": "v1"})
        got = await collect(ws, {"ack"})
        ack = (got.get("ack") or {}).get("payload", {})
        rec("voice.start 回 ack", ack.get("id") == "v1", f"payload={ack}")
        extra = ack.get("extra") or {}
        rec("voice.start 不再打开任何页面（不碰浏览器）", not opened_urls, f"opened={opened_urls}")
        rec("voice.start 的 ack 带 opened/page_opened 字段",
            "opened" in extra or "page_opened" in extra, f"extra={extra}")
        rec("voice.start 明确声明 page_opened=False", extra.get("page_opened") is False, f"extra={extra}")

        # 3) voice.text final:false 不回包
        before = voice.state()
        await ws.send_json({"type": "voice.text", "payload": {"text": "你好", "final": False}, "ts": 2, "id": "v2"})
        quiet = await collect(ws, {"ack", "error"}, timeout=1.2)
        rec("voice.text final:false 不回任何消息（高频预览）", not quiet, f"got={list(quiet)}")
        rec("final:false 只更新预览、不注入", not TYPO, f"typed={TYPO}")

        # 4) voice.text final:true 回 ack 并注入
        await ws.send_json({"type": "voice.text", "payload": {"text": "打开抖音", "final": True}, "ts": 3, "id": "v3"})
        got = await collect(ws, {"ack"})
        ack = (got.get("ack") or {}).get("payload", {})
        rec("voice.text final:true 回 ack", ack.get("id") == "v3", f"payload={ack}")
        rec("final:true 真的把文字注入电脑", TYPO == ["打开抖音"], f"typed={TYPO}")
        rec("注入后补一次回车", KEYS and KEYS[-1] == ["enter"], f"keys={KEYS}")

        # 5) voice.stop 回 ack，且不因为结束会话而改变「不打开页面」这个事实
        await ws.send_json({"type": "voice.stop", "payload": {}, "ts": 4, "id": "v4"})
        got = await collect(ws, {"ack"})
        ack = (got.get("ack") or {}).get("payload", {})
        rec("voice.stop 回 ack", ack.get("id") == "v4", f"payload={ack}")
        after = voice.state()
        rec("voice.stop 全程仍未打开任何页面", not opened_urls, f"opened={opened_urls}")
        rec("voice.stop 后会话不再 active", after.get("active") is False, f"active={after.get('active')}")

        # 6) 坏 payload 有明确错误码
        await ws.send_json({"type": "voice.text", "payload": {"text": 123}, "ts": 5, "id": "v5"})
        got = await collect(ws, {"error"})
        err = (got.get("error") or {}).get("payload", {})
        rec("voice.text 传非字符串 → BAD_PAYLOAD", err.get("code") == "BAD_PAYLOAD", f"payload={err}")

        await ws.close()
    finally:
        await client.close()

    LINES.append(f"VOICEWS_RESULT PASS={PASS} FAIL={FAIL} TOTAL={PASS + FAIL}")
    RESULT.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    print(LINES[-1])
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    code = asyncio.run(main())
    sys.stdout.flush()
    os._exit(code)
