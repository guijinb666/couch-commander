"""Windows 输入注入模块（纯 ``ctypes``，不依赖 pywin32 做输入）。

设计要点（对应 PROTOCOL 第 5 / 8 节）：

* 鼠标移动只用相对的 ``MOUSEEVENTF_MOVE``，**绝不调用 ``SetCursorPos``**，
  避免多显示器坐标跳变与 UWP/权限问题。
* 小数像素增量用模块级余量累加，保证高帧率小位移不丢精度。
* 组合键按数组顺序按下、**逆序释放**（``["ctrl","c"]`` → 按下 ctrl,c；释放 c,ctrl）。
* ``key.type`` 用 ``KEYEVENTF_UNICODE``，支持中文与非 BMP 字符（UTF-16 代理对）。
* 未知键名抛 ``UnknownKeyError``（``code = "UNKNOWN_KEY"``），由 dispatcher 转成
  ``error`` 消息。
* 在非 Windows 平台或 ``user32`` 不可用时，模块仍可被 import，只是调用时抛
  ``InputUnavailableError``，服务本身不会因此启动失败。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

# ---------------------------------------------------------------------------
# 平台可用性与基础类型
# ---------------------------------------------------------------------------
_IS_WINDOWS = ctypes.sizeof(ctypes.c_void_p) in (4, 8) and hasattr(ctypes, "windll")
try:
    _user32 = ctypes.windll.user32 if _IS_WINDOWS else None  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - 非 Windows 或受限环境
    _user32 = None

# ULONG_PTR 与指针同宽
_ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


if _user32 is not None:
    _SendInput = _user32.SendInput
    _SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _SendInput.restype = wintypes.UINT
else:
    _SendInput = None

# INPUT.type
INPUT_MOUSE = 0x0000
INPUT_KEYBOARD = 0x0001
INPUT_HARDWARE = 0x0002

# MOUSEINPUT.dwFlags
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000

# KEYBDINPUT.dwFlags
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

WHEEL_DELTA = 120

# 按键按下与释放之间的极短停顿，提升 Alt+Tab / Win+D 等组合键可靠性
KEY_HOLD_SECONDS = 0.012
# 单条 mouse.scroll 允许的最大滚动格数，防止异常大数值造成暴走
MAX_WHEEL_NOTCHES = 10
# 单条 media.control seek_forward / seek_back 重复按方向键的次数
SEEK_REPEAT = 1


class InputUnavailableError(RuntimeError):
    """当前进程无法使用 SendInput（非 Windows 或权限受限）。"""

    code = "INPUT_UNAVAILABLE"


class UnknownKeyError(ValueError):
    """``key.press.keys[]`` 中出现未登记的键名。"""

    code = "UNKNOWN_KEY"


# ---------------------------------------------------------------------------
# VK 表：覆盖 PROTOCOL 第 5 节全部键名
# ---------------------------------------------------------------------------
VK_TABLE: dict[str, int] = {}

# 字母 a-z / 数字 0-9
for _i in range(26):
    VK_TABLE[chr(ord("a") + _i)] = 0x41 + _i
for _i in range(10):
    VK_TABLE[str(_i)] = 0x30 + _i

# 功能键
VK_TABLE.update(
    {
        "enter": 0x0D,
        "esc": 0x1B,
        "tab": 0x09,
        "space": 0x20,
        "backspace": 0x08,
        "delete": 0x2E,
        "home": 0x24,
        "end": 0x23,
        "pageup": 0x21,
        "pagedown": 0x22,
        "up": 0x26,
        "down": 0x28,
        "left": 0x25,
        "right": 0x27,
        "ctrl": 0x11,
        "shift": 0x10,
        "alt": 0x12,
        "win": 0x5B,
        "capslock": 0x14,
        "insert": 0x2D,
        "printscreen": 0x2C,
        # 标点 / OEM 键
        "`": 0xC0,
        "-": 0xBD,
        "=": 0xBB,
        "[": 0xDB,
        "]": 0xDD,
        "\\": 0xDC,
        ";": 0xBA,
        "'": 0xDE,
        ",": 0xBC,
        ".": 0xBE,
        "/": 0xBF,
    }
)
for _i in range(1, 13):
    VK_TABLE[f"f{_i}"] = 0x6F + _i  # f1=0x70 ... f12=0x7B

# 系统虚拟媒体键（media.py 通过 send_vk 使用）
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_VOLUME_UP = 0xAF
VK_MEDIA_VOLUME_DOWN = 0xAE
VK_MEDIA_VOLUME_MUTE = 0xAD

# 需要 KEYEVENTF_EXTENDEDKEY 的扩展键
_EXTENDED_VKS: frozenset[int] = frozenset(
    {
        0x21, 0x22, 0x23, 0x24,  # pageup / pagedown / end / home
        0x25, 0x26, 0x27, 0x28,  # 方向键
        0x2C, 0x2D, 0x2E,        # printscreen / insert / delete
        0x5B, 0x5C, 0x5D,        # LWIN / RWIN / APPS
        0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7,
    }
)

# 按钮名 → (按下标志, 释放标志)
_BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

# 小数余量累加器（相对增量不丢精度）
_move_remainder_x = 0.0
_move_remainder_y = 0.0
_wheel_remainder_x = 0.0
_wheel_remainder_y = 0.0


def is_available() -> bool:
    """当前环境是否真的能注入输入。"""
    return _SendInput is not None


def _require_input() -> None:
    if _SendInput is None:
        raise InputUnavailableError(
            "SendInput 不可用：请在 Windows 桌面会话中运行本服务"
        )


def _send(inputs: list[_INPUT]) -> None:
    """一次性提交一批 INPUT；返回值校验并附带 GetLastError 便于排查。"""
    _require_input()
    count = len(inputs)
    array = (_INPUT * count)(*inputs)
    sent = _SendInput(count, array, ctypes.sizeof(_INPUT))
    if sent != count:
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        raise OSError(f"SendInput 只提交了 {sent}/{count} 个事件，GetLastError={err}")


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> _INPUT:
    item = _INPUT()
    item.type = INPUT_MOUSE
    item.mi = _MOUSEINPUT()
    item.mi.dx = dx
    item.mi.dy = dy
    item.mi.mouseData = data & 0xFFFFFFFF
    item.mi.dwFlags = flags
    item.mi.time = 0
    item.mi.dwExtraInfo = 0
    return item


def _scan_code(vk: int) -> int:
    """取 VK 对应的硬件扫描码（``MapVirtualKeyW`` 的 VK_TO_VSC 映射）。

    物理键盘每次按键都会同时携带 VK 与扫描码，浏览器据此算出
    ``KeyboardEvent.code``（``KeyD`` / ``ArrowRight``）。只发 VK 时 ``code``
    是空串，靠 ``event.code`` 匹配的网页快捷键就会失效，而靠 ``event.key``
    的照常工作 —— 表现为「有的键能用、有的键不能用」。所以这里补上扫描码，
    让注入的按键与真实键盘在 Receiver 侧完全一致。
    """
    if not _IS_WINDOWS or vk <= 0:
        return 0
    try:
        return int(_user32.MapVirtualKeyW(int(vk), 0)) & 0xFF
    except Exception:
        return 0


def _key_input(vk: int, flags: int, scan: int = -1) -> _INPUT:
    item = _INPUT()
    item.type = INPUT_KEYBOARD
    item.ki = _KEYBDINPUT()
    item.ki.wVk = vk
    # scan=-1 表示「按 VK 自动推导」，调用方仍可显式指定（key_type 用 UNICODE 时必须保持 0）。
    item.ki.wScan = _scan_code(vk) if scan < 0 else scan
    item.ki.dwFlags = flags
    item.ki.time = 0
    item.ki.dwExtraInfo = 0
    return item


def _extended_flag(vk: int) -> int:
    return KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0


def resolve_key(name: str) -> int:
    """把协议键名解析为 VK；未知键抛 ``UnknownKeyError``。"""
    if not isinstance(name, str):
        raise UnknownKeyError(f"键名必须是字符串，收到 {type(name).__name__}")
    key = name.strip().lower()
    vk = VK_TABLE.get(key)
    if vk is None:
        raise UnknownKeyError(f"未知键名: {name}")
    return vk


# ---------------------------------------------------------------------------
# 鼠标
# ---------------------------------------------------------------------------
def mouse_move(dx: float, dy: float) -> None:
    """相对移动鼠标（``MOUSEEVENTF_MOVE``），不使用 ``SetCursorPos``。

    ``dx`` / ``dy`` 允许小数；小数部分累加到下一次调用，避免 60fps 小位移被
    截断成 0。
    """
    global _move_remainder_x, _move_remainder_y
    total_x = float(dx) + _move_remainder_x
    total_y = float(dy) + _move_remainder_y
    step_x = int(total_x)
    step_y = int(total_y)
    _move_remainder_x = total_x - step_x
    _move_remainder_y = total_y - step_y
    if step_x == 0 and step_y == 0:
        return
    _send([_mouse_input(MOUSEEVENTF_MOVE, dx=step_x, dy=step_y)])


def _button_flags(button: str) -> tuple[int, int]:
    flags = _BUTTON_FLAGS.get(button)
    if flags is None:
        raise ValueError(f"未知按钮: {button}，可用 left / right / middle")
    return flags


def mouse_down(button: str) -> None:
    """按下鼠标键（拖拽开始）。"""
    down, _ = _button_flags(button)
    _send([_mouse_input(down)])


def mouse_up(button: str) -> None:
    """释放鼠标键（拖拽结束）。"""
    _, up = _button_flags(button)
    _send([_mouse_input(up)])


def mouse_click(button: str, clicks: int = 1) -> None:
    """单击 / 双击；``clicks`` 为 1 或 2，其它值按 1 次处理并做边界收敛。"""
    down, up = _button_flags(button)
    times = max(1, min(2, int(clicks)))
    batch: list[_INPUT] = []
    for _ in range(times):
        batch.append(_mouse_input(down))
        batch.append(_mouse_input(up))
    _send(batch)


def mouse_wheel(dx: float = 0.0, dy: float = 0.0) -> None:
    """双指滚动换算成 Windows 滚轮事件。

    约定：``dy > 0`` 向上滚；**1 格 = ``WHEEL_DELTA``(120) 单位**。
    入参 ``dx`` / ``dy`` 按「滚动格数」解释，小数部分跨帧累加，因此前端
    每帧发的小增量不会丢失；单次最多发送 ``MAX_WHEEL_NOTCHES`` 格。
    水平滚动用 ``MOUSEEVENTF_HWHEEL``。
    """
    global _wheel_remainder_x, _wheel_remainder_y
    total_x = float(dx) + _wheel_remainder_x
    total_y = float(dy) + _wheel_remainder_y
    notches_x = int(total_x)
    notches_y = int(total_y)
    _wheel_remainder_x = total_x - notches_x
    _wheel_remainder_y = total_y - notches_y
    notches_x = max(-MAX_WHEEL_NOTCHES, min(MAX_WHEEL_NOTCHES, notches_x))
    notches_y = max(-MAX_WHEEL_NOTCHES, min(MAX_WHEEL_NOTCHES, notches_y))
    batch: list[_INPUT] = []
    if notches_y:
        batch.append(
            _mouse_input(MOUSEEVENTF_WHEEL, data=notches_y * WHEEL_DELTA)
        )
    if notches_x:
        batch.append(
            _mouse_input(MOUSEEVENTF_HWHEEL, data=notches_x * WHEEL_DELTA)
        )
    if batch:
        _send(batch)


# ---------------------------------------------------------------------------
# 键盘
# ---------------------------------------------------------------------------
def key_down(name: str) -> None:
    """按下单个键（配合 ``key_up`` 用于 Ctrl+滚轮之类组合）。"""
    vk = resolve_key(name)
    _send([_key_input(vk, _extended_flag(vk))])


def key_up(name: str) -> None:
    """释放单个键。"""
    vk = resolve_key(name)
    _send([_key_input(vk, _extended_flag(vk) | KEYEVENTF_KEYUP)])


def send_vk(vk: int, repeat: int = 1) -> None:
    """发送一个原始 VK 的完整按下/释放（供 media.py 的虚拟媒体键使用）。"""
    times = max(1, min(50, int(repeat)))
    flags = _extended_flag(vk)
    batch: list[_INPUT] = []
    for _ in range(times):
        batch.append(_key_input(vk, flags))
        batch.append(_key_input(vk, flags | KEYEVENTF_KEYUP))
    if batch:
        _send(batch)


def key_press(keys: list[str] | tuple[str, ...]) -> None:
    """单键或组合键：按数组顺序按下、**逆序释放**。

    例：``["ctrl","c"]`` → down ctrl, down c, up c, up ctrl。
    未知键名抛 ``UnknownKeyError``。
    """
    if not isinstance(keys, (list, tuple)) or not keys:
        raise ValueError("keys 必须是非空数组")
    vks = [resolve_key(k) for k in keys]
    down_batch = [_key_input(vk, _extended_flag(vk)) for vk in vks]
    up_batch = [
        _key_input(vk, _extended_flag(vk) | KEYEVENTF_KEYUP)
        for vk in reversed(vks)
    ]
    _send(down_batch)
    if KEY_HOLD_SECONDS > 0:
        time.sleep(KEY_HOLD_SECONDS)
    _send(up_batch)


def key_combo(keys: list[str] | tuple[str, ...]) -> None:
    """``key_press`` 的组合键语义别名（协议里两条命名等价）。"""
    key_press(keys)


def key_type(text: str) -> None:
    """用 ``KEYEVENTF_UNICODE`` 直接输入文本，支持中文与 emoji。

    UTF-16 代理对被拆成两个 16 位单元分别发送，保证非 BMP 字符正确落地。
    """
    if not isinstance(text, str) or text == "":
        return
    raw = text.encode("utf-16-le", errors="replace")
    batch: list[_INPUT] = []
    for offset in range(0, len(raw), 2):
        unit = raw[offset] | (raw[offset + 1] << 8)
        batch.append(_key_input(0, KEYEVENTF_UNICODE, scan=unit))
        batch.append(_key_input(0, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, scan=unit))
    if batch:
        _send(batch)
