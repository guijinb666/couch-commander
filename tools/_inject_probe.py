"""方向键 / A-B-C 注入实证探针（本轮 m01602「右键无效」专项取证）。

做法：注册一个 WH_KEYBOARD_LL 低级键盘钩子，然后把焦点放到本进程自己创建的
临时窗口上（避免把键打进用户正在用的编辑器），再调用 input_win.key_press
逐个注入 up / down / left / right / b / j / h / space，记录钩子实际收到的
(vk, 扩展标志, down/up)。

结论用途：把「右键没反应」限定在「后端是否真的发出了 VK_RIGHT」这一层。
钩子收到 = 注入正确，问题在目标应用或前端未发送；钩子收不到 = 注入层有缺陷。

副作用：只向自己的临时窗口注入，不改系统设置、不动用户文件。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import threading
import time
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

sys.path.insert(0, str(_PROJECT_ROOT / "server"))
import input_win  # noqa: E402

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_EXTENDED = 0x01

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

captured: list[tuple[int, bool, bool]] = []   # (vk, extended, is_down)
_lock = threading.Lock()
_hook_handle = None
_hook_proc = None

# 关键：ctypes 默认把返回值当 int（32 位），64 位 HANDLE 会被截断，
# 导致 SetWindowsHookExW 看起来"失败 err=0"。必须显式声明签名。
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _hook_callback(code, wparam, lparam):
    if code >= 0:
        try:
            data = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            is_down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)
            with _lock:
                captured.append((int(data.vkCode), bool(data.flags & LLKHF_EXTENDED), is_down))
        except Exception:
            pass
    return user32.CallNextHookEx(_hook_handle, code, wparam, lparam)


def start_hook():
    global _hook_handle, _hook_proc
    _hook_proc = HOOKPROC(_hook_callback)
    _hook_handle = user32.SetWindowsHookExW(
        WH_KEYBOARD_LL, _hook_proc, kernel32.GetModuleHandleW(None), 0
    )
    if not _hook_handle:
        raise OSError(f"SetWindowsHookExW 失败, err={ctypes.get_last_error()}")
    thread = threading.Thread(target=_pump, name="hook-pump", daemon=True)
    thread.start()


def _pump():
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def make_focus_window():
    """创建一个临时置顶窗口并抢焦点，让注入的键落在这里而不是用户的编辑器里。"""
    import tkinter as tk

    root = tk.Tk()
    root.title("inject-probe")
    root.geometry("260x120+40+40")
    label = tk.Label(root, text="注入探针运行中，请勿操作", font=("Segoe UI", 10))
    label.pack(expand=True)
    root.update()
    root.focus_force()
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    root.update()
    time.sleep(0.35)
    return root


def main() -> int:
    start_hook()
    root = make_focus_window()

    cases = [
        ("up", 0x26),
        ("down", 0x28),
        ("left", 0x25),
        ("right", 0x27),
        ("b", 0x42),
        ("j", 0x4A),
        ("h", 0x48),
        ("space", 0x20),
        ("f11", 0x7A),
    ]

    results = []
    for name, expect_vk in cases:
        with _lock:
            captured.clear()
        try:
            input_win.key_press([name])
            error = ""
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.18)
        with _lock:
            got = list(captured)
        downs = [c for c in got if c[2]]
        ups = [c for c in got if not c[2]]
        vk_ok = any(c[0] == expect_vk for c in got)
        pair_ok = bool(downs) and bool(ups)
        ext = downs[0][1] if downs else None
        results.append(
            {
                "key": name,
                "expect_vk": f"0x{expect_vk:02X}",
                "got": [f"0x{c[0]:02X}{'(ext)' if c[1] else ''}{'↓' if c[2] else '↑'}" for c in got],
                "vk_ok": vk_ok,
                "pair_ok": pair_ok,
                "extended": ext,
                "error": error,
            }
        )

    try:
        root.destroy()
    except Exception:
        pass

    lines = []
    passed = 0
    for r in results:
        ok = r["vk_ok"] and r["pair_ok"] and not r["error"]
        passed += 1 if ok else 0
        lines.append(
            f"[{'PASS' if ok else 'FAIL'}] {r['key']:<6} expect={r['expect_vk']} "
            f"ext={r['extended']} got={r['got'] or '[]'} {r['error']}"
        )

    header = f"INJECT_PROBE PASS={passed} FAIL={len(results) - passed} TOTAL={len(results)}"
    out = "\n".join([header] + lines)
    print(out)
    with open(_PROJECT_ROOT / "_inject_result.txt", "w", encoding="utf-8") as fh:
        fh.write(out + "\n")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
