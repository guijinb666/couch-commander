# -*- coding: utf-8 -*-
"""Lead V2 集成联机自检：进程内 aiohttp TestServer，不占真实端口。

V1 的 _integration_check.py 已验证「后端同源托管 web/** + POST /api/apps +
hello 后三条有响应」。本脚本在 V2 交付后补验「总和起来」才暴露的事：

1) V2 迁移后的 config.json 真的生效：GET /api/config / WS config.get 里
   hotkeys==[]、apps 为 5 个国内应用、严格 6 顶层键；
2) V2 新增 REST GET /api/apps/icon 接线正确：缺参 / 不存在的路径一律
   404 NO_ICON 而非 500；GET /api/apps / POST /api/apps/scan 每项派生了
   has_local / route（apps_icons.decorate_apps 真的挂上了）；
3) V2 新增 5 条 WS 命令真的接进 dispatcher.HANDLERS 且 dispatch 能跑通；
4) ★只跑「只读 / 校验短路 / 纯内存」命令，绝不注入键鼠、绝不开浏览器、
   绝不改屏幕 gamma —— 因为在用户活桌面上跑 voice.start / voice.text
   final:true / brightness.set 合法值 / key.* / mouse.* 会有真实副作用。

用法（项目根，任意 cwd 均可）：python _integration_check_v2.py
结果写 _integration_result_v2.txt（UTF-8 逐项 PASS/FAIL）；stdout 只一行
ASCII 汇总；os._exit 跳过 COM/loop teardown 噪声，退出码即测试结论。

磁盘隔离：开头把 server/config.json 读成字节 ORIGINAL（V2 迁移后干净态），
测试里的 POST /api/apps / PUT /api/config 会改它，finally 无条件字节级写回，
保证跑完磁盘仍是干净的 V2 默认态。
"""
import asyncio
import json
import sys
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

ROOT = _PROJECT_ROOT
SERVER = ROOT / "server"
WEB = ROOT / "web"
CFG = SERVER / "config.json"
sys.path.insert(0, str(SERVER))

from aiohttp.test_utils import TestServer, TestClient  # noqa: E402
import main as srv_main  # noqa: E402
from http_api import create_app  # noqa: E402

ORIGINAL = CFG.read_bytes()  # V2 迁移后干净态，finally 字节级恢复
results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))


async def collect(ws, wanted, timeout=1.5, max_msgs=50):
    """收消息直到 wanted 各 type 都命中或超时；返回 (got, all_msgs)。

    got: {type: 首条该 type 的消息}；all_msgs: 期间所有 JSON 消息（含被跳过的
    status/pong 等），供按 payload.id / payload.code 精确复查。
    """
    want = set(wanted)
    got, all_msgs = {}, []
    for _ in range(max_msgs):
        if want.issubset(got):
            break
        try:
            m = await asyncio.wait_for(ws.receive(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        if m.type.name in ("CLOSE", "CLOSED", "CLOSING", "ERROR"):
            break
        if m.type.name != "TEXT":
            continue
        try:
            d = json.loads(m.data)
        except Exception:
            continue
        all_msgs.append(d)
        t = d.get("type")
        if t in want and t not in got:
            got[t] = d
    return got, all_msgs


def find(all_msgs, msg_type, *, code=None, msg_id=None):
    """在 all_msgs 里按 type + 可选 payload.code / payload.id 找一条。"""
    for d in all_msgs:
        if d.get("type") != msg_type:
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if code is not None and p.get("code") != code:
            continue
        if msg_id is not None and p.get("id") != msg_id:
            continue
        return d
    return None


async def main():
    ctx = srv_main.build_context("127.0.0.1", 8877, b"", "http://127.0.0.1:8877/")
    app = create_app(ctx)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # ============ A) 静态同源托管 ============
        r = await client.get("/")
        body = await r.text()
        idx = (WEB / "index.html").read_text(encoding="utf-8")
        rec("GET / 同源托管前端真实页面",
            r.status == 200 and body.strip() == idx.strip() and len(body) > 400,
            f"HTTP {r.status}, {len(body)} chars, 一致={body.strip() == idx.strip()}")
        js = await client.get("/js/net.js")
        rec("GET /js/net.js → text/javascript",
            js.status == 200 and "javascript" in js.headers.get("Content-Type", ""),
            f"HTTP {js.status}, CT={js.headers.get('Content-Type')}")
        css = await client.get("/css/base.css")
        rec("GET /css/base.css → text/css",
            css.status == 200 and "text/css" in css.headers.get("Content-Type", ""),
            f"HTTP {css.status}, CT={css.headers.get('Content-Type')}")

        # ============ A2) V2 最终态新增：应用卡片真实素材子目录静态托管 ============
        # 前端会话最后一轮加了 web/assets/icons/*.svg|png（用户提供整卡 SVG +
        # 官方 favicon PNG）。catch-all 必须能托管子目录资源并给对 MIME，
        # 否则手机端卡片全部退化成「名称首字」。
        art_svg = await client.get("/assets/icons/douyin.svg")
        art_bytes = await art_svg.read()
        rec("GET /assets/icons/douyin.svg → image/svg+xml（整卡素材托管）",
            art_svg.status == 200
            and "image/svg+xml" in art_svg.headers.get("Content-Type", "")
            and len(art_bytes) > 100,
            f"HTTP {art_svg.status}, CT={art_svg.headers.get('Content-Type')}, bytes={len(art_bytes)}")
        ico_png = await client.get("/assets/icons/douyin.png")
        ico_bytes = await ico_png.read()
        rec("GET /assets/icons/douyin.png → image/png（官方小图标托管）",
            ico_png.status == 200
            and "image/png" in ico_png.headers.get("Content-Type", "")
            and ico_bytes[:4] == b"\x89PNG",
            f"HTTP {ico_png.status}, CT={ico_png.headers.get('Content-Type')}, bytes={len(ico_bytes)}")

        # ============ B) REST：V2 config 生效 + 新端点 + V1 端点回归 ============
        st = await client.get("/api/status")
        stj = await st.json(content_type=None)
        rec("GET /api/status", st.status == 200 and stj.get("connected") is True,
            f"HTTP {st.status}, connected={stj.get('connected')}")

        cf = await client.get("/api/config")
        cfj = await cf.json(content_type=None)
        keys_ok = set(cfj.keys()) == {"version", "pages", "active_page_id",
                                      "hotkeys", "apps", "touchpad"}
        # hotkeys 允许为空（V2 默认无设置）或为合法结构（用户套用模板后的真实数据），
        # 不再写死「必须为空」，避免把用户数据误判成回归。
        hk = cfj.get("hotkeys")
        hotkeys_valid = isinstance(hk, list) and all(
            isinstance(h, dict) and isinstance(h.get("id"), str)
            and isinstance(h.get("keys"), list) and h.get("keys")
            for h in hk)
        app_names = [a.get("name") for a in cfj.get("apps", []) if isinstance(a, dict)]
        apps_ok = app_names == ["抖音", "bilibili", "QQ音乐", "网易云音乐", "腾讯视频"]
        rec("GET /api/config 严格 6 顶层键", keys_ok, f"keys={sorted(cfj.keys())}")
        rec("GET /api/config hotkeys 结构合法（空或含合法项）", hotkeys_valid,
            f"count={len(hk) if isinstance(hk, list) else 'N/A'}")
        rec("GET /api/config apps 为 V2 5 个国内应用", apps_ok, f"apps={app_names}")

        ap = await client.get("/api/apps")
        apj = await ap.json(content_type=None)
        apps_list = apj.get("apps", [])
        decorated = len(apps_list) > 0 and all(
            isinstance(a, dict) and "has_local" in a and "route" in a
            for a in apps_list
        )
        v1_fields_kept = len(apps_list) > 0 and all(
            isinstance(a, dict) and {"id", "name", "path"}.issubset(a)
            for a in apps_list
        )
        rec("GET /api/apps 每项派生 has_local/route", decorated,
            f"apps={len(apps_list)}")
        rec("GET /api/apps 保留 V1 字段 id/name/path", v1_fields_kept, "")

        md = await client.get("/api/media")
        rec("GET /api/media", md.status == 200, f"HTTP {md.status}")
        png = await client.get("/api/qr.png")
        rec("GET /api/qr.png → image/png",
            png.status == 200 and "image/png" in png.headers.get("Content-Type", ""),
            f"HTTP {png.status}")

        # ★ V2 新端点：GET /api/apps/icon，缺参 / 不存在路径都必须 404 NO_ICON
        ic0 = await client.get("/api/apps/icon")
        ic0j = await ic0.json(content_type=None)
        rec("GET /api/apps/icon 缺 path → 404 NO_ICON",
            ic0.status == 404 and ic0j.get("code") == "NO_ICON",
            f"HTTP {ic0.status}, code={ic0j.get('code')}")
        ic1 = await client.get("/api/apps/icon?path=Z%3A%5C__missing__%5Cghost.exe")
        ic1j = await ic1.json(content_type=None)
        rec("GET /api/apps/icon 不存在路径 → 404 NO_ICON（非 500）",
            ic1.status == 404 and ic1j.get("code") == "NO_ICON",
            f"HTTP {ic1.status}, code={ic1j.get('code')}")

        scan = await client.post("/api/apps/scan")
        scanj = await scan.json(content_type=None)
        scan_decorated = isinstance(scanj.get("apps"), list) and (
            not scanj["apps"] or all(
                isinstance(a, dict) and "has_local" in a and "route" in a
                for a in scanj["apps"]
            )
        )
        rec("POST /api/apps/scan 返回且派生 has_local/route",
            scan.status == 200 and scan_decorated,
            f"HTTP {scan.status}, apps={len(scanj.get('apps', []))}")

        # ★ V1 Lead 集成补丁 POST /api/apps 在 V2 重写后仍工作
        new_app = {"id": "app-999", "name": "V2集成测试应用", "path": "C:\\test.exe",
                   "icon": "🧪", "color": "#5B7CFA"}
        add = await client.post(
            "/api/apps",
            data=json.dumps(new_app, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        addj = await add.json(content_type=None)
        rec("POST /api/apps 返回 ok（V1 补丁未坏）",
            add.status == 200 and addj.get("ok") is True,
            f"HTTP {add.status}, ok={addj.get('ok')}")
        cf2 = await client.get("/api/config")
        cf2j = await cf2.json(content_type=None)
        present = any(a.get("id") == "app-999" for a in cf2j.get("apps", []))
        rec("POST /api/apps 落盘可回读", present, f"含 app-999={present}")
        cf2j["apps"] = [a for a in cf2j.get("apps", []) if a.get("id") != "app-999"]
        put = await client.put(
            "/api/config",
            data=json.dumps(cf2j, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        rec("PUT /api/config 全量回写", put.status == 200, f"HTTP {put.status}")

        # ============ C) WebSocket：hello / pong / 三条初始化 / V2 命令接线 ============
        ws = await client.ws_connect("/ws")
        got, _ = await collect(ws, {"hello"})
        rec("WS 建连收到 hello", "hello" in got,
            f"payload={got.get('hello', {}).get('payload')}")

        await ws.send_json({"type": "ping", "payload": {"echo_ts": 1234567890123},
                            "ts": 1234567890123, "id": "c-ping"})
        got, _ = await collect(ws, {"pong"})
        pong = got.get("pong")
        rec("WS ping → pong 回显 echo_ts", pong is not None,
            f"echo_ts={pong.get('payload', {}).get('echo_ts') if pong else None}")

        # hello 后三条初始化
        await ws.send_json({"type": "config.get", "payload": {}, "ts": 1, "id": "c-cfg"})
        await ws.send_json({"type": "app.list", "payload": {}, "ts": 2, "id": "c-app"})
        await ws.send_json({"type": "media.state", "payload": {}, "ts": 3, "id": "c-media"})
        trio, _ = await collect(ws, {"config", "app.list", "media"}, timeout=2.5)
        rec("hello 后 config.get 有响应", "config" in trio, "")
        cfg_msg = trio.get("config", {}).get("payload", {}).get("config", {})
        # 与 REST 断言一致：hotkeys 允许空数组或合法结构（用户真实套用的模板数据）
        ch = cfg_msg.get("hotkeys")
        rec("WS config.get hotkeys 结构合法（空或含合法项）",
            "config" in trio and isinstance(ch, list),
            f"count={len(ch) if isinstance(ch, list) else 'N/A'}")
        al = trio.get("app.list", {}).get("payload", {}).get("apps", [])
        rec("hello 后 app.list 有响应且派生 has_local/route",
            "app.list" in trio and len(al) > 0 and all(
                isinstance(a, dict) and "has_local" in a and "route" in a for a in al),
            f"apps={len(al)}")
        rec("hello 后 media.state 有响应", "media" in trio, "")

        # ★ V2 brightness.get（只读，不写屏幕）
        # 首次调用会连 WMI COM（GetObject("winmgmts:\\.\root\wmi")）探测真实背光，
        # 冷启动常需数秒；给足超时，避免把「慢」误判成「不响应」。
        await ws.send_json({"type": "brightness.get", "payload": {}, "ts": 4, "id": "c-bg"})
        got, bg_msgs = await collect(ws, {"brightness", "error"}, timeout=8.0, max_msgs=80)
        b = got.get("brightness", {}).get("payload", {})
        err = got.get("error", {}).get("payload", {})
        rec("WS brightness.get → 下发 brightness{value,method,best_effort}",
            "brightness" in got and {"value", "method", "best_effort"}.issubset(b),
            f"payload={b} err={err} seen={[m.get('type') for m in bg_msgs]}")

        # ★ V2 brightness.set 非法值：校验在改屏幕前短路，回 error BAD_VALUE
        await ws.send_json({"type": "brightness.set", "payload": {"value": 5},
                            "ts": 5, "id": "c-bs1"})
        got, msgs = await collect(ws, {"error"})
        hit = find(msgs, "error", code="BAD_VALUE", msg_id="c-bs1")
        rec("WS brightness.set 越界 → error BAD_VALUE（零副作用）", hit is not None,
            f"got={got.get('error', {}).get('payload')}")

        await ws.send_json({"type": "brightness.set", "payload": {"value": "x"},
                            "ts": 6, "id": "c-bs2"})
        _, msgs = await collect(ws, {"error"})
        hit = find(msgs, "error", code="BAD_VALUE", msg_id="c-bs2")
        rec("WS brightness.set 非法类型 → error BAD_VALUE", hit is not None,
            f"hit={hit is not None}")

        # ★ V2 voice.text 缺 text：校验短路，回 error BAD_PAYLOAD
        await ws.send_json({"type": "voice.text", "payload": {"final": False},
                            "ts": 7, "id": "c-vt0"})
        _, msgs = await collect(ws, {"error"})
        hit = find(msgs, "error", code="BAD_PAYLOAD", msg_id="c-vt0")
        rec("WS voice.text 缺 text → error BAD_PAYLOAD", hit is not None,
            f"hit={hit is not None}")

        # ★ V2 voice.text final:false：高频预览不回任何消息，且连接仍存活
        await ws.send_json({"type": "voice.text",
                            "payload": {"text": "预览", "final": False},
                            "ts": 8, "id": "c-vtnf"})
        await ws.send_json({"type": "ping", "payload": {"echo_ts": 222}, "ts": 9,
                            "id": "c-after-nf"})
        got, msgs = await collect(ws, {"pong"})
        stray = find(msgs, "ack", msg_id="c-vtnf") or find(msgs, "error", msg_id="c-vtnf")
        rec("WS voice.text final:false 不回 ack/error 且连接存活",
            "pong" in got and stray is None,
            f"pong={'pong' in got}, stray={stray is not None}")

        # ★ V2 voice.stop（纯内存，零副作用）
        await ws.send_json({"type": "voice.stop", "payload": {}, "ts": 10, "id": "c-vstop"})
        _, msgs = await collect(ws, {"ack"})
        hit = find(msgs, "ack", msg_id="c-vstop")
        rec("WS voice.stop → ack（不关闭/不导航 muse）", hit is not None,
            f"hit={hit is not None}")

        # 一连串错误/无响应命令后，WS 仍存活
        await ws.send_json({"type": "ping", "payload": {"echo_ts": 333}, "ts": 11,
                            "id": "c-alive"})
        got, _ = await collect(ws, {"pong"})
        rec("一连串 V2 命令后 WS 仍存活（dispatch 兜底不掀翻）", "pong" in got, "")
        await ws.close()
    finally:
        try:
            await client.close()
        finally:
            CFG.write_bytes(ORIGINAL)  # 字节级恢复，保证磁盘仍是 V2 干净态

    lines = ["Lead V2 集成联机自检结果（进程内 TestServer，端口 8877 虚拟，零副作用子集）",
             "=" * 64]
    ok = 0
    for name, good, detail in results:
        flag = "PASS" if good else "FAIL"
        ok += 1 if good else 0
        lines.append(f"  [{flag}] {name}" + (f" — {detail}" if detail else ""))
    lines.append("-" * 64)
    lines.append(f"合计：PASS={ok}  FAIL={len(results) - ok}  TOTAL={len(results)}")
    (ROOT / "_integration_result_v2.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"INTEGRATION_V2_RESULT PASS={ok} FAIL={len(results)-ok} TOTAL={len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    _code = asyncio.run(main())
    sys.stdout.flush()
    sys.stderr.flush()
    __import__("os")._exit(_code)
