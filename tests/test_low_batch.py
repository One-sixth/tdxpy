# -*- coding: utf-8 -*-
"""LOW 批次防线测试（2026-11-05）：六项 LOW 调查处理落地。

- 转债/债券/优先股段登记（get_security_type + SECURITY_COEFFICIENT；转债 0.0001
  系数经 amount 恒等式在线仲裁定案，SZ 14 段优先股 ×100 偏差修正）
- 翻页停滞守卫（_page_stall_guard 单元 + bars/_ticks 集成 mock）
- 指数板块码映射（_index_board_code）
- index_trade_stats 聚合门面（mock 成分+逐笔：增量/累计/竞价剔除/失败隔离）

运行：python tests/test_low_batch.py -v
"""
import unittest
from unittest import mock

import pandas as pd

from tdxpy.client import (
    TdxClient,
    _index_board_code,
    _page_stall_guard,
)
from tdxpy.constants import SECURITY_COEFFICIENT, SECURITY_TYPE
from tdxpy.exceptions import TdxFunctionCallError
from tdxpy.helper import get_security_coefficient, get_security_type


# --------------------------------------------------------------------------- #
# 1. 段登记与系数（在线实测 2026-11-05 定案）
# --------------------------------------------------------------------------- #
class TestSegmentRegistry(unittest.TestCase):
    """段登记：优先股/债券段/申购段 + 防御段 + 待吸收文档误传澄清。"""

    def test_sz_14_is_preferred_not_bond(self):
        # SZ 14 段实为优先股（宁行优01 dp=2，曾误吞 SZ_BOND 致 ×100 偏差——audit 实锤）
        self.assertEqual(get_security_type(market=0, code="140001"), "SZ_PREFERRED")
        self.assertEqual(get_security_coefficient(market=0, code="140001"), 0.01)

    def test_sh_36_is_preferred(self):
        self.assertEqual(get_security_type(market=1, code="360001"), "SH_PREFERRED")
        self.assertEqual(get_security_coefficient(market=1, code="360001"), 0.01)

    def test_sz_bond_new_segments(self):
        # 19 地方债 / 50 ABS / 52 公司债 / 56 地方债（dp=4 全量 + 恒等式 K=10 实测）
        for code in ("190000", "500005", "520000", "563000"):
            self.assertEqual(get_security_type(market=0, code=code), "SZ_BOND", code)
            self.assertEqual(get_security_coefficient(market=0, code=code), 0.0001)

    def test_sh_bond_new_segments(self):
        # 02 贴现国债 / 15-19 地方债城投 / 23 地方债 / 24 公司债 / 27 地方债
        for code in ("020812", "152010", "160003", "171000", "184002",
                     "198000", "230000", "240020", "270000"):
            self.assertEqual(get_security_type(market=1, code=code), "SH_BOND", code)
            self.assertEqual(get_security_coefficient(market=1, code=code), 0.0001)

    def test_sh_78_subscription_is_stock(self):
        # 78 科创板新股申购段（787827 钶锐申购）
        self.assertEqual(get_security_type(market=1, code="787827"), "SH_A_STOCK")

    def test_sh_79_stays_defensive(self):
        # 79 段"资金控制"= 交易所内部占用段，保持 raise 防御不登记
        with self.assertRaises(NotImplementedError):
            get_security_type(market=1, code="799970")

    def test_convertible_bond_always_in_bond_segment(self):
        # 澄清待吸收文档误传："SH_BOND 段不含 110/113/118"不成立——"11" 前两位
        # 本就覆盖 110/113/118；系数 0.0001 经 amount 恒等式在线仲裁定案（eltdx
        # ×1000 口径与 tdxpy raw 定义不同）
        for code in ("110076", "110090", "113065", "118000"):
            self.assertEqual(get_security_type(market=1, code=code), "SH_BOND", code)
        for code in ("123259", "127089", "128141"):
            self.assertEqual(get_security_type(market=0, code=code), "SZ_BOND", code)
        self.assertEqual(get_security_coefficient(market=1, code="110090"), 0.0001)
        self.assertEqual(get_security_coefficient(market=0, code="127089"), 0.0001)

    def test_coefficient_table_has_preferred(self):
        self.assertEqual(SECURITY_COEFFICIENT["SZ_PREFERRED"], [0.01, 0.01])
        self.assertEqual(SECURITY_COEFFICIENT["SH_PREFERRED"], [0.01, 0.01])

    def test_reader_coefficient_in_sync_with_constants(self):
        """离线 vipdoc reader 的段表副本与在线 constants 保持一致（R1 复审：
        09-17 债券系数修复曾漏 reader 副本致离线价格 ×10——加锁防再漂移）。"""
        from tdxpy.reader.daily_bar_reader import TdxDailyBarReader

        self.assertEqual(TdxDailyBarReader.SECURITY_COEFFICIENT, SECURITY_COEFFICIENT)
        self.assertEqual(TdxDailyBarReader.SECURITY_TYPE, SECURITY_TYPE)
        # 抽段：新段在 reader 侧路由正确
        r = TdxDailyBarReader.__new__(TdxDailyBarReader)
        self.assertEqual(r.get_security_type("sz140001.day"), "SZ_PREFERRED")
        self.assertEqual(r.get_security_type("sz190000.day"), "SZ_BOND")
        self.assertEqual(r.get_security_type("sh152010.day"), "SH_BOND")
        self.assertEqual(r.get_security_type("sh360001.day"), "SH_PREFERRED")
        self.assertEqual(r.get_security_type("sh787827.day"), "SH_A_STOCK")

    def test_security_type_list_complete(self):
        """constants.SECURITY_TYPE 列表须覆盖系数表全部键（R1 LOW：防新类型漏登列表）。"""
        self.assertEqual(sorted(SECURITY_TYPE), sorted(SECURITY_COEFFICIENT.keys()))


# --------------------------------------------------------------------------- #
# 2. 翻页停滞守卫
# --------------------------------------------------------------------------- #
class TestPageStallGuard(unittest.TestCase):
    def _row(self, dt):
        return {"datetime": dt}

    def test_first_page_returns_key(self):
        key = _page_stall_guard(None, [self._row("2026-11-04")], lambda r: r["datetime"], ctx="t")
        self.assertEqual(key, "2026-11-04")

    def test_same_key_raises(self):
        with self.assertRaises(RuntimeError):
            _page_stall_guard("d1", [self._row("d1")], lambda r: r["datetime"], ctx="t")

    def test_different_key_ok(self):
        key = _page_stall_guard("d1", [self._row("d2")], lambda r: r["datetime"], ctx="t")
        self.assertEqual(key, "d2")

    def test_none_key_skipped(self):
        # 键取 None（缺字段）时跳过检测，不误判
        key = _page_stall_guard(None, [{}], lambda r: r.get("missing"), ctx="t")
        self.assertIsNone(key)
        key = _page_stall_guard("d1", [{}], lambda r: r.get("missing"), ctx="t")
        self.assertEqual(key, "d1")

    def test_empty_page_passthrough(self):
        self.assertIsNone(_page_stall_guard(None, [], lambda r: r["x"], ctx="t"))
        self.assertEqual(_page_stall_guard("d1", [], lambda r: r["x"], ctx="t"), "d1")


class TestPagingStallIntegration(unittest.TestCase):
    """bars/ticks 翻页集成：mock 服务器对同一 pos 重复返回同页 → RuntimeError。"""

    def _client(self, pages_by_pos):
        c = TdxClient.__new__(TdxClient)  # 跳过 __init__（不建连）
        calls = {"n": 0}

        def fake_hq_call(method, *args, **kwargs):
            # bars: (category, market_id, code, pos, cnt) → pos=args[3]
            rows = pages_by_pos.get(args[3])
            if rows is None:
                return []
            calls["n"] += 1
            return [dict(r) for r in rows]

        c._hq_call = fake_hq_call
        c.calls = calls
        return c

    def test_bars_stall_raises(self):
        same_page = [{"datetime": f"2026-01-0{i}", "close": 1.0} for i in range(1, 6)]
        # 无论 pos 多少都回同一页 → 停滞
        c = self._client({0: same_page, 5: same_page, 10: same_page})
        with self.assertRaises(RuntimeError):
            c.bars("sh.600000", "day", start=0, count=5, all_pages=True, page_size=5)

    def test_bars_normal_paging_untouched(self):
        p0 = [{"datetime": f"2026-01-0{i}", "close": 1.0} for i in range(1, 6)]      # pos=0
        p5 = [{"datetime": f"2026-01-1{i}", "close": 1.0} for i in range(1, 4)]      # pos=5 短页
        c = self._client({0: p0, 5: p5})
        df = c.bars("sh.600000", "day", start=0, count=8, all_pages=True, page_size=5)
        self.assertEqual(len(df), 8)
        self.assertEqual(df.iloc[0]["datetime"], "2026-01-01")
        self.assertEqual(df.iloc[-1]["datetime"], "2026-01-13")

    def test_ticks_stall_raises(self):
        # std tick 键 = (time, price, vol) 三元组：同键重复页 raise
        same_page = [{"time": "09:30:0%s" % i, "price": 10.0, "vol": 100.0,
                      "buyorsell": 1} for i in range(4)]
        c = TdxClient.__new__(TdxClient)

        def fake_hq_call(method, *args, **kwargs):
            return [dict(r) for r in same_page]  # 永远同页

        c._hq_call = fake_hq_call
        with mock.patch("tdxpy.client.MAX_PAGE_SIZE", 4), self.assertRaises(RuntimeError):
            c.ticks("sh.600000", date="2026-11-04", all=True)

    def test_ticks_same_minute_multi_rows_ok(self):
        # 相邻页首行 (time,price,vol) 三元组不同即不误判（time 粒度到秒可区分）
        p0 = [{"time": f"09:30:{i:02d}", "price": 10.0, "vol": 100.0, "buyorsell": 1}
              for i in range(0, 60, 10)]  # 6 行满页
        p1 = [{"time": "09:31:00", "price": 10.1, "vol": 50.0, "buyorsell": 2}]  # 短页
        c = TdxClient.__new__(TdxClient)
        seq = {0: p0, 6: p1}

        def fake_hq_call(method, *args, **kwargs):
            rows = seq.get(args[2])
            return [dict(r) for r in rows] if rows else []

        c._hq_call = fake_hq_call
        with mock.patch("tdxpy.client.MAX_PAGE_SIZE", 6):
            df = c.ticks("sh.600000", date="2026-11-04", all=True)
        self.assertEqual(len(df), 7)


# --------------------------------------------------------------------------- #
# 3. 指数板块码映射
# --------------------------------------------------------------------------- #
class TestIndexBoardCode(unittest.TestCase):
    def test_mappings(self):
        self.assertEqual(_index_board_code("000300"), 31300)
        self.assertEqual(_index_board_code("000016"), 31016)
        self.assertEqual(_index_board_code("399001"), 30001)
        self.assertEqual(_index_board_code("399006"), 30006)
        self.assertEqual(_index_board_code("899050"), 32050)
        self.assertEqual(_index_board_code("880300"), 20300)

    def test_non_index_raises(self):
        with self.assertRaises(ValueError):
            _index_board_code("600000")
        with self.assertRaises(ValueError):
            _index_board_code("110090")


# --------------------------------------------------------------------------- #
# 4. index_trade_stats 聚合（mock 成分 + 逐笔，不联网）
# --------------------------------------------------------------------------- #
class _FakeTickClient(TdxClient):
    """mock 版：成分固定 2 只（深/沪），逐笔返回预设页；统计 close 次数验证回收。"""

    closed = 0

    def __init__(self, *a, **k):  # 不建连
        pass

    def close(self):
        _FakeTickClient.closed += 1

    def mac_members(self, board, sort_type=0, start=0, count=80):
        rows = [
            {"market": 0, "code": "000001", "name": "平安银行", "total": 2},
            {"market": 1, "code": "600000", "name": "浦发银行", "total": 2},
        ]
        return pd.DataFrame(rows[start:start + count])

    def mac_ticks(self, symbol, date=None, start=0, count=1000, drop_auction_orders=True):
        if start > 0:
            return pd.DataFrame()
        # 两页语义在此简化为单页（rows<1000 短页终止）
        return pd.DataFrame([
            # 平安银行 09:30 两笔 + 1 条竞价委托（bs_flag=8 应被剔除）
            {"time": "09:30:00", "price": 11.0, "vol": 100.0, "trade_count": 2, "bs_flag": 0},
            {"time": "09:30:10", "price": 11.1, "vol": 200.0, "trade_count": 3, "bs_flag": 0},
            {"time": "09:15:00", "price": 0.0, "vol": 9999.0, "trade_count": 0, "bs_flag": 8},
        ]) if symbol == "sz.000001" else pd.DataFrame([
            {"time": "09:30:00", "price": 8.0, "vol": 50.0, "trade_count": 1, "bs_flag": 0},
        ])


class TestIndexTradeStats(unittest.TestCase):
    def test_aggregation(self):
        _FakeTickClient.closed = 0
        c = _FakeTickClient()
        with mock.patch.object(
            __import__("tdxpy.client", fromlist=["TdxClient"]), "TdxClient", _FakeTickClient
        ):
            out = c.index_trade_stats("sh.000016")
        # 聚合：09:30 → vol=100+200+50=350（竞价委托 9999 剔除）、trades=2+3+1=6
        # （深/沪两票的 09:30 聚为同一分钟行 → 输出仅 1 行）
        self.assertEqual(len(out), 1)
        row = out[out["minute"] == "09:30"].iloc[0]
        self.assertEqual(float(row["vol"]), 350.0)
        self.assertEqual(int(row["trade_count"]), 6)
        self.assertEqual(float(row["cum_vol"]), 350.0)
        self.assertEqual(float(row["cum_trades"]), 6)
        # 每个并发 worker 的 client 都被 close（资源回收）
        self.assertGreaterEqual(_FakeTickClient.closed, 1)

    def test_failure_isolation(self):
        """单只成分抛异常 → skip_errors=True 下其余成分照常聚合。"""
        _FakeTickClient.closed = 0
        c = _FakeTickClient()

        boom = _FakeTickClient.mac_ticks

        def flaky(self, symbol, *a, **k):
            if symbol == "sz.000001":
                raise TdxFunctionCallError("mock 网络炸")
            return boom(self, symbol, *a, **k)

        with mock.patch.object(_FakeTickClient, "mac_ticks", flaky), mock.patch.object(
            __import__("tdxpy.client", fromlist=["TdxClient"]), "TdxClient", _FakeTickClient
        ):
            out = c.index_trade_stats("sh.000016")  # 默认 skip_errors=True
        row = out[out["minute"] == "09:30"].iloc[0]
        self.assertEqual(float(row["vol"]), 50.0)  # 仅沪票贡献

    def test_non_std_raises(self):
        c = _FakeTickClient()
        with self.assertRaises(ValueError):
            c.index_trade_stats("hk.01398")


if __name__ == "__main__":
    unittest.main(verbosity=2)
