"""已安装应用扫描、缓存与启动。

对应 PROTOCOL 第 3.5 / 7 节：

* 扫描 ``HKLM`` / ``HKCU`` 的 ``...\\Uninstall`` 注册表 + 开始菜单 ``.lnk``
  （``win32com`` 解析目标，缺失时降级不崩溃）。
* 产出统一结构 ``{id, name, path, icon, color}``，缓存到 ``server/apps.json``。
* ``/api/apps`` 返回「已安装应用 + ``config.apps`` 合并去重」，
  ``POST /api/apps/scan`` 强制重新扫描。
* ``app.activate`` 尽力启动/置前应用；启动方式按 URL / .lnk / 绝对路径 /
  裸命令四类分别处理，任何可选依赖缺失都降级为 best-effort。

扫描是同步的 IO 密集操作，调用方（dispatcher）应放进
``loop.run_in_executor``，避免阻塞 aiohttp 事件循环。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
_SERVER_DIR = Path(__file__).resolve().parent
_DEFAULT_CACHE_PATH = _SERVER_DIR / "apps.json"

# 扫描到的应用使用独立 id 命名空间，避免与 config.apps 的 app-<n> 冲突
SCAN_ID_PREFIX = "scan-"
# 开始菜单 .lnk 最多遍历数量，防止异常目录拖慢扫描
MAX_LINKS = 2000
# 应用名最大长度，避免注册表里超长字符串污染前端
MAX_NAME_LENGTH = 80
# 颜色板：按名称哈希稳定分配，保证每次扫描同一应用颜色一致
_COLOR_PALETTE: tuple[str, ...] = (
    "#1E5EFF", "#FF0033", "#111118", "#1DB954", "#1B2838",
    "#7C3AED", "#F59E0B", "#0EA5E9", "#EF4444", "#10B981",
    "#EC4899", "#6366F1", "#14B8A6", "#F97316", "#8B5CF6",
)
_FALLBACK_EMOJIS: tuple[str, ...] = (
    "🗔", "📦", "🎬", "🎵", "🎮", "🧩", "🛠", "📺", "🌐", "💿",
)


def _color_for(name: str) -> str:
    digest = hashlib.md5(name.encode("utf-8", errors="replace")).digest()
    return _COLOR_PALETTE[digest[0] % len(_COLOR_PALETTE)]


def _emoji_for(name: str) -> str:
    digest = hashlib.md5(name.encode("utf-8", errors="replace")).digest()
    return _FALLBACK_EMOJIS[digest[1] % len(_FALLBACK_EMOJIS)]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value


def _clean_name(name: Any) -> str:
    text = str(name or "").strip()
    text = " ".join(text.split())
    if len(text) > MAX_NAME_LENGTH:
        text = text[:MAX_NAME_LENGTH]
    return text


def _clean_icon_path(raw: Any) -> str:
    """注册表 ``DisplayIcon`` 常见 ``"C:\\a\\b.exe,0"``，去掉索引与引号。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = text.strip('"')
    if "," in text and text.rsplit(",", 1)[-1].strip().isdigit():
        text = text.rsplit(",", 1)[0].strip().strip('"')
    return text


def _norm_name(name: str) -> str:
    return "".join(ch for ch in name.casefold() if not ch.isspace())


def _norm_path(path: str) -> str:
    text = str(path or "").strip().strip('"').casefold().replace("/", "\\")
    if text.endswith(",0"):
        text = text[:-2]
    return text


def _is_update_entry(name: str) -> bool:
    """过滤明显是系统更新 / 补丁 / 运行库子包的条目。"""
    low = name.casefold()
    compact = low.replace(" ", "")
    if compact.startswith("kb") and compact[2:].isdigit():
        return True
    markers = (
        "security update for microsoft",
        "update for microsoft windows",
        "hotfix for microsoft",
        "microsoft visual c++",
        "microsoft .net",
        "windows sdk",
        "redistributable",
    )
    return any(marker in low for marker in markers)


def _make_entry(index: int, name: str, path: str) -> dict[str, Any]:
    clean = _clean_name(name)
    return {
        "id": f"{SCAN_ID_PREFIX}{index}",
        "name": clean,
        "path": path,
        "icon": _emoji_for(clean),
        "color": _color_for(clean),
    }


# ---------------------------------------------------------------------------
# 注册表扫描
# ---------------------------------------------------------------------------
def _registry_entries() -> list[tuple[str, str]]:
    """返回 ``(name, path)`` 列表；winreg 缺失时返回空列表。"""
    try:
        import winreg
    except Exception:
        return []

    hives: list[tuple[int, str]] = [
        (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
        (winreg.HKEY_CURRENT_USER, "HKCU"),
    ]
    sub_paths = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    access_flags = [0]
    wow64_64 = getattr(winreg, "KEY_WOW64_64KEY", 0)
    wow64_32 = getattr(winreg, "KEY_WOW64_32KEY", 0)
    if wow64_64:
        access_flags.append(wow64_64)
    if wow64_32:
        access_flags.append(wow64_32)

    found: list[tuple[str, str]] = []
    for hive, _hive_name in hives:
        for sub_path in sub_paths:
            for extra_flag in access_flags:
                try:
                    root = winreg.OpenKey(
                        hive, sub_path, 0, winreg.KEY_READ | extra_flag
                    )
                except OSError:
                    continue
                try:
                    count = winreg.QueryInfoKey(root)[0]
                except OSError:
                    winreg.CloseKey(root)
                    continue
                for i in range(count):
                    try:
                        sub_name = winreg.EnumKey(root, i)
                        sub = winreg.OpenKey(root, sub_name, 0, winreg.KEY_READ)
                    except OSError:
                        continue
                    try:
                        display = _read_value(sub, "DisplayName")
                        if not display:
                            continue
                        if _read_value(sub, "SystemComponent") not in (None, 0, "0"):
                            continue
                        if _read_value(sub, "ParentKeyName"):
                            continue
                        release = _read_value(sub, "ReleaseType")
                        if release and str(release).casefold() in ("update", "security update", "hotfix"):
                            continue
                        if _is_update_entry(display):
                            continue
                        icon_path = _clean_icon_path(_read_value(sub, "DisplayIcon"))
                        install_dir = str(_read_value(sub, "InstallLocation") or "").strip().strip('"')
                        path = ""
                        if icon_path and icon_path.casefold().endswith((".exe", ".lnk")):
                            path = icon_path
                        elif install_dir:
                            path = install_dir
                        elif icon_path:
                            path = icon_path
                        if not path:
                            # 没有可启动路径时仍保留卡片，path 用注册表键名做占位展示
                            path = str(sub_name)
                        found.append((str(display), path))
                    finally:
                        try:
                            winreg.CloseKey(sub)
                        except Exception:
                            pass
                try:
                    winreg.CloseKey(root)
                except Exception:
                    pass
    return found


def _read_value(key: Any, value_name: str) -> Any:
    """读取一个注册表值，缺失或类型异常返回 ``None``。"""
    import winreg

    try:
        value, _kind = winreg.QueryValueEx(key, value_name)
        return value
    except OSError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 开始菜单 .lnk 扫描
# ---------------------------------------------------------------------------
def _start_menu_dirs() -> list[Path]:
    dirs: list[Path] = []
    candidates: list[str] = []
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("ProgramData", "")
    if appdata:
        candidates.append(
            os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs")
        )
    if programdata:
        candidates.append(
            os.path.join(programdata, "Microsoft", "Windows", "Start Menu", "Programs")
        )
    # 兜底：某些服务会话里环境变量不完整
    drive = os.environ.get("SystemDrive", "C:")
    candidates.append(
        drive + os.sep + os.path.join("ProgramData", "Microsoft", "Windows", "Start Menu", "Programs")
    )
    for raw in candidates:
        try:
            path = Path(raw)
            if path.is_dir() and path not in dirs:
                dirs.append(path)
        except Exception:
            continue
    return dirs


def _shortcut_targets() -> list[tuple[str, str]]:
    """解析开始菜单快捷方式，返回 ``(name, target_path)``。

    ``win32com`` 不可用时降级为「用快捷方式文件名当应用名、``.lnk`` 自身当
    path」——``os.startfile`` 仍能启动它，服务不会崩溃。
    """
    results: list[tuple[str, str]] = []
    shell = None
    com_initialized = False
    try:
        import pythoncom  # type: ignore[import-not-found]
        pythoncom.CoInitialize()
        com_initialized = True
    except Exception:
        pythoncom = None  # type: ignore[assignment]
    try:
        import win32com.client  # type: ignore[import-not-found]
        shell = win32com.client.Dispatch("WScript.Shell")
    except Exception:
        shell = None

    count = 0
    try:
        for base in _start_menu_dirs():
            try:
                links = list(base.rglob("*.lnk"))
            except OSError:
                continue
            for link in links:
                if count >= MAX_LINKS:
                    break
                count += 1
                name = link.stem.strip()
                if not name or _is_update_entry(name):
                    continue
                target = ""
                if shell is not None:
                    try:
                        shortcut = shell.CreateShortcut(str(link))
                        target = str(shortcut.Targetpath or "").strip().strip('"')
                    except Exception:
                        target = ""
                if not target:
                    # 降级：.lnk 本身可被 os.startfile 直接启动
                    target = str(link)
                results.append((name, target))
            if count >= MAX_LINKS:
                break
    finally:
        if com_initialized:
            try:
                pythoncom.CoUninitialize()  # type: ignore[possibly-undefined]
            except Exception:
                pass
    return results


# ---------------------------------------------------------------------------
# 扫描与缓存
# ---------------------------------------------------------------------------
class AppStore:
    """已安装应用扫描器 + ``apps.json`` 缓存，线程安全。"""

    def __init__(self, cache_path: str | os.PathLike[str] | None = None) -> None:
        self.cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH
        self._lock = threading.RLock()
        self._apps: list[dict[str, Any]] = []
        self.last_scan_error: str | None = None
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            if not self.cache_path.is_file():
                return
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            apps = raw.get("apps") if isinstance(raw, dict) else None
            if isinstance(apps, list):
                self._apps = [a for a in apps if self._valid_entry(a)]
        except Exception as exc:
            self.last_scan_error = f"读取 apps.json 缓存失败: {exc}"
            self._apps = []

    @staticmethod
    def _valid_entry(item: Any) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("path"), str)
        )

    def _write_cache(self, apps: list[dict[str, Any]]) -> None:
        payload = {
            "updated_at": int(time.time() * 1000),
            "count": len(apps),
            "apps": apps,
        }
        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.cache_path)

    def scan(self) -> list[dict[str, Any]]:
        """同步执行一次完整扫描；返回扫描结果并原子写缓存。"""
        raw: list[tuple[str, str]] = []
        self.last_scan_error = None
        try:
            raw.extend(_registry_entries())
        except Exception as exc:
            self.last_scan_error = f"注册表扫描失败: {exc}"
        try:
            raw.extend(_shortcut_targets())
        except Exception as exc:
            extra = f"开始菜单扫描失败: {exc}"
            self.last_scan_error = (
                f"{self.last_scan_error}; {extra}" if self.last_scan_error else extra
            )

        # 去重：同名或同路径只保留第一条（注册表优先于快捷方式）
        apps: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        seen_paths: set[str] = set()
        for name, path in raw:
            clean = _clean_name(name)
            if not clean:
                continue
            name_key = _norm_name(clean)
            path_key = _norm_path(path)
            if name_key in seen_names:
                continue
            if path_key and path_key in seen_paths:
                continue
            seen_names.add(name_key)
            if path_key:
                seen_paths.add(path_key)
            apps.append(_make_entry(len(apps) + 1, clean, path))

        apps.sort(key=lambda item: item["name"].casefold())
        # 排序后重排 id，保证同一批结果 id 稳定且连续
        for index, item in enumerate(apps, start=1):
            item["id"] = f"{SCAN_ID_PREFIX}{index}"

        with self._lock:
            self._apps = apps
            try:
                self._write_cache(apps)
            except Exception as exc:
                extra = f"写入 apps.json 缓存失败: {exc}"
                self.last_scan_error = (
                    f"{self.last_scan_error}; {extra}"
                    if self.last_scan_error
                    else extra
                )
            return list(self._apps)

    def cached(self) -> list[dict[str, Any]]:
        """返回缓存的已安装应用（缓存为空时触发一次扫描）。"""
        with self._lock:
            if self._apps:
                return [dict(item) for item in self._apps]
        return self.scan()

    @staticmethod
    def merged(
        installed: list[dict[str, Any]], config_apps: list[Any]
    ) -> list[dict[str, Any]]:
        """``config.apps`` 置前（用户固定卡片），再补扫描到的应用，去重。

        去重维度：规范化应用名 + 规范化 path；config.apps 的字段原样保留，
        只补缺的 ``icon`` / ``color``。
        """
        result: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        seen_paths: set[str] = set()

        for raw in config_apps:
            if not isinstance(raw, dict):
                continue
            name = _clean_name(raw.get("name"))
            app_id = str(raw.get("id") or "")
            if not name or not app_id:
                continue
            path = str(raw.get("path") or "")
            item = {
                "id": app_id,
                "name": name,
                "path": path,
                "icon": str(raw.get("icon") or _emoji_for(name)),
                "color": str(raw.get("color") or _color_for(name)),
            }
            # 保留前端可能追加的其它字段，不删不改义
            for key, value in raw.items():
                if key not in item:
                    item[key] = value
            result.append(item)
            seen_names.add(_norm_name(name))
            path_key = _norm_path(path)
            if path_key:
                seen_paths.add(path_key)

        for raw in installed:
            if not isinstance(raw, dict):
                continue
            name = _clean_name(raw.get("name"))
            if not name:
                continue
            path = str(raw.get("path") or "")
            name_key = _norm_name(name)
            path_key = _norm_path(path)
            if name_key in seen_names:
                continue
            if path_key and path_key in seen_paths:
                continue
            app_id = str(raw.get("id") or f"{SCAN_ID_PREFIX}{len(result) + 1}")
            if any(existing["id"] == app_id for existing in result):
                app_id = f"{SCAN_ID_PREFIX}{len(result) + 1}"
            item = {
                "id": app_id,
                "name": name,
                "path": path,
                "icon": str(raw.get("icon") or _emoji_for(name)),
                "color": str(raw.get("color") or _color_for(name)),
            }
            result.append(item)
            seen_names.add(name_key)
            if path_key:
                seen_paths.add(path_key)
        return result

    @staticmethod
    def find(apps: list[dict[str, Any]], app_id: str) -> dict[str, Any] | None:
        for item in apps:
            if item.get("id") == app_id:
                return item
        return None


# ---------------------------------------------------------------------------
# 启动 / 置前
# ---------------------------------------------------------------------------
def _foreground_window() -> int:
    """当前前台窗口句柄；取不到返回 0。"""
    try:
        import win32gui  # type: ignore[import-not-found]

        return int(win32gui.GetForegroundWindow() or 0)
    except Exception:
        return 0


def force_foreground(hwnd: int) -> bool:
    """把 ``hwnd`` 强行切到前台。

    ``SetForegroundWindow`` 直接调用在 Windows 前台锁定（Foreground Lock）下会被
    静默拒绝——函数返回成功但窗口并没有真的到前台，于是后续 ``SendInput`` 打出来
    的按键落进上一个窗口，表现为「快捷键点了没反应」。

    这里用两个标准绕法叠加：
    1. ``AttachThreadInput`` 把本线程挂到当前前台线程的输入队列上，再
       ``BringWindowToTop`` / ``SetForegroundWindow`` / ``SetActiveWindow``；
    2. 只有上一步**没成功**时，才补一次 Alt 键的升降（``keybd_event``）——模拟
       用户输入，把「最近一次输入」的所有权抢到本进程，前台锁随即放开，然后再试一遍。

    为什么 Alt 要放第二步：Alt 是真实的按键，会打进目标窗口。放在第一步时，目标
    窗口会先收到一个多余的 ``Alt``（浏览器里表现为 ``keydown Alt``，客户端可能弹出
    菜单栏），而实际上多数情况下 ``BringWindowToTop`` 已经够了。E2E 取证就是在
    这里抓到多余的 Alt 事件。Alt 的抬起放在 ``finally`` 里，任何异常都不会把它卡住。

    最后用 ``GetForegroundWindow() == hwnd`` 复核真实结果，而不是相信
    ``SetForegroundWindow`` 的返回值。
    """
    if not hwnd:
        return False
    try:
        import win32api  # type: ignore[import-not-found]
        import win32con  # type: ignore[import-not-found]
        import win32gui  # type: ignore[import-not-found]
        import win32process  # type: ignore[import-not-found]
    except Exception:
        return False

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass

    def _already_foreground() -> bool:
        try:
            return int(win32gui.GetForegroundWindow() or 0) == int(hwnd)
        except Exception:
            return False

    def _bring() -> None:
        """挂前台线程 + 三重置前；不涉及任何模拟按键。"""
        attached: list[int] = []
        try:
            foreground = int(win32gui.GetForegroundWindow() or 0)
            if foreground and foreground != hwnd:
                fg_thread = int(win32process.GetWindowThreadProcessId(foreground)[0])
                my_thread = int(win32api.GetCurrentThreadId())
                if fg_thread and fg_thread != my_thread:
                    if win32process.AttachThreadInput(my_thread, fg_thread, True):
                        attached.append(fg_thread)
            for step in (
                lambda: win32gui.BringWindowToTop(hwnd),
                lambda: win32gui.SetForegroundWindow(hwnd),
                lambda: win32gui.SetActiveWindow(hwnd),
                lambda: win32gui.SetFocus(hwnd),
            ):
                try:
                    step()
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            for thread_id in attached:
                try:
                    win32process.AttachThreadInput(
                        int(win32api.GetCurrentThreadId()), thread_id, False
                    )
                except Exception:
                    pass

    _bring()
    if _already_foreground():
        return True

    # 前台锁还在：现在才用 Alt 抢「最近输入」所有权，然后重试一次。
    alt_down = False
    try:
        try:
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            alt_down = True
        except Exception:
            pass
        _bring()
    finally:
        if alt_down:
            try:
                win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            except Exception:
                pass

    return _already_foreground()


def _try_focus(name: str) -> bool:
    """按窗口标题模糊匹配并强行置前；找不到/失败返回 ``False``。

    命中后逐个候选尝试，直到 ``GetForegroundWindow`` 真的等于该窗口（见
    :func:`force_foreground`）。这样「锁定应用 → 发按键」才能保证按键落到
    目标窗口而不是上一个前台窗口。
    """
    try:
        import win32gui  # type: ignore[import-not-found]
    except Exception:
        return False
    needle = (name or "").casefold()
    if not needle:
        return False
    hit: list[int] = []

    def _callback(hwnd: int, _unused: Any) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if not win32gui.GetWindowText(hwnd):
                return
            if needle in win32gui.GetWindowText(hwnd).casefold():
                hit.append(hwnd)
        except Exception:
            return

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        return False
    if not hit:
        return False

    current = _foreground_window()
    if current and current in hit:
        return True
    for hwnd in hit:
        if force_foreground(hwnd):
            return True
    return False


def _try_focus_any(keywords: list[str]) -> bool:
    """按多个关键词依次尝试置前（进程名、窗口标题片段等）。"""
    for keyword in keywords:
        if not keyword:
            continue
        if _try_focus(keyword):
            return True
    return False


def _usable_local_path(raw: Any) -> bool:
    """是否指向本机存在的绝对路径（文件或目录），排除 URL / 注册表键名占位。"""
    try:
        path = _clean_icon_path(raw)
        if not path or path.casefold().startswith(("http://", "https://")):
            return False
        return os.path.isabs(path) and os.path.exists(path)
    except Exception:
        return False


def _names_match(alias: str, target: str) -> bool:
    """规范化模糊匹配：去空格 / 大小写，双向包含（覆盖「抖音」↔「抖音电脑版」）。"""
    a = _norm_name(alias)
    t = _norm_name(target)
    if not a or not t:
        return False
    return a == t or a in t or t in a


# 常见安装根目录环境变量（Program Files / AppData 等）
_COMMON_ROOT_ENV_KEYS: tuple[str, ...] = (
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "LOCALAPPDATA",
    "APPDATA",
)
# 浅层目录探测的上界，避免 app.activate / app.list 因全盘遍历变慢
_PROBE_MAX_DEPTH = 2
_PROBE_MAX_DIRS = 400


def _common_install_roots() -> list[str]:
    roots: list[str] = []
    for key in _COMMON_ROOT_ENV_KEYS:
        value = os.environ.get(key, "")
        if value and os.path.isdir(value) and value not in roots:
            roots.append(value)
    return roots


def _bounded_find_exe(root: str, exe_lower: str) -> str:
    """在 ``root`` 下有限深度 / 有限目录数内找同名 exe；命中即返回。"""
    visited = 0
    for dirpath, dirnames, filenames in os.walk(root):
        visited += 1
        try:
            depth = os.path.relpath(dirpath, root).count(os.sep)
        except Exception:
            depth = 0
        if depth >= _PROBE_MAX_DEPTH:
            dirnames[:] = []
        for filename in filenames:
            if filename.casefold() == exe_lower:
                return os.path.join(dirpath, filename)
        if visited >= _PROBE_MAX_DIRS:
            break
    return ""


def _probe_install(exes: list[str], extra_paths: list[str], deep: bool = True) -> str:
    """按 ``local.exe`` / ``local.paths`` 探测常见安装位置，返回首个命中路径。

    顺序：显式 paths → PATH（``shutil.which``）→ Program Files / AppData 浅层。
    ``local.names`` 对应的「开始菜单 .lnk 目标 / 注册表」已由 ``AppStore.cached()``
    扫描进 installed 列表，在 ``locate_local`` 的 a 步骤里覆盖，这里不重复全盘扫。
    ``deep=False`` 时跳过 Program Files / AppData 的 ``os.walk``（供 ``app.list``
    列表装饰这类高频只读场景用，只保留 paths + PATH 的轻量判定）。
    """
    for raw in extra_paths:
        path = _clean_icon_path(raw)
        if path and os.path.exists(path):
            return path
    for exe in exes:
        try:
            found = shutil.which(exe)
        except Exception:
            found = None
        if found and os.path.isfile(found):
            return found
    if deep:
        for root in _common_install_roots():
            for exe in exes:
                try:
                    found = _bounded_find_exe(root, exe.casefold())
                except Exception:
                    found = ""
                if found:
                    return found
    return ""


# 模块级共享 AppStore：launch_app(app) 单参数调用（离线自检 / 外部调用）时
# 复用同一份缓存，避免每次重新扫描；真正的请求路径由 dispatcher 传入 installed。
_shared_store: "AppStore | None" = None
_shared_store_lock = threading.Lock()


def _cached_installed() -> list[dict[str, Any]]:
    global _shared_store
    with _shared_store_lock:
        if _shared_store is None:
            _shared_store = AppStore()
        return _shared_store.cached()


def locate_local(
    app: dict[str, Any],
    installed: list[dict[str, Any]] | None = None,
    deep_probe: bool = True,
) -> str:
    """V2 §3 ``locate_local``：本地优先判定，返回命中的本地路径或空串。

    a. 在 ``AppStore.cached()``（或调用方传入的 installed）已安装列表里按
       ``local.names`` 规范化模糊匹配应用名，或按 ``local.exe`` 匹配 path 的
       basename → 命中取该项 path。
    b. 未命中则按 ``local.exe`` / ``local.paths`` 探测常见安装位置。

    ``deep_probe=False`` 只做 installed 名称匹配 + paths/PATH 轻量探测，
    供 ``apps_icons.decorate_app`` 派生 ``has_local`` 时避开 ``os.walk``；
    ``launch_app`` 保持 ``True``，按契约做完整常见安装位置探测。
    任何异常都降级返回空串，绝不抛给调用方。
    """
    try:
        local = app.get("local")
        if not isinstance(local, dict):
            return ""
        names = [str(x) for x in (local.get("names") or []) if x]
        exes = [str(x) for x in (local.get("exe") or []) if x]
        extra_paths = [str(x) for x in (local.get("paths") or []) if x]
        if installed is None:
            installed = _cached_installed()
        for item in installed or []:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("name") or "")
            item_path = _clean_icon_path(item.get("path"))
            base = os.path.basename(item_path).casefold()
            name_hit = any(_names_match(alias, item_name) for alias in names)
            exe_hit = any(base == exe.casefold() for exe in exes)
            if not exe_hit:
                exe_hit = any(
                    len(exe) > 8 and exe.casefold() in base for exe in exes
                )
            if (name_hit or exe_hit) and _usable_local_path(item_path):
                return item_path
        return _probe_install(exes, extra_paths, deep=deep_probe)
    except Exception:
        return ""


def _start_path(raw_path: str, result: dict[str, Any]) -> dict[str, Any]:
    """V1 四类启动分支（URL / .lnk / 绝对路径 / 裸命令），原语义不改。

    本地命中与「无 local 的 V1 条目」都复用这里，保证 V1 行为零改义。
    """
    lower = raw_path.casefold()
    if lower.startswith("http://") or lower.startswith("https://"):
        import webbrowser

        result["launched"] = bool(webbrowser.open(raw_path))
        result["method"] = "webbrowser.open"
    elif lower.endswith(".lnk") and os.path.isfile(raw_path):
        os.startfile(raw_path)  # type: ignore[attr-defined]
        result["launched"] = True
        result["method"] = "os.startfile(lnk)"
    elif os.path.isabs(raw_path) and os.path.exists(raw_path):
        if os.path.isdir(raw_path):
            os.startfile(raw_path)  # type: ignore[attr-defined]
            result["launched"] = True
            result["method"] = "os.startfile(dir)"
        else:
            subprocess.Popen(
                [raw_path],
                cwd=os.path.dirname(raw_path) or None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            result["launched"] = True
            result["method"] = "subprocess.Popen"
    elif os.path.isfile(raw_path):
        subprocess.Popen(
            [raw_path],
            cwd=os.path.dirname(raw_path) or None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result["launched"] = True
        result["method"] = "subprocess.Popen"
    else:
        # 裸命令交给 shell 解析 PATH（explorer.exe / spotify.exe / steam.exe）
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            raw_path,
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
        result["launched"] = True
        result["method"] = "shell"
    return result


def launch_app(
    app: dict[str, Any], installed: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """尽力启动并置前应用，返回 best-effort 结果供 ``ack.payload.extra``。

    V2 §3「本地优先、官网兜底」：有 ``app.local`` 时先 ``locate_local``；
    命中走本地（复用 V1 四类分支，``route="local"``），未命中
    ``webbrowser.open(app.web or app.path)``（``route="web"``）。无 ``local``
    的 V1 条目保持四类分支原语义。V1 字段 ``id/name/launched/focused/
    method/best_effort`` 全部保留，追加 ``route/matched_path/web_fallback``。
    ``installed`` 由 dispatcher 传入 ``AppStore.cached()``，省略时内部取共享缓存。
    """
    name = str(app.get("name") or "")
    raw_path = str(app.get("path") or "").strip()
    result: dict[str, Any] = {
        "id": app.get("id"),
        "name": name,
        "launched": False,
        "focused": False,
        "method": "",
        "best_effort": True,
        "route": "web",
        "matched_path": "",
        "web_fallback": True,
    }
    local = app.get("local")
    matched = locate_local(app, installed) if isinstance(local, dict) else ""

    try:
        if matched:
            result["route"] = "local"
            result["matched_path"] = matched
            result["web_fallback"] = False
            _start_path(matched, result)
        elif isinstance(local, dict):
            # 有 local 描述但未命中 → 官网兜底
            target = str(app.get("web") or raw_path or "").strip()
            if not target:
                result["message"] = "web/path 为空，跳过启动"
            else:
                import webbrowser

                result["launched"] = bool(webbrowser.open(target))
                result["method"] = "webbrowser.open"
            result["route"] = "web"
            result["web_fallback"] = True
        elif raw_path:
            # 无 local：保留 V1 四类分支语义，route 由路径是否本地决定
            _start_path(raw_path, result)
            result["route"] = "local" if _usable_local_path(raw_path) else "web"
            result["web_fallback"] = result["route"] == "web"
        else:
            result["message"] = "path 为空，跳过启动"
    except Exception as exc:
        result["message"] = f"启动失败: {exc}"
        result["launched"] = False

    result["focused"] = _try_focus(name)
    return result
