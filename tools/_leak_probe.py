"""验证「media.state 每秒一次 → 每次新起一个 SMTC 线程且可能永久不回收」这条泄漏假设。

如果 `_read_smtc_sync()` 的 `probe.join(timeout=1.0)` 超时，那个线程会继续跑
（daemon），前端每秒拉一次 media.state，就等于每秒泄漏一个线程。跑几小时
就会攒下上万个线程，最终 CPython 起不了新线程 / 内存爆掉，服务卡死 ——
表现出来正是「开一段时间莫名其妙断连」。

零副作用：只读媒体状态，不注入键鼠、不改配置、不动屏幕。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
_PROJECT_ROOT = next(
    _p for _p in Path(__file__).resolve().parents if (_p / "server" / "main.py").is_file()
)

ROOT = _PROJECT_ROOT
sys.path.insert(0, str(ROOT / "server"))
os.chdir(str(ROOT / "server"))

import media  # noqa: E402

OUT = ROOT / "_leak_result.txt"
lines: list[str] = []
results: list[tuple[bool, str, str]] = []


def rec(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + (f" — {detail}" if detail else ""))


def main() -> int:
    base = threading.active_count()
    rec(True, "起始线程数", str(base))

    # 计时单次调用：如果经常超过 1s，说明 join 超时、线程被留下来了
    samples = []
    for index in range(12):
        started = time.monotonic()
        state = media.state()
        cost = time.monotonic() - started
        samples.append(cost)
        time.sleep(0.05)

    leaked = threading.active_count()
    peak = max(samples)
    avg = sum(samples) / len(samples)
    growth = leaked - base

    lines.append(f"samples={[round(s, 3) for s in samples]}")
    lines.append(f"base={base} after={leaked} growth={growth} peak={peak:.3f} avg={avg:.3f}")

    rec(avg < 1.05, "单次 media.state 平均耗时可接受", f"avg={avg:.3f}s peak={peak:.3f}s")
    # 每次超时都可能留下一个线程；允许少量抖动，但不能线性增长
    rec(growth <= 2, "12 次调用后线程数没有线性增长", f"growth={growth}")

    if growth > 2:
        lines.append("=> 判定：线程泄漏成立，join 超时的线程没有被回收")

    return 0


if __name__ == "__main__":
    code = main()
    total = len(results)
    passed = sum(1 for ok, _, _ in results if ok)
    lines.append(f"LEAK_RESULT PASS={passed} FAIL={total - passed} TOTAL={total}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(lines[-1])
    os._exit(code)
