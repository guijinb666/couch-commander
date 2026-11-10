"""验证「服务端 wait_for 取消正在写的帧 → WebSocket 流损坏 → 客户端莫名断连」。

做法：起真实服务 → 客户端连上后**故意不读**（模拟手机 WiFi 卡顿 / socket 缓冲满）
→ 服务端每 10s 广播 status，`deliver(low=True)` 有 0.5s 超时，会取消 send_json
→ 观察客户端最终收到的东西：是干净的 close(code=1000/1001)，还是流损坏报错。

零副作用：不注入键鼠、不改配置、不动屏幕；只发 ping。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

ROOT = _PROJECT_ROOT
SERVER = ROOT / "server"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
OUT = ROOT / "_drop_result.txt"

results: list[tuple[bool, str, str]] = []


def rec(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (f" — {detail}" if detail else ""))


async def main() -> int:
    import aiohttp

    proc = subprocess.Popen(
        [sys.executable, "main.py", "--no-window", "--port", str(PORT)],
        cwd=str(SERVER),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    try:
        base = f"https://127.0.0.1:{PORT}"
        ctx = aiohttp.ClientSession()
        # 等服务起来
        ready = False
        for _ in range(40):
            try:
                async with ctx.get(base + "/api/status", ssl=False) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:
                await asyncio.sleep(0.25)
        rec(ready, "服务已就绪", base)
        if not ready:
            return 1

        ws = await ctx.ws_connect(
            f"wss://127.0.0.1:{PORT}/ws", ssl=False, heartbeat=None, autoping=True
        )
        rec(True, "WS 已连接")

        first = await asyncio.wait_for(ws.receive(), timeout=5)
        rec(
            first.type.name == "TEXT" and json.loads(first.data).get("type") == "hello",
            "收到 hello",
            first.type.name,
        )

        # 关键：接下来一段时间**完全不去读**。服务器每 10s 广播 status，
        # 缓冲区迟早被填满，旧的 deliver 会 wait_for 超时并取消写入。
        # 这里只是空转，不 recv，模拟一个卡住的手机。
        await asyncio.sleep(35)

        # 现在开始读：把积压全读出来，看流有没有坏。
        got = 0
        closed_reason = ""
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=3)
            except asyncio.TimeoutError:
                break
            if msg.type.name == "TEXT":
                got += 1
                continue
            closed_reason = f"{msg.type.name}:{getattr(msg, 'extra', '')}"
            break

        rec(got > 0, "不读 35s 后仍能读出积压消息", f"got={got}")
        rec(
            closed_reason == "",
            "流未被服务端写坏（没有提前 close/错误帧）",
            closed_reason or "no close frame",
        )
        if not ws.closed:
            await ws.close()
            rec(True, "客户端正常关闭")
        await ctx.close()
        return 0 if all(ok for ok, _, _ in results) else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    code = asyncio.run(main())
    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    lines = [f"{'[PASS]' if ok else '[FAIL]'} {n}" + (f" — {d}" if d else "") for ok, n, d in results]
    lines.append(f"DROP_RESULT PASS={passed} FAIL={total - passed} TOTAL={total}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[-1])
    os._exit(code)
