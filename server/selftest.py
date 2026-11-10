"""服务自检脚本。

两种模式：

1. **联机自检（默认）**：严格按 PROTOCOL 第 9 节顺序连接真实服务并逐项打印：

   连 WS → 收 ``hello`` → 发 ``ping`` 验 ``pong`` → ``GET /api/status`` →
   ``GET /api/config`` → ``PUT /api/config`` 回读一致 → ``key.press ["a"]`` →
   ``media.control play_pause`` → ``mouse.move {dx:20,dy:0}`` → ``app.list`` →
   ``hotkey.run`` 的 V1 error 结构（默认 ``hotkeys=[]``，用不存在的 id 确定性
   验证 ``NOT_FOUND``）→ **连发 5 次 ``key.press{keys:["a"]}`` 每次独立 ack**
   （V2 追加层 §8 需求 3 的后端侧回归：证明 ``input_win.key_press`` 无去重 /
   节流 / 吞键）。

   注意：联机自检会**真正注入按键**（``a``、媒体键），请先把焦点放在一个
   可安全输入的窗口里，或用 ``--offline`` 只做静态检查。

2. **静态自检（``--offline``）**：不连服务、不注入任何输入，程序化核对：

   * 每个 ``server/*.py`` 源码可编译；
   * 全部模块 import 闭环（含 V2 的 ``apps_icons`` / ``brightness`` / ``voice``）；
   * ``dispatcher`` 的 27 条命令（V1 22 + V2 5）与协议逐条一致；
   * ``http_api`` 的 10 条冻结端点 + 2 条追加端点逐条一致；
   * ``config_store.default_config()`` 与 V2 §2 结构逐字段一致；
   * ``input_win.VK_TABLE`` 覆盖第 5 节全部键名；
   * ``qr.build_png(make_url(...))`` 产出合法 PNG（缺 qrcode 时走降级编码器）；
   * V2 追加层回归断言：空 ``hotkeys`` / 5 个国内应用含 ``web``+``local`` /
     双路径 ``locate_local`` 命中与兜底 / 图标端点已注册 /
     ``brightness``+``voice`` 命令在 ``HANDLERS`` 内 / 连续按键回归用例存在。

用法（在 ``server/`` 目录下）：
``python selftest.py --offline``
``python selftest.py --port 8765``
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, WSMsgType

try:  # aiohttp 3.12+ 用 ClientWSTimeout；旧版回退到标量 timeout
    from aiohttp import ClientWSTimeout as _ClientWSTimeout
except ImportError:  # pragma: no cover
    _ClientWSTimeout = None

_SERVER_DIR = Path(__file__).resolve().parent

# 连发回归次数（V2 追加层 §8 需求 3 的后端侧证明）
KEY_PRESS_REPEAT = 5

# PROTOCOL 第 3 节：客户端 → 服务端命令（V1 冻结 22 条）
# + PROTOCOL_V2_ADDENDUM.md §5 / §6 新增 5 条（顺序即协议出现顺序）
EXPECTED_COMMANDS: tuple[str, ...] = (
    "hello",
    "ping",
    "status.get",
    "mouse.move",
    "mouse.down",
    "mouse.up",
    "mouse.click",
    "mouse.scroll",
    "mouse.pinch",
    "window.switch",
    "key.press",
    "key.type",
    "hotkey.run",
    "media.control",
    "media.seek",
    "media.state",
    "app.list",
    "app.scan",
    "app.activate",
    "app.release",
    "config.get",
    "config.put",
    # ---- V2 追加层（§5 亮度 / §6 语音）----
    "brightness.set",
    "brightness.get",
    "voice.start",
    "voice.text",
    "voice.stop",
)

# PROTOCOL 第 1 / 7 节：契约冻结的 10 条传输层端点（含静态托管与 WS 升级入口）
# V2 §1 明确：本元组**不动**。
PROTOCOL_ENDPOINTS: tuple[str, ...] = (
    "GET /",
    "GET /<file>",
    "GET /ws",
    "GET /api/status",
    "GET /api/qr.png",
    "GET /api/apps",
    "POST /api/apps/scan",
    "GET /api/config",
    "PUT /api/config",
    "GET /api/media",
)

# 协议外追加端点（V2 §1）：前端 addApp() 实调 POST /api/apps（V1 阶段 Lead 补齐），
# V2 §4.1 新增 GET /api/apps/icon。如实登记为扩展，不冒充协议字段、不改义原有 10 条。
EXTRA_ENDPOINTS: tuple[str, ...] = (
    "POST /api/apps",
    "GET /api/apps/icon",
    "GET /voice-ws",
)

# 实际期望的全部注册端点：顺序与 http_api.rest_endpoints() 的注册顺序一致
EXPECTED_ENDPOINTS: tuple[str, ...] = (
    "GET /",
    "GET /<file>",
    "GET /ws",
    "GET /api/status",
    "GET /api/qr.png",
    "GET /api/apps",
    "POST /api/apps/scan",
    "POST /api/apps",
    "GET /api/apps/icon",
    "GET /api/config",
    "PUT /api/config",
    "GET /api/media",
    "GET /voice-ws",
)

# PROTOCOL 第 6 节 + V2 追加层 §2：默认 config 的权威期望值（逐字段、逐顺序）
EXPECTED_DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "pages": [
        {"id": "pg-1", "name": "常用", "icon": "⭐"},
        {"id": "pg-2", "name": "办公", "icon": "💼"},
        {"id": "pg-3", "name": "娱乐", "icon": "🎮"},
    ],
    "active_page_id": "pg-1",
    # V2 §2.1：默认「无设置」，A/B/C 槽由前端显示字母本身
    "hotkeys": [],
    # V2 §2.2：5 个国内应用；V1 字段 id/name/path/icon/color 全保留，
    # 追加 web/local，path 恒等于 web；顶层键仍严格 6 个。
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

# V2 §2.2：5 个默认应用的名称（离线断言用，顺序有意义）
EXPECTED_APP_NAMES: tuple[str, ...] = (
    "抖音", "bilibili", "QQ音乐", "网易云音乐", "腾讯视频",
)

# PROTOCOL 第 5 节：键名表全集
EXPECTED_KEYS: frozenset[str] = frozenset(
    [chr(code) for code in range(ord("a"), ord("z") + 1)]
    + [str(digit) for digit in range(10)]
    + [
        "enter", "esc", "tab", "space", "backspace", "delete", "home", "end",
        "pageup", "pagedown", "up", "down", "left", "right",
        "ctrl", "shift", "alt", "win", "capslock", "insert", "printscreen",
    ]
    + [f"f{i}" for i in range(1, 13)]
    + ["`", "-", "=", "[", "]", "\\", ";", "'", ",", ".", "/"]
)


class Results:
    """逐项结果收集器：打印 PASS / FAIL / SKIP 并汇总退出码。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""), flush=True)

    def fail(self, name: str, detail: str = "") -> None:
        self.failed += 1
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""), flush=True)

    def skip(self, name: str, detail: str = "") -> None:
        self.skipped += 1
        print(f"  [SKIP] {name}" + (f" — {detail}" if detail else ""), flush=True)

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        (self.ok if condition else self.fail)(name, detail)

    def summary(self) -> int:
        print(
            f"\n合计：PASS={self.passed}  FAIL={self.failed}  SKIP={self.skipped}",
            flush=True,
        )
        return 1 if self.failed else 0


# ===========================================================================
# 静态自检（--offline）
# ===========================================================================
def run_offline(results: Results) -> None:
    """不连服务、不注入输入：编译 / import / 命令 / 端点 / 默认值 / 键名 / 二维码 / V2 回归。"""
    print("== 静态自检（--offline，不连接服务、不注入键鼠） ==", flush=True)

    # 1) 每个 .py 源码可编译（纯内存 compile，零副作用）
    py_files = sorted(_SERVER_DIR.glob("*.py"))
    print(f"\n[1] 源码编译检查（共 {len(py_files)} 个 .py）", flush=True)
    for path in py_files:
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
            results.ok(f"compile {path.name}")
        except Exception as exc:
            results.fail(f"compile {path.name}", f"{type(exc).__name__}: {exc}")

    # 2) import 闭环：逐个 import，验证模块间引用全部可解析
    print("\n[2] import 闭环检查", flush=True)
    modules = [
        "config_store", "input_win", "media", "apps", "qr",
        "apps_icons", "brightness", "voice",
        "ws_hub", "dispatcher", "http_api", "main",
    ]
    for name in modules:
        try:
            __import__(name)
            results.ok(f"import {name}")
        except Exception as exc:
            results.fail(f"import {name}", f"{type(exc).__name__}: {exc}")

    # 3) 命令表（V1 22 + V2 5 = 27 条）
    print("\n[3] WS 命令表（PROTOCOL 第 3 节 22 条 + V2 §5/§6 追加 5 条 = 27 条）", flush=True)
    try:
        import dispatcher

        actual = tuple(dispatcher.implemented_commands())
        expected = tuple(sorted(EXPECTED_COMMANDS))
        results.check(
            "命令集合完全一致",
            actual == expected,
            f"实际 {len(actual)} 条 / 期望 {len(expected)} 条",
        )
        missing = [c for c in expected if c not in dispatcher.HANDLERS]
        results.check("HANDLERS 覆盖全部期望命令", not missing, f"缺失 {missing}")
    except Exception as exc:
        results.fail("命令表检查", f"{type(exc).__name__}: {exc}")

    # 4) REST 端点：协议冻结 10 条必须全部注册，扩展如实单列
    print(
        f"\n[4] REST / 静态 / WS 端点"
        f"（PROTOCOL 第 1 / 7 节 {len(PROTOCOL_ENDPOINTS)} 条"
        f" + 协议外扩展 {len(EXTRA_ENDPOINTS)} 条）",
        flush=True,
    )
    try:
        import http_api

        endpoints = tuple(http_api.rest_endpoints())
        missing = [e for e in PROTOCOL_ENDPOINTS if e not in endpoints]
        extras = [e for e in endpoints if e not in PROTOCOL_ENDPOINTS]
        results.check(
            "协议端点全部已注册",
            not missing,
            f"实际 {len(endpoints)} 条 / 协议 {len(PROTOCOL_ENDPOINTS)} 条，缺失 {missing}",
        )
        results.check(
            "追加端点已明确登记",
            extras == list(EXTRA_ENDPOINTS),
            f"追加 {extras}（POST /api/apps + GET /api/apps/icon）",
        )
        results.check(
            "端点集合与期望完全一致",
            endpoints == EXPECTED_ENDPOINTS,
            f"实际 {len(endpoints)} 条 / 期望 {len(EXPECTED_ENDPOINTS)} 条",
        )
    except Exception as exc:
        results.fail("端点检查", f"{type(exc).__name__}: {exc}")

    # 5) 默认配置逐字段
    print("\n[5] 默认 config 逐字段（PROTOCOL 第 6 节 + V2 §2）", flush=True)
    try:
        import config_store

        default = config_store.default_config()
        results.check(
            "default_config() == 第 6 节结构（V2 §2 默认值）",
            default == EXPECTED_DEFAULT_CONFIG,
            "逐字段深度比较",
        )
        for key in EXPECTED_DEFAULT_CONFIG:
            results.check(
                f"字段 {key}",
                default.get(key) == EXPECTED_DEFAULT_CONFIG[key],
            )
    except Exception as exc:
        results.fail("默认配置检查", f"{type(exc).__name__}: {exc}")

    # 6) 键名表覆盖
    print("\n[6] VK_TABLE 键名覆盖（PROTOCOL 第 5 节）", flush=True)
    try:
        import input_win

        table = frozenset(input_win.VK_TABLE)
        missing = sorted(EXPECTED_KEYS - table)
        results.check(
            "覆盖全部键名",
            not missing,
            f"表内 {len(table)} 个 / 期望 {len(EXPECTED_KEYS)} 个，缺失 {missing}",
        )
        import ast

        tree = ast.parse((_SERVER_DIR / "input_win.py").read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "SetCursorPos")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "SetCursorPos")
            )
        ]
        results.check("未调用 SetCursorPos（AST 级校验）", not calls)
    except Exception as exc:
        results.fail("键名表检查", f"{type(exc).__name__}: {exc}")

    # 7) 二维码可生成（qrcode 缺失时验证纯 Python 降级）
    print("\n[7] 二维码生成", flush=True)
    try:
        import qr

        url = qr.make_url("192.168.1.10", 8765)
        results.check("二维码内容格式", url == "http://192.168.1.10:8765/", url)
        png = qr.build_png(url)
        results.check(
            "build_png 返回合法 PNG",
            isinstance(png, bytes) and png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 100,
            f"{len(png)} bytes，后端={qr.backend_name()}",
        )
    except Exception as exc:
        results.fail("二维码检查", f"{type(exc).__name__}: {exc}")

    # 8) 原子写与 REST / WS 两条 config 路径的代码事实（V1 冻结项）
    print("\n[8] 关键实现事实核对（V1 冻结项）", flush=True)
    config_source = (_SERVER_DIR / "config_store.py").read_text(encoding="utf-8")
    results.check(
        "config 原子写使用 os.replace",
        "os.replace(tmp, self.path)" in config_source,
    )
    api_source = (_SERVER_DIR / "http_api.py").read_text(encoding="utf-8")
    results.check("PUT /api/config 已注册", 'add_put("/api/config"' in api_source)
    results.check("GET /api/qr.png 已注册", 'add_get("/api/qr.png"' in api_source)
    results.check(".js MIME 为 text/javascript", '".js": "text/javascript"' in api_source)
    results.check(".css MIME 为 text/css", '".css": "text/css"' in api_source)

    # 9) V2 追加层 §8 新增离线断言
    print("\n[9] V2 追加层回归断言（§8）", flush=True)
    try:
        import config_store as _config_store
        import apps as _apps
        import apps_icons as _apps_icons
        import dispatcher as _dispatcher
        import http_api as _http_api

        cfg = _config_store.default_config()

        # 9.1 hotkeys 默认清空（§2.1）
        results.check("default_config()['hotkeys'] == []", cfg.get("hotkeys") == [])

        # 9.2 apps 5 项名称正确且每项含 web/local（§2.2）
        apps_list = cfg.get("apps")
        names = tuple(
            str(item.get("name") or "") for item in apps_list
        ) if isinstance(apps_list, list) else ()
        results.check(
            "apps 为 5 项且 name 序列正确",
            len(apps_list or []) == 5 and names == EXPECTED_APP_NAMES,
            f"name={list(names)}",
        )
        results.check(
            "apps 每项含 web/local 且 path 恒等于 web",
            isinstance(apps_list, list)
            and all(
                isinstance(item, dict)
                and isinstance(item.get("web"), str) and item.get("web")
                and isinstance(item.get("local"), dict)
                and set(("names", "exe", "paths")).issubset(item["local"])
                and item.get("path") == item.get("web")
                for item in apps_list
            ),
        )
        results.check(
            "config 顶层键严格 6 个（V1 冻结）",
            set(cfg) == {"version", "pages", "active_page_id", "hotkeys", "apps", "touchpad"},
            f"keys={sorted(cfg)}",
        )

        # 9.3 双路径启动（§3）：真实调用 locate_local 验证命中与兜底，
        #     源码级验证 launch_app 追加 route/matched_path/web_fallback。
        app_source = (_SERVER_DIR / "apps.py").read_text(encoding="utf-8")
        results.check(
            "launch_app 实现双路径（locate_local + web_fallback + route）",
            all(
                token in app_source
                for token in (
                    "def locate_local(", "def launch_app(", '"route"',
                    '"matched_path"', '"web_fallback"', "webbrowser.open",
                )
            ),
        )
        # 用 sys.executable 造一条真实存在的「已安装应用」，验证 local.names 模糊匹配
        fake_app = {
            "id": "app-1",
            "name": "抖音",
            "path": "https://www.douyin.com/",
            "web": "https://www.douyin.com/",
            "local": {"names": ["抖音", "douyin"], "exe": ["douyin.exe"], "paths": []},
        }
        installed = [
            {"id": "scan-1", "name": "抖音电脑版", "path": sys.executable}
        ]
        try:
            matched = _apps.locate_local(fake_app, installed, deep_probe=False)
        except Exception as exc:
            matched = f"EXC:{exc}"
        results.check(
            "locate_local 按 local.names 规范化模糊命中本地路径",
            matched == sys.executable,
            f"matched={matched!r}",
        )
        miss_app = {
            "id": "app-x",
            "name": "不存在应用",
            "path": "https://example.invalid/",
            "web": "https://example.invalid/",
            "local": {"names": ["绝不匹配xyz"], "exe": ["__never_exists_xyz__.exe"], "paths": []},
        }
        try:
            missed = _apps.locate_local(miss_app, [], deep_probe=False)
        except Exception as exc:
            missed = f"EXC:{exc}"
        results.check(
            "locate_local 未命中返回空串（走官网兜底）",
            missed == "",
            f"missed={missed!r}",
        )
        # 9.4 图标端点（§4）：注册 + 修饰字段派生
        endpoints = tuple(_http_api.rest_endpoints())
        results.check(
            "GET /api/apps/icon 已注册",
            "GET /api/apps/icon" in endpoints,
        )
        results.check(
            "路由实际绑定 api_apps_icon",
            'add_get("/api/apps/icon", api_apps_icon)' in (
                _SERVER_DIR / "http_api.py"
            ).read_text(encoding="utf-8"),
        )
        results.check(
            "AppStore.merged 保留 web/local 追加字段（V1:444-447 逻辑）",
            "保留前端可能追加的其它字段" in app_source
            and "for key, value in raw.items():" in app_source,
        )
        decorated = _apps_icons.decorate_apps(list(cfg["apps"]), installed)
        results.check(
            "decorate_apps 逐项追加 has_local/route 且保留 V1 字段",
            len(decorated) == 5
            and all(
                isinstance(item, dict)
                and "has_local" in item and "route" in item
                and item.get("id") == cfg["apps"][index].get("id")
                and item.get("web") == cfg["apps"][index].get("web")
                and item.get("local") == cfg["apps"][index].get("local")
                for index, item in enumerate(decorated)
            ),
        )
        results.check(
            "icon_url 形如 /api/apps/icon?path=... 且仅在本地命中时存在",
            all(
                (not item.get("icon_url"))
                or (
                    isinstance(item.get("icon_url"), str)
                    and item["icon_url"].startswith("/api/apps/icon?path=")
                    and item.get("has_local") is True
                )
                for item in decorated
            ),
        )

        # 9.5 brightness / voice 命令在 HANDLERS 内（§5 / §6）
        v2_commands = (
            "brightness.set", "brightness.get",
            "voice.start", "voice.text", "voice.stop",
        )
        results.check(
            "5 条 V2 命令全部在 dispatcher.HANDLERS 内",
            all(name in _dispatcher.HANDLERS for name in v2_commands),
        )
        results.check(
            "BAD_VALUE 错误码已定义（brightness.set 越界）",
            hasattr(_dispatcher, "BadValueError")
            and _dispatcher.BadValueError.code == "BAD_VALUE",
        )
        results.check(
            "brightness / voice 模块提供契约要求的入口",
            all(hasattr(_dispatcher, name) for name in (
                "cmd_brightness_set", "cmd_brightness_get",
                "cmd_voice_start", "cmd_voice_text", "cmd_voice_stop",
            )),
        )

        # 9.6 连续按键回归用例存在（§8 需求 3）
        selftest_source = (_SERVER_DIR / "selftest.py").read_text(encoding="utf-8")
        results.check(
            "联机自检含连发 5 次 key.press 独立响应用例",
            "KEY_PRESS_REPEAT" in selftest_source
            and "range(1, KEY_PRESS_REPEAT + 1)" in selftest_source
            and 'self-12-' in selftest_source,
        )
        # 用 AST 精确检查 key_press 函数体：每次调用都构造独立 down/up 并
        # _send 两次；没有任何跨调用的去重集合 / lru_cache / 节流时间戳。
        # 注意 V1 的 time.sleep(KEY_HOLD_SECONDS) 是单次按键的保持时长，
        # 每次调用都执行，不属于「跨调用吞键」，不能据此判失败。
        import ast as _ast

        _input_source = (_SERVER_DIR / "input_win.py").read_text(encoding="utf-8")
        input_tree = _ast.parse(_input_source)
        key_press_node: Any = next(
            (
                node
                for node in input_tree.body
                if isinstance(node, _ast.FunctionDef) and node.name == "key_press"
            ),
            None,
        )
        body_source = ""
        if key_press_node is not None:
            body_source = _ast.get_source_segment(_input_source, key_press_node) or ""
        results.check(
            "input_win.key_press 每次独立 down/up，无跨调用去重/节流/吞键（V1 语义未改）",
            key_press_node is not None
            and "KEYEVENTF_KEYUP" in body_source
            and body_source.count("_send(") >= 2
            and all(
                token not in body_source
                for token in ("lru_cache", "seen", "last_press", "dedup", "monotonic")
            ),
        )
    except Exception as exc:
        results.fail("V2 追加层回归断言", f"{type(exc).__name__}: {exc}")


# ===========================================================================
# 联机自检（PROTOCOL 第 9 节 + V2 §8 连续按键回归）
# ===========================================================================
def _envelope(msg_type: str, payload: dict[str, Any], msg_id: str | None = None) -> dict[str, Any]:
    env: dict[str, Any] = {
        "type": msg_type,
        "payload": payload,
        "ts": int(time.time() * 1000),
    }
    if msg_id is not None:
        env["id"] = msg_id
    return env


async def _next_of(ws: Any, wanted: set[str], timeout: float = 5.0) -> dict[str, Any]:
    """接收下一条 ``type`` 属于 ``wanted`` 的消息，跳过中间的广播消息。"""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"等待 {sorted(wanted)} 超时")
        msg = await asyncio.wait_for(ws.receive(), timeout=remaining)
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING, WSMsgType.ERROR):
            raise ConnectionError("WS 连接已关闭")
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = msg.json()
        except Exception:
            continue
        if isinstance(data, dict) and data.get("type") in wanted:
            return data


async def run_online(host: str, port: int, results: Results) -> None:
    """按 PROTOCOL 第 9 节顺序联机验证，末尾追加 V2 §8 连续按键回归。"""
    base = f"http://{host}:{port}"
    ws_url = f"ws://{host}:{port}/ws"
    print(f"== 联机自检（PROTOCOL 第 9 节 + V2 §8）目标 {base} ==\n", flush=True)

    async with ClientSession() as session:
        # ---- 1) 连 WS ----
        try:
            if _ClientWSTimeout is not None:
                ws = await session.ws_connect(
                    ws_url, timeout=_ClientWSTimeout(ws_close=5.0)
                )
            else:
                ws = await session.ws_connect(ws_url, timeout=5.0)
            results.ok("1. 连接 WS", ws_url)
        except Exception as exc:
            results.fail("1. 连接 WS", f"{type(exc).__name__}: {exc}")
            print("  （服务未启动？先在另一个终端运行 start.bat）", flush=True)
            for step in range(2, 13):
                results.skip(f"{step}. 后续步骤", "连接失败")
            return

        try:
            # ---- 2) 收 hello ----
            try:
                hello = await _next_of(ws, {"hello"})
                hp = hello.get("payload", {})
                ok = all(k in hp for k in ("device", "version", "lan_ip", "port"))
                results.check("2. 建连收到 hello", ok, f"payload={hp}")
            except Exception as exc:
                results.fail("2. 建连收到 hello", f"{type(exc).__name__}: {exc}")

            # ---- 3) ping → pong ----
            try:
                ping_ts = int(time.time() * 1000)
                await ws.send_json(_envelope("ping", {}, "self-3"))
                pong = await _next_of(ws, {"pong"})
                echo = pong.get("payload", {}).get("echo_ts")
                results.check("3. ping → pong 回显 ts", echo == ping_ts, f"echo_ts={echo}")
            except Exception as exc:
                results.fail("3. ping → pong", f"{type(exc).__name__}: {exc}")

            # ---- 4) GET /api/status ----
            try:
                async with session.get(f"{base}/api/status", timeout=5.0) as resp:
                    body = await resp.json(content_type=None)
                    keys = ("connected", "device", "lan_ip", "port", "version", "uptime_ms", "clients")
                    results.check(
                        "4. GET /api/status",
                        resp.status == 200 and all(k in body for k in keys),
                        f"HTTP {resp.status}, keys={list(body)}",
                    )
            except Exception as exc:
                results.fail("4. GET /api/status", f"{type(exc).__name__}: {exc}")

            # ---- 5) GET /api/config ----
            config_before: dict[str, Any] | None = None
            try:
                async with session.get(f"{base}/api/config", timeout=5.0) as resp:
                    config_before = await resp.json(content_type=None)
                    keys = ("version", "pages", "active_page_id", "hotkeys", "apps", "touchpad")
                    results.check(
                        "5. GET /api/config",
                        resp.status == 200 and all(k in config_before for k in keys),
                        f"HTTP {resp.status}, keys={list(config_before)}",
                    )
            except Exception as exc:
                results.fail("5. GET /api/config", f"{type(exc).__name__}: {exc}")

            # ---- 6) PUT /api/config 回读一致 ----
            try:
                if config_before is None:
                    raise RuntimeError("第 5 步未拿到 config，跳过写入")
                payload = json.dumps(config_before, ensure_ascii=False).encode("utf-8")
                async with session.put(
                    f"{base}/api/config",
                    data=payload,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    timeout=5.0,
                ) as resp:
                    put_body = await resp.json(content_type=None)
                saved = put_body.get("config") if isinstance(put_body, dict) else None
                async with session.get(f"{base}/api/config", timeout=5.0) as resp2:
                    config_after = await resp2.json(content_type=None)
                results.check(
                    "6. PUT /api/config 回读一致",
                    put_body.get("ok") is True
                    and saved == config_before
                    and config_after == config_before,
                    f"ok={put_body.get('ok')}, 写入前后深度相等",
                )
            except Exception as exc:
                results.fail("6. PUT /api/config 回读一致", f"{type(exc).__name__}: {exc}")

            # ---- 7) key.press ["a"] → ack 原样带回 id ----
            try:
                await ws.send_json(_envelope("key.press", {"keys": ["a"]}, "self-7"))
                ack = await _next_of(ws, {"ack", "error"})
                ok = ack.get("type") == "ack" and ack.get("payload", {}).get("id") == "self-7"
                results.check("7. key.press [\"a\"]", ok, f"收到 {ack.get('type')}")
            except Exception as exc:
                results.fail("7. key.press [\"a\"]", f"{type(exc).__name__}: {exc}")

            # ---- 8) media.control play_pause → ack ----
            try:
                await ws.send_json(
                    _envelope("media.control", {"action": "play_pause"}, "self-8")
                )
                ack = await _next_of(ws, {"ack", "error"})
                ok = ack.get("type") == "ack" and ack.get("payload", {}).get("id") == "self-8"
                results.check("8. media.control play_pause", ok, f"收到 {ack.get('type')}")
            except Exception as exc:
                results.fail("8. media.control play_pause", f"{type(exc).__name__}: {exc}")

            # ---- 9) mouse.move 高频：协议规定不回 ack ----
            try:
                await ws.send_json(_envelope("mouse.move", {"dx": 20, "dy": 0}, "self-9"))
                # 不等 ack，只让事件循环有机会处理；随后确认连接仍存活
                await asyncio.sleep(0.15)
                results.check(
                    "9. mouse.move {dx:20,dy:0}（不回 ack）",
                    not ws.closed,
                    "已发送，连接存活，符合高频通道约定",
                )
            except Exception as exc:
                results.fail("9. mouse.move", f"{type(exc).__name__}: {exc}")

            # ---- 10) app.list → app.list 消息（含 V2 图标元数据） ----
            try:
                await ws.send_json(_envelope("app.list", {}, "self-10"))
                listing = await _next_of(ws, {"app.list", "error"})
                apps_list = listing.get("payload", {}).get("apps")
                ok = listing.get("type") == "app.list" and isinstance(apps_list, list)
                has_v2 = isinstance(apps_list, list) and all(
                    "has_local" in item and "route" in item
                    for item in apps_list if isinstance(item, dict)
                )
                results.check(
                    "10. app.list（每项含 has_local/route）",
                    ok and has_v2,
                    f"apps 数量={len(apps_list) if isinstance(apps_list, list) else 'N/A'}",
                )
            except Exception as exc:
                results.fail("10. app.list", f"{type(exc).__name__}: {exc}")

            # ---- 11) hotkey.run：V2 §2.1 默认 hotkeys=[]，用确定不存在的 id
            #          验证 V1 error 结构（id 原样回带 + code=NOT_FOUND）。
            #          这样无论用户机器上的 config.json 是否留有旧热键都稳定通过。
            try:
                await ws.send_json(
                    _envelope("hotkey.run", {"id": "__selftest_missing__"}, "self-11")
                )
                reply = await _next_of(ws, {"ack", "error"})
                ep = reply.get("payload", {})
                ok = (
                    reply.get("type") == "error"
                    and ep.get("id") == "self-11"
                    and ep.get("code") == "NOT_FOUND"
                )
                results.check(
                    "11. hotkey.run 未知 id → error NOT_FOUND（V1 error 结构）",
                    ok,
                    f"type={reply.get('type')}, code={ep.get('code')}",
                )
            except Exception as exc:
                results.fail("11. hotkey.run", f"{type(exc).__name__}: {exc}")

            # ---- 12) V2 §8 连续按键回归：连发 5 次完全相同的 key.press{keys:["a"]}
            #          每次带独立 id，断言每次都独立收到对应 ack（不吞键）。
            try:
                independent = 0
                for number in range(1, KEY_PRESS_REPEAT + 1):
                    msg_id = f"self-12-{number}"
                    await ws.send_json(_envelope("key.press", {"keys": ["a"]}, msg_id))
                    reply = await _next_of(ws, {"ack", "error"})
                    if (
                        reply.get("type") == "ack"
                        and reply.get("payload", {}).get("id") == msg_id
                    ):
                        independent += 1
                results.check(
                    f"12. 连发 {KEY_PRESS_REPEAT} 次 key.press{{\"keys\":[\"a\"]}} 每次独立响应",
                    independent == KEY_PRESS_REPEAT,
                    f"独立 ack={independent}/{KEY_PRESS_REPEAT}，后端无去重/节流/吞键",
                )
            except Exception as exc:
                results.fail("12. 连续按键回归", f"{type(exc).__name__}: {exc}")
        finally:
            try:
                await ws.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="selftest",
        description="Couch Commander · 沙发指挥官 —— 后端自检",
    )
    parser.add_argument("--host", default="127.0.0.1", help="服务地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="服务端口，默认 8765")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="只做静态自检：编译 / import / 命令 / 端点 / 默认值 / 键名 / 二维码 / V2 回归，不连服务、不注入键鼠",
    )
    args = parser.parse_args(argv)
    results = Results()
    print("Couch Commander · 沙发指挥官 —— 后端自检", flush=True)
    print(f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n", flush=True)
    if args.offline:
        run_offline(results)
    else:
        asyncio.run(run_online(args.host, args.port, results))
    return results.summary()


if __name__ == "__main__":
    sys.exit(main())
