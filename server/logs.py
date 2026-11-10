"""服务端落盘日志：让「后台连着连着突然断了」下次可以被定位。

为什么必须要有这个模块
----------------------
用户反馈的现象是「电脑启动脚本拉起的那个后台，用一段时间就断了」。这个
现象有三种完全不同的成因，而旧版本一种都留不下证据：

1. **进程本身死了**（控制台被关、依赖崩溃、被系统回收）——日志会直接断流；
2. **进程活着但连接掉了**（TCP 被 NAT 丢弃、手机侧断）——日志里有 close 记录；
3. **进程活着、连接也在，但前端状态机卡在「已断开」**——日志里有存活行。

所以这里做四件事：

* ``setup()``：把 ``logging`` 落到 ``server/logs/service.log``（滚动，单文件
  2 MiB × 5 份），同时保留控制台输出（便携启动器里能看到）；
* ``install_excepthooks()``：主线程与**所有子线程**的未捕获异常都写进日志
  （``threading.excepthook`` 是 Python 3.8+ 的能力，守护线程里的崩溃以前
  是完全静默的）；
* ``event()``：结构化单行事件（键值对），便于 ``Select-String`` 直接筛；
* ``alive()``：周期性存活行，带连接数 / 线程数 / 内存 / 运行时长。

设计约束：日志模块绝不能自己抛异常把服务拖垮，所有对外函数都自带兜底。
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

# 日志目录与文件（相对 server/，跟 config.json 同级，便于整包搬走）
LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_FILE = LOG_DIR / "service.log"
# 单文件上限 2 MiB，保留 5 份（约 10 MiB 上限，够查十几天的断连）
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5
# 日志里的时间戳格式：本地时间 + 毫秒，方便和用户口述时间对齐
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

_logger = logging.getLogger("remote-control")
_configured = False
_START_MONOTONIC = time.monotonic()


class _MillisecondFormatter(logging.Formatter):
    """在时间后面补三位毫秒。

    标准 ``logging`` 的 ``%(asctime)s`` 只有秒级精度，排查「同一秒内断开又
    重连」时不够用，这里自己拼。
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        created = time.localtime(record.created)
        base = time.strftime(_TIME_FORMAT, created)
        return f"{base}.{int(record.msecs):03d}"


def setup(level: int = logging.INFO, *, to_console: bool = True) -> Path:
    """初始化日志；重复调用幂等。返回日志文件路径。

    失败（目录不可写、磁盘满等）时静默降级为「只打控制台」，绝不抛。
    """
    global _configured
    if _configured:
        return LOG_FILE
    _configured = True
    _logger.setLevel(level)
    _logger.propagate = False
    formatter = _MillisecondFormatter(
        fmt="%(asctime)s %(levelname)-7s [%(threadName)-12s] %(message)s"
    )

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=False,
        )
        handler.setFormatter(formatter)
        handler.setLevel(level)
        _logger.addHandler(handler)
    except Exception as exc:  # pragma: no cover - 只在环境异常时走这里
        print(f"[日志] 无法写入文件（仅控制台）：{exc}", flush=True)

    if to_console:
        try:
            stream = logging.StreamHandler(stream=sys.stdout)
            stream.setFormatter(formatter)
            stream.setLevel(level)
            _logger.addHandler(stream)
        except Exception:  # pragma: no cover
            pass

    return LOG_FILE


def install_excepthooks() -> None:
    """接管主线程与子线程的未捕获异常，全部落盘。

    ``threading.excepthook`` 覆盖守护线程——二维码窗口线程、SMTC 探测线程、
    WS 写协程之外的所有 ``threading.Thread`` 都走它，这正是以前「服务悄悄
    没了但控制台什么都没打印」的盲区。
    """

    def _hook(exc_type, exc_value, exc_tb) -> None:  # noqa: ANN001 - 标准签名
        if issubclass(exc_type, KeyboardInterrupt):
            _logger.info("收到 KeyboardInterrupt，准备退出")
            return
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _logger.error("主线程未捕获异常：\n%s", text.rstrip())

    sys.excepthook = _hook

    def _thread_hook(args: threading.ExceptHookArgs) -> None:  # noqa: ANN001
        if args.exc_type is SystemExit:
            return
        text = "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        name = args.thread.name if args.thread is not None else "?"
        _logger.error("线程 %s 未捕获异常：\n%s", name, text.rstrip())

    threading.excepthook = _thread_hook


def event(name: str, **fields: Any) -> None:
    """写一条结构化事件：``事件名 key=value key=value``。

    值里出现空格时自动加引号，便于人工阅读；键顺序按传入顺序（Python 3.7+
    字典有序），保证同类事件在日志里长得一样。
    """
    parts = [name]
    for key, value in fields.items():
        text = str(value)
        if text == "":
            continue
        if " " in text or '"' in text:
            text = '"' + text.replace('"', "'") + '"'
        parts.append(f"{key}={text}")
    _logger.info(" ".join(parts))


def warn(name: str, **fields: Any) -> None:
    """同 ``event``，但以 WARNING 级别写入（会进错误统计）。"""
    parts = [name]
    for key, value in fields.items():
        text = str(value)
        if text == "":
            continue
        if " " in text or '"' in text:
            text = '"' + text.replace('"', "'") + '"'
        parts.append(f"{key}={text}")
    _logger.warning(" ".join(parts))


def error(message: str, **fields: Any) -> None:
    """写一条错误行；``message`` 原样保留（通常是异常文本）。"""
    if fields:
        suffix = " ".join(f"{k}={v}" for k, v in fields.items())
        _logger.error("%s | %s", message, suffix)
    else:
        _logger.error("%s", message)


def exception(message: str) -> None:
    """记录当前异常并附完整 traceback（必须在 except 块里调用）。"""
    _logger.exception("%s", message)


def _rss_kb() -> int:
    """当前进程常驻内存（KB）；取不到返回 0。"""
    try:
        import ctypes
        from ctypes import wintypes

        class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_info.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        if get_info(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return int(counters.WorkingSetSize // 1024)
    except Exception:
        pass
    return 0


def alive(conns: int, *, extra: str = "") -> None:
    """周期性存活行：进程活着的最强证据。

    同时记录线程数与内存，用来抓「线程/内存缓慢增长」这类要跑几小时才显形
    的问题（以前只能靠用户说「用一段时间就断」）。
    """
    uptime = int(time.monotonic() - _START_MONOTONIC)
    hours, rest = divmod(uptime, 3600)
    minutes, seconds = divmod(rest, 60)
    fields: dict[str, Any] = {
        "uptime": f"{hours:02d}:{minutes:02d}:{seconds:02d}",
        "conns": conns,
        "threads": threading.active_count(),
        "rss_kb": _rss_kb(),
        "pid": os.getpid(),
    }
    if extra:
        fields["note"] = extra
    event("alive", **fields)


def tail(lines: int = 40) -> str:
    """读取日志末尾若干行（给 ``service.py status`` 用）；失败返回空串。"""
    try:
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-max(1, int(lines)):])
    except Exception:
        return ""


def path() -> Path:
    """日志文件路径（供横幅 / status 打印）。"""
    return LOG_FILE
