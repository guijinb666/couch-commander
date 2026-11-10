"""临时校验脚本：确认 8765 上跑的服务真的在发新版静态资源。

只做 GET，不注入、不改任何状态。
"""
from __future__ import annotations

import ssl
import sys
import urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "8765"
BASE = f"https://127.0.0.1:{PORT}"

CHECKS = [
    ("/js/dpad.js", "DIRECTION_KEYS", True),
    ("/js/dpad.js", "media.seek", True),
    ("/js/keyboard.js", "NUMBER_ROW", True),
    ("/js/voice.js", "session_open", False),   # 前端不该出现后端字段，负向校验
    # 长按不再把 url 递给后端（注释里提到 muse.ai 是允许的，只禁真的发 url）
    ("/js/voice.js", "voice.start', { url", False),
    ("/js/net.js", "resume", True),
    ("/css/touchpad.css", "keyboard-row-digits", True),
    ("/css/base.css", "media-time", True),
    ("/index.html", "progress-time", True),
    ("/index.html", "brightness-value", True),
]


def fetch(path: str) -> str:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(BASE + path, context=ctx, timeout=8) as r:
        return r.read().decode("utf-8", "replace")


def main() -> int:
    cache: dict[str, str] = {}
    ok = fail = 0
    for path, needle, want in CHECKS:
        if path not in cache:
            try:
                cache[path] = fetch(path)
            except Exception as exc:  # noqa: BLE001
                print(f"[FAIL] {path} 取不到：{exc}")
                fail += 1
                continue
        hit = needle in cache[path]
        good = hit == want
        tag = "PASS" if good else "FAIL"
        print(f"[{tag}] {path} 含 {needle!r} = {hit}（期望 {want}）")
        ok += good
        fail += (not good)
    print(f"SERVED_ASSETS PASS={ok} FAIL={fail} TOTAL={ok + fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
