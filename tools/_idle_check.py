# -*- coding: utf-8 -*-
"""真实起服务验证「开一段时间手机端就断连」修复。

场景复现：iPhone Safari 锁屏 / 切后台会冻结 setInterval，客户端 5s 一次的
ping 完全发不出来。旧版后端用「连续 2 个 5s 周期收不到客户端消息」≈10~15s
判定连接死亡并 close，用户前台静置一段时间就看到「连接已断开」。

本脚本：真实子进程起 main.py（--no-window --port 8788）→ aiohttp 真实 WS
客户端连上收 hello → **静置 20 秒不发任何客户端消息**（比旧误杀窗口更长）→
之后发一个 ping，验证仍能收到 pong 且连接 OPEN。20s < 新的 120s 空闲阈值，
健康连接必须存活。finally 无条件终止子进程，绝不留残留服务/端口占用。

结果写 _idle_result.txt（UTF-8），stdout 只一行 ASCII 汇总，os._exit 退出。
"""
import asyncio
import json
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
PORT = 8788
IDLE_SECONDS = 20
results = []

# HTTPS 现在是默认（手机端麦克风权限要求安全上下文），自签名证书需跳过校验。
SCHEME = "https"
WS_SCHEME = "wss"


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def rec(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))


async def wait_ready(session, timeout=40.0):
    deadline = time.monotonic() + timeout
    url = f"{SCHEME}://127.0.0.1:{PORT}/api/status"
    while time.monotonic() < deadline:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False


async def probe(session):
    ws_url = f"{WS_SCHEME}://127.0.0.1:{PORT}/ws"
    # heartbeat=None：客户端不自动发 ping，完全模拟「定时器被冻结」
    ws = await session.ws_connect(ws_url, heartbeat=None, autoping=True)
    try:
        # 收首条 hello（建连后服务端立刻下发）
        first = await asyncio.wait_for(ws.receive(), timeout=5)
        got_hello = first.type == aiohttp.WSMsgType.TEXT and \
            json.loads(first.data).get("type") == "hello"
        rec("真实服务建连收到 hello", got_hello,
            f"first_type={first.type.name}")

        # ★ 静置 IDLE_SECONDS 秒：不发任何客户端消息，也不主动读帧。
        #   服务端每 10s 广播的 status 会堆在缓冲里，正好证明「服务端下发
        #   不刷新 last_seen」这条旧误杀链路已不再导致 close。
        await asyncio.sleep(IDLE_SECONDS)

        # 静置结束后连接必须仍 OPEN（未被服务端 heartbeat timeout 关掉）
        alive = not ws.closed
        rec(f"静置 {IDLE_SECONDS}s 无 ping 后连接仍 OPEN（旧逻辑 10~15s 必断）",
            alive, f"ws.closed={ws.closed}")

        # 发一个 ping，收 pong：证明双向通道仍可用（先冲掉堆积的 status）
        await ws.send_json({"type": "ping", "payload": {"echo_ts": int(time.time()*1000)},
                            "ts": int(time.time()*1000), "id": "c-idle-probe"})
        pong = None
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            m = await asyncio.wait_for(ws.receive(), timeout=6)
            if m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                          aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                break
            if m.type == aiohttp.WSMsgType.TEXT:
                d = json.loads(m.data)
                if d.get("type") == "pong" and d.get("payload", {}).get("id") is None \
                        or (d.get("type") == "pong"):
                    pong = d
                    break
        rec("静置后 ping 仍收到 pong（双向通道健康）", pong is not None,
            f"pong={pong.get('payload') if pong else None}")

        # 再确认一次连接没被关
        rec("验证结束连接仍 OPEN", not ws.closed, f"ws.closed={ws.closed}")
    finally:
        try:
            await ws.close()
        except Exception:
            pass


def main():
    env = dict(__import__("os").environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--no-window", "--port", str(PORT)],
        cwd=str(SERVER), env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )

    async def runner():
        connector = aiohttp.TCPConnector(ssl=_ssl_ctx())
        async with aiohttp.ClientSession(connector=connector) as session:
            ready = await wait_ready(session)
            rec("真实服务在 8788 就绪", ready, f"ready={ready}")
            if ready:
                await probe(session)

    try:
        asyncio.run(runner())
    except Exception as exc:
        rec("真实服务探针无异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        rec("测试子进程已终止（无残留服务）", proc.poll() is not None,
            f"returncode={proc.poll()}")

    ok = sum(1 for _, good, _ in results if good)
    lines = ["真实起服务·静置断连修复验证（--no-window --port 8788）",
             "=" * 64]
    for name, good, detail in results:
        lines.append(f"  [{'PASS' if good else 'FAIL'}] {name}"
                     + (f" — {detail}" if detail else ""))
    lines.append("-" * 64)
    lines.append(f"合计：PASS={ok}  FAIL={len(results) - ok}  TOTAL={len(results)}")
    (ROOT / "_idle_result.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"IDLE_RESULT PASS={ok} FAIL={len(results)-ok} TOTAL={len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    __import__("os")._exit(_code)
