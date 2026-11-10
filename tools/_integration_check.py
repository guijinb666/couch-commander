# -*- coding: utf-8 -*-
"""Lead 集成联机自检：进程内 aiohttp TestServer，不占真实端口、不注入键鼠。

验证三件「总和起来」才暴露的事：
1) 后端 create_app 能同源托管前端 web/**（GET / 返回真实前端页、JS/CSS MIME 正确）；
2) Lead 集成补丁 POST /api/apps 真工作（设置页「添加应用」不再 404）；
3) 前端 hello 后三条 config.get / app.list / media.state 都有响应，ping/pong 正常。

用法（项目根，任意 cwd 均可）：
    python _integration_check.py
结果写入 ``_integration_result.txt``（UTF-8，含逐项 PASS/FAIL）；
stdout 只输出一行 ASCII 汇总，避免宿主 GBK 编码问题；
用 os._exit 跳过 COM/事件循环 teardown 噪声，退出码即测试结论。
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
sys.path.insert(0, str(SERVER))

from aiohttp.test_utils import TestServer, TestClient  # noqa: E402
import main as srv_main  # noqa: E402
from http_api import create_app  # noqa: E402

results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))


async def main():
    ctx = srv_main.build_context("127.0.0.1", 8877, b"", "http://127.0.0.1:8877/")
    app = create_app(ctx)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        # ---- 1) 静态托管：GET / 必须是前端真实页面，不能是占位说明页 ----
        r = await client.get("/")
        body = await r.text()
        idx = (WEB / "index.html").read_text(encoding="utf-8")
        rec(
            "GET / 同源托管前端真实页面",
            r.status == 200 and "后端服务已就绪" not in body and "<" in body and len(body) > 400,
            f"HTTP {r.status}, {len(body)} chars, 与 index.html 一致={body.strip() == idx.strip()}",
        )
        js = await client.get("/js/net.js")
        rec(
            "GET /js/net.js → text/javascript",
            js.status == 200 and "javascript" in js.headers.get("Content-Type", ""),
            f"HTTP {js.status}, CT={js.headers.get('Content-Type')}",
        )
        css = await client.get("/css/base.css")
        rec(
            "GET /css/base.css → text/css",
            css.status == 200 and "text/css" in css.headers.get("Content-Type", ""),
            f"HTTP {css.status}, CT={css.headers.get('Content-Type')}",
        )

        # ---- 2) REST 端点全通 ----
        st = await client.get("/api/status")
        stj = await st.json(content_type=None)
        rec("GET /api/status", st.status == 200 and "connected" in stj, f"HTTP {st.status}")
        cf = await client.get("/api/config")
        cfj = await cf.json(content_type=None)
        rec("GET /api/config", cf.status == 200 and "apps" in cfj and "touchpad" in cfj, f"HTTP {cf.status}")
        ap = await client.get("/api/apps")
        apj = await ap.json(content_type=None)
        rec("GET /api/apps", ap.status == 200 and isinstance(apj.get("apps"), list),
            f"HTTP {ap.status}, apps={len(apj.get('apps', []))}")
        md = await client.get("/api/media")
        rec("GET /api/media", md.status == 200, f"HTTP {md.status}")
        png = await client.get("/api/qr.png")
        rec("GET /api/qr.png", png.status == 200 and "image/png" in png.headers.get("Content-Type", ""),
            f"HTTP {png.status}")
        scan = await client.post("/api/apps/scan")
        rec("POST /api/apps/scan", scan.status == 200, f"HTTP {scan.status}")

        # ---- 3) ★ Lead 集成补丁：POST /api/apps（前端 addApp 实调）----
        new_app = {"id": "app-999", "name": "集成测试应用", "path": "C:\\test.exe",
                   "icon": "🧪", "color": "#5B7CFA"}
        add = await client.post(
            "/api/apps",
            data=json.dumps(new_app, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        addj = await add.json(content_type=None)
        rec("POST /api/apps 返回 ok", add.status == 200 and addj.get("ok") is True,
            f"HTTP {add.status}, ok={addj.get('ok')}, apps={len(addj.get('apps', []))}")
        cf2 = await client.get("/api/config")
        cf2j = await cf2.json(content_type=None)
        present = any(a.get("id") == "app-999" for a in cf2j.get("apps", []))
        rec("POST /api/apps 落盘可回读", present, f"config.apps 含 app-999={present}")
        # 清掉测试数据，恢复交付态
        cf2j["apps"] = [a for a in cf2j.get("apps", []) if a.get("id") != "app-999"]
        put = await client.put(
            "/api/config",
            data=json.dumps(cf2j, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        rec("PUT /api/config 全量回写", put.status == 200, f"HTTP {put.status}")

        # ---- 4) WebSocket：hello / pong / 三条初始化消息 ----
        ws = await client.ws_connect("/ws")
        got = {}
        for _ in range(30):
            try:
                m = await asyncio.wait_for(ws.receive(), timeout=1.0)
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
            got.setdefault(d.get("type"), d)
            if "hello" in got:
                break
        rec("WS 建连收到 hello", "hello" in got,
            f"hello payload={got.get('hello', {}).get('payload')}")

        await ws.send_json({"type": "ping", "payload": {"echo_ts": 1234567890123},
                            "ts": 1234567890123, "id": "c-ping"})
        pong = None
        for _ in range(10):
            try:
                m = await asyncio.wait_for(ws.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if m.type.name == "TEXT":
                d = json.loads(m.data)
                if d.get("type") == "pong":
                    pong = d
                    break
        rec("WS ping → pong 回显 echo_ts", pong is not None,
            f"pong.echo_ts={pong.get('payload', {}).get('echo_ts') if pong else None}")

        await ws.send_json({"type": "config.get", "payload": {}, "ts": 1, "id": "c-cfg"})
        await ws.send_json({"type": "app.list", "payload": {}, "ts": 1, "id": "c-app"})
        await ws.send_json({"type": "media.state", "payload": {}, "ts": 1, "id": "c-media"})
        trio = {}
        for _ in range(30):
            if {"config", "app.list", "media"}.issubset(trio):
                break
            try:
                m = await asyncio.wait_for(ws.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if m.type.name == "TEXT":
                d = json.loads(m.data)
                if d.get("type") in ("config", "app.list", "media"):
                    trio[d["type"]] = d
        rec("hello 后 config.get 有响应", "config" in trio, "")
        rec("hello 后 app.list 有响应", "app.list" in trio, "")
        rec("hello 后 media.state 有响应", "media" in trio, "")
        await ws.close()
    finally:
        await client.close()

    lines = ["Lead 集成联机自检结果（进程内 TestServer，端口 8877 虚拟）", "=" * 60]
    ok = 0
    for name, good, detail in results:
        flag = "PASS" if good else "FAIL"
        ok += 1 if good else 0
        lines.append(f"  [{flag}] {name}" + (f" — {detail}" if detail else ""))
    lines.append("-" * 60)
    lines.append(f"合计：PASS={ok}  FAIL={len(results) - ok}  TOTAL={len(results)}")
    (ROOT / "_integration_result.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"INTEGRATION_RESULT PASS={ok} FAIL={len(results) - ok} TOTAL={len(results)}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    _code = asyncio.run(main())
    sys.stdout.flush()
    sys.stderr.flush()
    __import__("os")._exit(_code)  # 跳过 COM/loop teardown 噪声，退出码即测试结论
