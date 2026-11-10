"""WS 命令分发器：按 ``type`` 路由到执行器、参数校验、异常统一回 ``error``。

对应 PROTOCOL 第 3 节全部 22 条客户端命令，以及第 2 / 4 / 8 节的下发约定：

* 低频命令完成后回 ``ack``，且 ``payload.id`` **原样带回**客户端的 ``id``；
* ``mouse.move`` / ``mouse.scroll`` 是高频命令：**不回 ack、不做 JSON 序列化
  日志**，直接进 ``SendInput``（仅参数错误/执行异常时回 ``error``）；
* ``hello`` / ``ping`` / ``status.get`` / ``media.state`` / ``app.list`` /
  ``app.scan`` / ``config.get`` / ``config.put`` / ``app.activate`` /
  ``app.release`` 回协议规定的专属消息（其中 ``config.put`` 是 ``ack`` +
  ``config`` 两条）；
* 参数错误 → ``BAD_PAYLOAD``；``brightness.set`` 的 ``value`` 非法 →
  ``BAD_VALUE``；未知键名 → ``UNKNOWN_KEY``；未知命令 → ``UNKNOWN_COMMAND``；
  查不到资源 → ``NOT_FOUND``；其余统一 ``INTERNAL_ERROR`` 或按底层异常映射，
  异常绝不掀翻 WS 连接。

V2 追加层（``docs/PROTOCOL_V2_ADDENDUM.md`` §5 / §6 / §9）新增 5 条命令：
``brightness.set`` / ``brightness.get`` / ``voice.start`` / ``voice.text`` /
``voice.stop``。全部第三方 / 平台调用放 ``loop.run_in_executor``，不阻塞
aiohttp 事件循环、不写 JSON 日志；缺依赖或调用失败由各自模块降级
``best_effort``，绝不抛异常掀翻连接。``app.list`` / ``app.scan`` 的列表经
``apps_icons.decorate_apps`` 派生 V2 图标元数据（§4.2）。

本模块运行时**不 import** ``ws_hub``（避免循环依赖），``ctx`` / ``conn`` 通过
duck typing 使用；类型注解仅在 ``TYPE_CHECKING`` 下引用。
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from typing import TYPE_CHECKING, Any, Callable, Awaitable

import apps as apps_module
import apps_icons
import brightness
import input_win
import media
import input_win
import voice

if TYPE_CHECKING:
    from ws_hub import AppContext, ClientConn

# 高频命令集合（与 ws_hub.HIGH_FREQ_TYPES 保持一致，V1 冻结项不动）
# V1：mouse.move / mouse.scroll 自身不回 ack、不写 JSON 日志。
# V2：brightness.set 走 executor 高频通道（§5「回也无妨」），voice.text
#     final:false 预览不逐条 ack；两者都不写 JSON 日志、不阻塞事件循环。
HIGH_FREQ_TYPES: frozenset[str] = frozenset({"mouse.move", "mouse.scroll"})

_MISSING = object()


class BadPayloadError(ValueError):
    """参数校验失败，映射为 ``error {code:"BAD_PAYLOAD"}``。"""

    code = "BAD_PAYLOAD"


class BadValueError(ValueError):
    """``brightness.set`` 的 ``value`` 越界 / 非法，映射为 ``error {code:"BAD_VALUE"}``。

    V2 追加层 §5.1 指定该独立错误码，不能并入 ``BAD_PAYLOAD``。
    """

    code = "BAD_VALUE"


class NotFoundError(LookupError):
    """按 id 查资源未命中，映射为 ``error {code:"NOT_FOUND"}``。"""

    code = "NOT_FOUND"


# ---------------------------------------------------------------------------
# 参数校验原语
# ---------------------------------------------------------------------------
def _payload_of(env: dict[str, Any]) -> dict[str, Any]:
    payload = env.get("payload")
    return payload if isinstance(payload, dict) else {}


def _number(payload: dict[str, Any], key: str, default: Any = _MISSING) -> float:
    """取 number（int/float，排除 bool 与 NaN/Inf）；不合法抛 ``BadPayloadError``。"""
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BadPayloadError(f"{key} must be number")
    number = float(value)
    if not math.isfinite(number):
        raise BadPayloadError(f"{key} must be finite number")
    return number


def _string(payload: dict[str, Any], key: str, default: Any = _MISSING) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or value == "":
        raise BadPayloadError(f"{key} must be non-empty string")
    return value


def _one_of(payload: dict[str, Any], key: str, allowed: frozenset[str] | tuple[str, ...]) -> str:
    value = _string(payload, key)
    if value not in allowed:
        raise BadPayloadError(
            f"{key} must be one of {', '.join(sorted(set(allowed)))}"
        )
    return value


def _button(payload: dict[str, Any]) -> str:
    return _one_of(payload, "button", ("left", "right", "middle"))


def _keys(payload: dict[str, Any]) -> list[str]:
    value = payload.get("keys")
    if not isinstance(value, list) or not value:
        raise BadPayloadError("keys must be non-empty array")
    if not all(isinstance(item, str) and item for item in value):
        raise BadPayloadError("keys[] must be string")
    return list(value)


def _config_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise BadPayloadError(f"{key} must be object")
    return value


def _brightness_value(payload: dict[str, Any]) -> float:
    """V2 §5.1：``value`` 必须是 0..1 的有限 number；否则抛 ``BadValueError``。"""
    raw = payload.get("value", _MISSING)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BadValueError("value 必须是 0..1 之间的 number")
    value = float(raw)
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise BadValueError("value 必须是 0..1 之间的 number")
    return value


# ---------------------------------------------------------------------------
# 下发小工具
# ---------------------------------------------------------------------------
async def _ack(
    ctx: "AppContext",
    conn: "ClientConn",
    env: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> None:
    """低频命令完成回 ``ack``，原样带回 ``id``。"""
    await ctx.send_ack(conn, env.get("id"), extra)


async def _merged_apps(ctx: "AppContext") -> list[dict[str, Any]]:
    """在线程池里读缓存/扫描 + 合并 config.apps，并派生 V2 图标元数据。

    ``apps_icons.decorate_apps`` 逐项追加 ``has_local`` / ``route`` / 可选
    ``icon_url``（§4.2），V1 字段全部原样保留；单项失败也降级不抛。
    """
    loop = asyncio.get_running_loop()

    def _work() -> list[dict[str, Any]]:
        installed = ctx.app_store.cached()
        return apps_icons.decorate_apps(ctx.merged_apps(installed), installed)

    return await loop.run_in_executor(None, _work)


# ---------------------------------------------------------------------------
# 各命令 handler（统一签名）
# ---------------------------------------------------------------------------
async def cmd_hello(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """客户端首条 hello：服务端回 ``hello``（第 3.1 / 4 节）。"""
    await ctx.send_msg(conn, "hello", ctx.hello_payload())


async def cmd_ping(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """``ping`` → ``pong {echo_ts:<ping 的 ts>}``，走低频通道。"""
    await ctx.send_msg(conn, "pong", {"echo_ts": env.get("ts")}, low=True)


async def cmd_status_get(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """请求立即回一次 ``status``。"""
    await ctx.send_status(conn, low=True)


async def cmd_mouse_move(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """高频：相对增量移动，直接进 SendInput，不回 ack、不记日志。"""
    payload = _payload_of(env)
    # 必须先校验完两个参数再执行，避免 dx 生效后 dy 才报错
    dx = _number(payload, "dx")
    dy = _number(payload, "dy")
    input_win.mouse_move(dx, dy)


async def cmd_mouse_scroll(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """高频：双指滚动换算，直接进 SendInput，不回 ack、不记日志。"""
    payload = _payload_of(env)
    dx = _number(payload, "dx")
    dy = _number(payload, "dy")
    input_win.mouse_wheel(dx, dy)


async def cmd_mouse_down(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    button = _button(_payload_of(env))
    input_win.mouse_down(button)
    await _ack(ctx, conn, env)


async def cmd_mouse_up(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    button = _button(_payload_of(env))
    input_win.mouse_up(button)
    await _ack(ctx, conn, env)


async def cmd_mouse_click(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """``mouse.click``：``button`` 与 ``clicks`` 均为必填，``clicks`` 只取 1|2。"""
    payload = _payload_of(env)
    button = _button(payload)
    # _number 要求字段存在且为有限 number；缺省会以 _MISSING 触发 BAD_PAYLOAD
    raw = _number(payload, "clicks")
    clicks = int(raw)
    if raw != clicks or clicks not in (1, 2):
        raise BadPayloadError("clicks must be 1 or 2")
    input_win.mouse_click(button, clicks)
    await _ack(ctx, conn, env)


async def cmd_mouse_pinch(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """捏合比例 → Ctrl + 滚轮（media.py 的映射原语）。"""
    scale = _number(_payload_of(env), "scale")
    if scale <= 0:
        raise BadPayloadError("scale must be > 0")
    notches = media.pinch_zoom(scale)
    await _ack(ctx, conn, env, {"best_effort": True, "notches": notches})


async def cmd_window_switch(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """三指滑动：``next``→Alt+Tab，``prev``→Alt+Shift+Tab。"""
    direction = _one_of(_payload_of(env), "direction", ("prev", "next"))
    if direction == "next":
        input_win.key_press(["alt", "tab"])
    else:
        input_win.key_press(["alt", "shift", "tab"])
    await _ack(ctx, conn, env)


_FOCUS_MIN_INTERVAL_S = 0.8
_focus_last_at: float = 0.0
_focus_last_name: str = ""


def _focus_keywords(ctx: "AppContext", name: str) -> list[str]:
    """为一个应用生成置前用的候选关键词（标题片段 / 进程名 / 别名）。

    只按窗口标题匹配太脆：浏览器播放抖音时窗口标题是「抖音 - Google Chrome」，
    客户端是「抖音」，某些版本还叫「抖音直播」。所以把 config 里这一项的
    ``name``、``local.names`` 别名、以及 ``exe`` 主名都作为候选，逐个尝试。
    """
    keywords: list[str] = [str(name)]
    try:
        config = ctx.get_config()
    except Exception:
        return keywords
    entry: dict[str, Any] | None = None
    for item in config.get("apps") or []:
        if not isinstance(item, dict):
            continue
        if item.get("name") == name or item.get("id") == getattr(ctx, "locked_app_id", None):
            entry = item
            break
    if entry is None:
        return keywords
    local = entry.get("local")
    if isinstance(local, dict):
        for alias in local.get("names") or []:
            if isinstance(alias, str) and alias.strip():
                keywords.append(alias.strip())
        for exe in local.get("exe") or []:
            if isinstance(exe, str) and exe.strip():
                stem = os.path.splitext(os.path.basename(exe.strip()))[0]
                if stem:
                    keywords.append(stem)
    # 去重保序
    seen: set[str] = set()
    ordered: list[str] = []
    for word in keywords:
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(word)
    return ordered


def _ensure_target_foreground(ctx: "AppContext", override: str | None = None) -> bool:
    """按键注入前把目标应用强行置前，返回是否成功置前。

    为什么必须这样做：``SendInput`` 只把按键交给**前台窗口**。用户锁定了抖音
    之后往往又点回手机浏览器看手机端页面，或切到别的窗口；此时后端照发
    ``b`` / ``j`` / ``h`` / 方向键，按键全部落到了错误窗口，表现就是
    「快捷键不起作用」。这里在每次注入前用目标应用的多组关键词找窗口，
    再用 :func:`apps_module.force_foreground` 绕过 Windows 前台锁真正置前。

    目标应用的确定顺序：

    1. ``override``——前端在 payload 里显式给的应用名（点 A/B/C 或方向键时
       前端知道自己选的是谁，即使还没长按锁定）；
    2. ``ctx.active_app``——已长按锁定的应用。

    两者都没有时不做置前（用户没表达「要操控谁」，就不动前台焦点）。
    失败静默返回否，不影响注入本身。

    连按（长按方向键 120ms 一次）会走到这里，``EnumWindows`` 虽快但没必要
    每 120ms 做一次，因此按「同一目标 + 0.8s 内不重复」节流。
    """
    global _focus_last_at, _focus_last_name
    # 自检替身 ctx 可能不带这些字段，用 getattr 兜底而不是硬取属性。
    name = override or getattr(ctx, "active_app", None)
    if not name:
        return False
    now = time.monotonic()
    if name == _focus_last_name and (now - _focus_last_at) < _FOCUS_MIN_INTERVAL_S:
        return True
    keywords = _focus_keywords(ctx, name)
    try:
        ok = bool(apps_module._try_focus_any(keywords))
    except Exception:
        ok = False
    _focus_last_at = now
    _focus_last_name = str(name)
    return ok


async def cmd_key_press(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """V1 原语义：每次独立 down/up 组合，无去重/节流/吞键，逐条回 ack。

    本版本追加两处**向后兼容**的增强（V1 不带的客户端行为完全不变）：

    * ``payload.app``（可选字符串）：本次按键想送进的应用名。给了就先把它
      置前（见 ``_ensure_target_foreground``），保证按键真的进那个应用；
    * ``ack.extra.focused``：告诉前端这次是否成功置前，便于排查。

    ``payload.keys`` 仍是唯一的必填字段，校验与错误码不变。
    """
    payload = _payload_of(env)
    keys = _keys(payload)
    override = payload.get("app")
    override_name = override.strip() if isinstance(override, str) and override.strip() else None
    loop = asyncio.get_running_loop()
    focused = await loop.run_in_executor(None, _ensure_target_foreground, ctx, override_name)
    input_win.key_press(keys)
    await _ack(ctx, conn, env, {"keys": keys, "focused": focused})


async def cmd_key_type(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    text = _payload_of(env).get("text")
    if not isinstance(text, str):
        raise BadPayloadError("text must be string")
    input_win.key_type(text)
    await _ack(ctx, conn, env)


async def cmd_hotkey_run(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """查 ``config.hotkeys`` 里的 id，转成 ``key.press``。

    修复「快捷键不起作用」：手机端 config 与服务端 config 可能短暂不同步
    （断连态套用模板、WS config.put 与 REST PUT 双写竞态）。前端 dpad.js 现在
    点击 A/B/C 时除了 ``id`` 还会附带本机那份 ``keys``；若按 id 查不到，就
    用这份 keys 兜底注入，而不是回 ``NOT_FOUND`` 让按键完全无反应。仍然先
    以服务端 config 为准（服务端是唯一权威），只有缺位时才降级到附带 keys。
    """
    payload = _payload_of(env)
    hotkey_id = payload.get("id")
    if not isinstance(hotkey_id, str) or not hotkey_id:
        raise BadPayloadError("id must be non-empty string")
    config = ctx.get_config()
    hit = None
    for item in config.get("hotkeys", []):
        if isinstance(item, dict) and item.get("id") == hotkey_id:
            hit = item
            break
    if hit is not None:
        keys = _keys({"keys": hit.get("keys")})
        source = "config"
    else:
        # 服务端没有该 id（本机刚套模板还没同步过来）：用前端附带的 keys。
        try:
            keys = _keys({"keys": payload.get("keys")})
        except BadPayloadError:
            raise NotFoundError(f"hotkey 不存在: {hotkey_id}")
        source = "payload_fallback"
    loop = asyncio.get_running_loop()
    focused = await loop.run_in_executor(None, _ensure_target_foreground, ctx)
    input_win.key_press(keys)
    await _ack(
        ctx,
        conn,
        env,
        {"id": hotkey_id, "keys": keys, "source": source, "focused": focused},
    )


async def cmd_media_control(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    action = _one_of(_payload_of(env), "action", media.VALID_ACTIONS)
    media.control(action)
    await _ack(ctx, conn, env)


async def cmd_media_seek(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """尽力映射进度条，``ack.extra.best_effort`` 恒为 true。"""
    position = _number(_payload_of(env), "position")
    if position < 0.0 or position > 1.0:
        raise BadPayloadError("position must be between 0 and 1")
    extra = media.seek(position)
    await _ack(ctx, conn, env, extra)


async def cmd_media_state(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """回 ``media`` 消息；SMTC 探测可能耗时，放线程池。"""
    loop = asyncio.get_running_loop()
    current = await loop.run_in_executor(None, media.state)
    await ctx.send_msg(conn, "media", current)


async def cmd_app_list(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """回 ``app.list``：已安装应用 + config.apps 合并去重 + V2 图标元数据。"""
    merged = await _merged_apps(ctx)
    await ctx.send_msg(conn, "app.list", {"apps": merged})


async def cmd_app_scan(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """触发重新扫描，完成后回 ``app.list``（同样带 V2 图标元数据）。"""
    loop = asyncio.get_running_loop()

    def _work() -> list[dict[str, Any]]:
        installed = ctx.app_store.scan()
        return apps_icons.decorate_apps(ctx.merged_apps(installed), installed)

    merged = await loop.run_in_executor(None, _work)
    await ctx.send_msg(conn, "app.list", {"apps": merged})


async def cmd_app_activate(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """锁定应用：尽力启动/置前，广播 ``status`` 带 ``locked_app_id``。"""
    app_id = _string(_payload_of(env), "id")
    merged = await _merged_apps(ctx)
    app = apps_module.AppStore.find(merged, app_id)
    if app is None:
        raise NotFoundError(f"应用不存在: {app_id}")
    loop = asyncio.get_running_loop()

    def _launch() -> dict[str, Any]:
        # V2 §3：把缓存的已安装列表交给 launch_app，供 local.names 模糊匹配
        installed = ctx.app_store.cached()
        return apps_module.launch_app(app, installed)

    launch_result = await loop.run_in_executor(None, _launch)
    ctx.set_lock(app_id, str(app.get("name") or ""))
    # 锁定变化属于第 4 节 status 的触发条件，广播给全部连接
    await ctx.broadcast_status()
    # 同时对本连接补一条携带启动细节的 ack，便于前端排查 best-effort 结果
    await _ack(ctx, conn, env, launch_result)


async def cmd_app_release(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """解锁：清除锁定，广播 ``status``。"""
    ctx.clear_lock()
    await ctx.broadcast_status()
    await _ack(ctx, conn, env)


async def cmd_config_get(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """回 ``config`` 消息（第 6 节完整对象）。"""
    config = ctx.get_config()
    await ctx.send_msg(conn, "config", {"config": config})


async def cmd_config_put(ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]) -> None:
    """WS 路径 payload 是 ``{"config":{...}}``；回 ``ack`` + ``config`` 两条。"""
    config = _config_object(_payload_of(env), "config")
    saved = ctx.put_config(config)
    await _ack(ctx, conn, env)
    await ctx.send_msg(conn, "config", {"config": saved})


# ---------------------------------------------------------------------------
# V2 追加层 §5：屏幕亮度
# ---------------------------------------------------------------------------
async def cmd_brightness_set(
    ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]
) -> None:
    """``brightness.set{value:0..1}`` → ``ack``（extra 含 applied/method/value/best_effort）。

    WMI / gamma 都是同步 Win32 / COM 调用，放 ``run_in_executor``（§5 / §9），
    绝不阻塞事件循环、绝不写 JSON 日志。§5 高频约定「后端可不逐条回 ack
    （但回也无妨，前端不依赖）」，这里按 §5.1 表格回一条 ack 便于排查。
    """
    value = _brightness_value(_payload_of(env))
    loop = asyncio.get_running_loop()
    extra = await loop.run_in_executor(None, brightness.set, value)
    await _ack(ctx, conn, env, extra)


async def cmd_brightness_get(
    ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]
) -> None:
    """``brightness.get{}`` → 下发消息 ``brightness {value,method,best_effort}``。"""
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, brightness.get)
    await ctx.send_msg(conn, "brightness", payload)


# ---------------------------------------------------------------------------
# V2 追加层 §6：语音（muse = https://muse.ai/）
# ---------------------------------------------------------------------------
async def cmd_voice_start(
    ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]
) -> None:
    """``voice.start{url?}`` → ``ack``：**只建立语音会话，不打开任何页面**。

    历史：这一版曾经在这里用 ``webbrowser.open`` 打开 muse 页面。实测
    ``https://muse.ai/`` 是 Meta Muse 的落地页，**没有可用的语音输入按钮**，
    打开它既帮不上忙，还会突然弹窗打断用户正在做的事 —— YG 明确要求
    「长按语音还是会弹出 muse，关掉」。

    所以现在：``voice.start`` 完全不碰浏览器，只把会话标记为进行中，
    真正的语音入口是手机端自己：手机请求麦克风权限 → 浏览器识别成文字 →
    ``voice.text{final:true}`` → 后端把文字注入电脑焦点输入框。

    为了向后兼容，payload 里带 ``url`` 也**一律忽略**（不会打开页面）。
    """
    extra = await _ack_voice_session_open(ctx)
    await _ack(ctx, conn, env, extra)


async def _ack_voice_session_open(ctx: "AppContext") -> dict[str, Any]:
    """把语音会话置为进行中（纯内存，不启动任何进程/窗口）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _voice_session_open_sync)


def _voice_session_open_sync() -> dict[str, Any]:
    try:
        return voice.session_open()
    except Exception as exc:  # 任何异常一律降级，不掀翻 WS
        return {
            "opened": True,
            "page_opened": False,
            "focused": False,
            "error": str(exc),
            "best_effort": True,
        }


async def cmd_voice_text(
    ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]
) -> None:
    """``voice.text{text,final}``：``final:false`` 不逐条 ack；``final:true`` 回 ack。

    ``final:true`` 由 ``voice.text`` 复用 V1 ``input_win.key_type`` +
    ``input_win.key_press(["enter"])`` 把文本送进焦点输入框并提交。
    """
    payload = _payload_of(env)
    raw_text = payload.get("text")
    if not isinstance(raw_text, str):
        raise BadPayloadError("text must be string")
    final = payload.get("final", False)
    if not isinstance(final, bool):
        raise BadPayloadError("final must be boolean")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, voice.text, raw_text, final)
    if result.get("acked"):
        await _ack(
            ctx,
            conn,
            env,
            {
                "text_length": result.get("text_length"),
                "injected": result.get("injected"),
                "sent": result.get("sent"),
                "best_effort": True,
            },
        )
    # final:false 不回任何消息：高频预览，不逐条 ack、不写 JSON 日志


async def cmd_voice_stop(
    ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]
) -> None:
    """``voice.stop{}`` → ``ack``：结束会话、标记已发送；绝不关闭 / 导航 muse。"""
    loop = asyncio.get_running_loop()
    extra = await loop.run_in_executor(None, voice.stop)
    await _ack(ctx, conn, env, extra)


Handler = Callable[["AppContext", "ClientConn", dict[str, Any]], Awaitable[None]]

HANDLERS: dict[str, Handler] = {
    "hello": cmd_hello,
    "ping": cmd_ping,
    "status.get": cmd_status_get,
    "mouse.move": cmd_mouse_move,
    "mouse.down": cmd_mouse_down,
    "mouse.up": cmd_mouse_up,
    "mouse.click": cmd_mouse_click,
    "mouse.scroll": cmd_mouse_scroll,
    "mouse.pinch": cmd_mouse_pinch,
    "window.switch": cmd_window_switch,
    "key.press": cmd_key_press,
    "key.type": cmd_key_type,
    "hotkey.run": cmd_hotkey_run,
    "media.control": cmd_media_control,
    "media.seek": cmd_media_seek,
    "media.state": cmd_media_state,
    "app.list": cmd_app_list,
    "app.scan": cmd_app_scan,
    "app.activate": cmd_app_activate,
    "app.release": cmd_app_release,
    "config.get": cmd_config_get,
    "config.put": cmd_config_put,
    # V2 追加层 §5 / §6（只新增，不改义以上 22 条）
    "brightness.set": cmd_brightness_set,
    "brightness.get": cmd_brightness_get,
    "voice.start": cmd_voice_start,
    "voice.text": cmd_voice_text,
    "voice.stop": cmd_voice_stop,
}


def implemented_commands() -> list[str]:
    """返回全部已注册命令名，供 selftest / README 自检使用。"""
    return sorted(HANDLERS)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
async def dispatch(
    ctx: "AppContext", conn: "ClientConn", env: dict[str, Any]
) -> None:
    """分发一条客户端消息；异常统一转成 ``error`` 下发。

    ``ws_hub`` 已在解析信封时保证 ``type`` 是 string、``payload`` 是 object、
    ``ts`` 是 number，本函数只负责命令级路由与执行。
    """
    msg_type = env.get("type")
    handler = HANDLERS.get(msg_type) if isinstance(msg_type, str) else None
    if handler is None:
        await ctx.send_error(
            conn,
            env.get("id"),
            "UNKNOWN_COMMAND",
            f"未知命令: {msg_type}",
        )
        return
    try:
        await handler(ctx, conn, env)
    except BadPayloadError as exc:
        await ctx.send_error(conn, env.get("id"), BadPayloadError.code, str(exc))
    except BadValueError as exc:
        # 必须先于 (ValueError, TypeError, KeyError) 捕获，保证 code=BAD_VALUE
        await ctx.send_error(conn, env.get("id"), BadValueError.code, str(exc))
    except input_win.UnknownKeyError as exc:
        await ctx.send_error(conn, env.get("id"), input_win.UnknownKeyError.code, str(exc))
    except input_win.InputUnavailableError as exc:
        await ctx.send_error(
            conn, env.get("id"), input_win.InputUnavailableError.code, str(exc)
        )
    except NotFoundError as exc:
        await ctx.send_error(conn, env.get("id"), NotFoundError.code, str(exc))
    except (ValueError, TypeError, KeyError) as exc:
        # 底层参数/注册表类错误，按参数错误归类，避免泄露堆栈给前端
        await ctx.send_error(conn, env.get("id"), BadPayloadError.code, str(exc))
    except OSError as exc:
        await ctx.send_error(conn, env.get("id"), "INPUT_ERROR", str(exc))
    except Exception as exc:  # 兜底：单条消息异常不影响 WS 连接
        await ctx.send_error(
            conn,
            env.get("id"),
            "INTERNAL_ERROR",
            f"{type(exc).__name__}: {exc}",
        )
