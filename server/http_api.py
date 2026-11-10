"""aiohttp 应用装配：静态托管 ``web/`` + REST ``/api/`` + WS ``/ws``。

对应 PROTOCOL 第 1 / 7 节：

* ``GET /`` → ``web/index.html``；``GET /<file>`` → ``web/<file>``，
  带正确 MIME（``.js``→``text/javascript``、``.css``→``text/css``）。
  前端由另一路并行开发；若启动时 ``web/index.html`` 尚不存在，``GET /``
  返回一段简短中文说明页而不是 500，**服务启动不受影响**。
* 全部 ``/api/`` 端点；错误统一 ``{ok:false,code,message}``，
  HTTP 400 / 404 / 500 对应语义。
* 所有响应带 ``Access-Control-Allow-Origin: *``，并支持 OPTIONS 预检。
* V2 追加层 §4：新增 ``GET /api/apps/icon?path=``（32×32 PNG / 404 NO_ICON），
  ``GET /api/apps`` 与 ``POST /api/apps/scan`` 的每项派生
  ``has_local`` / ``route`` / 可选 ``icon_url``；``POST /api/apps`` 原样保留。

``create_app(ctx)`` 是主入口：main.py 只需拿返回的 ``web.Application`` 交给
``web.run_app`` / ``AppRunner``。若 ``ctx.hub`` 未初始化，这里自动补建
``WsHub``，保证单进程内 hub 与 app 一一对应。
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
from email.utils import formatdate
from pathlib import Path
from typing import Any

from aiohttp import web

import apps_icons
import media
import qr
from ws_hub import WsHub

# 静态根：server/ 的上级就是项目根，web/ 与 server/ 同级
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = _PROJECT_ROOT / "web"

# 单请求体上限：config.json 很小，放宽到 4 MiB 便于后续追加字段
CLIENT_MAX_SIZE = 4 << 20

# 显式 MIME 表（优先级高于 mimetypes，确保 .js/.css 严格符合协议）
_EXPLICIT_MIME: dict[str, str] = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain",
}

# 需要追加 charset=utf-8 的文本类 MIME
_TEXT_MIME_PREFIXES = ("text/", "application/json", "application/javascript", "image/svg+xml")

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Couch Commander · 沙发指挥官</title>
<style>
  body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #0f1115;
         color: #e8eaf0; margin: 0; padding: 12vh 6vw; line-height: 1.8; }
  h1 { font-size: 1.4rem; color: #6ea8ff; margin-bottom: .6em; }
  code { background: #1c2029; padding: .15em .45em; border-radius: 4px; }
  p { max-width: 42em; }
</style>
</head>
<body>
<h1>后端服务已就绪 ✅</h1>
<p>这是 <code>Couch Commander · 沙发指挥官</code> 项目的 Windows 后台服务。</p>
<p>前端页面 <code>web/index.html</code> 由另一路会话并行开发，当前尚未写入。</p>
<p>把手机与电脑连到同一 Wi-Fi，扫描电脑弹出的二维码即可打开控制页。</p>
<p>接口自检：<code>GET /api/status</code>、<code>GET /api/config</code>、<code>GET /api/qr.png</code>。</p>
</body>
</html>
"""


def _dumps(obj: Any) -> str:
    """统一序列化：中文不转义。"""
    return json.dumps(obj, ensure_ascii=False)


def _json(data: Any, status: int = 200) -> web.Response:
    """构造 JSON 响应，显式固定 ``application/json; charset=utf-8``。"""
    response = web.json_response(data, status=status, dumps=_dumps)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    return response


def _error(code: str, message: str, status: int) -> web.Response:
    """PROTOCOL 第 7 节：错误统一 ``{ok:false,code,message}``。"""
    return _json({"ok": False, "code": code, "message": message}, status)


# ---------------------------------------------------------------------------
# CORS 中间件
# ---------------------------------------------------------------------------
@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """给所有响应加 ``Access-Control-Allow-Origin: *``，并短路 OPTIONS 预检。"""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            # 路由层抛出的 404/405 也要带 CORS 头
            try:
                exc.headers["Access-Control-Allow-Origin"] = "*"
                exc.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
                exc.headers["Access-Control-Allow-Headers"] = "Content-Type"
            except Exception:
                pass
            raise
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ---------------------------------------------------------------------------
# REST handlers
# ---------------------------------------------------------------------------
def _ctx(request: web.Request) -> Any:
    return request.app["ctx"]


async def api_status(request: web.Request) -> web.Response:
    """``GET /api/status``（PROTOCOL 第 7 节完整字段）。"""
    return _json(_ctx(request).rest_status())


async def api_qr_png(request: web.Request) -> web.Response:
    """``GET /api/qr.png``：返回内存 PNG，内容 ``http://<LAN_IP>:<PORT>/``。"""
    ctx = _ctx(request)
    png = ctx.qr_png
    if not png:
        try:
            png = qr.build_png(qr.make_url(ctx.lan_ip, ctx.port))
            ctx.qr_png = png
        except Exception as exc:
            return _error("QR_ERROR", str(exc), 500)
    response = web.Response(body=png)
    response.headers["Content-Type"] = "image/png"
    response.headers["Cache-Control"] = "no-store"
    return response


async def api_apps(request: web.Request) -> web.Response:
    """``GET /api/apps``：已安装应用 + ``config.apps`` 合并去重 + V2 图标元数据。

    V1 字段全部保留；V2 §4.2 追加 ``has_local`` / ``route`` / 可选 ``icon_url``。
    合并与图标元数据派生都放线程池，避免 Win32 调用阻塞事件循环。
    """
    ctx = _ctx(request)
    loop = asyncio.get_running_loop()

    def _work() -> list[dict[str, Any]]:
        installed = ctx.app_store.cached()
        return apps_icons.decorate_apps(ctx.merged_apps(installed), installed)

    try:
        apps_list = await loop.run_in_executor(None, _work)
    except Exception as exc:
        return _error("APPS_ERROR", str(exc), 500)
    return _json({"apps": apps_list})


async def api_apps_scan(request: web.Request) -> web.Response:
    """``POST /api/apps/scan``：强制重新扫描，返回扫描后的合并结果（含 V2 元数据）。"""
    ctx = _ctx(request)
    loop = asyncio.get_running_loop()

    def _work() -> list[dict[str, Any]]:
        installed = ctx.app_store.scan()
        return apps_icons.decorate_apps(ctx.merged_apps(installed), installed)

    try:
        apps_list = await loop.run_in_executor(None, _work)
    except Exception as exc:
        return _error("SCAN_ERROR", str(exc), 500)
    return _json({"apps": apps_list})


async def api_apps_icon(request: web.Request) -> web.Response:
    """``GET /api/apps/icon?path=``（V2 §4.1）：返回 32×32 PNG 图标。

    成功 ``200 image/png``；无图标 / 路径不存在 / 提取失败 / 缺可选依赖 →
    ``404 {"ok":false,"code":"NO_ICON","message":...}``，**绝不 500、绝不崩服务**。
    图标提取是同步 Win32 IO，放 ``run_in_executor``；所有第三方调用在
    ``apps_icons.icon_png`` 内部逐层 ``try/except`` 降级。
    """
    raw_path = request.query.get("path", "")
    if not raw_path:
        return _error("NO_ICON", "缺少 path 参数", 404)
    loop = asyncio.get_running_loop()
    try:
        png = await loop.run_in_executor(None, apps_icons.icon_png, raw_path)
    except Exception as exc:
        # run_in_executor / 提取层兜底：任何异常都转成 404，不掀翻服务
        return _error("NO_ICON", f"图标提取失败: {exc}", 404)
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return _error("NO_ICON", "路径不存在或无可用图标（可选依赖缺失 / 非 Windows）", 404)
    response = web.Response(body=png)
    response.headers["Content-Type"] = "image/png"
    response.headers["Cache-Control"] = "no-store"
    return response


async def api_apps_add(request: web.Request) -> web.Response:
    """``POST /api/apps``：Lead 集成补丁（PROTOCOL v1.0 允许的**追加端点**）。

    前端 ``web/js/net.js`` 的 ``addApp()`` 与 ``web/js/settings.js``
    「设置 → 应用 → 添加到应用页」实际调用本端点，请求体是**单个** app 对象
    ``{id,name,path,icon,color}``（不包一层）。契约第 7 节原本只登记了
    ``GET /api/apps`` 与 ``POST /api/apps/scan``；此处补齐后，设置页添加应用
    不再 404，且**不改变**那两个端点原有的语义。

    行为：按 ``id`` 去重/覆盖追加进 ``config.apps``，整份 config 原子写回，
    返回 ``{"ok":true,"apps":[...当前完整列表...]}``。缺少 id 时按
    ``app-<n>`` 递增生成（与 config_store.next_app_id 同规则）。前端成功后
    还会再发一次 ``PUT /api/config`` 全量覆盖，两次写入内容一致 → 幂等。
    """
    import copy
    import re

    try:
        raw = await request.text()
        body = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        return _error("BAD_JSON", f"请求体不是合法 JSON: {exc}", 400)
    except Exception as exc:
        return _error("BAD_JSON", str(exc), 400)

    if not isinstance(body, dict):
        return _error("BAD_PAYLOAD", "请求体必须是单个 app 对象", 400)
    # 容错：若调用方误包了一层 {"app":{...}}，自动解包
    if "app" in body and not any(k in body for k in ("id", "name", "path")):
        inner = body.get("app")
        if isinstance(inner, dict):
            body = inner

    name = str(body.get("name") or "").strip()
    if not name:
        return _error("BAD_PAYLOAD", "app.name 不能为空", 400)

    try:
        ctx = _ctx(request)
        config = copy.deepcopy(ctx.get_config())
        if not isinstance(config, dict):
            return _error("CONFIG_ERROR", "配置读取失败", 500)
        apps = config.get("apps")
        if not isinstance(apps, list):
            apps = []
            config["apps"] = apps
        item = {
            "id": str(body.get("id") or ""),
            "name": name,
            "path": str(body.get("path") or ""),
            "icon": str(body.get("icon") or "📱"),
            "color": str(body.get("color") or "#2C2C35"),
        }
        # 无 id → 生成 app-<n> 递增 id（与 config_store.next_app_id 一致）
        if not item["id"]:
            used = [
                int(m.group(1))
                for a in apps
                if isinstance(a, dict)
                for m in [re.match(r"^app-(\d+)$", str(a.get("id", "")))]
                if m
            ]
            item["id"] = f"app-{(max(used) + 1) if used else 1}"
        # 按 id 去重/覆盖
        replaced = False
        for i, existing in enumerate(apps):
            if isinstance(existing, dict) and str(existing.get("id", "")) == item["id"]:
                apps[i] = item
                replaced = True
                break
        if not replaced:
            apps.append(item)
        ctx.put_config(config)
    except ValueError as exc:
        return _error("BAD_PAYLOAD", str(exc), 400)
    except Exception as exc:
        return _error("APPS_ERROR", str(exc), 500)
    return _json({"ok": True, "apps": apps})


async def api_config_get(request: web.Request) -> web.Response:
    """``GET /api/config``：返回第 6 节完整对象（**不包一层**）。"""
    try:
        config = _ctx(request).get_config()
    except Exception as exc:
        return _error("CONFIG_ERROR", str(exc), 500)
    return _json(config)


async def api_config_put(request: web.Request) -> web.Response:
    """``PUT /api/config``：请求体就是完整 config 对象本身。

    返回 ``{"ok":true,"config":{...}}``。写入与 WS ``config.put`` 用同一个
    ``ConfigStore``、同一份 ``config.json``。为容错，若调用方误把对象包在
    ``{"config":{...}}`` 里（且没有任何第 6 节顶层键），也自动解包。
    """
    try:
        raw = await request.text()
        body = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        return _error("BAD_JSON", f"请求体不是合法 JSON: {exc}", 400)
    except Exception as exc:
        return _error("BAD_JSON", str(exc), 400)

    if not isinstance(body, dict):
        return _error("BAD_PAYLOAD", "请求体必须是 config 对象", 400)
    top_keys = ("version", "pages", "active_page_id", "hotkeys", "apps", "touchpad")
    if "config" in body and not any(key in body for key in top_keys):
        inner = body.get("config")
        if isinstance(inner, dict):
            body = inner
    if not isinstance(body, dict):
        return _error("BAD_PAYLOAD", "config 必须是对象", 400)

    try:
        saved = _ctx(request).put_config(body)
    except ValueError as exc:
        return _error("BAD_PAYLOAD", str(exc), 400)
    except Exception as exc:
        return _error("CONFIG_ERROR", str(exc), 500)
    return _json({"ok": True, "config": saved})


async def api_media(request: web.Request) -> web.Response:
    """``GET /api/media``：SMTC 读不到时按协议返回默认字段。"""
    loop = asyncio.get_running_loop()
    try:
        current = await loop.run_in_executor(None, media.state)
    except Exception as exc:
        return _error("MEDIA_ERROR", str(exc), 500)
    return _json(current)


async def api_voice_ws(request: web.Request) -> web.WebSocketResponse:
    """``GET /voice-ws``：已下线（410，保留路径以免旧手机页面拿到 404 困惑）。

    早期设计想把手机 PCM 直接喂进 muse 的麦克风；实测 ``https://muse.ai/``
    当前是 Meta Muse 落地页、没有可用的语音输入按钮，那条链路不成立，已整体
    移除。语音改由手机端自己识别成文字后走 ``voice.text`` 注入。
    """
    raise web.HTTPGone(text="voice-ws retired; use voice.text instead")


# ---------------------------------------------------------------------------
# 静态托管
# ---------------------------------------------------------------------------
def _content_type(path: Path) -> str:
    """返回带可选 charset 的完整 Content-Type。"""
    suffix = path.suffix.casefold()
    base = _EXPLICIT_MIME.get(suffix)
    if base is None:
        guessed, _encoding = mimetypes.guess_type(path.name)
        base = guessed or "application/octet-stream"
    if base.startswith(_TEXT_MIME_PREFIXES):
        return f"{base}; charset=utf-8"
    return base


async def static_handler(request: web.Request) -> web.StreamResponse:
    """``GET /`` 与 ``GET /<file>``：从 ``web/`` 读文件，防目录穿越。"""
    tail = request.match_info.get("tail", "")

    # 兜底：即使 catch-all 抢在 /api 前面，也不会把 API 当静态文件
    if tail == "ws" or tail.startswith("ws/") or tail.startswith("api/"):
        return _error("NOT_FOUND", f"未知接口: /{tail}", 404)

    rel = tail.strip("/")
    if not rel:
        rel = "index.html"

    try:
        root = WEB_ROOT.resolve()
        target = (root / rel).resolve()
        target.relative_to(root)  # 路径穿越会抛 ValueError
    except (ValueError, OSError):
        return _error("NOT_FOUND", f"非法路径: {rel}", 404)

    if target.is_dir():
        target = target / "index.html"

    if not target.is_file():
        is_root = tail in ("", "/") or rel == "index.html"
        if is_root:
            # 前端并行开发中：web/index.html 尚不存在也要给可读页面，不能 500
            response = web.Response(text=_PLACEHOLDER_HTML)
            response.headers["Content-Type"] = "text/html; charset=utf-8"
            return response
        return _error("NOT_FOUND", f"未找到静态文件: {rel}", 404)

    try:
        data = target.read_bytes()
        stat = target.stat()
    except OSError as exc:
        return _error("STATIC_ERROR", str(exc), 500)

    response = web.Response(body=data)
    response.headers["Content-Type"] = _content_type(target)
    # ------------------------------------------------------------------
    # 手机端必须能拿到「刚刚改过的」前端文件。
    #
    # 之前这里没有任何缓存头，iPhone Safari 会按启发式缓存把 dpad.js /
    # *.css 长期留在本地 —— 服务端改了代码，手机上跑的还是旧脚本，于是
    # 「修了但没生效」。
    #
    # no-cache 表示「可以存，但每次必须回源校验」；配合强 ETag（用
    # mtime_ns + size 构造，文件一改必然变）与 Last-Modified，正常访问
    # 走 304 几乎零开销，文件一变立刻就是新内容。比 no-store 更省流量，
    # 也比「什么都不发」可靠。
    # ------------------------------------------------------------------
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    response.headers["ETag"] = etag
    response.headers["Last-Modified"] = formatdate(stat.st_mtime, usegmt=True)
    response.headers["Cache-Control"] = "no-cache"
    if_none_match = request.headers.get("If-None-Match")
    if if_none_match and etag in {part.strip() for part in if_none_match.split(",")}:
        not_modified = web.Response(status=304)
        not_modified.headers["ETag"] = etag
        not_modified.headers["Cache-Control"] = "no-cache"
        return not_modified
    return response


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------
def create_app(ctx: Any) -> web.Application:
    """创建并装配单进程 aiohttp 应用（静态 + REST + WS）。

    路由注册顺序：``/ws`` → 各 ``/api/`` 具体端点 → 静态 catch-all。
    aiohttp 按注册顺序线性匹配，因此具体端点永远优先于 catch-all。
    """
    if ctx.hub is None:
        ctx.hub = WsHub(ctx)
    hub = ctx.hub

    app = web.Application(
        middlewares=[cors_middleware],
        client_max_size=CLIENT_MAX_SIZE,
    )
    app["ctx"] = ctx

    # WS 与后台任务（心跳 / 状态广播）
    app.router.add_get("/ws", hub.handle_ws)
    app.on_startup.append(hub.on_startup)
    app.on_cleanup.append(hub.on_cleanup)

    # REST（PROTOCOL 第 7 节逐条）
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/qr.png", api_qr_png)
    app.router.add_get("/api/apps", api_apps)
    app.router.add_post("/api/apps/scan", api_apps_scan)
    # Lead 集成补丁：前端「设置→应用→添加到应用页」实调 POST /api/apps
    app.router.add_post("/api/apps", api_apps_add)
    # V2 追加层 §4.1：图标端点排在 POST /api/apps 之后，
    # 与 selftest.EXTRA_ENDPOINTS / rest_endpoints() 的登记顺序一致。
    app.router.add_get("/api/apps/icon", api_apps_icon)
    app.router.add_get("/api/config", api_config_get)
    app.router.add_put("/api/config", api_config_put)
    app.router.add_get("/api/media", api_media)
    # V2.2：手机麦克风 → muse 自带语音输入的转发通道（二进制 PCM）。
    # 手机端连 ?role=phone 推帧；muse 页面里的注入脚本连 ?role=muse 收帧。
    app.router.add_get("/voice-ws", api_voice_ws)

    # 静态托管（含 GET /）
    app.router.add_get("/", static_handler)
    app.router.add_get("/{tail:.*}", static_handler)

    return app


def rest_endpoints() -> list[str]:
    """返回全部已注册 REST 端点，供 selftest / README 自检使用。"""
    return [
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
    ]
