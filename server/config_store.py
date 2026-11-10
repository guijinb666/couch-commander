"""配置持久化模块。

负责 ``config.json`` 的读取、原子写入、默认值管理，以及快捷键、
多页面、应用页条目的增删查改。REST ``/api/config`` 与 WS 的
``config.get`` / ``config.put`` 最终都落到这里的同一份文件，
保证 PROTOCOL 第 6 节所说的「同源」。

原子写策略：先写 ``config.json.tmp``，flush + fsync 后用
``os.replace`` 覆盖目标文件，避免写一半时崩溃留下坏 JSON。
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path
from typing import Any


def default_config() -> dict[str, Any]:
    """返回 PROTOCOL 第 6 节的完整默认配置（每次返回全新副本）。"""
    return {
        "version": 1,
        "pages": [
            {"id": "pg-1", "name": "常用", "icon": "⭐"},
            {"id": "pg-2", "name": "办公", "icon": "💼"},
            {"id": "pg-3", "name": "娱乐", "icon": "🎮"},
        ],
        "active_page_id": "pg-1",
        # V2 §2.1：默认「无设置」，A/B/C 槽由前端显示字母本身。
        # V1 的 hotkeys 结构 {id,name,icon,keys,page_id,slot} 仍由设置页写入，未改义。
        "hotkeys": [],
        # V2 §2.2：5 个国内应用；保留 V1 的 id/name/path/icon/color，
        # 追加 web/local；path 恒等于 web，保证 V1 launch_app/merged 不改义。
        # 顶层键仍严格 6 个，icon_url 只在运行期派生，不落盘。
        "apps": [
            {
                "id": "app-1",
                "name": "抖音",
                "path": "https://www.douyin.com/",
                "web": "https://www.douyin.com/",
                "local": {"names": ["抖音", "douyin", "抖音电脑版"], "exe": ["douyin.exe", "Douyin.exe"], "paths": []},
                "icon": "🎵",
                "color": "#111118",
            },
            {
                "id": "app-2",
                "name": "bilibili",
                "path": "https://www.bilibili.com/",
                "web": "https://www.bilibili.com/",
                "local": {"names": ["哔哩哔哩", "bilibili", "bilibili pc"], "exe": ["bilibili.exe", "bili.exe"], "paths": []},
                "icon": "📺",
                "color": "#FB7299",
            },
            {
                "id": "app-3",
                "name": "QQ音乐",
                "path": "https://y.qq.com/",
                "web": "https://y.qq.com/",
                "local": {"names": ["QQ音乐", "QQMusic", "QQMusicPC"], "exe": ["QQMusic.exe"], "paths": []},
                "icon": "🎧",
                "color": "#31C27C",
            },
            {
                "id": "app-4",
                "name": "网易云音乐",
                "path": "https://music.163.com/",
                "web": "https://music.163.com/",
                "local": {"names": ["网易云音乐", "NeteaseCloudMusic", "cloudmusic"], "exe": ["cloudmusic.exe"], "paths": []},
                "icon": "🎶",
                "color": "#C20C0C",
            },
            {
                "id": "app-5",
                "name": "腾讯视频",
                "path": "https://v.qq.com/",
                "web": "https://v.qq.com/",
                "local": {"names": ["腾讯视频", "QQLive", "TencentVideo"], "exe": ["QQLive.exe", "TencentVideo.exe"], "paths": []},
                "icon": "▶",
                "color": "#FF5C00",
            },
        ],
        "touchpad": {
            "move_sensitivity": 1.6,
            "scroll_speed": 0.6,
            "double_click_ms": 300,
            "drag_hold_ms": 350,
            "lock_hold_ms": 2000,
        },
    }


class ConfigError(ValueError):
    """配置结构不合法时抛出，由 HTTP/WS 层转成错误响应。"""


_TOP_KEYS = ("version", "pages", "active_page_id", "hotkeys", "apps", "touchpad")
_APP_ID_RE = re.compile(r"^app-(\d+)$")


class ConfigStore:
    """线程安全的配置存储。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            path = Path(__file__).resolve().parent / "config.json"
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ensure_file()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _ensure_file(self) -> None:
        """首次运行时生成默认 config.json；文件已存在则原样保留。"""
        with self._lock:
            if not self.path.exists():
                self._atomic_write(default_config())

    def _atomic_write(self, config: dict[str, Any]) -> None:
        """写临时文件后原子替换，Windows 上 os.replace 可覆盖已存在文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(config, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def _normalize(self, raw: Any) -> dict[str, Any]:
        """轻度规范化：补齐缺失的顶层键与 touchpad 子键，保留调用方追加的字段。

        PROTOCOL 第 6 节规定 PUT /api/config 是「全量覆盖」，因此这里不删除
        任何调用方给出的字段；只在字段缺失或类型明显错误时补默认值，让前端
        旧版本配置也能被安全读写。
        """
        if not isinstance(raw, dict):
            raise ConfigError("config must be a JSON object")

        base = default_config()
        result: dict[str, Any] = {}
        for key in _TOP_KEYS:
            result[key] = copy.deepcopy(raw.get(key, base[key]))
        # 调用方追加的顶层字段一律保留（契约允许追加，不允许删改）
        for key, value in raw.items():
            if key not in result:
                result[key] = copy.deepcopy(value)

        # 顶层类型兜底
        if not isinstance(result.get("version"), (int, float)):
            result["version"] = 1
        for list_key in ("pages", "hotkeys", "apps"):
            if not isinstance(result.get(list_key), list):
                result[list_key] = copy.deepcopy(base[list_key])
        if not isinstance(result.get("active_page_id"), str):
            result["active_page_id"] = base["active_page_id"]

        # touchpad 子键补齐
        touchpad = copy.deepcopy(base["touchpad"])
        if isinstance(raw.get("touchpad"), dict):
            touchpad.update(raw["touchpad"])
        result["touchpad"] = touchpad

        # active_page_id 若指向不存在的页面，回落到第一页
        page_ids = {
            str(p.get("id"))
            for p in result["pages"]
            if isinstance(p, dict) and p.get("id") is not None
        }
        if page_ids and result["active_page_id"] not in page_ids:
            result["active_page_id"] = next(iter(page_ids))
        return result

    @staticmethod
    def _find(items: list[Any], item_id: str) -> tuple[int, dict[str, Any]] | tuple[int, None]:
        for index, item in enumerate(items):
            if isinstance(item, dict) and str(item.get("id")) == str(item_id):
                return index, item
        return -1, None

    # ------------------------------------------------------------------
    # 对外读写
    # ------------------------------------------------------------------
    def load(self) -> dict[str, Any]:
        """读取当前配置；文件损坏时回退默认值并重写，保证服务不中断。"""
        with self._lock:
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
                return self._normalize(raw)
            except (OSError, ValueError, TypeError) as exc:
                fallback = default_config()
                try:
                    self._atomic_write(fallback)
                except OSError:
                    pass
                # 记录一次降级原因，便于排查而不抛异常给调用方
                self.last_load_error = f"{type(exc).__name__}: {exc}"
                return fallback

    def save(self, config: Any) -> dict[str, Any]:
        """全量覆盖保存配置，返回实际写入的规范化对象。"""
        normalized = self._normalize(config)
        with self._lock:
            self._atomic_write(normalized)
        return normalized

    def reset(self) -> dict[str, Any]:
        """恢复出厂默认配置。"""
        return self.save(default_config())

    def get_default(self) -> dict[str, Any]:
        """只读地取一份默认配置副本。"""
        return default_config()

    # ------------------------------------------------------------------
    # 快捷键增删查改
    # ------------------------------------------------------------------
    def list_hotkeys(self, page_id: str | None = None) -> list[dict[str, Any]]:
        """列出快捷键；传入 page_id 时只返回该页面的项。"""
        hotkeys = [h for h in self.load().get("hotkeys", []) if isinstance(h, dict)]
        if page_id is not None:
            hotkeys = [h for h in hotkeys if str(h.get("page_id")) == str(page_id)]
        return copy.deepcopy(hotkeys)

    def get_hotkey(self, hotkey_id: str) -> dict[str, Any] | None:
        """按 id 查单个快捷键，找不到返回 None。"""
        _, found = self._find(self.load().get("hotkeys", []), hotkey_id)
        return copy.deepcopy(found) if found is not None else None

    def add_hotkey(self, hotkey: dict[str, Any]) -> dict[str, Any]:
        """新增快捷键。缺少 id 时自动生成 ``hk-<n>``；返回新增项。"""
        if not isinstance(hotkey, dict):
            raise ConfigError("hotkey must be a JSON object")
        config = self.load()
        item = copy.deepcopy(hotkey)
        if not item.get("id"):
            used = {
                int(m.group(1))
                for h in config["hotkeys"]
                if isinstance(h, dict)
                for m in [re.match(r"^hk-(\d+)$", str(h.get("id", "")))]
                if m
            }
            item["id"] = f"hk-{(max(used) + 1) if used else 1}"
        index, existing = self._find(config["hotkeys"], str(item["id"]))
        if existing is not None:
            config["hotkeys"][index] = item
        else:
            config["hotkeys"].append(item)
        self.save(config)
        return copy.deepcopy(item)

    def update_hotkey(self, hotkey_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        """局部更新快捷键字段（id 不允许改）；找不到返回 None。"""
        if not isinstance(changes, dict):
            raise ConfigError("changes must be a JSON object")
        config = self.load()
        index, existing = self._find(config["hotkeys"], hotkey_id)
        if existing is None:
            return None
        merged = dict(existing)
        merged.update(changes)
        merged["id"] = existing["id"]
        config["hotkeys"][index] = merged
        self.save(config)
        return copy.deepcopy(merged)

    def remove_hotkey(self, hotkey_id: str) -> bool:
        """删除快捷键，返回是否真的删掉了。"""
        config = self.load()
        index, existing = self._find(config["hotkeys"], hotkey_id)
        if existing is None:
            return False
        config["hotkeys"].pop(index)
        self.save(config)
        return True

    # ------------------------------------------------------------------
    # 多页面管理
    # ------------------------------------------------------------------
    def list_pages(self) -> list[dict[str, Any]]:
        """列出全部快捷键页面。"""
        return copy.deepcopy(
            [p for p in self.load().get("pages", []) if isinstance(p, dict)]
        )

    def get_active_page_id(self) -> str:
        """取当前激活页 id。"""
        return str(self.load().get("active_page_id", "pg-1"))

    def set_active_page(self, page_id: str) -> bool:
        """切换激活页；页面不存在返回 False。"""
        config = self.load()
        _, found = self._find(config["pages"], page_id)
        if found is None:
            return False
        config["active_page_id"] = str(page_id)
        self.save(config)
        return True

    def add_page(self, page: dict[str, Any]) -> dict[str, Any]:
        """新增页面；缺少 id 时自动生成 ``pg-<n>``。"""
        if not isinstance(page, dict):
            raise ConfigError("page must be a JSON object")
        config = self.load()
        item = copy.deepcopy(page)
        if not item.get("id"):
            used = {
                int(m.group(1))
                for p in config["pages"]
                if isinstance(p, dict)
                for m in [re.match(r"^pg-(\d+)$", str(p.get("id", "")))]
                if m
            }
            item["id"] = f"pg-{(max(used) + 1) if used else 1}"
        index, existing = self._find(config["pages"], str(item["id"]))
        if existing is not None:
            config["pages"][index] = item
        else:
            config["pages"].append(item)
        self.save(config)
        return copy.deepcopy(item)

    def update_page(self, page_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        """局部更新页面字段（id 不允许改）；找不到返回 None。"""
        if not isinstance(changes, dict):
            raise ConfigError("changes must be a JSON object")
        config = self.load()
        index, existing = self._find(config["pages"], page_id)
        if existing is None:
            return None
        merged = dict(existing)
        merged.update(changes)
        merged["id"] = existing["id"]
        config["pages"][index] = merged
        self.save(config)
        return copy.deepcopy(merged)

    def remove_page(self, page_id: str) -> bool:
        """删除页面；同时把属于该页的快捷键一并清理，避免孤儿数据。"""
        config = self.load()
        index, existing = self._find(config["pages"], page_id)
        if existing is None:
            return False
        config["pages"].pop(index)
        config["hotkeys"] = [
            h for h in config["hotkeys"]
            if not (isinstance(h, dict) and str(h.get("page_id")) == str(page_id))
        ]
        if str(config.get("active_page_id")) == str(page_id):
            remaining = [p for p in config["pages"] if isinstance(p, dict)]
            config["active_page_id"] = str(remaining[0]["id"]) if remaining else "pg-1"
        self.save(config)
        return True

    # ------------------------------------------------------------------
    # 应用页增删
    # ------------------------------------------------------------------
    def list_apps(self) -> list[dict[str, Any]]:
        """列出配置里的应用卡片。"""
        return copy.deepcopy(
            [a for a in self.load().get("apps", []) if isinstance(a, dict)]
        )

    def get_app(self, app_id: str) -> dict[str, Any] | None:
        """按 id 查应用卡片。"""
        _, found = self._find(self.load().get("apps", []), app_id)
        return copy.deepcopy(found) if found is not None else None

    def next_app_id(self) -> str:
        """按 PROTOCOL 第 6 节规则生成下一个 ``app-<n>`` 递增 id。"""
        used = [
            int(m.group(1))
            for a in self.load().get("apps", [])
            if isinstance(a, dict)
            for m in [_APP_ID_RE.match(str(a.get("id", "")))]
            if m
        ]
        return f"app-{(max(used) + 1) if used else 1}"

    def add_app(self, app: dict[str, Any]) -> dict[str, Any]:
        """新增应用卡片；缺少 id 时用 next_app_id() 递增。"""
        if not isinstance(app, dict):
            raise ConfigError("app must be a JSON object")
        config = self.load()
        item = copy.deepcopy(app)
        if not item.get("id"):
            item["id"] = self.next_app_id()
        index, existing = self._find(config["apps"], str(item["id"]))
        if existing is not None:
            config["apps"][index] = item
        else:
            config["apps"].append(item)
        self.save(config)
        return copy.deepcopy(item)

    def update_app(self, app_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        """局部更新应用卡片字段（id 不允许改）；找不到返回 None。"""
        if not isinstance(changes, dict):
            raise ConfigError("changes must be a JSON object")
        config = self.load()
        index, existing = self._find(config["apps"], app_id)
        if existing is None:
            return None
        merged = dict(existing)
        merged.update(changes)
        merged["id"] = existing["id"]
        config["apps"][index] = merged
        self.save(config)
        return copy.deepcopy(merged)

    def remove_app(self, app_id: str) -> bool:
        """删除应用卡片，返回是否真的删掉了。"""
        config = self.load()
        index, existing = self._find(config["apps"], app_id)
        if existing is None:
            return False
        config["apps"].pop(index)
        self.save(config)
        return True
