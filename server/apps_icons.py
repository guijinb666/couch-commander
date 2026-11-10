"""应用图标提取与列表元数据派生（V2 追加层 §4）。

本模块只做两件事，全部对可选依赖做 ``try/except`` 降级：

1. ``icon_png(path)``：从本地 ``.exe`` / ``.lnk`` / ``.ico`` 提取 32×32 RGBA
   PNG 字节。优先用 Windows 原生 ``ExtractIconExW`` + ``GetIconInfo`` +
   ``GetDIBits``（纯 ``ctypes``，不强依赖 pywin32），再用 ``pywin32`` 的
   ``win32gui.ExtractIconEx`` 分支，最后用 ``Pillow`` 直接打开图片文件兜底。
   任一环节失败、路径不存在、无图标、非 Windows 一律返回 ``None``，由
   ``http_api`` 转成 ``404 {"ok":false,"code":"NO_ICON"}``，**绝不 500**。
2. ``decorate_app(app, installed)`` / ``decorate_apps(apps, installed)``：
   给 ``app.list`` / ``GET /api/apps`` 的每个条目派生 ``has_local``、
   ``route``、可选 ``icon_url``。``icon_url`` 形如
   ``/api/apps/icon?path=<urlencoded>``，仅在本地命中且路径像可提图标的文件时
   下发；``icon_url`` 是运行期派生字段，不写回 ``config.json``。

V1 的 ``{id,name,path,icon,color}`` 与 ``web`` / ``local`` 全部原样保留，
``decorate_app`` 只追加字段，不删不改义。
"""
from __future__ import annotations

import ctypes
import os
import urllib.parse
from ctypes import wintypes
from pathlib import Path
from typing import Any

import apps as apps_module

# 输出图标固定 32×32（契约 §4.1）
ICON_SIZE = 32
# 视作「可提图标」的文件后缀；.lnk 交由 ExtractIconEx 直接处理
_ICON_SUFFIXES: frozenset[str] = frozenset(
    {".exe", ".lnk", ".ico", ".dll", ".png", ".jpg", ".jpeg", ".bmp"}
)

_IS_WINDOWS = hasattr(ctypes, "windll")


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


def _clean_path(raw: Any) -> str:
    """去掉引号与 ``DisplayIcon`` 常见的 ``,N`` 索引，返回可探测的路径文本。"""
    text = str(raw or "").strip().strip('"')
    if not text:
        return ""
    if "," in text and text.rsplit(",", 1)[-1].strip().isdigit():
        text = text.rsplit(",", 1)[0].strip().strip('"')
    return text


def _lnk_target(path: str) -> str:
    """可选：用 win32com 解析 ``.lnk`` 目标；失败时返回空串由调用方兜底。"""
    if not path.casefold().endswith(".lnk"):
        return ""
    pythoncom = None
    initialized = False
    try:
        import pythoncom  # type: ignore[import-not-found]

        pythoncom.CoInitialize()
        initialized = True
    except Exception:
        pythoncom = None
    try:
        import win32com.client  # type: ignore[import-not-found]

        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(path)
        target = _clean_path(shortcut.Targetpath)
        return target if target and os.path.exists(target) else ""
    except Exception:
        return ""
    finally:
        if initialized and pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _resolve_path(raw: Any) -> str:
    """把请求里的 path 归一成本地可读取的文件路径；不存在返回空串。"""
    try:
        path = _clean_path(raw)
        if not path or path.casefold().startswith(("http://", "https://")):
            return ""
        if path.casefold().endswith(".lnk"):
            target = _lnk_target(path)
            if target and os.path.isfile(target):
                path = target
        candidate = Path(path)
        if not candidate.is_file():
            return ""
        return str(candidate)
    except Exception:
        return ""


def _encode_png_bgra(pixels: bytes, width: int, height: int) -> bytes | None:
    """把 top-down BGRA 字节流转成 32×32 RGBA PNG；Pillow 缺失返回 None。"""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        import io

        image = Image.frombuffer(
            "RGBA", (width, height), pixels, "raw", "BGRA", 0, 1
        ).copy()  # copy 脱离原 buffer，后续 resize / 保存才安全
        # 部分 32bpp BI_RGB 图标 alpha 字节全 0（透明靠 AND mask 表达），
        # 这里兜底成不透明，避免整张图全透明看不见图标。
        alpha = image.getchannel("A")
        if alpha.getextrema() == (0, 0):
            image.putalpha(255)
        if image.size != (ICON_SIZE, ICON_SIZE):
            image = image.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        return data if data.startswith(b"\x89PNG\r\n\x1a\n") else None
    except Exception:
        return None


def _extract_via_ctypes(path: str) -> bytes | None:
    """Windows 原生 HICON → 32bpp DIB → PNG（不依赖 pywin32）。"""
    if not _IS_WINDOWS:
        return None
    shell32 = None
    user32 = None
    gdi32 = None
    hdc = None
    hicon_large = wintypes.HICON()
    hicon_small = wintypes.HICON()
    icon_info = _ICONINFO()
    try:
        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        shell32.ExtractIconExW.argtypes = (
            wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HICON),
            ctypes.POINTER(wintypes.HICON),
            wintypes.UINT,
        )
        shell32.ExtractIconExW.restype = wintypes.UINT
        user32.GetIconInfo.argtypes = (wintypes.HICON, ctypes.POINTER(_ICONINFO))
        user32.GetIconInfo.restype = wintypes.BOOL
        user32.GetDC.argtypes = (wintypes.HWND,)
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
        user32.DestroyIcon.argtypes = (wintypes.HICON,)
        gdi32.GetDIBits.argtypes = (
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.POINTER(_BITMAPINFO),
            wintypes.UINT,
        )
        gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)

        extracted = shell32.ExtractIconExW(
            path, 0, ctypes.byref(hicon_large), ctypes.byref(hicon_small), 1
        )
        if not extracted:
            return None
        hicon = hicon_large or hicon_small
        if not hicon:
            return None
        if not user32.GetIconInfo(hicon, ctypes.byref(icon_info)):
            return None
        bitmap = icon_info.hbmColor or icon_info.hbmMask
        if not bitmap:
            return None

        hdc = user32.GetDC(None)
        if not hdc:
            return None
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = ICON_SIZE
        info.bmiHeader.biHeight = -ICON_SIZE  # top-down，避免上下翻转
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        pixels = (ctypes.c_ubyte * (ICON_SIZE * ICON_SIZE * 4))()
        rows = gdi32.GetDIBits(
            hdc,
            bitmap,
            0,
            ICON_SIZE,
            ctypes.cast(pixels, ctypes.c_void_p),
            ctypes.byref(info),
            0,
        )
        if not rows:
            return None
        return _encode_png_bgra(bytes(pixels), ICON_SIZE, ICON_SIZE)
    except Exception:
        return None
    finally:
        # 所有 Win32 句柄逐个释放，任何一步失败都不影响其余释放
        try:
            if hdc is not None and user32 is not None:
                user32.ReleaseDC(None, hdc)
        except Exception:
            pass
        for handle in (icon_info.hbmColor, icon_info.hbmMask):
            try:
                if handle and gdi32 is not None:
                    gdi32.DeleteObject(handle)
            except Exception:
                pass
        for handle in (hicon_large, hicon_small):
            try:
                if handle and user32 is not None:
                    user32.DestroyIcon(handle)
            except Exception:
                pass


def _extract_via_pywin32(path: str) -> bytes | None:
    """可选 pywin32 分支：``win32gui.ExtractIconEx`` + ``win32ui`` 位图读取。"""
    try:
        import win32gui  # type: ignore[import-not-found]
        import win32ui  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        large, small = win32gui.ExtractIconEx(path, 0)
        icons = list(large or []) + list(small or [])
        if not icons:
            return None
        hicon = icons[0]
        screen_dc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(screen_dc, ICON_SIZE, ICON_SIZE)
        mem_dc = screen_dc.CreateCompatibleDC()
        mem_dc.SelectObject(bitmap)
        try:
            win32gui.DrawIconEx(
                mem_dc.GetSafeHdc(), 0, 0, hicon,
                ICON_SIZE, ICON_SIZE, 0, None, 0x0003,  # DI_IMAGE | DI_MASK
            )
        except Exception:
            mem_dc.DrawIcon((0, 0), hicon)
        raw = bitmap.GetBitmapBits(True)
        result = _encode_png_bgra(raw, ICON_SIZE, ICON_SIZE)
        try:
            win32gui.DestroyIcon(hicon)
        except Exception:
            pass
        return result
    except Exception:
        return None


def _extract_via_pillow(path: str) -> bytes | None:
    """图片文件（``.ico`` / ``.png`` / ``.bmp`` 等）直接读成 32×32 PNG。"""
    if Path(path).suffix.casefold() not in _ICON_SUFFIXES:
        return None
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        import io

        with Image.open(path) as source:
            image = source.convert("RGBA")
        if image.size != (ICON_SIZE, ICON_SIZE):
            image = image.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        return None


def icon_png(raw_path: Any) -> bytes | None:
    """提取 ``raw_path`` 的 32×32 PNG 图标；任何失败返回 ``None``（调用方回 404）。

    该函数是同步 Win32 IO，``http_api`` 会放进 ``loop.run_in_executor``，
    不会阻塞 aiohttp 事件循环。
    """
    path = _resolve_path(raw_path)
    if not path:
        return None
    for extractor in (_extract_via_ctypes, _extract_via_pywin32, _extract_via_pillow):
        try:
            png = extractor(path)
        except Exception:
            png = None
        if isinstance(png, bytes) and png.startswith(b"\x89PNG\r\n\x1a\n"):
            return png
    return None


def build_icon_url(path: str) -> str:
    """构造契约 §4.2 的 ``icon_url``，路径整体 urlencode（含盘符冒号与反斜杠）。"""
    return "/api/apps/icon?path=" + urllib.parse.quote(_clean_path(path), safe="")


def _is_local_path(raw: Any) -> bool:
    """路径是否指向本机存在的文件/目录（排除 URL 与注册表键名占位）。"""
    try:
        path = _clean_path(raw)
        if not path or path.casefold().startswith(("http://", "https://")):
            return False
        return os.path.isabs(path) and os.path.exists(path)
    except Exception:
        return False


def _icon_candidate(raw: Any) -> str:
    """本地路径里「看起来能提图标」的文件；目录 / 占位 / URL 返回空串。"""
    try:
        path = _clean_path(raw)
        if not _is_local_path(path):
            return ""
        if os.path.isdir(path):
            return ""
        suffix = Path(path).suffix.casefold()
        if suffix and suffix not in _ICON_SUFFIXES:
            return ""
        return path
    except Exception:
        return ""


def decorate_app(app: dict[str, Any], installed: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """给单个 app 追加 ``has_local`` / ``route`` / 可选 ``icon_url``。

    * 有 ``local`` 描述的 config 卡片：调 ``apps.locate_local`` 做「已安装列表
      名称匹配 + exe/paths 常见位置探测」，命中即 ``route="local"``。
    * 扫描到的应用（无 ``local``）：``path`` 本身就是本地路径，
      ``has_local`` 直接由路径存在性判定。
    * ``icon_url`` 只在 ``has_local=true`` 且路径是「可提图标的文件」时下发；
      前端 ``<img>`` 加载失败会自行回退名称首字。
    """
    item = dict(app)
    local = app.get("local")
    if isinstance(local, dict):
        try:
            # 列表装饰属于高频只读：只做 installed 名称匹配 + paths/PATH 轻量判定，
            # 不走 Program Files / AppData 的 os.walk（完整探测留给 launch_app）。
            matched = apps_module.locate_local(app, installed or [], deep_probe=False)
        except Exception:
            matched = ""
        has_local = bool(matched)
        icon_source = matched if has_local else ""
    else:
        icon_source = str(app.get("path") or "")
        has_local = _is_local_path(icon_source)
    item["has_local"] = has_local
    item["route"] = "local" if has_local else "web"
    if has_local and _icon_candidate(icon_source):
        item["icon_url"] = build_icon_url(icon_source)
    return item


def decorate_apps(
    apps: list[dict[str, Any]], installed: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """批量派生；逐项 ``try/except``，单项失败也原样保留 V1 字段不崩列表。"""
    result: list[dict[str, Any]] = []
    for app in apps:
        if not isinstance(app, dict):
            result.append(app)
            continue
        try:
            result.append(decorate_app(app, installed))
        except Exception:
            fallback = dict(app)
            fallback.setdefault("has_local", False)
            fallback.setdefault("route", "web")
            result.append(fallback)
    return result
