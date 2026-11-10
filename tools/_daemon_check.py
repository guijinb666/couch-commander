"""守护进程与日志专项回归（零副作用：只用测试端口，结束时清理）。

验证 YG 报的「电脑启动脚本拉起的后台，连着连着突然就断了」这条链路上
新加的三件能力：

  1. service.py start 能把 main.py 拉成**脱离控制台**的进程（DETACHED_PROCESS）
  2. main.py 被杀后 supervisor 会按退避自动重启，端口重新监听
  3. server/logs/service.log 真的落盘，并且有 boot / ws.open / ws.close 行
  4. service.py stop 能把守护进程 + 子进程一起收干净，端口释放

用法:
    python _daemon_check.py [PORT]
默认端口 8971（避开 8765 上正在运行的真实服务）。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

SERVER_DIR = _PROJECT_ROOT / "server"
PY = sys.executable
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8971
STATE = SERVER_DIR / "run" / "service.state.json"
LOG = SERVER_DIR / "logs" / "service.log"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.7):
            return True
    except OSError:
        return False


def run(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [PY, str(SERVER_DIR / "service.py"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        cwd=str(SERVER_DIR),
    )


def status_json() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ws_roundtrip(port: int) -> bool:
    """真的连一次 WS，用来在服务日志里留下 ws.open / ws.close。"""
    try:
        import asyncio

        import aiohttp
    except Exception:
        return False

    async def _go() -> bool:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            async with aiohttp.ClientSession() as s:
                async with s.ws_connect(
                    f"wss://127.0.0.1:{port}/ws", ssl=ctx, timeout=aiohttp.ClientTimeout(total=8)
                ) as ws:
                    await ws.receive(timeout=6)
                    await ws.close()
            return True
        except Exception:
            return False

    return asyncio.run(_go())


def http_ok(port: int) -> bool:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(
            f"https://127.0.0.1:{port}/api/status", context=ctx, timeout=6
        ) as r:
            return r.status == 200 and b"connected" in r.read(400)
    except Exception:
        return False


def main() -> int:
    print(f"=== daemon check on port {PORT} ===")
    clean: list[Path] = []

    # 0) 前置清理
    run("stop", "--port", str(PORT))
    time.sleep(0.6)
    check(not port_open(PORT), "起点干净：端口未被占用", f"port={PORT}")

    # 1) 启动
    log_before_size = LOG.stat().st_size if LOG.exists() else 0
    started = run("start", "--port", str(PORT))
    ok_start = started.returncode == 0 and port_open(PORT)
    check(ok_start, "service.py start 拉起后端口监听", f"rc={started.returncode}")
    if not ok_start:
        print(started.stdout)
        print(started.stderr)
        return report()

    st = status_json()
    sup = int(st.get("supervisor_pid") or 0)
    child = int(st.get("child_pid") or 0)
    check(sup > 0 and child > 0 and sup != child, "守护进程与 main.py 是两个不同 pid",
          f"supervisor={sup} child={child}")

    # 2) 真的能服务（HTTPS + WS）
    check(http_ok(PORT), "HTTPS /api/status 正常")
    check(ws_roundtrip(PORT), "WS 握手 / hello / 正常关闭")

    # 3) 日志落盘且有结构化事件
    time.sleep(0.8)
    log_text = LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""
    new_part = log_text[log_before_size:] if log_text else ""
    check(LOG.exists() and LOG.stat().st_size > log_before_size, "service.log 有新内容写入",
          f"size={LOG.stat().st_size if LOG.exists() else 0}")
    check("boot" in new_part, "日志含 boot 行", first_line(new_part, "boot"))
    check("ws.open" in new_part, "日志含 ws.open 行", first_line(new_part, "ws.open"))
    check("ws.close" in new_part, "日志含 ws.close 行（含 close code）",
          first_line(new_part, "ws.close"))

    # 4) 杀掉 main.py，守护应自动重启
    kill_ok = kill_pid(child)
    check(kill_ok, "已强制结束 main.py（模拟崩溃）", f"pid={child}")
    recovered = False
    new_child = 0
    for _ in range(24):  # 最多约 12s（退避第一档 3s）
        time.sleep(0.5)
        st2 = status_json()
        restarts = int(st2.get("restarts") or 0)
        c2 = int(st2.get("child_pid") or 0)
        if restarts >= 1 and c2 and c2 != child and port_open(PORT):
            recovered = True
            new_child = c2
            break
    check(recovered, "崩溃后自动重启，端口重新监听",
          f"new_child={new_child} restarts={status_json().get('restarts')}")

    # 5) 重启后仍能服务
    if recovered:
        check(http_ok(PORT), "重启后 HTTPS 仍正常")

    # 6) 控制台无关性：supervisor 的父进程不是当前 shell
    detached = is_detached(sup)
    check(detached, "守护进程已脱离控制台（DETACHED / 无 console）", f"pid={sup}")

    # 7) stop 收干净
    stopped = run("stop", "--port", str(PORT))
    time.sleep(0.8)
    check(
        stopped.returncode == 0 and not port_open(PORT),
        "service.py stop 后端口释放",
        f"rc={stopped.returncode}",
    )
    st3 = status_json()
    check(not int(st3.get("supervisor_pid") or 0), "stop 后 state 里不再有守护 pid",
          f"state={st3}")
    check(not pid_alive(sup), "守护进程已退出", f"pid={sup}")

    for p in clean:
        shutil.rmtree(p, ignore_errors=True)
    return report()


def first_line(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line.strip()[:160]
    return ""


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def kill_pid(pid: int) -> bool:
    try:
        r = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True, timeout=15
        )
        return r.returncode == 0
    except Exception:
        return False


def is_detached(pid: int) -> bool:
    """用 GetConsoleWindow 间接判断太绕，这里看进程的父 pid：
    守护进程由 DETACHED_PROCESS 拉起，父进程应是已退出的 start 进程，
    因此 GetParentProcessId 指向的 pid 要么已死、要么不是当前 shell。"""
    if not pid_alive(pid):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        ntdll = ctypes.WinDLL("ntdll")
        # PROCESS_BASIC_INFORMATION 的 InheritedFromUniqueProcessId
        class PBI(ctypes.Structure):
            _fields_ = [
                ("ExitStatus", ctypes.c_long),
                ("PebBaseAddress", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_void_p),
                ("BasePriority", ctypes.c_long),
                ("UniqueProcessId", ctypes.c_void_p),
                ("InheritedFromUniqueProcessId", ctypes.c_void_p),
            ]

        h = k.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            pbi = PBI()
            ret = ctypes.c_ulong()
            ntdll.NtQueryInformationProcess(
                wintypes.HANDLE(h), 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret)
            )
            ppid = int(pbi.InheritedFromUniqueProcessId or 0)
        finally:
            k.CloseHandle(h)
        # 父进程要么已经退出，要么不是我们这条 shell 链上的 python/cmd
        return ppid == 0 or not pid_alive(ppid) or ppid != os.getpid()
    except Exception:
        return False


def report() -> int:
    passed = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    failed = total - passed
    print(f"\nDAEMON_RESULT PASS={passed} FAIL={failed} TOTAL={total}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
