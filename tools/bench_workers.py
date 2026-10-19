# -*- coding: utf-8 -*-
"""cp315t 双环境基准（新特性提案 4.4-1/2）：finance_all workers 缩放 + index_trade_stats。

同一脚本换解释器跑两遍（cwd=tdxpy 仓库根，源码目录 import）：
  基线: D:/Software/miniconda3/envs/normal/python.exe tools/bench_workers.py
  对照: D:/Software/python_env/py315_normal/python.exe tools/bench_workers.py

输出: tools/bench_result_gil-on.json / bench_result_gil-off.json（对照用）
加速比与 Amdahl 并行占比（解析段占比的间接估计）自动计算。
"""
import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def env_info():
    gil = None
    try:
        gil = sys._is_gil_enabled()
    except AttributeError:
        pass
    return {
        "python": sys.version,
        "implementation": sys.implementation.name,
        "gil_enabled": gil,
        "executable": sys.executable,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smoke", action="store_true", help="冒烟模式：8 股 × workers=1/2/4")
    ap.add_argument("--symbols", type=int, default=100, help="全量模式股票数（默认 100）")
    args = ap.parse_args()

    if args.smoke:
        n_symbols, worker_levels = 8, [1, 2, 4]
    else:
        n_symbols, worker_levels = args.symbols, [1, 4, 8]

    from tdxpy import TdxClient

    symbols = [f"sh.600{i:03d}" for i in range(n_symbols)]  # 固定清单可复现
    info = env_info()
    tag = "gil-off" if info["gil_enabled"] is False else "gil-on"
    print(f"环境: {info['python'].splitlines()[0]} gil_enabled={info['gil_enabled']} tag={tag}")
    print(f"股票数: {len(symbols)}  workers 档位: {worker_levels}")

    client = TdxClient.auto()  # ⚠️ 只在主线程调用（嵌套线程池约束）
    results = {"env": info, "mode": "smoke" if args.smoke else "full",
               "n_symbols": n_symbols, "finance_all": {}, "index_trade_stats": {}}

    try:
        for w in worker_levels:
            time.sleep(3)  # 档位间缓冲，避开服务器限速窗口
            t0 = time.perf_counter()
            df = client.finance_all(symbols, workers=w)
            dt = time.perf_counter() - t0
            fail = int(df["error"].notna().sum()) if "error" in df.columns else 0
            results["finance_all"][str(w)] = {
                "seconds": round(dt, 2), "rows": len(df), "fail": fail,
            }
            print(f"finance_all workers={w}: {dt:.1f}s  ({len(df)} 行 / {fail} 失败)")

        time.sleep(3)
        t0 = time.perf_counter()
        stats = client.index_trade_stats("sh.000016")
        dt = time.perf_counter() - t0
        results["index_trade_stats"] = {"seconds": round(dt, 2), "rows": len(stats)}
        print(f"index_trade_stats(sh.000016): {dt:.2f}s  ({len(stats)} 行)")
    finally:
        client.close()

    # Amdahl 并行占比估计（从加速比反推解析段占比：S(n)=1/((1-p)+p/n)）
    fin = {float(k): v["seconds"] for k, v in results["finance_all"].items()}
    amdahl = {}
    if 1 in fin:
        for n in (4, 8):
            if n in fin and fin[n] > 0:
                s = fin[1] / fin[n]
                amdahl[f"speedup_{n}"] = round(s, 2)
                amdahl[f"parallel_frac_from_{n}"] = round((1 - 1 / s) / (1 - 1 / n), 3)
    results["amdahl"] = amdahl
    print(f"Amdahl: {amdahl}")

    out = Path(__file__).parent / f"bench_result_{tag}{'_smoke' if args.smoke else ''}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写 {out}")


if __name__ == "__main__":
    main()
