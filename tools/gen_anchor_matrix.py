"""生成「历史锚点矩阵」基线（tests/fixtures/anchor_matrix.json）。

设计意图
--------
在线守卫有两种互补范式：
1. **动态锚点**（test_server_guard / test_online_smoke）：取最近交易日、跨日不失效，
   守「数据面活着 + 双通道一致」；缺点是不管历史数据是否被改。
2. **静态广谱锚点矩阵**（本脚本 + tests/test_anchor_matrix.py）：用大量
   「固定市场 × 固定品种 × 固定日期 × 固定价格/笔数」的组合钉住历史数据。
   任何一格变红 → 服务器历史数据可能被改（重灌/缺失/格式变更）或客户端解析回归。

采集纪律
--------
- **双服务器交叉验证**：同一锚点至少 2 台服务器返回一致才写入基线，避免把单台坏数据固化。
- **不复权（adjust=0）**：不复权历史价是常量；前复权会随最新价变化。
- 采集报告打印「成功 / 双台不一致 / 取不到」三栏，取不到的锚点不写入。

用法
----
    python tools/gen_anchor_matrix.py            # 采集并写入基线
    python tools/gen_anchor_matrix.py --dry-run  # 只打印不写文件

扩充方式：在 ANCHOR_SPECS 里加一行（见各 kind 的字段说明），重跑本脚本即可。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys

TDXPY_ROOT = pathlib.Path(r"D:\Data\github_repo\tdxpy")
sys.path.insert(0, str(TDXPY_ROOT))
import tdxpy.logger  # noqa: E402

tdxpy.logger.logger.setLevel(logging.CRITICAL)

from tdxpy.exhq import TdxExHq_API  # noqa: E402
from tdxpy.hq import TdxHq_API  # noqa: E402
from tdxpy.mac import TdxMac_API  # noqa: E402

OUT = TDXPY_ROOT / "tests" / "fixtures" / "anchor_matrix.json"

STD_HOSTS = ["111.229.247.189", "116.205.171.132"]
EX_HOSTS = ["139.9.191.175", "116.205.143.214"]
MAC_HOSTS = ["121.37.207.165", "123.60.47.136"]
STD_PORT, EX_PORT = 7709, 7727

#: tick 全天笔数允许的跨服务器差异（实测 000001@2021-01-04 两台差 1 笔——
#: 集合竞价笔归属不同）。取绝对值与相对值中的较大者。
TICK_TOL_ABS = 3
TICK_TOL_REL = 0.002


def _tick_tol(total: int) -> int:
    return max(TICK_TOL_ABS, int(total * TICK_TOL_REL))

# ---------------------------------------------------------------------------
# 锚点清单（维护点）
#   kind=std_bar   : market/code/category/date            → ohlcv
#   kind=std_tick  : market/code/date                     → total/first_time/last_time/first_price/last_price
#   kind=std_minute: market/code/date                     → n/first_time/last_time/first_price/last_price
#   kind=std_xdxr  : market/code                          → n/first/last
#   kind=std_fin   : market/code                          → ipo_date/industry/zongguben
#   kind=mac_bar   : market/code/category/date            → close/amount（MAC 独有 amount）
#   kind=ex_bar    : market/code/category/date            → close
#   kind=ex_range  : market/code/date                     → n/first_dt/last_dt
# ---------------------------------------------------------------------------

STD_BARS: list[tuple[int, str, str]] = [
    # (market, code, date) —— 时间跨度 1999→2026，覆盖牛熊/停牌/长假/熔断
    (1, "600000", "1999-11-10"), (1, "600000", "2005-06-06"), (1, "600000", "2007-05-30"),
    (1, "600000", "2008-10-28"), (1, "600000", "2015-06-05"), (1, "600000", "2016-01-04"),
    (1, "600000", "2020-02-03"), (1, "600000", "2026-11-06"),
    (1, "600519", "2001-08-27"), (1, "600519", "2008-10-28"), (1, "600519", "2015-06-12"),
    (1, "600519", "2026-11-06"),
    (1, "600036", "2008-10-28"), (1, "600036", "2026-11-06"),
    (1, "601318", "2008-10-28"), (1, "601318", "2026-11-06"),
    (1, "600030", "2008-10-28"), (1, "600030", "2026-11-06"),
    (1, "688981", "2020-07-16"), (1, "688981", "2026-11-06"),
    (0, "000001", "2008-10-28"), (0, "000001", "2015-06-12"), (0, "000001", "2026-11-06"),
    (0, "000002", "2005-06-06"), (0, "000002", "2021-12-31"),
    (0, "000858", "2008-10-28"), (0, "000858", "2026-11-06"),
    (0, "002594", "2011-06-30"), (0, "002594", "2020-02-03"),
    (0, "300750", "2018-06-11"), (0, "300750", "2021-12-31"), (0, "300750", "2026-11-06"),
    (1, "510050", "2005-02-23"), (1, "510050", "2015-06-12"),
    (1, "510300", "2012-05-28"), (1, "510300", "2026-11-06"),
    (2, "920992", "2026-10-15"), (2, "920992", "2026-11-06"),
    # 指数（走 get_index_bars）
    (1, "999999", "1999-12-30"), (1, "999999", "2007-10-16"), (1, "999999", "2015-06-12"),
    (1, "999999", "2026-11-06"),
    (0, "399001", "2007-10-16"), (0, "399001", "2015-06-12"),
    (0, "399006", "2015-06-12"), (0, "399006", "2026-11-06"),
    (2, "899050", "2026-10-15"), (2, "899050", "2026-11-06"),
]
STD_INDEX_CODES = {"999999", "399001", "399006", "899050"}

STD_TICKS = [
    (1, "600000", "2000-06-09"), (1, "600000", "2015-06-05"), (1, "600000", "2026-11-06"),
    (1, "600519", "2015-06-12"), (0, "000001", "2021-01-04"), (0, "000002", "2021-12-31"),
    (2, "920992", "2026-09-23"),
]

STD_MINUTES = [
    (1, "600000", "2001-02-05"), (1, "600000", "2015-06-05"), (1, "600000", "2026-11-06"),
    (1, "600519", "2020-02-03"), (0, "000001", "2021-01-04"), (2, "899050", "2026-11-06"),
]

STD_XDXR = [(1, "600000"), (1, "600519"), (0, "000001"), (2, "920992")]
STD_FIN = [(1, "600000"), (1, "600519"), (0, "300750"), (0, "002594"), (1, "688981"), (2, "920992")]

MAC_BARS = [
    (1, "600000", "2020-02-03"), (1, "600000", "2026-11-06"),
    (1, "600519", "2026-11-06"), (0, "000001", "2026-11-06"),
    (0, "300750", "2026-11-06"), (2, "920992", "2026-11-06"),
]

EX_BARS = [
    (31, "00700", "2010-01-04"), (31, "00700", "2020-03-23"), (31, "00700", "2026-11-06"),
    (74, "AAPL", "1990-01-02"), (74, "AAPL", "2008-09-15"), (74, "AAPL", "2026-11-06"),
    (74, "MSFT", "2000-01-03"), (74, "MSFT", "2026-11-06"),
    (73, "SAP", "2026-11-06"), (78, "D05", "2026-11-06"),
    (27, "HSI", "2026-11-06"),
    (28, "RSL8", "2020-01-02"), (29, "ML8", "2020-01-02"),
    (30, "RBL8", "2020-01-02"), (47, "IFL8", "2020-01-02"),
    (30, "RBL8", "2026-11-06"),
]
EX_RANGES = [
    (28, "APL8", "2026-11-03"), (30, "CUL8", "2026-11-03"),
]


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

class BarCache:
    """按 (market, code) 缓存日线翻页结果，供多日期锚点复用。"""

    def __init__(self, api):
        self.api = api
        self.pages: dict[tuple[int, str], list[dict]] = {}

    def _all(self, market: int, code: str, category: int = 4) -> list[dict]:
        key = (market, code, category)
        if key in self.pages:
            return self.pages[key]
        rows: list[dict] = []
        for p in range(12):  # 12×800 = 9600 根，覆盖 26 年
            fetch = self.api.get_index_bars if code in STD_INDEX_CODES else self.api.get_security_bars
            try:
                page = fetch(category, market, code, p * 800, 800) or []
            except Exception:
                break
            if not page:
                break
            rows.extend(page)
            if len(page) < 800:
                break
        self.pages[key] = rows
        return rows

    def on_date(self, market: int, code: str, date: str) -> dict | None:
        for b in self._all(market, code):
            if str(b["datetime"]).startswith(date):
                return b
        return None


def _bar_snapshot(b: dict) -> dict:
    return {
        "open": round(float(b["open"]), 3),
        "high": round(float(b["high"]), 3),
        "low": round(float(b["low"]), 3),
        "close": round(float(b["close"]), 3),
        "vol": round(float(b["vol"]), 3),
    }


def collect_std(hosts: list[str]) -> tuple[list[dict], list[str]]:
    anchors: list[dict] = []
    problems: list[str] = []

    results: dict[str, list[dict]] = {}
    for host in hosts:
        api = TdxHq_API(raise_exception=False)
        if api.connect(host, STD_PORT, time_out=6) is False:
            problems.append(f"std {host} 连接失败")
            continue
        try:
            cache = BarCache(api)
            for market, code, date in STD_BARS:
                b = cache.on_date(market, code, date)
                if b is None:
                    problems.append(f"std_bar {code}@{date} 无数据 ({host})")
                    continue
                anchor = {
                    "id": f"std_bar.{code}.{date}",
                    "kind": "std_bar",
                    "market": market,
                    "code": code,
                    "date": date,
                    "expect": _bar_snapshot(b),
                }
                results.setdefault(anchor["id"], []).append(anchor)

            for market, code, date in STD_TICKS:
                rows, start, total = [], 0, 0
                for _ in range(12):
                    r = api.get_history_transaction_data(
                        market, code, start, 800, date.replace("-", "")
                    ) or []
                    if not r:
                        break
                    rows.extend(r)
                    start += 800
                    total += len(r)
                    if len(r) < 800:
                        break
                if not rows:
                    problems.append(f"std_tick {code}@{date} 无数据 ({host})")
                    continue
                anchor = {
                    "id": f"std_tick.{code}.{date}",
                    "kind": "std_tick",
                    "market": market,
                    "code": code,
                    "date": date,
                    "expect": {
                        "total": total,
                        "tolerance": _tick_tol(total),
                        "first_time": rows[0]["time"],
                        "last_time": rows[-1]["time"],
                        "first_price": round(float(rows[0]["price"]), 3),
                        "last_price": round(float(rows[-1]["price"]), 3),
                    },
                }
                results.setdefault(anchor["id"], []).append(anchor)

            for market, code, date in STD_MINUTES:
                rows = api.get_history_minute_time_data(market, code, date.replace("-", "")) or []
                if not rows:
                    problems.append(f"std_minute {code}@{date} 无数据 ({host})")
                    continue
                anchor = {
                    "id": f"std_minute.{code}.{date}",
                    "kind": "std_minute",
                    "market": market,
                    "code": code,
                    "date": date,
                    "expect": {
                        # 历史分时返回 (price, vol) 序列，无时间字段：索引 0 = 09:31，索引 239 = 15:00
                        "n": len(rows),
                        "first_price": round(float(rows[0]["price"]), 3),
                        "last_price": round(float(rows[-1]["price"]), 3),
                    },
                }
                results.setdefault(anchor["id"], []).append(anchor)

            for market, code in STD_XDXR:
                rows = api.get_xdxr_info(market, code) or []
                if not rows:
                    problems.append(f"std_xdxr {code} 无数据 ({host})")
                    continue
                anchor = {
                    "id": f"std_xdxr.{code}",
                    "kind": "std_xdxr",
                    "market": market,
                    "code": code,
                    "expect": {
                        "n": len(rows),
                        "first": f"{rows[0]['year']}-{rows[0]['month']:02d}",
                        "last": f"{rows[-1]['year']}-{rows[-1]['month']:02d}",
                    },
                }
                results.setdefault(anchor["id"], []).append(anchor)

            for market, code in STD_FIN:
                f = api.get_finance_info(market, code)
                if not f:
                    problems.append(f"std_fin {code} 无数据 ({host})")
                    continue
                anchor = {
                    "id": f"std_fin.{code}",
                    "kind": "std_fin",
                    "market": market,
                    "code": code,
                    "expect": {
                        "ipo_date": int(f.get("ipo_date") or 0),
                        "province": int(f.get("province") or 0),
                        "industry": int(f.get("industry") or 0),
                    },
                }
                results.setdefault(anchor["id"], []).append(anchor)
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    # 交叉验证：两台一致才写入（tick 笔数按容差比较）
    for aid, got in results.items():
        if len(got) >= 2:
            a, b = got[0]["expect"], got[1]["expect"]
            if "tolerance" in a and "total" in a:
                same = (
                    abs(a["total"] - b["total"]) <= a["tolerance"]
                    and a["first_time"] == b["first_time"]
                    and a["last_time"] == b["last_time"]
                    and abs(a["first_price"] - b["first_price"]) < 1e-6
                    and abs(a["last_price"] - b["last_price"]) < 1e-6
                )
            else:
                same = a == b
            if same:
                anchors.append(got[0])
            else:
                problems.append(f"双台不一致 {aid}: {a} vs {b}")
        else:
            # 单台也能取到（另一台失败）——仍写入，但标注
            got[0]["single_host"] = True
            anchors.append(got[0])
    return anchors, problems


def collect_mac(hosts: list[str]) -> tuple[list[dict], list[str]]:
    anchors, problems = [], []
    for host in hosts:
        api = TdxMac_API(raise_exception=False)
        if api.connect(host, STD_PORT, time_out=6) is False:
            problems.append(f"mac {host} 连接失败")
            continue
        try:
            cache: dict[tuple[int, str], list[dict]] = {}
            for market, code, date in MAC_BARS:
                if (market, code) not in cache:
                    rows = []
                    # 4 页 × 700 = 2800 根；与 tests/test_anchor_matrix.py 的取值窗口保持一致
                    for p in range(4):
                        page = api.get_mac_bars(market, code, 4, p * 700, 700) or []
                        if not page:
                            break
                        rows.extend(page)
                        if len(page) < 700:
                            break
                    cache[(market, code)] = rows
                hit = next((b for b in cache[(market, code)] if str(b["datetime"]).startswith(date)), None)
                if hit is None:
                    problems.append(f"mac_bar {code}@{date} 无数据 ({host})")
                    continue
                expect = {"close": round(float(hit["close"]), 3), "amount_pos": float(hit.get("amount") or 0) > 0}
                aid = f"mac_bar.{code}.{date}"
                existing = next((a for a in anchors if a["id"] == aid), None)
                if existing is None:
                    anchors.append({"id": aid, "kind": "mac_bar", "market": market, "code": code, "date": date, "expect": expect})
                elif existing["expect"] != expect:
                    problems.append(f"mac 双台不一致 {aid}")
                    anchors.remove(existing)
        finally:
            try:
                api.disconnect()
            except Exception:
                pass
    return anchors, problems


def collect_ex(hosts: list[str]) -> tuple[list[dict], list[str]]:
    anchors, problems = [], []
    per_host: dict[str, dict[str, dict]] = {}
    for host in hosts:
        api = TdxExHq_API(raise_exception=False)
        if api.connect(host, EX_PORT, time_out=6) is False:
            problems.append(f"ex {host} 连接失败")
            continue
        try:
            cache: dict[tuple[int, str], list[dict]] = {}
            for market, code, date in EX_BARS:
                if (market, code) not in cache:
                    rows = []
                    for p in range(20):
                        page = api.get_instrument_bars(4, market, code, p * 700, 700) or []
                        if not page:
                            break
                        rows.extend(page)
                        if len(page) < 700:
                            break
                    cache[(market, code)] = rows
                hit = next((b for b in cache[(market, code)] if str(b["datetime"]).startswith(date)), None)
                if hit is None:
                    problems.append(f"ex_bar {code}@{date} 无数据 ({host})")
                    continue
                per_host.setdefault(host, {})[f"ex_bar.{code}.{date}"] = {
                    "id": f"ex_bar.{code}.{date}",
                    "kind": "ex_bar",
                    "market": market,
                    "code": code,
                    "date": date,
                    "expect": {"close": round(float(hit["close"]), 3)},
                }
            for market, code, date in EX_RANGES:
                rows = api.get_history_instrument_bars_range(market, code, int(date.replace("-", "")), int(date.replace("-", ""))) or []
                if not rows:
                    problems.append(f"ex_range {code}@{date} 无数据 ({host})")
                    continue
                per_host.setdefault(host, {})[f"ex_range.{code}.{date}"] = {
                    "id": f"ex_range.{code}.{date}",
                    "kind": "ex_range",
                    "market": market,
                    "code": code,
                    "date": date,
                    "expect": {"n": len(rows), "first_dt": str(rows[0].get("datetime")), "last_dt": str(rows[-1].get("datetime"))},
                }
        finally:
            try:
                api.disconnect()
            except Exception:
                pass

    # 交叉验证
    ids = set()
    for host_map in per_host.values():
        ids |= set(host_map)
    for aid in sorted(ids):
        vals = [h[aid] for h in per_host.values() if aid in h]
        if len(vals) >= 2:
            if vals[0]["expect"] == vals[1]["expect"]:
                anchors.append(vals[0])
            else:
                problems.append(f"ex 双台不一致 {aid}: {vals[0]['expect']} vs {vals[1]['expect']}")
        else:
            vals[0]["single_host"] = True
            anchors.append(vals[0])
    return anchors, problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    all_anchors: list[dict] = []
    all_problems: list[str] = []
    for name, fn, hosts in (("std", collect_std, STD_HOSTS), ("mac", collect_mac, MAC_HOSTS), ("ex", collect_ex, EX_HOSTS)):
        print(f"--- 采集 {name} ---")
        got, probs = fn(hosts)
        print(f"  {name}: {len(got)} 锚点，{len(probs)} 问题")
        for p in probs:
            print("   !", p)
        all_anchors.extend(got)
        all_problems.extend(probs)

    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "generator": "tools/gen_anchor_matrix.py",
        "policy": "adjust=0 不复权；双服务器交叉验证；固定日期+固定值",
        "std_hosts": STD_HOSTS,
        "ex_hosts": EX_HOSTS,
        "mac_hosts": MAC_HOSTS,
        "count": len(all_anchors),
        "anchors": sorted(all_anchors, key=lambda a: a["id"]),
    }
    print(f"\n=== 总计 {len(all_anchors)} 锚点（问题 {len(all_problems)}）===")
    kinds: dict[str, int] = {}
    for a in all_anchors:
        kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print("按类型:", kinds)
    if args.dry_run:
        print("dry-run，不写文件")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written:", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
