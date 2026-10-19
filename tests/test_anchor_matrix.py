# -*- coding: utf-8 -*-
"""历史锚点矩阵：用大批「固定市场 × 固定品种 × 固定日期 × 固定值」钉住服务器历史数据。

与动态锚点测试（test_server_guard / test_online_smoke）互补：
- 动态锚点守「数据面活着 + 双通道一致」，跨日不失效，但不检查历史数据是否被改；
- 本矩阵守「历史数据不变」——任何一格变红，说明服务器历史数据可能被重灌/缺失/
  格式变更，或客户端解析回归。

基线文件：tests/fixtures/anchor_matrix.json（由 tools/gen_anchor_matrix.py 采集，
双服务器交叉验证；tick 笔数带容差——实测不同服务器可能差 1~3 笔）。

红的处置顺序：
1. 换另一台服务器复测同一格（区分「服务器 A 变了」还是「所有服务器都变了」）；
2. 确认是服务器侧变化后，重跑 tools/gen_anchor_matrix.py 更新基线；
3. 若是解析回归，修 parser 而不是改基线。

跳过：设 TDXPY_SKIP_ANCHOR_MATRIX=1 可整体跳过（CI 无网时用）。
"""
import json
import os
import pathlib
import sys
import unittest

from tdxpy.exceptions import TdxFunctionCallError
from tdxpy.exhq import TdxExHq_API
from tdxpy.hq import TdxHq_API
from tdxpy.mac import TdxMac_API

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "anchor_matrix.json"
STD_HOST = os.environ.get("TDXPY_TEST_HQ_HOST", "111.229.247.189")
EX_HOST = os.environ.get("TDXPY_TEST_EX_HOST", "139.9.191.175")
MAC_HOST = os.environ.get("TDXPY_TEST_MAC_HOST", "121.37.207.165")
STD_PORT, EX_PORT = 7709, 7727
TIMEOUT = 8

GUARD_MISSING = (
    "\n⚠️⚠️⚠️ 锚点矩阵无法连接服务器——历史数据不变性守护本次未验证: {}"
)

INDEX_CODES = {"999999", "399001", "399006", "899050"}


def _load() -> dict:
    if not FIXTURE.exists():
        raise unittest.SkipTest(f"锚点基线不存在（先跑 tools/gen_anchor_matrix.py）：{FIXTURE}")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _connect_or_skip(cls, host, port):
    api = cls(raise_exception=False)
    try:
        ok = api.connect(host, port, time_out=TIMEOUT)
    except Exception as e:  # noqa
        api.disconnect()
        print(GUARD_MISSING.format(f"{host}:{port} {e}"), file=sys.stderr)
        raise unittest.SkipTest(f"连接失败 {host}:{port}: {e}")
    if ok is False:
        api.disconnect()
        print(GUARD_MISSING.format(f"{host}:{port}"), file=sys.stderr)
        raise unittest.SkipTest(f"连接失败 {host}:{port}")
    return api


class _BarCache:
    """按 (market, code) 缓存翻页结果，多日期锚点复用。"""

    def __init__(self, api):
        self.api = api
        self.pages: dict[tuple, list[dict]] = {}

    def _all(self, market, code, category=4):
        key = (market, code, category)
        if key in self.pages:
            return self.pages[key]
        rows: list[dict] = []
        fetch = self.api.get_index_bars if code in INDEX_CODES else self.api.get_security_bars
        for p in range(12):
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

    def on_date(self, market, code, date):
        for b in self._all(market, code):
            if str(b["datetime"]).startswith(date):
                return b
        return None


class _AnchorBase(unittest.TestCase):
    payload: dict = {}
    std_api = None
    mac_api = None
    ex_api = None

    @classmethod
    def setUpClass(cls):
        if os.environ.get("TDXPY_SKIP_ANCHOR_MATRIX") == "1":
            raise unittest.SkipTest("TDXPY_SKIP_ANCHOR_MATRIX=1")
        cls.payload = _load()
        cls.anchors = cls.payload["anchors"]

    @classmethod
    def tearDownClass(cls):
        for name in ("std_api", "mac_api", "ex_api"):
            api = getattr(cls, name, None)
            if api is not None:
                try:
                    api.disconnect()
                except Exception:
                    pass

    # ---- 懒连接 ----
    @classmethod
    def _std(cls):
        if cls.std_api is None:
            cls.std_api = _connect_or_skip(TdxHq_API, STD_HOST, STD_PORT)
            cls._bar_cache = _BarCache(cls.std_api)
        return cls.std_api

    @classmethod
    def _mac(cls):
        if cls.mac_api is None:
            cls.mac_api = _connect_or_skip(TdxMac_API, MAC_HOST, STD_PORT)
        return cls.mac_api

    @classmethod
    def _ex(cls):
        if cls.ex_api is None:
            cls.ex_api = _connect_or_skip(TdxExHq_API, EX_HOST, EX_PORT)
        return cls.ex_api

    # ---- 断言工具（失败消息统一带处置指引）----
    def _fail_msg(self, a, detail):
        return (
            f"锚点 {a['id']} 变化：{detail}\n"
            f"  → 服务器历史数据可能被重灌/缺失/格式变更，或解析回归。\n"
            f"  → 处置：换一台服务器复测；确认服务器侧变化后重跑 tools/gen_anchor_matrix.py 更新基线。"
        )

    def _by_kind(self, kind):
        return [a for a in self.anchors if a["kind"] == kind]

    def _num(self, a, key, actual, places=2):
        expect = a["expect"][key]
        self.assertAlmostEqual(
            float(actual), float(expect), places=places,
            msg=self._fail_msg(a, f"{key} 预期 {expect} 实际 {round(float(actual), 3)}"))


class TestStdBar(_AnchorBase):
    def test_std_bar(self):
        rows = self._by_kind("std_bar")
        self.assertGreater(len(rows), 0, "基线里没有 std_bar 锚点")
        api = self._std()
        for a in rows:
            with self.subTest(anchor=a["id"]):
                bar = self._bar_cache.on_date(a["market"], a["code"], a["date"])
                self.assertIsNotNone(
                    bar, self._fail_msg(a, f"{a['date']} 当日 bar 不再返回——数据面消失或该日已从历史移除"))
                for key in ("open", "high", "low", "close"):
                    self._num(a, key, bar[key], places=2)
                # 成交量四舍五入到整数股/手，允许 1 单位误差（两端 float 表示差异）
                self.assertAlmostEqual(
                    float(bar["vol"]), float(a["expect"]["vol"]), delta=max(1.0, abs(float(a["expect"]["vol"])) * 1e-6),
                    msg=self._fail_msg(a, f"vol 预期 {a['expect']['vol']} 实际 {bar['vol']}"))


class TestStdTick(_AnchorBase):
    def test_std_tick(self):
        rows = self._by_kind("std_tick")
        self.assertGreater(len(rows), 0)
        api = self._std()
        for a in rows:
            with self.subTest(anchor=a["id"]):
                e = a["expect"]
                ticks, start, total = [], 0, 0
                for _ in range(12):
                    page = api.get_history_transaction_data(
                        a["market"], a["code"], start, 800, a["date"].replace("-", "")) or []
                    if not page:
                        break
                    ticks.extend(page)
                    start += 800
                    total += len(page)
                    if len(page) < 800:
                        break
                self.assertGreater(total, 0, self._fail_msg(a, "该日分笔整体消失"))
                self.assertLessEqual(
                    abs(total - e["total"]), e.get("tolerance", 3),
                    self._fail_msg(a, f"分笔总数预期 {e['total']}±{e.get('tolerance', 3)} 实际 {total}"))
                self.assertEqual(ticks[0]["time"], e["first_time"], self._fail_msg(a, f"首笔时间 {ticks[0]['time']} != {e['first_time']}"))
                self.assertEqual(ticks[-1]["time"], e["last_time"], self._fail_msg(a, f"末笔时间 {ticks[-1]['time']} != {e['last_time']}"))
                self.assertAlmostEqual(float(ticks[0]["price"]), e["first_price"], places=3,
                                       msg=self._fail_msg(a, f"首笔价 {ticks[0]['price']} != {e['first_price']}"))
                self.assertAlmostEqual(float(ticks[-1]["price"]), e["last_price"], places=3,
                                       msg=self._fail_msg(a, f"末笔价 {ticks[-1]['price']} != {e['last_price']}"))


class TestStdMinute(_AnchorBase):
    def test_std_minute(self):
        rows = self._by_kind("std_minute")
        self.assertGreater(len(rows), 0)
        api = self._std()
        for a in rows:
            with self.subTest(anchor=a["id"]):
                e = a["expect"]
                data = api.get_history_minute_time_data(
                    a["market"], a["code"], a["date"].replace("-", "")) or []
                self.assertEqual(len(data), e["n"], self._fail_msg(a, f"分时点数 {len(data)} != {e['n']}"))
                self.assertAlmostEqual(float(data[0]["price"]), e["first_price"], places=3,
                                       msg=self._fail_msg(a, f"首点价 {data[0]['price']} != {e['first_price']}"))
                self.assertAlmostEqual(float(data[-1]["price"]), e["last_price"], places=3,
                                       msg=self._fail_msg(a, f"末点价 {data[-1]['price']} != {e['last_price']}"))


class TestStdXdxr(_AnchorBase):
    def test_std_xdxr(self):
        rows = self._by_kind("std_xdxr")
        self.assertGreater(len(rows), 0)
        api = self._std()
        for a in rows:
            with self.subTest(anchor=a["id"]):
                e = a["expect"]
                data = api.get_xdxr_info(a["market"], a["code"]) or []
                self.assertEqual(len(data), e["n"], self._fail_msg(a, f"除权记录数 {len(data)} != {e['n']}"))
                first = f"{data[0]['year']}-{data[0]['month']:02d}"
                last = f"{data[-1]['year']}-{data[-1]['month']:02d}"
                self.assertEqual(first, e["first"], self._fail_msg(a, f"最早除权 {first} != {e['first']}"))
                self.assertEqual(last, e["last"], self._fail_msg(a, f"最新除权 {last} != {e['last']}"))


class TestStdFinance(_AnchorBase):
    def test_std_finance(self):
        rows = self._by_kind("std_fin")
        self.assertGreater(len(rows), 0)
        api = self._std()
        for a in rows:
            with self.subTest(anchor=a["id"]):
                e = a["expect"]
                f = api.get_finance_info(a["market"], a["code"])
                self.assertIsNotNone(f, self._fail_msg(a, "finance_info 返回 None"))
                self.assertEqual(int(f.get("ipo_date") or 0), e["ipo_date"],
                                 self._fail_msg(a, f"ipo_date {f.get('ipo_date')} != {e['ipo_date']}"))
                self.assertEqual(int(f.get("province") or 0), e["province"], self._fail_msg(a, "province 变化"))
                self.assertEqual(int(f.get("industry") or 0), e["industry"], self._fail_msg(a, "industry 变化"))


class TestMacBar(_AnchorBase):
    def test_mac_bar(self):
        rows = self._by_kind("mac_bar")
        self.assertGreater(len(rows), 0)
        api = self._mac()
        cache: dict[tuple, list[dict]] = {}
        for a in rows:
            with self.subTest(anchor=a["id"]):
                key = (a["market"], a["code"])
                if key not in cache:
                    acc = []
                    for p in range(4):
                        page = api.get_mac_bars(a["market"], a["code"], 4, p * 700, 700) or []
                        if not page:
                            break
                        acc.extend(page)
                        if len(page) < 700:
                            break
                    cache[key] = acc
                bar = next((b for b in cache[key] if str(b["datetime"]).startswith(a["date"])), None)
                self.assertIsNotNone(bar, self._fail_msg(a, f"MAC {a['date']} bar 消失"))
                self._num(a, "close", bar["close"], places=2)
                if a["expect"].get("amount_pos"):
                    self.assertGreater(float(bar.get("amount") or 0), 0,
                                       self._fail_msg(a, "MAC amount 字段变为非正——amount 通道可能失效"))


class TestExBar(_AnchorBase):
    def test_ex_bar(self):
        rows = self._by_kind("ex_bar")
        self.assertGreater(len(rows), 0)
        api = self._ex()
        cache: dict[tuple, list[dict]] = {}
        for a in rows:
            with self.subTest(anchor=a["id"]):
                key = (a["market"], a["code"])
                if key not in cache:
                    acc = []
                    for p in range(20):
                        page = api.get_instrument_bars(4, a["market"], a["code"], p * 700, 700) or []
                        if not page:
                            break
                        acc.extend(page)
                        if len(page) < 700:
                            break
                    cache[key] = acc
                bar = next((b for b in cache[key] if str(b["datetime"]).startswith(a["date"])), None)
                self.assertIsNotNone(bar, self._fail_msg(a, f"EX {a['date']} bar 消失（或市场/代码变更）"))
                self._num(a, "close", bar["close"], places=2)


class TestExRange(_AnchorBase):
    def test_ex_range(self):
        rows = self._by_kind("ex_range")
        self.assertGreater(len(rows), 0)
        api = self._ex()
        for a in rows:
            with self.subTest(anchor=a["id"]):
                e = a["expect"]
                day = int(a["date"].replace("-", ""))
                data = api.get_history_instrument_bars_range(a["market"], a["code"], day, day) or []
                self.assertEqual(len(data), e["n"], self._fail_msg(a, f"分钟条数 {len(data)} != {e['n']}"))
                self.assertEqual(str(data[0].get("datetime")), e["first_dt"],
                                 self._fail_msg(a, f"首根 {data[0].get('datetime')} != {e['first_dt']}"))
                self.assertEqual(str(data[-1].get("datetime")), e["last_dt"],
                                 self._fail_msg(a, f"末根 {data[-1].get('datetime')} != {e['last_dt']}"))


if __name__ == "__main__":
    unittest.main()
