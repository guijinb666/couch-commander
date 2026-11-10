"""长连接浸泡测试：完全照手机的行为跑，抓「功能都好着却断连」的真实证据。

手机端实际行为（见 web/js/net.js 与 main.js）：
  * WSS 连到 /ws
  * 每 5s 发一次 ping{echo_ts}
  * 每 1s 发一次 media.state{}
  * 收到任意消息都算链路存活
  * 断线后按指数退避重连

本脚本用一条（或多条）真实 WSS 连接重复上述行为，持续 SOAK_SECONDS，
记录：收到多少帧、什么时候断、断的 close code / reason、服务端日志里有没有异常。
打印 SOAK_RESULT PASS=n FAIL=n，并把明细写进 _soak_result.txt。

用法：python _soak.py [PORT] [SECONDS] [CLIENTS]
      端口默认 8951；脚本自己拉起服务并在结束时关闭。

零副作用：不注入任何键盘/鼠标、不改亮度、不改 config。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import aiohttp
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

ROOT = _PROJECT_ROOT
SERVER = ROOT / "server"
RESULT_FILE = ROOT / "_soak_result.txt"

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8951
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 150.0
CLIENTS = int(sys.argv[3]) if len(sys.argv) > 3 else 1

results: list[tuple[bool, str, str]] = []


def rec(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}", flush=True)


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


async def wait_ready(url: str, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    conn = aiohttp.TCPConnector(ssl=ssl_ctx())
    while time.monotonic() < deadline:
        try:
            async with aiohttp.ClientSession(connector=conn) as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        return True
        except Exception:
            await asyncio.sleep(0.4)
    return False


class Phone:
    """模拟一部手机：连上后按 5s ping / 1s media.state 的节奏，记录一切。"""

    def __init__(self, index: int, url: str) -> None:
        self.index = index
        self.url = url
        self.frames = 0
        self.closes: list[tuple[float, int, str]] = []
        self.reconnects = 0
        self.start = time.monotonic()
        self.last_frame_at = 0.0
        self.types: dict[str, int] = {}
        self.errors: list[str] = []

    async def run(self, deadline: float) -> None:
        attempt = 0
        while time.monotonic() < deadline:
            try:
                await self._session(deadline)
            except Exception as exc:  # 连接层异常也要如实记录
                self.errors.append(f"{type(exc).__name__}: {exc}")
            if time.monotonic() >= deadline:
                return
            attempt += 1
            self.reconnects += 1
            await asyncio.sleep(min(4.0, 0.6 * (2 ** min(attempt, 3))))

    async def _session(self, deadline: float) -> None:
        conn = aiohttp.TCPConnector(ssl=ssl_ctx())
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with aiohttp.ClientSession(connector=conn, timeout=timeout) as sess:
            async with sess.ws_connect(self.url, heartbeat=None, max_msg_size=0) as ws:
                closed_at = None
                code = 0
                reason = ""
                ping_task = asyncio.create_task(self._pinger(ws))
                state_task = asyncio.create_task(self._state_poller(ws))
                try:
                    while time.monotonic() < deadline:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=3.0)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            self.frames += 1
                            self.last_frame_at = time.monotonic()
                            try:
                                data = json.loads(msg.data)
                                t = data.get("type", "?")
                                self.types[t] = self.types.get(t, 0) + 1
                            except Exception:
                                self.errors.append("服务端下发了无法解析的 JSON")
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            closed_at = time.monotonic()
                            code = ws.close_code or 0
                            reason = str(getattr(msg, "extra", "") or msg.data or "")
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            self.errors.append(f"WSMsgType.ERROR: {ws.exception()}")
                            break
                finally:
                    ping_task.cancel()
                    state_task.cancel()
                    for t in (ping_task, state_task):
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    if closed_at is not None:
                        self.closes.append((closed_at - self.start, code, reason))

    async def _pinger(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        n = 0
        while True:
            await asyncio.sleep(5.0)
            n += 1
            await ws.send_str(json.dumps({
                "type": "ping", "payload": {"echo_ts": int(time.time() * 1000)},
                "ts": int(time.time() * 1000), "id": f"p-{self.index}-{n}",
            }, ensure_ascii=False))

    async def _state_poller(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        n = 0
        while True:
            await asyncio.sleep(1.0)
            n += 1
            await ws.send_str(json.dumps({
                "type": "media.state", "payload": {},
                "ts": int(time.time() * 1000), "id": f"s-{self.index}-{n}",
            }, ensure_ascii=False))


async def main() -> int:
    print(f"SOAK port={PORT} seconds={SECONDS:.0f} clients={CLIENTS}", flush=True)
    if not port_free(PORT):
        rec(False, f"端口 {PORT} 空闲", "已被占用，换一个端口再跑")
        return _finish()
    rec(True, f"端口 {PORT} 空闲")

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    log_path = ROOT / "_soak_server.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--no-window", "--port", str(PORT)],
        cwd=str(SERVER), stdout=log, stderr=subprocess.STDOUT, env=env,
    )
    try:
        base = f"https://127.0.0.1:{PORT}"
        ok = await wait_ready(f"{base}/api/status")
        rec(ok, "HTTPS 服务已就绪", f"{base}/api/status")
        if not ok:
            return _finish()

        ws_url = f"wss://127.0.0.1:{PORT}/ws"
        phones = [Phone(i, ws_url) for i in range(CLIENTS)]
        deadline = time.monotonic() + SECONDS
        await asyncio.gather(*(p.run(deadline) for p in phones), return_exceptions=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()

    detailed: list[str] = []
    for p in phones:
        span = SECONDS
        per_min = len(p.closes) / max(span / 60.0, 1e-6)
        detailed.append(
            f"client#{p.index} frames={p.frames} reconnects={p.reconnects} "
            f"closes={[(round(t, 1), c, r[:40]) for t, c, r in p.closes]} "
            f"types={p.types} errors={p.errors[:3]}"
        )
        # 判定 1：整段跑完没有出现任何断连
        rec(len(p.closes) == 0, f"client#{p.index} 全程未断开",
            f"closes={len(p.closes)} 每 分钟={per_min:.2f}")
        # 判定 2：确实收到服务端消息（证明链路真的在跑，不是空连接）
        rec(p.frames > 0, f"client#{p.index} 收到服务端消息", f"frames={p.frames}")
        # 判定 3：连接层没有异常
        rec(not p.errors, f"client#{p.index} 无连接层异常", f"errors={p.errors[:2]}")
        # 判定 4：status 广播持续到达（服务端每 10s 一次，说明写路径一直活着）
        expect = int(SECONDS / 10.0) - 2
        got = p.types.get("status", 0)
        rec(got >= max(1, expect), f"client#{p.index} status 广播持续到达",
            f"got={got} 期望≥{max(1, expect)}")

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    bad = [
        line for line in log_text.splitlines()
        if any(k in line for k in ("Traceback", "ERROR", "Exception", "cancel"))
    ]
    rec(not bad, "服务端日志无异常", f"{bad[:3]}")

    body = ["SOAK_DETAIL"] + detailed + ["", "SOAK_SERVER_LOG_BAD"] + (bad[:20] or ["(none)"])
    RESULT_FILE.write_text("\n".join(body) + "\n", encoding="utf-8")
    for line in detailed:
        print("  " + line, flush=True)
    return _finish()


def _finish() -> int:
    passed = sum(1 for ok, _, _ in results if ok)
    failed = len(results) - passed
    line = f"SOAK_RESULT PASS={passed} FAIL={failed} TOTAL={len(results)}"
    print("\n" + line, flush=True)
    try:
        with RESULT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
