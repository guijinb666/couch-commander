"""Windows 后台服务入口。

职责（对应 PROTOCOL 第 1 / 7 / 9 节与总方案第 9 节）：

1. 解析 ``--port``（默认 ``8765``）与 ``--no-https``；
2. 探测局域网 IP（UDP 出口法 + ``gethostbyname_ex`` 兜底）；
3. 生成二维码：内容 ``https://<LAN_IP>:<PORT>/``（``--no-https`` 时为
   ``http://``），落盘 ``server/qr.png`` 并保留内存 PNG 供
   ``GET /api/qr.png`` 直接返回；
   之所以默认 HTTPS：手机浏览器只在安全上下文暴露 ``navigator.mediaDevices``，
   HTTP 下麦克风权限弹窗根本不会出现（详见 ``certs.py``）；
4. 拉起 tkinter 无边框窗口展示二维码与访问地址（无显示环境时打印路径、
   不崩溃）；
5. 单进程 aiohttp：``0.0.0.0:<port>`` 同时提供静态 ``web/``、REST ``/api/``、
   WS ``/ws``；
6. 打印访问地址；服务常驻后台（关闭二维码窗口不影响服务），仅 Ctrl+C /
   结束进程走优雅关闭（触发 ``on_cleanup`` 关连接、取消后台任务）。

前端 ``web/**`` 由另一路会话并行编写；``create_app`` 已处理
``web/index.html`` 尚不存在的情形，服务启动不受影响。
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
from typing import Any

from aiohttp import web

import certs
import logs
import qr
from apps import AppStore
from config_store import ConfigStore
from http_api import create_app
from ws_hub import AppContext, detect_lan_ip

DEFAULT_PORT = 8765
# 二维码窗口轮询服务停止事件的间隔（毫秒）
_WINDOW_POLL_MS = 200


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。``--port`` 覆盖默认 8765，``--no-window`` 便于自检。"""
    parser = argparse.ArgumentParser(
        prog="remote-control-server",
        description="Couch Commander · 沙发指挥官 —— Windows 后台服务",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="PORT",
        help=f"监听端口，默认 {DEFAULT_PORT}",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="不弹出二维码窗口（无显示环境 / 自动化自测时使用）",
    )
    parser.add_argument(
        "--no-https",
        action="store_true",
        help="回退纯 HTTP（不启用自签名 TLS；手机端将无法请求麦克风权限）",
    )
    args = parser.parse_args(argv)
    if not (1 <= args.port <= 65535):
        parser.error(f"--port 必须是 1..65535，收到 {args.port}")
    return args


def print_banner(
    lan_ip: str,
    port: int,
    url: str,
    qr_path: str,
    backend: str,
    *,
    https: bool = True,
    tls_note: str = "",
    log_path: str = "",
) -> None:
    """启动横幅：把访问地址、二维码路径、后端信息一次性打全。"""
    scheme = "https" if https else "http"
    ws_scheme = "wss" if https else "ws"
    line = "=" * 62
    print(line, flush=True)
    print("  Couch Commander · 沙发指挥官 —— 服务已启动", flush=True)
    print(line, flush=True)
    print(f"  手机访问（同 Wi-Fi）： {url}", flush=True)
    print(f"  本机访问：             {scheme}://127.0.0.1:{port}/", flush=True)
    print(f"  REST 状态：            {scheme}://127.0.0.1:{port}/api/status", flush=True)
    print(f"  WebSocket：            {ws_scheme}://{lan_ip}:{port}/ws", flush=True)
    print(f"  二维码图片：           {qr_path}", flush=True)
    print(f"  二维码后端：           {backend}", flush=True)
    print(f"  局域网 IP：            {lan_ip}", flush=True)
    if https:
        print("  HTTPS：                已启用自签名证书（手机首次访问需点「高级 → 继续前往」）", flush=True)
    else:
        print("  HTTPS：                未启用（手机端无法请求麦克风权限）", flush=True)
    if tls_note:
        print(f"  TLS 说明：             {tls_note}", flush=True)
    if log_path:
        print(f"  日志文件：             {log_path}", flush=True)
    print("  服务常驻后台运行，关闭二维码窗口 / 关闭启动器窗口都不影响服务", flush=True)
    print("  停止服务：双击「一键停止手机遥控.bat」，或运行 python service.py stop", flush=True)
    print(line, flush=True)


def _show_qr_window(
    qr_path: str,
    url: str,
    lan_ip: str,
    port: int,
    stop_event: threading.Event,
) -> None:
    """在独立守护线程里跑 tkinter 无边框窗口。

    tkinter 的所有调用都留在本线程内；主线程只通过 ``stop_event`` 通知退出，
    由窗口自己的 ``after`` 回调读取，避免跨线程操作 Tk 对象。
    无显示环境（``TclError``）、Pillow 缺失等可选问题一律降级：打印提示后
    直接返回，**绝不影响 aiohttp 服务**。
    """
    try:
        import tkinter as tk
    except Exception as exc:
        print(f"[窗口] tkinter 不可用，跳过二维码窗口：{exc}", flush=True)
        print(f"[窗口] 请直接打开图片：{qr_path}", flush=True)
        return

    try:
        window = tk.Tk()
    except Exception as exc:
        # 典型情形：Linux 无 $DISPLAY、Windows 无交互桌面会话
        print(f"[窗口] 无显示环境，跳过二维码窗口：{exc}", flush=True)
        print(f"[窗口] 二维码已保存：{qr_path}", flush=True)
        return

    try:
        window.overrideredirect(True)  # 无边框
        window.configure(bg="white")
        window.attributes("-topmost", True)

        photo = None
        try:
            # Tk 8.6+ 原生支持 PNG；加载失败时仍能显示文字
            photo = tk.PhotoImage(file=qr_path)
        except Exception as exc:
            print(f"[窗口] PNG 加载失败，仅显示文字：{exc}", flush=True)

        if photo is not None:
            image_label = tk.Label(window, image=photo, bg="white", bd=0)
            image_label.pack(padx=16, pady=(16, 8))
            # 保持引用，防止被 GC 后出现空白
            window._qr_photo = photo  # type: ignore[attr-defined]

        title = tk.Label(
            window,
            text="Couch Commander · 扫码遥控这台电脑",
            bg="white",
            fg="#111118",
            font=("Microsoft YaHei", 13, "bold"),
        )
        title.pack(pady=(4, 2))
        address = tk.Label(
            window,
            text=url,
            bg="white",
            fg="#1E5EFF",
            font=("Consolas", 11),
        )
        address.pack(pady=(0, 2))
        hint = tk.Label(
            window,
            text=f"同一 Wi-Fi · {lan_ip}:{port} · 点击窗口关闭",
            bg="white",
            fg="#666666",
            font=("Microsoft YaHei", 9),
        )
        hint.pack(pady=(0, 12))

        # 让窗口出现在屏幕中央
        window.update_idletasks()
        width = window.winfo_width() or 360
        height = window.winfo_height() or 420
        screen_w = window.winfo_screenwidth()
        screen_h = window.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        window.geometry(f"{width}x{height}+{x}+{y}")

        def _on_click(_event: Any) -> None:
            stop_event.set()

        window.bind("<Button-1>", _on_click)
        window.protocol("WM_DELETE_WINDOW", lambda: stop_event.set())

        def _poll() -> None:
            if stop_event.is_set():
                try:
                    window.destroy()
                except Exception:
                    pass
                return
            try:
                window.after(_WINDOW_POLL_MS, _poll)
            except Exception:
                pass

        window.after(_WINDOW_POLL_MS, _poll)
        window.mainloop()
    except Exception as exc:
        # 任何窗口期异常都只降级，不掀翻服务
        print(f"[窗口] 展示二维码时出错（服务继续运行）：{exc}", flush=True)
        print(f"[窗口] 二维码已保存：{qr_path}", flush=True)
        try:
            window.destroy()
        except Exception:
            pass
    finally:
        # 窗口被关闭即视作请求停止服务（Ctrl+C 之外的优雅退出路径）
        stop_event.set()


def start_qr_window(
    qr_path: str,
    url: str,
    lan_ip: str,
    port: int,
    stop_event: threading.Event,
    enabled: bool = True,
) -> threading.Thread | None:
    """启动二维码窗口守护线程；``enabled=False`` 时只打印路径。"""
    if not enabled:
        print(f"[窗口] --no-window 已指定，二维码仅落盘：{qr_path}", flush=True)
        return None
    thread = threading.Thread(
        target=_show_qr_window,
        args=(qr_path, url, lan_ip, port, stop_event),
        name="qr-window",
        daemon=True,
    )
    thread.start()
    return thread


def build_context(
    lan_ip: str,
    port: int,
    qr_png: bytes,
    url: str,
    *,
    https: bool = True,
) -> AppContext:
    """装配共享运行时上下文：配置存储、应用扫描缓存、设备名、二维码字节。"""
    try:
        device = socket.gethostname()
    except Exception:
        device = "Windows-PC"
    config_store = ConfigStore()
    app_store = AppStore()
    return AppContext(
        device=device,
        lan_ip=lan_ip,
        port=port,
        config_store=config_store,
        app_store=app_store,
        version="1.0",
        qr_png=qr_png,
        url=url,
        https=bool(https),
    )


def main(argv: list[str] | None = None) -> int:
    """程序主流程；返回进程退出码。"""
    args = parse_args(argv)
    port = args.port

    # 0) 落盘日志 + 全局异常钩子。
    #    先于一切可能失败的步骤：证书、二维码、tkinter、WinRT 任一崩掉，
    #    都必须留下 traceback —— 否则用户只会看到「后台连着连着就断了」，
    #    而日志里什么都没有（这正是上一轮无法定位断连的根因之一）。
    log_path = logs.setup()
    logs.install_excepthooks()
    logs.event("boot", pid=os.getpid(), port=port, argv=" ".join(sys.argv[1:]))

    # 1) 局域网 IP + 协议选择
    #    HTTPS 不是"更安全"的选择题，而是麦克风权限的硬前提：手机浏览器只在
    #    安全上下文暴露 navigator.mediaDevices，HTTP 下语音按钮永远拿不到权限。
    lan_ip = detect_lan_ip()
    use_https = not args.no_https
    ssl_context = None
    tls_note = ""
    if use_https:
        ok, why = certs.ensure_for_ip(lan_ip)
        tls_note = why
        if ok:
            try:
                ssl_context = certs.build_ssl_context(lan_ip)
            except certs.CertError as exc:
                use_https = False
                tls_note = f"{why}；但构造 SSLContext 失败：{exc}"
        else:
            use_https = False
            tls_note = why

    url = qr.make_url(lan_ip, port, https=use_https)
    qr_path = str(qr.DEFAULT_QR_PATH)

    # 2) 生成二维码（落盘 + 内存 PNG）；二维码失败也要继续起服务
    qr_png = b""
    try:
        qr_png = qr.write_qr(url)
    except Exception as exc:
        print(f"[qr] 生成二维码失败（服务继续，/api/qr.png 将按需重试）：{exc}", flush=True)

    # 3) 共享上下文 + aiohttp 应用
    ctx = build_context(lan_ip, port, qr_png, url, https=use_https)
    app = create_app(ctx)

    # 4) 打印访问地址
    print_banner(
        lan_ip,
        port,
        url,
        qr_path,
        qr.backend_name(),
        https=use_https,
        tls_note=tls_note,
        log_path=str(log_path),
    )

    # 5) 二维码窗口（守护线程）；窗口关闭 / 无显示环境都不影响服务
    stop_event = threading.Event()
    start_qr_window(qr_path, url, lan_ip, port, stop_event, enabled=not args.no_window)

    # 6) 阻塞运行 aiohttp。web.run_app 自带优雅关闭：
    #    Ctrl+C / SIGTERM → 触发 app.on_cleanup（WsHub 关连接、取消后台任务）。
    #    打印交给本文件，故传 print=None 关闭 aiohttp 自带横幅。
    try:
        web.run_app(
            app,
            host="0.0.0.0",
            port=port,
            print=None,
            handle_signals=True,
            ssl_context=ssl_context,
        )
    except KeyboardInterrupt:
        print("\n[服务] 收到 Ctrl+C，正在优雅关闭…", flush=True)
        logs.event("shutdown", reason="keyboard interrupt")
    except OSError as exc:
        print(f"[服务] 端口 {port} 监听失败：{exc}", flush=True)
        logs.error(f"端口 {port} 监听失败", detail=str(exc))
        stop_event.set()
        return 1
    except BaseException as exc:  # 崩溃也要留证据（守护进程会据此重启）
        logs.error("服务崩溃退出", type=type(exc).__name__, detail=str(exc))
        logs.exception("服务崩溃退出")
        raise
    finally:
        stop_event.set()
    print("[服务] 已停止", flush=True)
    logs.event("exit", code=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
