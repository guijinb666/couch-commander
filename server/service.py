"""Windows 后台守护：让服务真正「脱离控制台常驻」并在崩溃后自动拉起。

为什么需要这个文件
------------------
用户的反馈是「电脑启动脚本拉起的那个后台，连着连着突然就断了」。旧启动器
的做法是:

    python "<APP_DIR>\\main.py"      :: cmd 的前台子进程

这有两个致命问题：

1. **关掉控制台 = 杀掉服务**。Windows 关闭 console 会把前台子进程一起终止，
   而用户根本不知道那个黑窗口是「服务本体」，随手关掉就断了；
2. **崩溃没有重启**。tkinter 二维码窗口线程、WinRT（SMTC）、pywin32 的 COM
   任一崩掉，整个进程就没了，还要人手动再点一次启动器。

本模块的处理：

* ``start``：用 **DETACHED_PROCESS + CREATE_NO_WINDOW** 拉起一个**守护进程**
  （本文件的 ``_supervise`` 子命令），守护进程再拉起 ``main.py``。因为守护
  进程与桌面控制台完全脱离，**关掉启动器的黑窗口不会影响服务**；
* ``_supervise``：以子进程方式跑 ``main.py``，退出码非 0（崩溃 / 端口被抢）
  就按 3s→6s→12s→24s→60s 退避重启，并把每次退出码写进日志；
* ``stop``：``taskkill /T /F`` 整棵树（守护进程 + main.py），再清 pid 文件；
* ``status``：pid 是否活着、端口是否在听、日志末尾若干行；
* ``logs``：直接看 ``server/logs/service.log`` 末尾。

设计约束：所有函数都不抛给调用方（除了参数错误），失败返回非 0 退出码并
打印原因 —— 启动器是给用户双击的，不能弹一堆 traceback。
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
RUN_DIR = SERVER_DIR / "run"
PID_FILE = RUN_DIR / "service.pid"
STATE_FILE = RUN_DIR / "service.state.json"
STDOUT_LOG = SERVER_DIR / "logs" / "service.out.log"
LOG_FILE = SERVER_DIR / "logs" / "service.log"
MAIN_PY = SERVER_DIR / "main.py"

DEFAULT_PORT = 8765
# Windows 进程创建标志：脱离控制台 + 不建窗口 + 新进程组（Ctrl+C 不会波及）
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DETACHED_FLAGS = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
# 崩溃重启退避：3s → 6s → 12s → 24s → 60s（封顶），避免疯狂重试烧 CPU
RESTART_BACKOFF = (3, 6, 12, 24, 60)


# ----------------------------------------------------------------------
# 基础探测
# ----------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    """判断 pid 是否还活着（Windows 上用 OpenProcess，不依赖 psutil）。"""
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    """端口是否有进程在听（纯 socket 探测，不依赖 netstat）。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pid() -> int:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _write_pid(pid: int) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(int(pid)), encoding="utf-8")


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(**fields) -> None:
    try:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        state = _read_state()
        state.update(fields)
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _log_line(text: str) -> None:
    """守护进程自己的日志：写进 logs/service.out.log（main.py 的日志另在 service.log）。"""
    try:
        STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with STDOUT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [supervisor] {text}\n")
    except Exception:
        pass


# ----------------------------------------------------------------------
# start / stop / status / logs
# ----------------------------------------------------------------------
def cmd_start(port: int, *, no_window: bool = False, https: bool = True) -> int:
    """若未运行则拉起守护进程；已运行则只报告。"""
    pid = _read_pid()
    if pid and _pid_alive(pid) and _port_listening(port):
        print(f"[服务] 已在运行（守护进程 pid={pid}，端口 {port} 正在监听）")
        print(f"[服务] 日志：{LOG_FILE}")
        return 0
    if _port_listening(port):
        print(
            f"[服务] 端口 {port} 已被其它程序占用，先释放再启动：\n"
            f"       netstat -ano | findstr \":{port} \""
        )
        return 3
    # 清掉过期 pid 文件，避免误判
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "_supervise",
        "--port",
        str(int(port)),
    ]
    if no_window:
        argv.append("--no-window")
    if not https:
        argv.append("--no-https")

    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(SERVER_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=DETACHED_FLAGS,
            close_fds=True,
        )
    except Exception as exc:
        print(f"[服务] 拉起守护进程失败：{exc}")
        return 4

    _write_pid(proc.pid)
    _write_state(port=int(port), supervisor_pid=proc.pid, started_at=time.time())

    # 等端口真正开始监听（首次生成证书 / 装依赖时会慢一些，最多 20s）
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if _port_listening(port):
            print("[服务] 已启动（后台常驻，关闭本窗口不影响服务）")
            print(f"[服务] 守护进程 pid={proc.pid}，端口 {port}")
            print(f"[服务] 服务日志：{LOG_FILE}")
            print(f"[服务] 守护日志：{STDOUT_LOG}")
            print("[服务] 停止：运行 stop（或双击 一键停止手机遥控.bat）")
            return 0
        if not _pid_alive(proc.pid):
            print("[服务] 守护进程启动后立即退出，请查看日志：")
            print(f"       {STDOUT_LOG}")
            for line in _tail(STDOUT_LOG, 20):
                print(f"       {line}")
            return 5
        time.sleep(0.4)
    print(f"[服务] 已拉起守护进程 pid={proc.pid}，但端口 {port} 仍未监听。")
    print(f"[服务] 请看日志：{LOG_FILE}")
    return 6


def cmd_stop(port: int) -> int:
    """taskkill 整棵进程树（守护进程 + main.py）。"""
    pid = _read_pid()
    killed = False
    if pid and _pid_alive(pid):
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            killed = True
        except Exception as exc:
            print(f"[服务] 结束守护进程失败：{exc}")
    # 兜底：pid 文件丢了但端口还在听，按端口找持有者
    if not killed and _port_listening(port):
        holder = _port_owner(port)
        if holder:
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(holder)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=False,
                )
                killed = True
            except Exception:
                pass
    try:
        PID_FILE.unlink(missing_ok=True)
        STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    for _ in range(20):
        if not _port_listening(port):
            break
        time.sleep(0.2)
    if _port_listening(port):
        print(f"[服务] 端口 {port} 仍在监听，可能由其它程序占用（非本服务）。")
        return 2
    print("[服务] 已停止。" if killed else "[服务] 服务本来就没在运行。")
    return 0


def _port_owner(port: int) -> int:
    """用 netstat 找监听某端口的 pid；失败返回 0。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            encoding="mbcs",
            errors="replace",
        ).stdout
    except Exception:
        return 0
    needle = f":{port} "
    for line in out.splitlines():
        if "LISTENING" not in line.upper():
            continue
        if needle not in line:
            continue
        parts = line.split()
        if not parts:
            continue
        try:
            return int(parts[-1])
        except ValueError:
            continue
    return 0


def _tail(path: Path, lines: int) -> list[str]:
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in handle.readlines()[-max(1, lines):]]
    except Exception:
        return []


def cmd_status(port: int) -> int:
    pid = _read_pid()
    alive = bool(pid) and _pid_alive(pid)
    listening = _port_listening(port)
    state = _read_state()
    print("=== 服务状态 ===")
    print(f"守护进程 pid   = {pid or '-'} ({'运行中' if alive else '未运行'})")
    print(f"端口 {port}      = {'监听中' if listening else '未监听'}")
    if state.get("started_at"):
        try:
            started = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(float(state["started_at"]))
            )
            print(f"启动时间       = {started}")
        except Exception:
            pass
    if state.get("restarts") is not None:
        print(f"崩溃重启次数   = {state.get('restarts')}")
    print(f"服务日志       = {LOG_FILE}")
    print(f"守护日志       = {STDOUT_LOG}")
    tail = _tail(LOG_FILE, 12)
    if tail:
        print("=== 服务日志（末 12 行） ===")
        for line in tail:
            print(line)
    return 0 if (alive and listening) else 1


def cmd_logs(lines: int) -> int:
    tail = _tail(LOG_FILE, lines)
    if not tail:
        print(f"（暂无日志：{LOG_FILE}）")
        return 1
    for line in tail:
        print(line)
    return 0


# ----------------------------------------------------------------------
# 守护进程本体
# ----------------------------------------------------------------------
def cmd_supervise(port: int, *, no_window: bool = False, https: bool = True) -> int:
    """守护循环：跑 main.py，崩了按退避重启，直到被 stop 或正常退出。"""
    _write_pid(os.getpid())
    _write_state(port=int(port), supervisor_pid=os.getpid(), started_at=time.time(),
                 restarts=0)
    _log_line(f"supervisor started pid={os.getpid()} port={port}")

    argv = [sys.executable, str(MAIN_PY), "--port", str(int(port))]
    if no_window:
        argv.append("--no-window")
    if not https:
        argv.append("--no-https")

    STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    attempt = 0
    while True:
        started = time.monotonic()
        _log_line("launching: " + " ".join(argv))
        try:
            with STDOUT_LOG.open("a", encoding="utf-8") as out:
                child = subprocess.Popen(
                    argv,
                    cwd=str(SERVER_DIR),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                    close_fds=True,
                )
        except Exception as exc:
            _log_line(f"launch failed: {exc}")
            return 7
        _write_state(child_pid=child.pid)

        code = child.wait()
        lived = time.monotonic() - started
        _log_line(f"main.py exited code={code} after {lived:.1f}s")

        if code == 0:
            # 0 = 正常退出（用户 Ctrl+C / 收到停止信号）→ 守护进程一并收工
            _log_line("clean exit, supervisor stops")
            return 0

        attempt += 1
        delay = RESTART_BACKOFF[min(attempt - 1, len(RESTART_BACKOFF) - 1)]
        # 活得够久（>5 分钟）说明是一次偶发崩溃，退避重置，别惩罚性等待
        if lived > 300:
            attempt = 1
            delay = RESTART_BACKOFF[0]
        _write_state(restarts=attempt, last_exit_code=code,
                     last_exit_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        _log_line(f"restarting in {delay}s (attempt {attempt})")
        # 分片 sleep，便于将来插入停止信号检查
        slept = 0.0
        while slept < delay:
            time.sleep(min(1.0, delay - slept))
            slept += 1.0


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="service.py",
        description="后台守护：start / stop / restart / status / logs",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["start", "stop", "restart", "status", "logs", "_supervise"],
        help="默认 status",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="PORT")
    parser.add_argument("--no-window", action="store_true", help="不弹二维码窗口")
    parser.add_argument("--no-https", action="store_true", help="回退纯 HTTP")
    parser.add_argument("-n", "--lines", type=int, default=40, help="logs 显示行数")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    port = int(args.port)
    if not (1 <= port <= 65535):
        print(f"--port 必须在 1..65535，收到 {port}")
        return errno.EINVAL

    if args.command == "start":
        return cmd_start(port, no_window=args.no_window, https=not args.no_https)
    if args.command == "stop":
        return cmd_stop(port)
    if args.command == "restart":
        cmd_stop(port)
        time.sleep(0.6)
        return cmd_start(port, no_window=args.no_window, https=not args.no_https)
    if args.command == "status":
        return cmd_status(port)
    if args.command == "logs":
        return cmd_logs(args.lines)
    if args.command == "_supervise":
        return cmd_supervise(port, no_window=args.no_window, https=not args.no_https)
    print(f"未知命令：{args.command}")
    return errno.EINVAL


if __name__ == "__main__":
    sys.exit(main())
