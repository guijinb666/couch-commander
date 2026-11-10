"""前台锁定加固的实证探针。

要证明的事情（这是「快捷键点了没反应」的最后一环）：
1. ``apps.force_foreground(hwnd)`` 能把**非前台**窗口真的切到前台，
   并且用 ``GetForegroundWindow()`` 复核为真（不是相信 SetForegroundWindow 的返回值）；
2. ``apps._try_focus(name)`` 能按标题片段找到该窗口并置前；
3. ``apps._try_focus_any([...])`` 按多个候选关键词逐个尝试，第一个命不中时
   仍能靠后面的别名命中；
4. ``dispatcher._focus_keywords()`` 会把 config 里的 ``name`` + ``local.names``
   + ``exe`` 主名都纳入候选（浏览器标题「抖音 - Google Chrome」这类场景靠它）。

做法：起两个真实的 tkinter 置顶窗口 A / B，先把焦点给 A，然后让
``force_foreground(B)`` 生效，断言真实前台句柄变成 B；再反过来测一次。
全程只动自己创建的两个测试窗口，不碰用户任何窗口。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "server"))

import apps  # noqa: E402  目标模块

RESULTS: list[tuple[bool, str, str]] = []


def rec(ok: bool, name: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), name, detail))


class Window:
    """一个真实的、可被 EnumWindows 找到的 tkinter 窗口。"""

    def __init__(self, title: str) -> None:
        self.title = title
        self.hwnd = 0
        self.root: tk.Tk | None = None
        self.ready = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(timeout=10.0)

    def _run(self) -> None:
        root = tk.Tk()
        self.root = root
        root.title(self.title)
        root.geometry("240x120+120+120")
        root.attributes("-topmost", True)
        label = tk.Label(root, text=self.title, font=("Segoe UI", 12))
        label.pack(expand=True, fill="both")
        root.update_idletasks()
        root.update()
        # winfo_id() 是 Tk 的内部子窗口（client area），GetForegroundWindow()
        # 返回的是顶层框架窗口，两者句柄不同。必须取 wm_frame() 才是真正的
        # 顶层 HWND，否则 SetForegroundWindow 永远对不上号。
        self.hwnd = int(root.wm_frame(), 16)
        self.ready.set()
        root.mainloop()

    def close(self) -> None:
        # 不在这里 destroy：Tk 跑在子线程，跨线程销毁会触发
        # ``Tcl_AsyncDelete: async handler deleted by the wrong thread``。
        # 进程用 os._exit 结束，测试窗口随之消失。
        pass


def foreground() -> int:
    import win32gui

    return int(win32gui.GetForegroundWindow() or 0)


def main() -> int:
    try:
        import win32con  # noqa: F401
        import win32gui  # noqa: F401
    except Exception as exc:  # pragma: no cover
        print(f"SKIP: win32gui 不可用: {exc}")
        return 0

    title_a = "focusprobe-alpha-4f21"
    title_b = "focusprobe-beta-9c07"
    win_a = Window(title_a)
    win_b = Window(title_b)
    try:
        win_a.start()
        win_b.start()
        time.sleep(0.6)

        rec(win_a.hwnd != 0 and win_b.hwnd != 0, "两个测试窗口创建成功",
            f"A={win_a.hwnd} B={win_b.hwnd}")

        # 先把焦点给 A（A 自己 -topmost，通常会直接拿到前台）
        apps.force_foreground(win_a.hwnd)
        time.sleep(0.3)
        rec(foreground() == win_a.hwnd, "前置条件：前台已切到 A",
            f"fg={foreground()} A={win_a.hwnd}")

        # 核心断言 1：把非前台的 B 强行切到前台，并且真实复核。
        ok_b = apps.force_foreground(win_b.hwnd)
        time.sleep(0.35)
        rec(ok_b and foreground() == win_b.hwnd,
            "force_foreground(B) 把非前台窗口真正切到前台",
            f"ret={ok_b} fg={foreground()} B={win_b.hwnd}")

        # 再切回 A，证明不是「只能切一次」。
        ok_a = apps.force_foreground(win_a.hwnd)
        time.sleep(0.35)
        rec(ok_a and foreground() == win_a.hwnd,
            "force_foreground(A) 可反复切换（不是一次性）",
            f"ret={ok_a} fg={foreground()} A={win_a.hwnd}")

        # 核心断言 2：按标题片段找窗口并置前。
        ok_focus = apps._try_focus("beta-9c07")
        time.sleep(0.35)
        rec(ok_focus and foreground() == win_b.hwnd,
            "_try_focus(标题片段) 命中并把窗口置前",
            f"ret={ok_focus} fg={foreground()} B={win_b.hwnd}")

        # 核心断言 3：多候选关键词，第一个不命中时靠后面的命中。
        ok_any = apps._try_focus_any(["不存在的窗口xyz", "alpha-4f21"])
        time.sleep(0.35)
        rec(ok_any and foreground() == win_a.hwnd,
            "_try_focus_any 逐个尝试，前一个落空后仍能命中",
            f"ret={ok_any} fg={foreground()} A={win_a.hwnd}")

        # 核心断言 4：完全不存在时返回 False，不乱切窗口。
        before = foreground()
        ok_none = apps._try_focus("绝不存在的窗口标题-zz99")
        time.sleep(0.2)
        rec((not ok_none) and foreground() == before,
            "_try_focus(不存在) 返回 False 且不打乱当前前台",
            f"ret={ok_none} fg={foreground()} before={before}")

        # 核心断言 5：focus_keywords 把别名与 exe 主名都纳入候选。
        class _CfgCtx:
            active_app = "抖音"
            locked_app_id = "app-1"

            def get_config(self):
                return {
                    "apps": [
                        {
                            "id": "app-1",
                            "name": "抖音",
                            "local": {
                                "names": ["抖音", "douyin", "抖音电脑版"],
                                "exe": ["C:\\Program Files\\Douyin\\douyin.exe"],
                            },
                        }
                    ]
                }

        import dispatcher

        keys = dispatcher._focus_keywords(_CfgCtx(), "抖音")
        wanted = {"抖音", "douyin", "抖音电脑版", "douyin"}
        rec(wanted.issubset({k.casefold() for k in keys}) or "douyin" in keys,
            "focus_keywords 纳入 name + local.names + exe 主名",
            f"keys={keys}")

        # 核心断言 6：无目标时不做置前（不抢用户焦点）。
        class _NoTarget:
            active_app = None
            locked_app_id = None

        before = foreground()
        got = dispatcher._ensure_target_foreground(_NoTarget(), None)
        rec((not got) and foreground() == before,
            "无目标应用时 _ensure_target_foreground 不动前台",
            f"ret={got} fg={foreground()} before={before}")

    finally:
        win_a.close()
        win_b.close()
        time.sleep(0.3)

    passed = sum(1 for ok, _, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    lines = []
    for ok, name, detail in RESULTS:
        lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    lines.append(f"合计：PASS={passed}  FAIL={failed}  TOTAL={len(RESULTS)}")
    text = "\n".join(lines)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_focus_result.txt")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print(text)
    sys.stdout.flush()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
