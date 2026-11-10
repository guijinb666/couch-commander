"""验证「有界发件箱 + 专职写协程」是否真的消除了半帧断连。

两条独立取证：

A. 单元级（fake WS）：让 `send_json` 第一次调用就永久阻塞，模拟对端不读、
   TCP 缓冲塞满。断言
     - `enqueue()` 永不被阻塞（同步返回）；
     - 队列满时不无限增长，且低频消息被优先丢弃；
     - **正在写的帧不会被 wait_for 取消**（旧的 `deliver()` 会）。

B. 网络级（真实 aiohttp 服务）：客户端连上后故意长时间不读，服务端灌
   大量大消息制造积压，然后恢复读取，断言收到的每一帧都是完整 JSON
   （没有半帧），且连接没有因为服务端主动取消写帧而断开。

零副作用：不注入键鼠、不改配置、不动屏幕、不碰系统亮度。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

ROOT = _PROJECT_ROOT
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))
os.chdir(str(SERVER))

from aiohttp import WSMsgType, web  # noqa: E402

import ws_hub  # noqa: E402

OUT = ROOT / "_outbox_result.txt"
lines: list[str] = []
results: list[tuple[bool, str, str]] = []


def rec(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (f" — {detail}" if detail else ""))


class BlockingWS:
    """send_json 永久阻塞的假连接，用来复现「对端不读、缓冲塞满」。"""

    def __init__(self) -> None:
        self.closed = False
        self.started = asyncio.Event()
        self.cancelled = False
        self.finished = 0

    async def send_json(self, msg, dumps=None):  # noqa: ARG002
        self.started.set()
        try:
            await asyncio.sleep(3600)  # 永不返回，模拟 TCP 缓冲满
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished += 1

    async def close(self, *a, **k):  # noqa: ARG002
        self.closed = True

    def drain(self):
        return asyncio.sleep(0)


async def part_a() -> None:
    fake = BlockingWS()
    conn = ws_hub.ClientConn(fake)
    conn.start_writer()
    await asyncio.sleep(0.05)

    # 1) enqueue 同步返回，不阻塞
    started = time.monotonic()
    accepted = conn.enqueue({"type": "hello", "payload": {}}, low=False)
    cost = time.monotonic() - started
    rec(cost < 0.05, "enqueue 立即返回（不阻塞调用方）", f"cost={cost * 1000:.1f}ms")
    rec(bool(accepted), "第一条消息被接受")

    # 2) 写协程已经开始写这条帧，并且**没有被取消**
    try:
        await asyncio.wait_for(fake.started.wait(), timeout=2.0)
        entered = True
    except asyncio.TimeoutError:
        entered = False
    rec(entered, "写协程已进入 send_json（开始写帧）")
    await asyncio.sleep(0.2)
    rec(not fake.cancelled, "进行中的写帧没有被取消", f"cancelled={fake.cancelled}")

    # 3) 队列有界：灌满后不无限增长
    for index in range(ws_hub.OUTBOX_MAX_MESSAGES + 200):
        conn.enqueue({"type": "status", "payload": {"i": index}}, low=True)
    size = conn._queue.qsize()
    rec(
        size <= ws_hub.OUTBOX_MAX_MESSAGES,
        "队列容量有上限，不会无限增长",
        f"size={size} max={ws_hub.OUTBOX_MAX_MESSAGES}",
    )
    rec(conn.backlog_drops > 0, "溢出时低频消息被丢弃并计数", f"drops={conn.backlog_drops}")

    # 4) 普通（非低频）消息溢出时，丢的是最旧的、新消息仍能进队
    conn2 = ws_hub.ClientConn(BlockingWS())
    conn2.start_writer()
    await asyncio.sleep(0.05)
    for index in range(ws_hub.OUTBOX_MAX_MESSAGES + 50):
        conn2.enqueue({"type": "ack", "payload": {"i": index}}, low=False)
    # 读出队首，应该是被丢过之后剩下的较新消息
    ok_fifo = True
    try:
        first, _low = conn2._queue.get_nowait()
        ok_fifo = first["payload"]["i"] > 0
    except Exception:
        ok_fifo = False
    rec(ok_fifo, "普通消息溢出时丢弃最旧的一条（新消息保留）")

    await conn.close_writer()
    await conn2.close_writer()
    rec(True, "写协程可正常关闭")


async def part_b() -> None:
    """真实网络：不读 → 灌积压 → 恢复读，检查有无半帧。"""
    inbox: list[str] = []
    app = web.Application()

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None, autoping=True, max_msg_size=1 << 20)
        await ws.prepare(request)
        conn = ws_hub.ClientConn(ws)
        conn.start_writer()
        # 灌 300 条 ~8KB 的大消息，远超 socket 缓冲
        big = "x" * 8000
        for index in range(300):
            conn.enqueue({"type": "bulk", "payload": {"i": index, "blob": big}}, low=False)
        # 等一会儿，让写协程在「对端不读」的状态下反复遇到缓冲满
        await asyncio.sleep(1.2)
        alive_after_stall = not ws.closed
        # 再等写协程继续推
        await asyncio.sleep(1.0)
        await conn.close_writer()
        request["alive"] = alive_after_stall
        return ws

    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8891)
    await site.start()

    try:
        import aiohttp

        session = aiohttp.ClientSession()
        try:
            # 关键：连接后先不读，模拟手机卡住
            ws = await session.ws_connect("http://127.0.0.1:8891/ws", heartbeat=None, max_msg_size=0)
            await asyncio.sleep(2.5)  # 服务端此时正在被背压卡住
            rec(not ws.closed, "对端不读期间连接没有被服务端主动关闭", f"closed={ws.closed}")

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                except asyncio.TimeoutError:
                    break
                if msg.type != WSMsgType.TEXT:
                    break
                inbox.append(msg.data)
                if len(inbox) >= 40:
                    break
            rec(len(inbox) > 0, "恢复读取后能收到完整消息", f"count={len(inbox)}")

            bad = 0
            for raw in inbox:
                try:
                    obj = json.loads(raw)
                    if obj.get("type") != "bulk":
                        bad += 1
                except Exception:
                    bad += 1
            rec(bad == 0, "收到的每一帧都是完整 JSON（无半帧）", f"bad={bad} total={len(inbox)}")
            await ws.close()
        finally:
            await session.close()
    finally:
        await runner.cleanup()


def main() -> int:
    asyncio.run(part_a())
    asyncio.run(part_b())
    return 0


if __name__ == "__main__":
    code = main()
    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    lines.append(f"OUTBOX_RESULT PASS={passed} FAIL={total - passed} TOTAL={total}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[-1])
    os._exit(code)
