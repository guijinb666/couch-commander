# -*- coding: utf-8 -*-
"""本轮两个 bug 的专项回归（零副作用）：
  ①「开一段时间手机端就断连」= 前后端双重激进心跳超时；
  ②「快捷键不起作用」= 全局 touchend 吞 click + A/B/C 用 click + hotkey.run
     只按 id 查表、config 不同步就 NOT_FOUND。

验证手法：
  - 后端逻辑：直接 import ws_hub / dispatcher，读常量与源码做事实断言；
    再 mock 掉 input_win.key_press（绝不注入真键鼠），用假 ctx/conn 直接跑
    cmd_hotkey_run 的三条分支（命中 config / 未知 id 带 keys 兜底 / 未知 id
    无 keys 报 NOT_FOUND / 缺 id 报 BAD_PAYLOAD）。
  - 前端逻辑：对 net.js / main.js / dpad.js 做源码级事实断言（本环境无浏览器，
    前端语法已由 node --check 单独验证）。

结果写 _bugfix_result.txt（UTF-8），stdout 只一行 ASCII 汇总，退出码即结论。
"""
import asyncio
import json
import sys
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

ROOT = _PROJECT_ROOT
SERVER = ROOT / "server"
WEB = ROOT / "web"
sys.path.insert(0, str(SERVER))

import input_win  # noqa: E402
import ws_hub  # noqa: E402
import dispatcher  # noqa: E402

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))


# ── mock 掉真实按键注入：dispatcher 里凡 key_press 全部变成纯记录 ──────────
_injected = []


def _recording_key_press(keys, *a, **k):
    _injected.append(list(keys))


_orig_key_press = input_win.key_press
input_win.key_press = _recording_key_press


class _FakeConn:
    def __init__(self):
        self.acks = []


class _FakeCtx:
    def __init__(self, config):
        self.config = config

    def get_config(self):
        return self.config

    async def send_ack(self, conn, msg_id, extra=None):
        conn.acks.append((msg_id, extra or {}))
        return True


def _env(payload):
    return {"type": "hotkey.run", "payload": payload, "ts": 1, "id": "c-hk"}


async def _run_hotkey_checks():
    cfg = {
        "version": 1, "pages": [], "active_page_id": None, "apps": [],
        "touchpad": {}, "hotkeys": [
            {"id": "hk-known", "name": "复制", "icon": "", "keys": ["ctrl", "c"],
             "page_id": "pg-1", "slot": "A"},
        ],
    }
    ctx = _FakeCtx(cfg)
    conn = _FakeConn()

    # ① 命中 config：source=config，注入 config 的 keys（服务端权威）
    _injected.clear()
    await dispatcher.cmd_hotkey_run(ctx, conn, _env({"id": "hk-known"}))
    rec("hotkey.run 命中 config → source=config 且注入权威 keys",
        conn.acks and conn.acks[-1][1].get("source") == "config"
        and _injected and _injected[-1] == ["ctrl", "c"],
        f"extra={conn.acks[-1][1] if conn.acks else None} injected={_injected}")

    # ② 未知 id + 前端附带 keys：兜底注入（这正是「快捷键不起作用」的修复）
    _injected.clear()
    before = len(conn.acks)
    await dispatcher.cmd_hotkey_run(
        ctx, conn, _env({"id": "__not_in_config__", "keys": ["b"]}))
    rec("hotkey.run 未知 id 带 keys → payload_fallback 兜底注入",
        len(conn.acks) > before
        and conn.acks[-1][1].get("source") == "payload_fallback"
        and _injected and _injected[-1] == ["b"],
        f"extra={conn.acks[-1][1] if conn.acks else None} injected={_injected}")

    # ③ 未知 id 且无 keys：仍报 NOT_FOUND（V1 语义保留，selftest 第 11 步依赖）
    try:
        await dispatcher.cmd_hotkey_run(ctx, conn, _env({"id": "__missing__"}))
        rec("hotkey.run 未知 id 无 keys → NotFoundError", False, "未抛异常")
    except dispatcher.NotFoundError as exc:
        rec("hotkey.run 未知 id 无 keys → NotFoundError", True, str(exc))

    # ④ 缺 id：BAD_PAYLOAD
    try:
        await dispatcher.cmd_hotkey_run(ctx, conn, _env({"keys": ["a"]}))
        rec("hotkey.run 缺 id → BadPayloadError", False, "未抛异常")
    except dispatcher.BadPayloadError as exc:
        rec("hotkey.run 缺 id → BadPayloadError", True, str(exc))


def _source_facts():
    hub = (SERVER / "ws_hub.py").read_text(encoding="utf-8")
    net = (WEB / "js" / "net.js").read_text(encoding="utf-8")
    main = (WEB / "js" / "main.js").read_text(encoding="utf-8")
    dpad = (WEB / "js" / "dpad.js").read_text(encoding="utf-8")
    disp = (SERVER / "dispatcher.py").read_text(encoding="utf-8")

    # —— 断连根治（本轮：服务端永不空闲关连接 + 前端永不主动 close）—— #
    rec("ws_hub 保留 IDLE_TIMEOUT_S 常量（兼容历史自检）",
        getattr(ws_hub, "IDLE_TIMEOUT_S", None) == 120.0,
        f"IDLE_TIMEOUT_S={getattr(ws_hub, 'IDLE_TIMEOUT_S', None)}")
    rec("ws_hub 不再因空闲主动关闭连接（仅清理 ws.closed）",
        "missed_heartbeats >= MISSED_HEARTBEAT_LIMIT" not in hub
        and "now - conn.last_seen) >= IDLE_TIMEOUT_S" not in hub
        and "idle timeout" not in hub, "")
    rec("net.js 不再因时间/缺 pong 主动 close（前台静置不误杀）",
        "this.missedPings >= 2" not in net
        and "150000" not in net
        and "this.lastMessageAt" in net, "")
    rec("net.js 含 online / pageshow 自动重连",
        "addEventListener('online'" in net
        and "addEventListener('pageshow'" in net, "")
    rec("net.js HTTP 4xx 不再计入断连（仅网络层失败计入）",
        "if (!gotResponse) this.noteRestFailure()" in net
        and "gotResponse = true" in net, "")
    rec("main.js 全局 touchend 对交互元素不 preventDefault（不吞 click）",
        "closest('button, input" in main, "")
    rec("main.js 回前台 connecting/disconnected 都立即重连",
        "['disconnected', 'connecting'].includes(connection.status)" in main, "")

    # —— 快捷键根治（本轮：A/B/C 直发物理键，绕开 hotkey.run 查表）—— #
    rec("dpad.js A/B/C pointerdown 直发 key.press 物理键",
        "button.addEventListener('pointerdown'" in dpad
        and "'key.press'" in dpad
        and "this.pressKeys(hotkey.keys)" in dpad, "")
    rec("dispatcher.cmd_hotkey_run 仍保留 payload_fallback 兜底（契约兼容）",
        "payload_fallback" in disp and "payload.get(\"keys\")" in disp, "")


def main():
    _source_facts()
    asyncio.run(_run_hotkey_checks())
    # 还原（本进程即将退出，仍显式还原保持整洁）
    input_win.key_press = _orig_key_press

    ok = sum(1 for _, good, _ in results if good)
    lines = ["本轮两 bug 专项回归结果（零副作用 / mock 按键注入）",
             "=" * 64]
    for name, good, detail in results:
        lines.append(f"  [{'PASS' if good else 'FAIL'}] {name}"
                     + (f" — {detail}" if detail else ""))
    lines.append("-" * 64)
    lines.append(f"合计：PASS={ok}  FAIL={len(results) - ok}  TOTAL={len(results)}")
    (ROOT / "_bugfix_result.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"BUGFIX_RESULT PASS={ok} FAIL={len(results)-ok} TOTAL={len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    __import__("os")._exit(_code)
