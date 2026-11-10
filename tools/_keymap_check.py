# -*- coding: utf-8 -*-
"""本轮「所有按键直接映射物理键盘键」专项回归（零副作用）。

用户 m01403 的新方案：A/B/C 快捷键、上/下/左/右方向键、进度条、播放键
全部「按下就相当于敲一次电脑上的物理键」，彻底绕开 media.control 虚拟
媒体键、media.seek 尽力估算、hotkey.run 按 id 查表这三处间接层；断连根治
为「服务端永不因空闲关连接 + 前端永不主动 close + 服务后台常驻」。

验证手法：
  - 前端：对 dpad.js / net.js / main.js / ws_hub.py / main.py 做源码级事实断言；
    并从 dpad.js 动态提取所有 connection.send('<type>') 的 <type>，逐个确认
    都命中 dispatcher.implemented_commands()（前端发出的命令后端都接得住）。
  - 后端：mock 掉 input_win.key_press（绝不注入真键鼠），直接跑
    dispatcher.cmd_key_press 连发同键 5 次 / 组合键 / 空格，确认每次独立
    down/up、无吞键、无节流。
  - 契约零改：implemented_commands() 仍 27 条、仍含 key.press。

结果写 _keymap_result.txt（UTF-8），stdout 只一行 ASCII 汇总，退出码即结论。
"""
import asyncio
import re
import sys
import time
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

ROOT = _PROJECT_ROOT
SERVER = ROOT / "server"
WEB = ROOT / "web"
sys.path.insert(0, str(SERVER))

import input_win  # noqa: E402
import dispatcher  # noqa: E402
import ws_hub  # noqa: E402

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))


# ── mock 掉真实按键注入：cmd_key_press 里的 key_press 全部变成纯记录 ──────────
_injected = []


def _recording_key_press(keys, *a, **k):
    _injected.append(list(keys))


_orig_key_press = input_win.key_press
input_win.key_press = _recording_key_press


class _FakeConn:
    def __init__(self):
        self.acks = []


class _FakeCtx:
    async def send_ack(self, conn, msg_id, extra=None):
        conn.acks.append((msg_id, extra or {}))
        return True


def _env(keys, msg_id="c-km"):
    return {"type": "key.press", "payload": {"keys": keys}, "ts": 1, "id": msg_id}


async def _run_injection_checks():
    ctx = _FakeCtx()
    conn = _FakeConn()

    # ① 连发 5 次同一方向键：5 次独立注入（后端无吞键 / 无节流 / 无去重）
    _injected.clear()
    for i in range(5):
        await dispatcher.cmd_key_press(ctx, conn, _env(["up"], f"c-{i}"))
    rec("key.press 连发 5 次 ['up'] → 5 次独立注入（无吞键）",
        _injected == [["up"]] * 5 and len(conn.acks) == 5,
        f"injected={_injected} acks={len(conn.acks)}")

    # ② 组合键（A/B/C 模板、用户自定义 Ctrl+C 之类）
    _injected.clear()
    await dispatcher.cmd_key_press(ctx, conn, _env(["ctrl", "c"]))
    rec("key.press 组合键 ['ctrl','c'] → 原样注入",
        _injected == [["ctrl", "c"]], f"injected={_injected}")

    # ③ 空格（播放 / 暂停），方向键四向全覆盖
    _injected.clear()
    for key in ("space", "up", "down", "left", "right"):
        await dispatcher.cmd_key_press(ctx, conn, _env([key]))
    rec("key.press 空格 + 四方向键 → 逐个原样注入",
        _injected == [["space"], ["up"], ["down"], ["left"], ["right"]],
        f"injected={_injected}")

    # ④ 契约零改：命令集合仍 27 条、含 key.press
    impl = dispatcher.implemented_commands()
    rec("dispatcher.implemented_commands() 仍 27 条且含 key.press（契约零改）",
        len(impl) == 27 and "key.press" in impl,
        f"count={len(impl)} has_key_press={'key.press' in impl}")

    # ⑤ 运行时心跳验证：把连接 last_seen 设成远超 IDLE_TIMEOUT_S 之前，
    #    临时加速巡检（HEARTBEAT_INTERVAL_S 调小），跑多轮后确认服务端
    #    仍不主动关闭这条空闲连接 —— 「永不因空闲关连接」的运行时硬证据。
    class _FakeWs:
        def __init__(self):
            self.closed = False
            self.close_codes = []

        async def close(self, code=None, message=b""):
            self.close_codes.append((code, message))
            self.closed = True

    class _FakeHub:
        def __init__(self, conn):
            self.conns = {conn}

        async def broadcast_status(self):
            return None

    old_interval = ws_hub.HEARTBEAT_INTERVAL_S
    try:
        ws_hub.HEARTBEAT_INTERVAL_S = 0.01  # 加速：让循环快速跑过多个巡检周期
        fake_ws = _FakeWs()
        conn = ws_hub.ClientConn(fake_ws)
        conn.last_seen = time.monotonic() - (ws_hub.IDLE_TIMEOUT_S + 3600)
        hub = ws_hub.WsHub.__new__(ws_hub.WsHub)
        hub.conns = {conn}
        task = asyncio.create_task(ws_hub.WsHub._heartbeat_loop(hub))
        await asyncio.sleep(0.08)  # 至少 5+ 个巡检周期
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        rec("运行时：空闲远超 120s 的连接跑过多轮心跳巡检仍不被关闭",
            not fake_ws.closed and not fake_ws.close_codes and conn in hub.conns,
            f"closed={fake_ws.closed} close_calls={fake_ws.close_codes} in_set={conn in hub.conns}")
    finally:
        ws_hub.HEARTBEAT_INTERVAL_S = old_interval


def _source_facts():
    dpad = (WEB / "js" / "dpad.js").read_text(encoding="utf-8")
    net = (WEB / "js" / "net.js").read_text(encoding="utf-8")
    main = (WEB / "js" / "main.js").read_text(encoding="utf-8")
    hub = (SERVER / "ws_hub.py").read_text(encoding="utf-8")
    mainpy = (SERVER / "main.py").read_text(encoding="utf-8")

    # —— 方向键 / A/B/C / 进度条 / 播放键 全部直发物理键 —— #
    rec("dpad.js 方向键常量 up/down/left/right 四向齐全",
        all(f"{d}: '{d}'" in dpad for d in ("up", "down", "left", "right")),
        "")
    rec("dpad.js 统一 pressKeys() 直发 key.press{keys,app}",
        "this.connection.send('key.press'" in dpad
        and "pressKeys(keys)" in dpad
        and "targetAppName()" in dpad, "")
    rec("dpad.js bindDirection 直发物理方向键（不再 moveApp/media.control）",
        "const send = () => this.pressKeys([key]);" in dpad
        and "this.state.moveApp" not in dpad
        and "actions[direction]" not in dpad, "")
    rec("dpad.js A/B/C 直发 key.press(hotkey.keys)，不再 hotkey.run 查表",
        "this.pressKeys(hotkey.keys)" in dpad
        and "'hotkey.run'" not in dpad, "")
    rec("dpad.js 进度条改走 media.seek 真实定位（不再靠方向键次数估算）",
        "bindProgress()" in dpad
        and "connection.send('media.seek', { position })" in dpad
        and "SEEK_KEYS_PER_UNIT" not in dpad
        and "SEEK_MAX_PER_FLUSH" not in dpad, "")
    rec("dpad.js 拖动时标记 seeking，暂停 media.state 轮询避免回包拽回手指",
        "document.body.dataset.seeking = '1'" in dpad
        and "delete document.body.dataset.seeking" in dpad, "")
    rec("dpad.js 播放键短按发物理空格键",
        "this.pressKeys(['space'])" in dpad, "")
    rec("dpad.js 音量键保留 media.control（系统音量，非视频控制）",
        "connection.send('media.control', { action: button.dataset.action })" in dpad, "")
    rec("dpad.js 长按 2s 锁定/解锁仍发 app.activate / app.release",
        "connection.send('app.activate', { id: app.id })" in dpad
        and "connection.send('app.release', {})" in dpad, "")

    # —— 前端发出的命令，后端都接得住（动态提取 send 的第一个参数）—— #
    sent_types = sorted(set(re.findall(r"\.send\(\s*['\"]([a-z][a-z0-9_.]+)['\"]", dpad)))
    impl = dispatcher.implemented_commands()
    unknown = [t for t in sent_types if t not in impl]
    rec("dpad.js 全部 send 命令都在后端命令表（接线无孤儿命令）",
        bool(sent_types) and not unknown,
        f"sent={sent_types} unknown={unknown}")

    # —— 断连根治 + 后台常驻 —— #
    rec("ws_hub 不因空闲主动 close（无 idle timeout 关闭逻辑）",
        "idle timeout" not in hub
        and "now - conn.last_seen) >= IDLE_TIMEOUT_S" not in hub, "")
    rec("net.js 无时间兜底主动 close（前端永不主动关连接）",
        "150000" not in net and "this.missedPings >= 2" not in net, "")
    rec("main.js 回前台 disconnected/connecting 都立即重连",
        "['disconnected', 'connecting'].includes(connection.status)" in main, "")
    rec("main.py 横幅注明服务常驻后台、关二维码窗不停服务",
        "服务常驻后台运行" in mainpy and "关闭二维码窗口不影响服务" in mainpy, "")


def main():
    _source_facts()
    asyncio.run(_run_injection_checks())
    input_win.key_press = _orig_key_press

    ok = sum(1 for _, good, _ in results if good)
    lines = ["本轮「物理键直映」专项回归结果（零副作用 / mock 按键注入）",
             "=" * 64]
    for name, good, detail in results:
        lines.append(f"  [{'PASS' if good else 'FAIL'}] {name}"
                     + (f" — {detail}" if detail else ""))
    lines.append("-" * 64)
    lines.append(f"合计：PASS={ok}  FAIL={len(results) - ok}  TOTAL={len(results)}")
    (ROOT / "_keymap_result.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"KEYMAP_RESULT PASS={ok} FAIL={len(results)-ok} TOTAL={len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    __import__("os")._exit(_code)
