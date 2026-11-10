"""WebSocket 中枢：连接管理、消息分发、心跳超时检测、状态广播。

对应 PROTOCOL 第 2 / 4 / 8 节。本模块同时定义全局共享运行时上下文
``AppContext``（供 ``http_api`` / ``dispatcher`` / ``main`` 共用），这样不必
额外新增任务清单外的 .py 文件。

依赖关系（单向，零循环 import）：

* ``ws_hub`` 运行时 ``import dispatcher``；
* ``dispatcher`` / ``http_api`` 仅在 ``TYPE_CHECKING`` 下引用 ``AppContext``，
  运行时通过 duck typing 访问 ``ctx``，不 import 本模块。

低延迟约束（PROTOCOL 第 8 节）：

* ``mouse.move`` / ``mouse.scroll`` 由 dispatcher 直接进 ``SendInput``，
  本模块对这两类消息不做任何 JSON 序列化日志；
* 心跳与状态广播走独立低频通道；下发改为**有界发件箱 + 每条连接一个专职
  写协程**（FIFO，永不阻塞调用方、永不取消进行中的写帧），队列满时按优先级
  丢弃，绝不把半帧写进 WS 流；
* 服务端**不做任何基于空闲时长的主动关闭**：连接只在对端真实关闭 / TCP 断开
  时才被清理，`IDLE_TIMEOUT_S` 仅为兼容历史自检保留。
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

import dispatcher
import logs
from apps import AppStore
from config_store import ConfigStore

if TYPE_CHECKING:
    pass

# 心跳检查周期（秒）；客户端协议约定每 5s 发一次 ping
HEARTBEAT_INTERVAL_S = 5.0
# 历史常量：V1 曾按「连续 2 个 5s 周期无消息」关闭连接（≈10s）。iPhone Safari
# 锁屏 / 切后台会把 setInterval 大幅节流甚至冻结，ping 发不出来，该判定会误杀
# 健康连接（用户反馈「开一段时间就断连」）。保留常量仅为兼容既有文档/自检引用，
# 关闭判定已改为下面的「绝对空闲超时」。
MISSED_HEARTBEAT_LIMIT = 2
# 绝对空闲超时（秒）：距上次收到该客户端任意消息超过该值才判定僵死并关闭。
# 覆盖手机锁屏、后台唤醒、局域网短暂抖动；前台 5s 一次的 ping 永不触发。
IDLE_TIMEOUT_S = 120.0
# 状态广播周期（PROTOCOL：每 10s 一次）
STATUS_BROADCAST_INTERVAL_S = 10.0
# 低频通道（状态广播 / 心跳）单条消息的下发超时。
# 已废弃：写帧改为「有界发件箱 + 专职写协程」，不再用 wait_for 取消写帧
# （取消会写出半帧，浏览器按协议错误直接断开 = 用户感知的「莫名断连」）。
# 保留常量仅为兼容既有文档 / 自检引用。
LOW_PRIORITY_TIMEOUT_S = 0.5
# 普通下发的历史兜底超时，同样仅为兼容保留
NORMAL_TIMEOUT_S = 2.0
# 每条连接发件箱的最大条数：只为吸收网络抖动下的突发，正常使用远达不到
OUTBOX_MAX_MESSAGES = 256
# WS 单条消息最大字节，防止异常大包
MAX_WS_MESSAGE_BYTES = 1 << 20  # 1 MiB

# 高频命令：不回 ack、不做 JSON 序列化日志、直接进 SendInput
HIGH_FREQ_TYPES: frozenset[str] = frozenset({"mouse.move", "mouse.scroll"})


def now_ms() -> int:
    """当前 UTC 毫秒时间戳（消息信封 ``ts``）。"""
    return int(time.time() * 1000)


def _dumps(obj: Any) -> str:
    """WS/REST 统一序列化：中文不转义，保持 UTF-8 可读。"""
    return json.dumps(obj, ensure_ascii=False)


def make_message(msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """构造服务端下发信封 ``{type, payload, ts}``。

    按 PROTOCOL 第 2 节示例，服务端下发消息**顶层不带 ``id``**；低频命令的
    ``id`` 由 dispatcher 放进 ``ack`` / ``error`` 的 ``payload`` 里原样回带。
    """
    return {
        "type": msg_type,
        "payload": payload if isinstance(payload, dict) else {},
        "ts": now_ms(),
    }


class AppContext:
    """全局共享运行时上下文（单进程内唯一实例）。"""

    def __init__(
        self,
        *,
        device: str,
        lan_ip: str,
        port: int,
        config_store: ConfigStore,
        app_store: AppStore,
        version: str = "1.0",
        qr_png: bytes = b"",
        url: str = "",
        https: bool = False,
    ) -> None:
        self.device = device
        self.lan_ip = lan_ip
        self.port = int(port)
        self.version = version
        self.config_store = config_store
        self.app_store = app_store
        self.qr_png = qr_png
        # 是否以 HTTPS 提供服务：决定二维码/访问 URL 的 scheme，也决定前端
        # 能否请求麦克风权限（见 certs.py）。默认 False 保持 V1 行为。
        self.https = bool(https)
        scheme = "https" if self.https else "http"
        self.url = url or f"{scheme}://{lan_ip}:{port}/"

        self.hub: "WsHub | None" = None
        # 应用锁定状态（app.activate / app.release）
        self.locked_app_id: str | None = None
        self.active_app: str | None = None
        # 状态栏显示的延迟估计（best-effort，见 README 已知限制）
        self.latency_ms = 0
        self._start_monotonic = time.monotonic()

    # ------------------------------------------------------------------
    # 协议 payload 构造
    # ------------------------------------------------------------------
    def hello_payload(self) -> dict[str, Any]:
        """PROTOCOL 第 4 节 ``hello``。"""
        return {
            "device": self.device,
            "version": self.version,
            "lan_ip": self.lan_ip,
            "port": self.port,
        }

    def status_payload(self) -> dict[str, Any]:
        """PROTOCOL 第 4 节 ``status``。"""
        return {
            "connected": True,
            "latency_ms": self.latency_ms,
            "locked_app_id": self.locked_app_id,
            "active_app": self.active_app,
            "service_up": True,
        }

    def rest_status(self) -> dict[str, Any]:
        """PROTOCOL 第 7 节 ``GET /api/status``。"""
        return {
            "connected": True,
            "device": self.device,
            "lan_ip": self.lan_ip,
            "port": self.port,
            "version": self.version,
            "uptime_ms": int((time.monotonic() - self._start_monotonic) * 1000),
            "clients": len(self.hub.conns) if self.hub is not None else 0,
        }

    def record_ping(self, client_ts: Any) -> None:
        """用客户端 ``ping.ts`` 与服务端时钟差粗略估计延迟。

        两端都用 UTC 毫秒时，差值约等于单向网络延迟；时钟不同步（差值超
        2000ms）时保持上次值，避免出现荒谬的「99999ms」。这是 best-effort
        显示值，真实 RTT 由前端 ``net.js`` 自行计算。
        """
        try:
            delta = now_ms() - float(client_ts)
        except (TypeError, ValueError):
            return
        if 0 <= delta < 2000:
            self.latency_ms = int(delta)

    # ------------------------------------------------------------------
    # 配置与应用（REST / WS 共用同一份文件与缓存）
    # ------------------------------------------------------------------
    def get_config(self) -> dict[str, Any]:
        return self.config_store.load()

    def put_config(self, config: dict[str, Any]) -> dict[str, Any]:
        return self.config_store.save(config)

    def merged_apps(
        self, installed: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """``config.apps`` + 已安装应用，合并去重。"""
        if installed is None:
            installed = self.app_store.cached()
        return AppStore.merged(installed, self.get_config().get("apps", []))

    def set_lock(self, app_id: str, name: str) -> None:
        self.locked_app_id = app_id
        self.active_app = name

    def clear_lock(self) -> None:
        self.locked_app_id = None
        self.active_app = None

    # ------------------------------------------------------------------
    # 下发原语（全部经 hub.deliver 串行化 + 超时丢弃）
    # ------------------------------------------------------------------
    async def send_msg(
        self,
        conn: "ClientConn",
        msg_type: str,
        payload: dict[str, Any],
        *,
        low: bool = False,
    ) -> bool:
        if self.hub is None:
            return False
        return await self.hub.deliver(conn, make_message(msg_type, payload), low=low)

    async def send_ack(
        self, conn: "ClientConn", msg_id: str | None, extra: dict[str, Any] | None = None
    ) -> bool:
        """低频命令完成：``ack`` 的 payload 原样带回 ``id``。"""
        payload = {"id": msg_id, "ok": True, "extra": extra or {}}
        return await self.send_msg(conn, "ack", payload)

    async def send_error(
        self, conn: "ClientConn", msg_id: str | None, code: str, message: str
    ) -> bool:
        return await self.send_msg(
            conn, "error", {"id": msg_id, "code": code, "message": message}
        )

    async def send_status(self, conn: "ClientConn", *, low: bool = False) -> bool:
        return await self.send_msg(conn, "status", self.status_payload(), low=low)

    async def broadcast_status(self) -> None:
        if self.hub is not None:
            await self.hub.broadcast_status()


class ClientConn:
    """一条 WS 连接：有界发件箱 + 专职写协程 + 心跳计数。

    **为什么不再是「加锁 + wait_for 超时」**：旧实现用
    ``asyncio.wait_for(conn.send(...), 0.5s)`` 包住写帧，超时会取消
    ``send_json``。取消可能发生在「帧头已写、载荷未写」之间，WS 流里就留下
    半帧；浏览器按 RFC 6455 判定协议错误并直接断开（Close 1002/1006）。
    手机网络短暂抖动、Safari/Chrome 后台限流都会让低频 0.5s 超时命中，
    表现为「人没动过手机却莫名其妙断连」，且服务端日志里看不到任何主动关闭。

    现在的做法：生产者只往**有界队列**里放消息（永不阻塞、永不取消写帧），
    由本连接专属的写协程按 FIFO 真正下发。队列满时按优先级丢弃：
    低频（status/pong 等）直接丢旧的，普通消息丢队首腾位；丢包只累加
    ``backlog_drops``，绝不影响已写出的帧，因此流始终保持完整。
    """

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.ws = ws
        # 保留字段：外部自检/文档仍引用；实际串行化由专职写协程保证
        self.send_lock = asyncio.Lock()
        self.last_seen = time.monotonic()
        self.missed_heartbeats = 0
        self.backlog_drops = 0
        self.sent_frames = 0
        self._queue: asyncio.Queue[tuple[dict[str, Any], bool] | None] = asyncio.Queue(
            maxsize=OUTBOX_MAX_MESSAGES
        )
        self._writer: asyncio.Task[None] | None = None
        self._closing = False

    def touch(self) -> None:
        self.last_seen = time.monotonic()
        self.missed_heartbeats = 0

    # -- 写协程 --------------------------------------------------------
    def start_writer(self) -> None:
        """在连接建立后启动专职写协程（幂等）。"""
        if self._writer is None and not self._closing:
            self._writer = asyncio.create_task(self._writer_loop(), name="ws-writer")

    async def _writer_loop(self) -> None:
        """按 FIFO 真正写帧；写操作一旦开始就不会被超时取消。"""
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                msg, low = item
                if self.ws.closed:
                    return
                await self.ws.send_json(msg, dumps=_dumps)
                self.sent_frames += 1
                if low:
                    drain = getattr(self.ws, "drain", None)
                    if drain is not None:
                        try:
                            await drain()
                        except Exception:
                            # 某些 aiohttp 版本对 WS 的 drain 不可用：消息已发出，
                            # 背压刷新失败不影响送达，忽略即可。
                            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            # 连接已坏（对端重置 / TLS 关闭）：结束写协程，交给 close 事件处理
            return

    def enqueue(self, msg: dict[str, Any], *, low: bool = False) -> bool:
        """把消息放进发件箱；永不阻塞、永不取消正在写的帧。"""
        if self.ws.closed or self._closing:
            return False
        try:
            self._queue.put_nowait((msg, low))
            return True
        except asyncio.QueueFull:
            pass
        # 队列满：低频消息直接丢弃（状态广播下一轮还会来）
        if low:
            self.backlog_drops += 1
            return False
        # 普通消息：丢队首最旧的一条腾位，保证 ack/error 这类关键消息能进队
        try:
            self._queue.get_nowait()
            self.backlog_drops += 1
        except Exception:
            pass
        try:
            self._queue.put_nowait((msg, low))
            return True
        except asyncio.QueueFull:
            self.backlog_drops += 1
            return False

    async def send(self, msg: dict[str, Any], *, low: bool) -> bool:
        """兼容旧调用点：等价于入队。"""
        return self.enqueue(msg, low=low)

    async def close_writer(self) -> None:
        """停止写协程（连接结束时调用）。

        ``CancelledError`` 自 Python 3.8 起继承 ``BaseException``，单独的
        ``except Exception`` 拦不住它 —— 必须显式吞掉，否则连接清理时会把
        取消异常抛进 ``handle_ws`` 的 finally，掩盖真正的关闭原因。
        """
        self._closing = True
        writer = self._writer
        self._writer = None
        if writer is not None:
            writer.cancel()
            try:
                await writer
            except (asyncio.CancelledError, Exception):  # noqa: B014  (BaseException)
                pass


class WsHub:
    """``/ws`` 连接管理、消息分发、心跳超时检测、每 10s 广播 status。"""

    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.conns: set[ClientConn] = set()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # 生命周期（由 main 注册到 app.on_startup / app.on_cleanup）
    # ------------------------------------------------------------------
    async def on_startup(self, app: web.Application) -> None:
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="ws-heartbeat"
        )
        self._status_task = asyncio.create_task(
            self._status_loop(), name="ws-status-broadcast"
        )
        print("[ws] 心跳与状态广播任务已启动", flush=True)

    async def on_cleanup(self, app: web.Application) -> None:
        for task in (self._heartbeat_task, self._status_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(
                t
                for t in (self._heartbeat_task, self._status_task)
                if t is not None
            ),
            return_exceptions=True,
        )
        self._heartbeat_task = None
        self._status_task = None
        for conn in list(self.conns):
            try:
                await conn.ws.close(code=1001, message=b"server shutdown")
            except Exception:
                pass
        self.conns.clear()
        print("[ws] 已关闭全部连接", flush=True)

    # ------------------------------------------------------------------
    # 下发
    # ------------------------------------------------------------------
    async def deliver(
        self, conn: ClientConn, msg: dict[str, Any], *, low: bool = False
    ) -> bool:
        """把一条消息放进该连接的发件箱。

        永不阻塞调用方、永不取消正在进行的写帧：真正的 ``send_json`` 由
        ``ClientConn`` 的专职写协程按 FIFO 执行。队列满时低频消息被丢弃、
        普通消息丢最旧一条，因此任何情况下都不会把半帧写到 WS 流里
        （旧实现用 ``wait_for`` 超时取消写帧，会切出半帧 → 浏览器断开，
        正是「开一段时间莫名其妙断连」的根因）。
        """
        return conn.enqueue(msg, low=low)

    async def broadcast_status(self) -> None:
        """给所有连接广播 ``status``（独立低频通道，逐连接独立超时）。"""
        msg = make_message("status", self.ctx.status_payload())
        for conn in list(self.conns):
            await self.deliver(conn, msg, low=True)

    # ------------------------------------------------------------------
    # /ws 处理
    # ------------------------------------------------------------------
    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(
            # 刻意保持 heartbeat=None：aiohttp 的原生心跳在收不到 PONG 时会
            # 主动 close，手机网络抖动 / 后台限流下同样是「误杀健康连接」。
            # 保活交给浏览器侧 5s 一次的 JSON ping（只用于显示延迟）与 TCP
            # 自身；服务端不做任何基于时间的主动关闭。
            heartbeat=None,
            autoping=True,
            max_msg_size=MAX_WS_MESSAGE_BYTES,
        )
        await ws.prepare(request)
        _enable_tcp_keepalive(request)
        conn = ClientConn(ws)
        conn.start_writer()
        self.conns.add(conn)
        peer = _peer_text(request)
        logs.event("ws.open", peer=peer, conns=len(self.conns))
        close_code: int | None = None
        close_reason = ""
        try:
            # 建连后立刻下发 hello（PROTOCOL 第 4 节）
            await self.deliver(conn, make_message("hello", self.ctx.hello_payload()))
            # 建连同时下发一次 status（第 4 节 status 触发条件之一）
            await self.deliver(
                conn, make_message("status", self.ctx.status_payload()), low=True
            )
            async for raw in ws:
                if raw.type == WSMsgType.TEXT:
                    conn.touch()
                    await self._on_text(conn, raw.data)
                elif raw.type == WSMsgType.BINARY:
                    conn.touch()
                    # 协议只定义文本 JSON，二进制帧忽略，不回 ack
                elif raw.type == WSMsgType.ERROR:
                    close_reason = f"WSMsgType.ERROR: {ws.exception() or ''}"
                    break
                elif raw.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSING,
                    WSMsgType.CLOSED,
                ):
                    if raw.data is not None:
                        close_code = raw.data
                    close_reason = "peer close frame"
                    break
        except asyncio.CancelledError:
            close_reason = "cancelled"
            raise
        except Exception as exc:  # 连接级异常：记下来，下一轮复现能直接定位
            close_reason = f"{type(exc).__name__}: {exc}"
            logs.error("ws.error", peer=peer, detail=close_reason)
        finally:
            self.conns.discard(conn)
            await conn.close_writer()
            # 迭代器正常结束（对端直接 RST / 半关）时上面所有分支都不会命中，
            # 这里以 aiohttp 记录的 close_code 为准，仍拿不到就记 eof。
            if close_code is None:
                try:
                    close_code = ws.close_code
                except Exception:
                    close_code = None
            logs.event(
                "ws.close",
                peer=peer,
                code=close_code if close_code is not None else "-",
                reason=close_reason or "eof",
                sent=conn.sent_frames,
                drops=conn.backlog_drops,
                conns=len(self.conns),
            )
        return ws

    async def _on_text(self, conn: ClientConn, data: str) -> None:
        """解析信封后交给 dispatcher；此处不做任何消息体日志。"""
        try:
            msg = json.loads(data)
        except Exception:
            await self.deliver(
                conn,
                make_message(
                    "error",
                    {"id": None, "code": "BAD_JSON", "message": "JSON 解析失败"},
                ),
            )
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            await self.deliver(
                conn,
                make_message(
                    "error",
                    {
                        "id": None,
                        "code": "BAD_PAYLOAD",
                        "message": "消息必须包含 string 类型的 type",
                    },
                ),
            )
            return
        payload = msg.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        raw_ts = msg.get("ts")
        ts = raw_ts if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool) else now_ms()
        raw_id = msg.get("id")
        msg_id = raw_id if isinstance(raw_id, str) else None
        envelope = {
            "type": msg["type"],
            "payload": payload,
            "ts": ts,
            "id": msg_id,
        }
        if msg["type"] == "ping":
            self.ctx.record_ping(ts)
        try:
            await dispatcher.dispatch(self.ctx, conn, envelope)
        except Exception as exc:
            # dispatcher 内部已统一回 error；这里兜底防止单条消息掀翻连接
            try:
                await self.deliver(
                    conn,
                    make_message(
                        "error",
                        {
                            "id": msg_id,
                            "code": "INTERNAL_ERROR",
                            "message": str(exc),
                        },
                    ),
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 后台任务
    # ------------------------------------------------------------------
    async def _heartbeat_loop(self) -> None:
        """每 5s 巡检：只把已关闭的连接移出集合，绝不因空闲主动关闭。

        修复「开一段时间手机端就断连 / 要保持电脑后台一直运行」：旧实现
        先后用「2 个 5s 周期无消息」≈10s、后来的「120s 绝对空闲」主动
        ``ws.close``，但 iPhone Safari 锁屏、切后台、局域网抖动都会让 ping
        停发或延迟，任何基于空闲时长的服务端主动关闭都会误杀健康连接。
        这里改为仅清理 ``ws.closed`` 连接；真正的断线交给 aiohttp / TCP 的
        close 事件，服务端本身永久常驻。``IDLE_TIMEOUT_S`` 常量保留仅为兼容
        历史自检，不再参与关闭判定。
        """
        try:
            ticks = 0
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                for conn in list(self.conns):
                    if conn.ws.closed:
                        self.conns.discard(conn)
                # 每 12 个周期（≈60s）写一条存活行：这是「进程还活着」的
                # 最强证据。同时带线程数与内存，用来抓要跑几小时才显形的
                # 缓慢增长问题（旧版本这类问题完全无迹可寻）。
                ticks += 1
                if ticks % 12 == 0:
                    try:
                        logs.alive(len(self.conns))
                    except Exception:
                        pass
        except asyncio.CancelledError:
            return

    async def _status_loop(self) -> None:
        """每 10s 广播一次 status。"""
        try:
            while True:
                await asyncio.sleep(STATUS_BROADCAST_INTERVAL_S)
                await self.broadcast_status()
        except asyncio.CancelledError:
            return
        except Exception:
            # 单次广播异常不应终止任务，下一轮继续
            pass


def _peer_text(request: web.Request) -> str:
    """把对端地址压成一行 ``ip:port``（日志用）；取不到返回 ``?``。"""
    try:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if not peer:
            return "?"
        host = str(peer[0])
        port = peer[1] if len(peer) > 1 else 0
        return f"{host}:{port}"
    except Exception:
        return "?"


def _enable_tcp_keepalive(request: web.Request) -> None:
    """给这条 WS 连接的底层 TCP 打开 keepalive，并收紧死亡探测参数。

    **为什么需要**：手机切后台、锁屏、走过一段信号差的走廊，都会让 Wi-Fi
    侧悄悄丢掉 NAT 映射或让连接进入「对端已消失但本地还认为 ESTABLISHED」的
    僵死状态。默认的 OS keepalive 是 2 小时起，等于没有；于是表现就是
    「用着用着突然断了」，且两端都收不到 FIN。

    这里把 Windows / Linux 两套参数都试一遍（Windows 用
    ``SIO_KEEPALIVE_VALS``，Linux 用 ``TCP_KEEPIDLE`` 等），失败全部忽略：
    这只是加分项，绝不因为设不上就影响连接。
    """
    try:
        transport = request.transport
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is None:
            return
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # 探测节奏：空闲 30s 开始探、每 10s 一次、3 次失败即判定断开（≈60s）
        idle_s, interval_s, probes = 30, 10, 3
        if hasattr(socket, "TCP_KEEPIDLE"):  # Linux / macOS
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle_s)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval_s)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, probes)
            except OSError:
                pass
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
            try:
                sock.ioctl(
                    socket.SIO_KEEPALIVE_VALS,
                    (1, idle_s * 1000, interval_s * 1000),
                )
            except OSError:
                pass
    except Exception:
        # 拿不到底层 socket（例如被反代包裹）时静默跳过
        pass


def detect_lan_ip() -> str:
    """探测局域网 IP。

    主路径：UDP ``connect(8.8.8.8:80)`` 后取本地 socket 名（不发真实数据包，
    只让 OS 选出对外出口网卡）；兜底 ``gethostbyname_ex`` 枚举本机地址；
    最终回落 ``127.0.0.1``，保证二维码内容始终可拼。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except Exception:
        ip = ""
    finally:
        try:
            sock.close()
        except Exception:
            pass
    if ip and not ip.startswith("127.") and ip.count(".") == 3:
        return ip
    try:
        hostname = socket.gethostname()
        _name, _aliases, addresses = socket.gethostbyname_ex(hostname)
        for candidate in addresses:
            if (
                candidate
                and not candidate.startswith("127.")
                and candidate.count(".") == 3
            ):
                return candidate
    except Exception:
        pass
    return "127.0.0.1"
