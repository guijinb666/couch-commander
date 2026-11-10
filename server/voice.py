"""语音会话落地（V2 追加层 §6 / §9）。

交互链路（前端采集 / 手势 + 后端落地）：

* 长按语音按钮 → 前端 ``getUserMedia``（**这一步才是真正的麦克风权限请求**）
  + Web Speech API 识别，同时发 ``voice.start{}``；后端只建立内存会话，
  **不打开任何浏览器页面**（曾经的 ``webbrowser.open(muse)`` 已移除，
  见 :func:`session_open` 的说明）。
* 识别过程中 → ``voice.text{text,final:false}`` 仅记预览，不逐条 ack。
* 松手 → ``voice.text{text,final:true}``：把文本送入当前焦点输入框并按回车
  提交（复用 V1 ``input_win.key_type`` / ``input_win.key_press``，不新造键）。
* ``voice.stop{}``：结束本次按住说话会话、标记已发送；不关闭 / 不导航任何页面。

**如实限制**：Windows 无「把手机音频流注入电脑麦克风」的虚拟音频设备能力
（本机枚举确认无 VB-CABLE 一类设备），本版走「手机端识别为文本 → 后端送文本」
的路径；真实麦克风音频输入需虚拟声卡配合前端推流，属后续扩展，本版不承诺。

所有第三方 / 平台能力（``webbrowser``、``win32gui``、``input_win``）调用
一律 ``try/except`` 降级，缺依赖、非桌面会话、非 Windows 不崩服务。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import input_win

DEFAULT_MUSE_URL = "https://muse.ai/"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "active": False,
    "url": "",
    "opened": False,
    "focused": False,
    "preview": "",
    "final_text": "",
    "sent": False,
}


def _focus_window(names: tuple[str, ...] = ("muse",)) -> bool:
    """可选：按窗口标题模糊匹配把 muse 置前；缺失/失败返回 ``False``。"""
    try:
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]
    except Exception:
        return False
    hit: list[int] = []

    def _callback(hwnd: int, _unused: Any) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).casefold()
            if any(name in title for name in names):
                hit.append(hwnd)
        except Exception:
            return

    try:
        win32gui.EnumWindows(_callback, None)
        if not hit:
            return False
        win32gui.ShowWindow(hit[0], win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hit[0])
        return True
    except Exception:
        return False


def _muse_window() -> int:
    """返回第一个可见的 muse 窗口句柄；找不到返回 0。"""
    try:
        import win32gui  # type: ignore[import-not-found]
    except Exception:
        return 0
    found: list[int] = []

    def _callback(hwnd: int, _unused: Any) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if "muse" in win32gui.GetWindowText(hwnd).casefold():
                found.append(hwnd)
        except Exception:
            return

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        return 0
    return found[0] if found else 0


POINT_FILE = Path(__file__).resolve().parent / "voice_point.json"


def load_point() -> dict[str, float] | None:
    """读取 muse 语音键的点击位置（客户区相对比例 0..1）；无配置返回 None。"""
    try:
        raw = json.loads(POINT_FILE.read_text(encoding="utf-8"))
        fx = float(raw["x"])
        fy = float(raw["y"])
    except Exception:
        return None
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    return {"x": fx, "y": fy}


def save_point(x: float, y: float) -> bool:
    """写入点击位置（由标定脚本调用）。"""
    try:
        POINT_FILE.write_text(
            json.dumps({"x": round(float(x), 4), "y": round(float(y), 4)}, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


def _click_relative(hwnd: int, fx: float, fy: float) -> bool:
    """在 hwnd 客户区内按相对比例点一下（用来点 muse 自带的语音键）。"""
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        left, top, right, bottom = win32gui.GetClientRect(hwnd)
        origin_x, origin_y = win32gui.ClientToScreen(hwnd, (left, top))
        width = max(1, right - left)
        height = max(1, bottom - top)
        px = origin_x + int(round(width * max(0.0, min(1.0, fx))))
        py = origin_y + int(round(height * max(0.0, min(1.0, fy))))
        win32api.SetCursorPos((px, py))
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True
    except Exception:
        return False


def activate_mic() -> dict[str, Any]:
    """聚焦 muse 并尝试点它自带的语音键；无标定坐标时只聚焦不动鼠标。

    跨设备链路里「muse 明明开在电脑上、语音键得有人点」这一环只能由电脑侧
    的鼠标点击完成，因此位置必须标定一次（``python _voice_point.py``），
    标定结果存 ``server/voice_point.json``；未标定时**不点**，避免误触发送键。
    """
    focused = _focus_window()
    point = load_point()
    if not point:
        return {"focused": focused, "mic_clicked": False, "mic_point_configured": False}
    hwnd = _muse_window()
    if not hwnd:
        return {"focused": focused, "mic_clicked": False, "mic_point_configured": True}
    time.sleep(0.25)
    clicked = _click_relative(hwnd, point["x"], point["y"])
    return {"focused": focused or clicked, "mic_clicked": clicked, "mic_point_configured": True}


def session_open() -> dict[str, Any]:
    """``voice.start`` 的新语义：**只开一个内存会话，不打开任何页面**。

    历史教训（务必保留这段说明）：早期版本在这里用 ``webbrowser.open`` 打开
    ``https://muse.ai/``，但那站现在是 Meta Muse 的落地页、没有语音输入按钮，
    打开它既不解决问题，还会突然弹窗打断用户 —— YG 明确反馈「长按语音还是会
    弹出 muse，关掉」。所以现在这里**完全不碰浏览器**，返回里用
    ``page_opened: False`` 如实说明「没有打开任何页面」。
    """
    with _lock:
        _state.update(
            {
                "active": True,
                "url": "",          # 不再关联任何外部页面
                "opened": False,    # 从没打开过页面
                "focused": False,
                "preview": "",
                "final_text": "",
                "sent": False,
            }
        )
        return {
            "opened": True,          # 会话本身已建立（不是页面）
            "page_opened": False,    # 明确：没有开任何窗口
            "focused": False,
            "url": "",
            "best_effort": False,
        }


def start(url: str = DEFAULT_MUSE_URL, activate: bool = True) -> dict[str, Any]:
    """【历史接口，已不被 dispatcher 调用】幂等打开官网 + 聚焦 + 尝试唤起 muse 语音键。

    保留仅为兼容可能存在的旧调用点 / 自检；``voice.start`` 现在的实现是
    :func:`session_open`（不打开任何页面）。
    """
    target = str(url or DEFAULT_MUSE_URL).strip() or DEFAULT_MUSE_URL
    opened = False
    with _lock:
        already_open = bool(_state["opened"] and _state["url"] == target)
    if not already_open:
        try:
            import webbrowser

            opened = bool(webbrowser.open(target))
        except Exception:
            opened = False
    with _lock:
        if already_open:
            opened = bool(_state["opened"])
        focused = _focus_window()
        _state.update(
            {
                "active": True,
                "url": target,
                "opened": opened,
                "focused": focused,
                "preview": "",
                "final_text": "",
                "sent": False,
            }
        )
        result: dict[str, Any] = {
            "opened": opened,
            "focused": focused,
            "url": target,
            "best_effort": True,
        }
    if activate:
        # 页面刚打开时 DOM 还没就绪，等一拍再去点语音键。
        time.sleep(0.8)
        result.update(activate_mic())
    else:
        result.update({"mic_clicked": False, "mic_point_configured": bool(load_point())})
    return result


def _inject_text(text: str) -> bool:
    """聚焦后用 V1 输入注入把文本送进当前焦点输入框并回车提交。"""
    if not text:
        return False
    try:
        _focus_window()
        input_win.key_type(text)
        input_win.key_press(["enter"])
        return True
    except input_win.UnknownKeyError:
        # "enter" 必然在 V1 键名表内；真异常也按 best-effort 吞掉
        return False
    except input_win.InputUnavailableError:
        return False
    except Exception:
        return False


def text(value: str, final: bool = False) -> dict[str, Any]:
    """``voice.text``：``final:false`` 只记预览；``final:true`` 送文本 + 回车。

    返回 dict 供 dispatcher 判断是否需要 ack；函数本身不抛异常。
    """
    body = str(value or "")
    with _lock:
        _state["active"] = True
        if final:
            _state["final_text"] = body
            _state["preview"] = ""
        else:
            _state["preview"] = body
    if not final:
        return {"acked": False, "final": False, "best_effort": True}
    injected = _inject_text(body)
    with _lock:
        _state["sent"] = injected
        _state["inject_ok"] = injected
    return {
        "acked": True,
        "final": True,
        "text_length": len(body),
        "injected": injected,
        "sent": injected,
        "best_effort": True,
    }


def stop() -> dict[str, Any]:
    """``voice.stop``：结束本次会话并标记已发送；不关闭 / 不导航 / 不清空 muse。"""
    with _lock:
        sent = bool(_state["sent"] or _state["final_text"])
        _state["active"] = False
        _state["sent"] = sent
        return {
            "sent": sent,
            "opened": bool(_state["opened"]),
            "url": str(_state["url"] or DEFAULT_MUSE_URL),
            "best_effort": True,
        }


def state() -> dict[str, Any]:
    """供离线自检 / 调试读取内部状态（不参与协议）。"""
    with _lock:
        return dict(_state)
