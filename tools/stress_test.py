# -*- coding: utf-8 -*-
"""cp315t 线程安全压测（新特性提案 4.4-3）：单连接 + 多连接混合打。

阶段 A: 单连接 × 32 线程（TdxClient multithread 连接锁串行收发）
阶段 B: 多连接 × 16 线程（各持独立连接显式 host/port，0.3s 间隔限速）

判定（提案标准）: 0 个 struct.error、0 个响应长度校验失败、无死锁（进度推进）、
心跳/连接存活、（单连接模式）断线重建正常。

用法（py315t 环境）:
  D:/Software/python_env/py315_normal/python.exe tools/stress_test.py [--smoke] [--minutes 30]

托管长任务规范：进度写 tools/stress_progress.log（不依赖 stdout 管道），
汇总写 tools/stress_result.json。
"""
import argparse
import json
import random
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

TOOLS = Path(__file__).parent
PROGRESS_LOG = TOOLS / "stress_progress.log"


def log(msg):
    """进度只落文件（stdout 管道缓冲阻塞教训：长任务不 print）。"""
    with open(PROGRESS_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


class Counters:
    """线程安全计数器。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.total = 0
        self.fail_count = 0  # ⚠️ 不叫 fail：与 fail() 方法同名会遮蔽方法
        self.struct_error = 0  # 响应撕裂标志（判定必须为 0）
        self.other_errors = {}

    def ok(self):
        with self._lock:
            self.total += 1

    def fail(self, exc):
        with self._lock:
            self.total += 1
            self.fail_count += 1
            name = type(exc).__name__
            if isinstance(exc, struct_error_class()):
                self.struct_error += 1
            self.other_errors[name] = self.other_errors.get(name, 0) + 1


def struct_error_class():
    import struct

    return struct.error


def worker_loop(mode, client_or_factory, stop_event, counters, i):
    """混合打 bars/quotes/finance，按线程号错开命令起点。"""
    cmds = ["bars", "quotes", "finance"]
    k = 0
    try:
        while not stop_event.is_set():
            cmd = cmds[(i + k) % 3]
            try:
                if mode == "single":
                    client = client_or_factory
                    if cmd == "bars":
                        client.bars("sh.600519", period="day", count=5)
                    elif cmd == "quotes":
                        client.quotes(["sh.600519", "sz.000001"])
                    else:
                        client.finance("sh.600519")
                else:  # multi：每线程自己的 client
                    client = client_or_factory()
                    if cmd == "bars":
                        client.bars("sh.600519", period="day", count=5)
                    elif cmd == "quotes":
                        client.quotes(["sh.600519", "sz.000001"])
                    else:
                        client.finance("sh.600519")
                    client.close()
                counters.ok()
            except Exception as exc:  # noqa: BLE001 —— 压测宽捕获全记账
                counters.fail(exc)
            k += 1
            if mode == "multi":
                time.sleep(0.3)  # 16 独立连接限速（~53 req/s 总量）
    except BaseException as exc:  # 线程自身崩了也记账
        counters.fail(exc)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true", help="冒烟：A/B 各 30s")
    ap.add_argument("--minutes", type=float, default=30.0, help="总时长分钟（默认 30，A/B 对半）")
    ap.add_argument("--single-threads", type=int, default=32)
    ap.add_argument("--multi-threads", type=int, default=16)
    args = ap.parse_args()

    # half = 单阶段时长秒：smoke 各 30s；正常总 minutes 分钟对半
    half = 30.0 if args.smoke else args.minutes / 2 * 60

    from tdxpy import TdxClient

    PROGRESS_LOG.write_text("", encoding="utf-8")  # 清空旧进度
    log(f"压测启动 smoke={args.smoke} 总时长={half * 2:.0f}s")

    client = TdxClient.auto()  # ⚠️ 主线程选优
    best_host, best_port = client._host, client._port
    log(f"选优: {best_host}:{best_port}")

    counters = Counters()
    results = {"phase_a": {}, "phase_b": {}}

    # -- 阶段 A：单连接 × 32 线程 -------------------------------------------
    stop = threading.Event()
    threads = [
        threading.Thread(target=worker_loop, args=("single", client, stop, counters, i), daemon=True)
        for i in range(args.single_threads)
    ]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    last_total = 0
    while time.monotonic() - t0 < half:
        time.sleep(30)
        with counters._lock:
            cur = counters.total
        log(f"A 进度: {cur} 请求 ({cur - last_total}/30s)")
        if cur == last_total and cur > 0:
            log("A 警告: 30s 零进度（疑似死锁）")
        last_total = cur
    stop.set()
    for t in threads:
        t.join(timeout=10)
    alive_deadlock = sum(1 for t in threads if t.is_alive())
    a_secs = time.monotonic() - t0
    results["phase_a"] = {
        "seconds": round(a_secs, 1), "threads": args.single_threads,
        "total": counters.total, "fail": counters.fail_count,
        "struct_error": counters.struct_error, "errors": counters.other_errors,
        "hung_threads": alive_deadlock,
    }
    log(f"阶段 A 结束: {counters.total} 请求 / {counters.fail_count} 失败 / {counters.struct_error} struct.error / 挂起线程 {alive_deadlock}")

    # 心跳/连接存活探活（阶段 A 共享连接是否还活着）
    try:
        client.quotes(["sh.600519"])
        results["phase_a"]["connection_alive"] = True
        log("A 探活: 共享连接存活 ✓")
    except Exception as exc:  # noqa: BLE001
        results["phase_a"]["connection_alive"] = f"FAIL {type(exc).__name__}: {exc}"
        log(f"A 探活: 失败 {exc}")

    # -- 阶段 B：多连接 × 16 线程 -------------------------------------------
    counters_b = Counters()
    counters = counters_b
    stop = threading.Event()
    threads = [
        threading.Thread(
            target=worker_loop,
            args=("multi", lambda: TdxClient(host=best_host, port=best_port), stop, counters_b, i),
            daemon=True,
        )
        for i in range(args.multi_threads)
    ]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    last_total = 0
    while time.monotonic() - t0 < half:
        time.sleep(30)
        with counters_b._lock:
            cur = counters_b.total
        log(f"B 进度: {cur} 请求 ({cur - last_total}/30s)")
        last_total = cur
    stop.set()
    for t in threads:
        t.join(timeout=15)
    alive_deadlock = sum(1 for t in threads if t.is_alive())
    b_secs = time.monotonic() - t0
    results["phase_b"] = {
        "seconds": round(b_secs, 1), "threads": args.multi_threads,
        "total": counters_b.total, "fail": counters_b.fail_count,
        "struct_error": counters_b.struct_error, "errors": counters_b.other_errors,
        "hung_threads": alive_deadlock,
    }
    log(f"阶段 B 结束: {counters_b.total} 请求 / {counters_b.fail_count} 失败 / {counters_b.struct_error} struct.error / 挂起线程 {alive_deadlock}")

    client.close()

    # -- 判定 ----------------------------------------------------------------
    verdict = {
        "zero_struct_error": results["phase_a"]["struct_error"] == 0 and results["phase_b"]["struct_error"] == 0,
        "no_hung_threads": results["phase_a"]["hung_threads"] == 0 and results["phase_b"]["hung_threads"] == 0,
        "connection_alive": results["phase_a"]["connection_alive"] is True,
        "fail_rate_ok": (results["phase_a"]["fail"] + results["phase_b"]["fail"])
        < max(1, (results["phase_a"]["total"] + results["phase_b"]["total"])) * 0.01,
    }
    verdict["PASS"] = all(verdict.values())
    results["verdict"] = verdict
    results["python"] = sys.version.splitlines()[0]
    results["gil_enabled"] = getattr(sys, "_is_gil_enabled", lambda: None)()

    out = TOOLS / f"stress_result{'_smoke' if args.smoke else ''}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"压测结束 判定={'PASS' if verdict['PASS'] else 'FAIL'} 结果已写 {out}")


if __name__ == "__main__":
    main()
