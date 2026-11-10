"""屏幕亮度控制（V2 追加层 §5 / §9）。

实现优先级（本条为 YG 实测后补充 DDC/CI 通道后的顺序）：

1. **DDC/CI 外接屏**：``dxva2.SetMonitorBrightness``（VCP 0x10），
   ``method="ddc"``。台式 / 外接显示器唯一真正能改背光的通道，
   也就是 YG 实测「亮度调节不行」时缺的那一条。
2. **WMI 真实背光**：``root\\WMI`` → ``WmiMonitorBrightnessMethods.WmiSetBrightness``
   （``Timeout=1, Brightness=0..100``），``method="wmi"``。笔记本内置屏有效，
   外接显示器无效（所以放在 DDC/CI 之后）。
3. **软件 gamma 近似**：``GetDeviceGammaRamp`` 读取原始 ramp 并缓存，
   ``SetDeviceGammaRamp`` 写近似亮度，``method="gamma"``，``best_effort:true``。
   gamma 只改色阶、不改背光，这一点在 ``README.md`` 如实标注。
4. 三者都失败：返回 ``applied:false, best_effort:true``，**不抛异常**。

上一次亮度值只保存在进程内存（V2 §2.3 决定不新增 config 顶层键），
进程重启回 ``1.0``。所有 Win32 / COM / WMI 调用都包 ``try/except``，
缺依赖、非 Windows、非桌面会话一律降级。本模块的 ``set()`` / ``get()``
是同步函数，dispatcher 放进 ``loop.run_in_executor``，不阻塞 aiohttp 事件循环。
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Any

_IS_WINDOWS = hasattr(ctypes, "windll")

# 进程内亮度状态：默认全亮，重启即重置（不落盘，保持 config 顶层 6 键不变）
_lock = threading.Lock()
_current_value: float = 1.0
_last_method: str = ""
_applied: bool = False
_original_ramp: tuple[int, ...] | None = None


# ---------------------------------------------------------------------------
# DDC/CI（外接显示器背光，VCP 0x10）——YG 实测「亮度不行」的补齐通道
# ---------------------------------------------------------------------------
class _PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [
        ("hPhysicalMonitor", wintypes.HANDLE),
        ("szPhysicalMonitorDescription", wintypes.WCHAR * 128),
    ]


def _ddc_monitors() -> list[Any]:
    """枚举物理显示器句柄；非 Windows / 缺 dxva2 返回空列表。"""
    if not _IS_WINDOWS:
        return []
    try:
        dxva2 = ctypes.windll.dxva2
        user32 = ctypes.windll.user32
    except Exception:
        return []
    handles: list[Any] = []
    try:
        user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
        user32.MonitorFromWindow.restype = wintypes.HMONITOR
        user32.GetDesktopWindow.restype = wintypes.HWND

        dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = (
            wintypes.HMONITOR,
            ctypes.POINTER(wintypes.DWORD),
        )
        dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
        dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = (
            wintypes.HMONITOR,
            wintypes.DWORD,
            ctypes.POINTER(_PHYSICAL_MONITOR),
        )
        dxva2.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL

        # 主显示器优先，先把最常见的一块接上；全部枚举以便多屏统一调
        desktop = user32.GetDesktopWindow()
        hmon = user32.MonitorFromWindow(desktop, 1)  # MONITOR_DEFAULTTOPRIMARY
        if not hmon:
            return []
        count = wintypes.DWORD(0)
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)):
            return []
        if count.value <= 0:
            return []
        array_type = _PHYSICAL_MONITOR * count.value
        array = array_type()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, array):
            return []
        handles.extend(array[index].hPhysicalMonitor for index in range(count.value))
    except Exception:
        return []
    return handles


def _ddc_set_percent(percent: int) -> bool:
    """用 DDC/CI 写背光亮度（VCP 0x10）；成功返回 True。"""
    handles = _ddc_monitors()
    if not handles:
        return False
    value = max(0, min(100, int(percent)))
    hit = 0
    try:
        dxva2 = ctypes.windll.dxva2
        dxva2.SetMonitorBrightness.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        dxva2.SetMonitorBrightness.restype = wintypes.BOOL
        for handle in handles:
            try:
                if dxva2.SetMonitorBrightness(handle, wintypes.DWORD(value)):
                    hit += 1
            except Exception:
                continue
    except Exception:
        return False
    finally:
        try:
            dxva2 = ctypes.windll.dxva2
            dxva2.DestroyPhysicalMonitor.argtypes = (wintypes.HANDLE,)
            dxva2.DestroyPhysicalMonitor.restype = wintypes.BOOL
            for handle in handles:
                try:
                    dxva2.DestroyPhysicalMonitor(handle)
                except Exception:
                    pass
        except Exception:
            pass
    return hit > 0


def _ddc_current_percent() -> int | None:
    """用 DDC/CI 读背光亮度（VCP 0x10）；不可用返回 ``None``。"""
    handles = _ddc_monitors()
    if not handles:
        return None
    try:
        dxva2 = ctypes.windll.dxva2
        dxva2.GetMonitorBrightness.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        )
        dxva2.GetMonitorBrightness.restype = wintypes.BOOL
        for handle in handles:
            try:
                minimum = wintypes.DWORD(0)
                current = wintypes.DWORD(0)
                maximum = wintypes.DWORD(0)
                if dxva2.GetMonitorBrightness(
                    handle,
                    ctypes.byref(minimum),
                    ctypes.byref(current),
                    ctypes.byref(maximum),
                ):
                    if maximum.value > 0:
                        span = maximum.value - minimum.value
                        if span > 0:
                            pct = int(
                                round(
                                    (current.value - minimum.value) * 100.0 / span
                                )
                            )
                            if 0 <= pct <= 100:
                                return pct
            except Exception:
                continue
        return None
    except Exception:
        return None
    finally:
        try:
            dxva2 = ctypes.windll.dxva2
            dxva2.DestroyPhysicalMonitor.argtypes = (wintypes.HANDLE,)
            dxva2.DestroyPhysicalMonitor.restype = wintypes.BOOL
            for handle in handles:
                try:
                    dxva2.DestroyPhysicalMonitor(handle)
                except Exception:
                    pass
        except Exception:
            pass


def _wmi_namespace() -> Any:
    """尝试连 ``root\\wmi``；返回 COM 命名空间对象或 ``None``。"""
    try:
        import pythoncom  # type: ignore[import-not-found]
        import win32com.client  # type: ignore[import-not-found]

        pythoncom.CoInitialize()
        try:
            return win32com.client.GetObject("winmgmts:\\\\.\\root\\wmi"), pythoncom
        except Exception:
            pythoncom.CoUninitialize()
            return None
    except Exception:
        return None


def _wmi_close(bundle: Any) -> None:
    """释放 ``_wmi_namespace`` 里初始化的 COM。"""
    try:
        _namespace, pythoncom = bundle
        del _namespace
        pythoncom.CoUninitialize()
    except Exception:
        pass


def _wmi_set_percent(percent: int) -> bool:
    """调用 ``WmiSetBrightness(Timeout=1, Brightness=percent)``；成功返回 True。

    ★必须走 ``ExecMethod_`` + ``InParameters`` 显式装箱，不能写
    ``monitor.WmiSetBrightness(1, value)``：

    WMI 的 ``WmiSetBrightness`` 形参声明是 ``uint8 Timeout`` / ``uint32
    Brightness``，而 pywin32 用位置参数调用时会把两个 Python int 都按
    ``VT_I4`` 装箱，WMI 类型不匹配直接抛
    ``com_error (-2147352567, '发生意外。', (0, 'SWbemObjectEx',
    '无效的参数', None, 0, -2147217400), None)``。

    这个异常被原来的 ``except Exception: continue`` 吞掉，于是
    ``_wmi_set_percent`` 静默返回 False、``set()`` 落到 ``applied: false``，
    表现就是 YG 报的「亮度调节不了」——而 ``_wmi_current_percent()``
    读的是 ``WmiMonitorBrightness.CurrentBrightness``，读操作没问题，
    所以 ``brightness.get()`` 一直正常返回真实值，极具误导性。

    显式装箱后实测 ``CurrentBrightness`` 1 → 45 立即生效。
    """
    value = max(0, min(100, int(percent)))
    bundle = _wmi_namespace()
    if bundle is None:
        return False
    try:
        namespace, _pythoncom = bundle
        hit = 0
        for monitor in namespace.InstancesOf("WmiMonitorBrightnessMethods"):
            try:
                signature = monitor.Methods_("WmiSetBrightness")
                params = signature.InParameters.SpawnInstance_()
                params.Properties_.Item("Timeout").Value = 1
                params.Properties_.Item("Brightness").Value = value
                monitor.ExecMethod_("WmiSetBrightness", params)
                hit += 1
            except Exception:
                continue
        return hit > 0
    except Exception:
        return False
    finally:
        _wmi_close(bundle)


def _wmi_current_percent() -> int | None:
    """读取 ``WmiMonitorBrightness.CurrentBrightness``；不可用返回 ``None``。"""
    bundle = _wmi_namespace()
    if bundle is None:
        return None
    try:
        namespace, _pythoncom = bundle
        for monitor in namespace.InstancesOf("WmiMonitorBrightness"):
            try:
                current = int(monitor.CurrentBrightness)
                if 0 <= current <= 100:
                    return current
            except Exception:
                continue
        return None
    except Exception:
        return None
    finally:
        _wmi_close(bundle)


def _gamma_apply(value: float) -> bool:
    """用 ``SetDeviceGammaRamp`` 做软件 gamma 近似；返回是否真的写入。"""
    global _original_ramp
    if not _IS_WINDOWS:
        return False
    hdc = None
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.argtypes = (ctypes.c_void_p,)
        user32.GetDC.restype = ctypes.c_void_p
        user32.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        gdi32.GetDeviceGammaRamp.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        gdi32.GetDeviceGammaRamp.restype = ctypes.c_int
        gdi32.SetDeviceGammaRamp.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        gdi32.SetDeviceGammaRamp.restype = ctypes.c_int

        hdc = user32.GetDC(None)
        if not hdc:
            return False
        ramp_type = ctypes.c_ushort * (256 * 3)
        original = ramp_type()
        if not gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(original)):
            return False
        if _original_ramp is None:
            _original_ramp = tuple(int(word) for word in original)

        # value=1 → 恢复出厂 ramp；否则按 gamma 曲线压暗三个通道
        ramp = ramp_type()
        if value >= 0.999999 and _original_ramp is not None:
            for index, word in enumerate(_original_ramp):
                ramp[index] = word
        else:
            gamma = 1.0 + (1.0 - value) * 2.5
            for index in range(256):
                word = int(round(((index / 255.0) ** gamma) * 65535.0))
                word = max(0, min(65535, word))
                ramp[index] = word
                ramp[256 + index] = word
                ramp[512 + index] = word
        return bool(gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)))
    except Exception:
        return False
    finally:
        try:
            if hdc is not None and _IS_WINDOWS:
                ctypes.windll.user32.ReleaseDC(None, hdc)
        except Exception:
            pass


def set(value: float) -> dict[str, Any]:
    """``brightness.set``：返回 ``ack.payload.extra``，任何失败都不抛异常。

    只走**真正改背光**的两条通道：DDC/CI（外接显示器）→ WMI（笔记本内置屏）。

    刻意不再回退到 ``SetDeviceGammaRamp``：那是改色阶（R/G/B 三个通道的
    查找表），屏幕背光根本没变，观感上是「颜色发灰/整体压暗」而不是变暗，
    用户明确要求「只调节显示屏亮度」。gamma 兜底会让「亮度没变但颜色变了」，
    所以这里宁可如实返回 ``applied: false`` 也不动色阶。
    """
    global _current_value, _last_method, _applied
    target = float(value)
    with _lock:
        percent = int(round(target * 100))
        # 1) DDC/CI：外接显示器唯一真正改背光的通道，放第一位
        try:
            if _ddc_set_percent(percent):
                _current_value = target
                _last_method = "ddc"
                _applied = True
                return {
                    "applied": True,
                    "method": "ddc",
                    "value": target,
                    "best_effort": False,
                }
        except Exception:
            pass
        # 2) WMI：笔记本内置屏
        try:
            if _wmi_set_percent(percent):
                _current_value = target
                _last_method = "wmi"
                _applied = True
                return {
                    "applied": True,
                    "method": "wmi",
                    "value": target,
                    "best_effort": False,
                }
        except Exception:
            pass
        # 3) 到这一步说明这块屏幕两条背光通道都不给用。
        #    不写 gamma —— 改色阶不等于改亮度，会把颜色弄坏。
        _applied = False
        return {
            "applied": False,
            "method": "",
            "value": target,
            "best_effort": True,
            "message": "这台显示器不支持调节亮度：DDC/CI 无响应且 WMI 背光不可用"
                       "（常见于外接显示器未启用 DDC/CI、或走 HDMI/转接器）。"
                       "已跳过色阶近似，不会改变画面颜色。",
        }


def get() -> dict[str, Any]:
    """``brightness.get``：返回下发消息 ``brightness`` 的 payload。"""
    global _current_value, _last_method
    with _lock:
        # 1) DDC/CI 读外接屏
        try:
            ddc_percent = _ddc_current_percent()
        except Exception:
            ddc_percent = None
        if ddc_percent is not None:
            _current_value = ddc_percent / 100.0
            _last_method = "ddc"
            return {"value": _current_value, "method": "ddc", "best_effort": False}
        # 2) WMI 读内置屏
        try:
            percent = _wmi_current_percent()
        except Exception:
            percent = None
        if percent is not None:
            _current_value = percent / 100.0
            _last_method = "wmi"
            return {"value": _current_value, "method": "wmi", "best_effort": False}
        return {
            "value": _current_value,
            "method": _last_method or "gamma",
            "best_effort": True,
        }


def state() -> dict[str, Any]:
    """供离线自检 / 调试读取内部状态（不参与协议）。"""
    with _lock:
        return {
            "value": _current_value,
            "method": _last_method,
            "applied": _applied,
            "windows": _IS_WINDOWS,
        }
