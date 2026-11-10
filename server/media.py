"""媒体控制与状态读取。

对应 PROTOCOL 第 3.4 / 4 / 7 节：

* ``media.control`` 使用系统虚拟媒体键 ``VK_MEDIA_*``；
  ``seek_forward`` / ``seek_back`` 用左右方向键重复发送。
* ``media.seek`` 是「尽力映射」：拿不到当前播放进度时只能按目标位置估算
  方向键次数，``ack.payload.extra.best_effort`` 恒为 ``true``。
* ``media.state`` / ``GET /api/media`` 读不到真实播放状态时，按协议返回
  ``{"playing":false,"title":"","position":0.0,"best_effort":true}``。
* ``mouse.pinch`` 的 Ctrl+滚轮映射也放在这里，复用滚轮原语。

所有 Win32 调用都经过 ``input_win``；可选依赖（winsdk / SMTC）用
``try/except`` 降级，任一环节失败都不影响服务启动。
"""
from __future__ import annotations

import asyncio
import math
import threading
from typing import Any

import input_win

# 系统虚拟媒体键 → 协议 action
_ACTION_VK: dict[str, int] = {
    "play_pause": input_win.VK_MEDIA_PLAY_PAUSE,
    "next": input_win.VK_MEDIA_NEXT_TRACK,
    "prev": input_win.VK_MEDIA_PREV_TRACK,
    "volume_up": input_win.VK_MEDIA_VOLUME_UP,
    "volume_down": input_win.VK_MEDIA_VOLUME_DOWN,
    "mute": input_win.VK_MEDIA_VOLUME_MUTE,
}

VALID_ACTIONS: frozenset[str] = frozenset(
    set(_ACTION_VK) | {"seek_forward", "seek_back"}
)

# 单条 media.seek 允许发送的最大方向键次数（避免异常值造成暴走）
MAX_SEEK_STEPS = 10
# 探测 SMTC 的最长等待，避免拖慢 REST / WS 响应
SMTC_PROBE_TIMEOUT_SECONDS = 1.0
# 降级原因，写进 README 的已知限制
SMTC_REASON = (
    "真实播放状态需 WinRT 的 Windows.Media.Control 异步 API（新分拆包 "
    "winrt-runtime + winrt-Windows.Media.Control，旧 winsdk 已停发）；"
    "缺失或读取失败时按协议返回 best_effort 默认字段"
)


def default_state() -> dict[str, Any]:
    """PROTOCOL 第 4 / 7 节规定的默认媒体状态（每次返回新对象）。

    ``duration`` 为 V2.1 追加字段（秒，未知为 0.0），V1 客户端忽略即可。
    """
    return {
        "playing": False,
        "title": "",
        "position": 0.0,
        "duration": 0.0,
        "best_effort": True,
    }


def _smtc_module() -> Any:
    """返回提供 ``GlobalSystemMediaTransportControlsSessionManager`` 的模块。

    优先新的分拆 WinRT 包（``winrt-runtime`` +
    ``winrt-Windows.Media.Control``，``winsdk`` 已停发装不到），
    再兼容旧的单体 ``winsdk``。都不可用返回 ``None``。
    """
    try:
        from winrt.windows.media.control import (  # type: ignore[import-not-found]
            GlobalSystemMediaTransportControlsSessionManager,
        )

        class _Shim:
            pass

        shim = _Shim()
        shim.GlobalSystemMediaTransportControlsSessionManager = (
            GlobalSystemMediaTransportControlsSessionManager
        )
        return shim
    except Exception:
        pass
    try:
        from winsdk.windows.media.control import (  # type: ignore[import-not-found]
            GlobalSystemMediaTransportControlsSessionManager as _Legacy,
        )

        class _LegacyShim:
            pass

        legacy = _LegacyShim()
        legacy.GlobalSystemMediaTransportControlsSessionManager = _Legacy
        return legacy
    except Exception:
        return None


def _read_smtc_sync() -> dict[str, Any] | None:
    """读取系统媒体传输控件（SMTC）真实状态，含真实播放进度。

    优先用新的分拆包 ``winrt-runtime`` + ``winrt-Windows.Media.Control``
    （``winsdk`` 已停止发布，PyPI 上装不到）；同时兼容旧的 ``winsdk`` 单体包。
    WinRT API 是异步的，这里在独立线程里跑独立事件循环，避免与 aiohttp
    主线程的循环冲突。任何 import / API 差异 / 超时都返回 ``None``，
    由调用方走 ``best_effort`` 降级。
    """
    mod = _smtc_module()
    if mod is None:
        return None
    SessionManager = mod.GlobalSystemMediaTransportControlsSessionManager

    async def _read() -> dict[str, Any] | None:
        manager = await SessionManager.request_async()
        session = manager.get_current_session()
        if session is None:
            return {"playing": False, "title": "", "position": 0.0, "duration": 0.0}
        playback = session.get_playback_info()
        # PlaybackStatus 枚举：0=Closed 1=Opened 2=Paused 3=Playing 4=Stopped
        status = getattr(playback, "playback_status", 0)
        playing = int(status) == 3
        title = ""
        app_id = ""
        try:
            props = await session.try_get_media_properties_async()
            title = str(getattr(props, "title", "") or "")
            app_id = str(getattr(props, "artist", "") or "")
        except Exception:
            title = ""
        position = 0.0
        duration = 0.0
        try:
            timeline = session.get_timeline_properties()
            pos_s = getattr(getattr(timeline, "position", None), "duration", None)
            end_s = getattr(getattr(timeline, "end_time", None), "duration", None)
            # WinRT TimeSpan 的 duration 单位是 100ns
            if end_s is not None:
                duration = max(0.0, float(end_s) / 10_000_000.0)
            if pos_s is not None:
                pos_seconds = max(0.0, float(pos_s) / 10_000_000.0)
                position = max(0.0, min(1.0, pos_seconds / duration)) if duration > 0 else 0.0
        except Exception:
            position = 0.0
            duration = 0.0
        return {
            "playing": playing,
            "title": title,
            "position": position,
            "duration": duration,
            "app_id": app_id,
        }

    box: dict[str, Any] = {"value": None}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = loop.run_until_complete(_read())
        except Exception:
            box["value"] = None
        finally:
            try:
                loop.close()
            except Exception:
                pass

    probe = threading.Thread(target=_runner, name="smtc-probe", daemon=True)
    try:
        probe.start()
        probe.join(timeout=SMTC_PROBE_TIMEOUT_SECONDS)
    except Exception:
        return None
    value = box.get("value")
    return value if isinstance(value, dict) else None


def state() -> dict[str, Any]:
    """返回当前媒体状态；读不到真实状态时按协议返回默认字段。

    ``duration`` 是 V2.1 **追加**字段（协议第 4 节允许追加、不得删除/改义）：
    单位秒，读不到时为 ``0.0``。前端用它把 0..1 的 ``position`` 显示成
    「01:23 / 03:45」，并让进度条拖动预览有真实时长可依据。
    """
    observed = _read_smtc_sync()
    if isinstance(observed, dict):
        result = default_state()
        result.update(
            {
                "playing": bool(observed.get("playing", False)),
                "title": str(observed.get("title", "") or ""),
                "position": float(observed.get("position", 0.0) or 0.0),
                "duration": float(observed.get("duration", 0.0) or 0.0),
                "best_effort": False,
            }
        )
        return result
    return default_state()


def control(action: str) -> None:
    """执行一条 ``media.control``。未知 action 抛 ``ValueError``。"""
    if action not in VALID_ACTIONS:
        raise ValueError(f"未知 media action: {action}")
    vk = _ACTION_VK.get(action)
    if vk is not None:
        input_win.send_vk(vk, 1)
        return
    # 快进 / 快退：用左右方向键重复；前端长按会连续发本命令
    key_name = "right" if action == "seek_forward" else "left"
    input_win.send_vk(input_win.VK_TABLE[key_name], input_win.SEEK_REPEAT)


def _seek_exact_sync(position: float) -> bool:
    """用 SMTC 的 ``try_change_playback_position_async`` 直接定位到目标位置。

    这是「实时映射播放进度」的真实通道：拿到当前 session 的
    ``end_time``（总时长），把 0..1 的目标换算成 TimeSpan 后一次性跳转，
    不做任何方向键估算。成功返回 True；无 session / 无时长 / API 失败返回 False，
    由调用方回退到方向键 best-effort。
    """
    mod = _smtc_module()
    if mod is None:
        return False
    SessionManager = mod.GlobalSystemMediaTransportControlsSessionManager
    target = max(0.0, min(1.0, float(position)))

    async def _apply() -> bool:
        manager = await SessionManager.request_async()
        session = manager.get_current_session()
        if session is None:
            return False
        timeline = session.get_timeline_properties()
        end_s = getattr(getattr(timeline, "end_time", None), "duration", None)
        if end_s is None:
            return False
        duration = float(end_s)
        if duration <= 0:
            return False
        # WinRT TimeSpan：构造用 100ns 为单位的整数
        try:
            from winrt.windows.foundation import TimeSpan  # type: ignore[import-not-found]

            span = TimeSpan(int(round(target * duration)))
        except Exception:
            try:
                from datetime import timedelta

                span = timedelta(seconds=target * duration / 10_000_000.0)
            except Exception:
                return False
        await session.try_change_playback_position_async(span)
        return True

    box: dict[str, Any] = {"value": False}

    def _runner() -> None:
        loop = asyncio.new_event_loop()
        try:
            box["value"] = bool(loop.run_until_complete(_apply()))
        except Exception:
            box["value"] = False
        finally:
            try:
                loop.close()
            except Exception:
                pass

    probe = threading.Thread(target=_runner, name="smtc-seek", daemon=True)
    try:
        probe.start()
        probe.join(timeout=SMTC_PROBE_TIMEOUT_SECONDS)
    except Exception:
        return False
    return bool(box.get("value"))


def seek(position: float) -> dict[str, Any]:
    """``media.seek``：优先用 SMTC 真实定位，拿不到才退回方向键估算。

    * 能与播放器建立 SMTC 会话且拿到总时长 → ``try_change_playback_position_async``
      一次性跳到目标位置，``best_effort:false``，``steps:0``。
    * 否则退回原来的「按离中点距离估算方向键次数」，``best_effort:true``。

    返回值作为 ``ack.payload.extra`` 下发给前端。
    """
    value = float(position)
    if _seek_exact_sync(value):
        return {
            "best_effort": False,
            "position": value,
            "steps": 0,
            "method": "smtc",
        }
    distance = abs(value - 0.5)
    steps = int(round(distance * 20.0))
    steps = max(0, min(MAX_SEEK_STEPS, steps))
    if steps > 0:
        key_name = "right" if value >= 0.5 else "left"
        input_win.send_vk(input_win.VK_TABLE[key_name], steps)
    return {
        "best_effort": True,
        "position": value,
        "steps": steps,
        "method": "keys" if steps else "noop",
    }


def pinch_zoom(scale: float) -> int:
    """``mouse.pinch``：``scale > 1`` 放大，映射为 Ctrl + 滚轮。

    返回实际发送的滚轮格数，供 ``ack.payload.extra`` 说明。
    """
    value = float(scale)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("scale 必须是大于 0 的有限数")
    notches = int(round(math.log(value) * 3.0))
    limit = input_win.MAX_WHEEL_NOTCHES
    notches = max(-limit, min(limit, notches))
    if notches == 0:
        return 0
    input_win.key_down("ctrl")
    try:
        input_win.mouse_wheel(dx=0.0, dy=float(notches))
    finally:
        input_win.key_up("ctrl")
    return notches
