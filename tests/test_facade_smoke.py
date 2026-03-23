# -*- coding: utf-8 -*-
"""门面在线冒烟测试：TdxClient（std + ex，点分式 symbol）与 Affair。

需要真实通达信服务器；网络不可达时自动 skip。
服务器地址可用环境变量覆盖：TDXPY_TEST_HQ_HOST / TDXPY_TEST_EX_HOST。

运行：
    python tests/test_facade_smoke.py -v
    或 pytest tests/test_facade_smoke.py -v
"""
import inspect
import os
import unittest

from tdxpy.affair import Affair
from tdxpy.client import TdxClient, normalize_adjust, normalize_date, normalize_period

HQ_HOST = os.environ.get("TDXPY_TEST_HQ_HOST", "111.229.247.189")
HQ_PORT = 7709
EX_HOST = os.environ.get("TDXPY_TEST_EX_HOST", "139.9.191.175")
EX_PORT = 7727

CONNECT_TIMEOUT = 8


def _client_or_skip(**kwargs):
    try:
        client = TdxClient(**kwargs)
        client._ensure_hq()
    except Exception as e:  # noqa
        raise unittest.SkipTest(f"无法连接标准市场: {type(e).__name__}: {e}")
    return client


class TestNormalize(unittest.TestCase):
    """纯解析逻辑（不联网）。"""

    def test_parse_symbol_basics(self):
        # 离线解析细节已由 tests/test_routing_and_coefficient.py 全覆盖，这里保底
        from tdxpy.client import _parse_symbol

        self.assertEqual(_parse_symbol("sh.601919"), ("std", 1, "601919"))
        self.assertEqual(_parse_symbol("us.AAPL"), ("ex", 74, "AAPL"))

    def test_old_syntax_raises(self):
        from tdxpy.client import _parse_symbol

        with self.assertRaises(ValueError) as ctx:
            _parse_symbol("601919")  # 裸代码
        self.assertIn("裸代码", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            _parse_symbol("sh601919")  # 旧式无点前缀
        self.assertIn("旧式无点前缀", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            _parse_symbol("xx.123")  # 市场段不识别
        self.assertIn("无法识别的市场", str(ctx.exception))

    def test_normalize_period(self):
        self.assertEqual(normalize_period("1min"), 7)
        self.assertEqual(normalize_period("5min"), 0)
        self.assertEqual(normalize_period("day"), 4)
        self.assertEqual(normalize_period("d"), 4)
        self.assertEqual(normalize_period("week"), 5)
        with self.assertRaises(ValueError):
            normalize_period("7min")

    def test_normalize_adjust_and_date(self):
        self.assertEqual(normalize_adjust(None), 0)
        self.assertEqual(normalize_adjust("qfq"), 1)
        self.assertEqual(normalize_adjust("hfq"), 2)
        self.assertEqual(normalize_date("2020-01-02"), 20200102)
        self.assertEqual(normalize_date(20200102), 20200102)

    def test_signature_no_market_param(self):
        """品种定位方法的 market 参数已物理删除（2026-09-17 点分式定版）。"""
        for name in ("bars", "minute", "minute_history", "ticks", "quotes", "xdxr", "finance"):
            sig = inspect.signature(getattr(TdxClient, name))
            self.assertNotIn("market", sig.parameters, name)
        # ex_instruments 的 market 是查询主体，保留
        self.assertIn("market", inspect.signature(TdxClient.ex_instruments).parameters)


class TestFacadeStd(unittest.TestCase):
    """标准市场门面（7709；EX 品种经点分 symbol 懒加载 7727）。"""

    @classmethod
    def setUpClass(cls):
        cls.client = _client_or_skip(host=HQ_HOST, port=HQ_PORT)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_bars_day(self):
        df = self.client.bars("sh.601919", period="day", count=5)
        self.assertGreater(len(df), 0)
        self.assertGreater(float(df.iloc[-1]["close"]), 0)
        self.assertIn("datetime", df.columns)

    def test_bars_1min(self):
        df = self.client.bars("sh.601919", period="1min", count=5)
        self.assertGreater(len(df), 0)

    def test_bars_all_pages(self):
        df = self.client.bars("sh.601919", period="5min", all_pages=True, page_size=800, max_pages=3)
        self.assertGreater(len(df), 800)  # 3 页应超过 800 根
        # 时间升序
        times = list(df["datetime"])
        self.assertEqual(times, sorted(times))

    def test_bars_adjust(self):
        raw = self.client.bars("sh.601919", period="day", count=1, adjust=None)
        hfq = self.client.bars("sh.601919", period="day", count=1, adjust="hfq")
        self.assertGreater(float(hfq.iloc[-1]["close"]), float(raw.iloc[-1]["close"]))  # 后复权价更高

    # ---- bars 万能路由（2026-09-17 点分式改造批次）----

    def test_bars_index_auto_route(self):
        """指数段自动路由：sh.000001 走指数命令（修复前：错位垃圾日期 322141-02-52）。"""
        df = self.client.bars("sh.000001", period="day", count=5)
        self.assertGreater(len(df), 0)
        times = list(df["datetime"])
        self.assertEqual(times, sorted(times))  # 升序（错位时会出现乱序垃圾行）
        for t in times:
            year = int(str(t)[:4])
            self.assertGreaterEqual(year, 2020)
            self.assertLessEqual(year, 2100)
        self.assertGreater(float(df.iloc[-1]["close"]), 1000)  # 上证指数量级

    def test_bars_us_route(self):
        df = self.client.bars("us.AAPL", period="day", count=3)
        self.assertGreater(len(df), 0)
        self.assertGreater(float(df.iloc[-1]["close"]), 1)

    def test_bars_hk_route(self):
        """hk.01398 主板（细分 market=31）。"""
        df = self.client.bars("hk.01398", period="day", count=3)
        self.assertGreater(len(df), 0)
        self.assertGreater(float(df.iloc[-1]["close"]), 1)

    def test_bars_hk_index_route(self):
        """hk.HSI → 香港指数（细分 market=27）。"""
        df = self.client.bars("hk.HSI", period="day", count=3)
        self.assertGreater(len(df), 0)
        self.assertGreater(float(df.iloc[-1]["close"]), 10000)

    def test_bars_main_route(self):
        """main.AL8 → 主力连续（EX 60）：路由走 EX 且不抛市场不识别错。

        .. note:: 2026-09-17 实测三台 EX 服务器的 main(60) 市场均无 K 线/报价
           数据推送（AL8/CU8/AP8 连实时报价都全 0，qs.CUL8@30 正常），故数据为
        空时 skip——路由正确性由离线 _parse_symbol 测试保证。
        """
        df = self.client.bars("main.AL8", period="day", count=3)
        if df.empty:
            self.skipTest("main(60) 市场当前服务器无 K 线数据推送（路由已验证走 EX）")
        self.assertGreater(float(df.iloc[-1]["close"]), 0)

    def test_bars_main_1min_route(self):
        df = self.client.bars("main.AL8", period="1min", count=5)
        if df.empty:
            self.skipTest("main(60) 市场当前服务器无 K 线数据推送（路由已验证走 EX）")
        self.assertGreater(len(df), 0)

    def test_bars_ex_shortname_route(self):
        """qs.CUL8 → 上期所（short_name 别名 qs=30）：EX 点分真实数据流。"""
        df = self.client.bars("qs.CUL8", period="1min", count=5)
        self.assertGreater(len(df), 0)

    def test_bars_adjust_ex_raises(self):
        """EX 无服务端复权 → raise（文案含市场中文名）。"""
        with self.assertRaises(ValueError) as ctx:
            self.client.bars("us.AAPL", period="day", count=3, adjust="qfq")
        self.assertIn("不支持服务端复权", str(ctx.exception))

    # ---- 分时/分笔 ----

    def test_minute_and_history(self):
        df = self.client.minute("sh.601919")
        # 当日分时在非交易时段（凌晨/周末/节假日）返回空属正常，只验证结构
        self.assertIsNotNone(df)
        hist = self.client.minute_history("sh.601919", date="2020-01-02")
        self.assertEqual(len(hist), 240)

    def test_minute_bond(self):
        """sz.127089 可转债当日分时结构断言（非交易时段允许空）。"""
        df = self.client.minute("sz.127089")
        self.assertIsNotNone(df)

    def test_minute_hk_route(self):
        """hk 当日分时路由 EX（非交易时段返回空属正常，不抛协议错）。"""
        df = self.client.minute("hk.01398")
        self.assertIsNotNone(df)

    def test_minute_history_hk_route(self):
        """hk 指定日分时路由 EX（2026-09-15 为交易日；深度不足返回空属正常）。"""
        df = self.client.minute_history("hk.01398", date="2026-09-15")
        self.assertIsNotNone(df)

    def test_ticks_history(self):
        df = self.client.ticks("sh.601919", date="2020-01-02", all=True)
        self.assertGreater(len(df), 1000)  # 全天 3000+ 笔
        self.assertIn("price", df.columns)

    def test_ticks_empty_has_columns(self):
        """无数据日返回同列结构空表（2026-10-19 修复：旧版返回无列空 df，下游按列访问 KeyError）。"""
        df = self.client.ticks("sh.000001", date="1990-01-01", all=True)
        self.assertEqual(len(df), 0)
        self.assertEqual(list(df.columns), ["time", "price", "vol", "buyorsell"])

    # ---- 快照 ----

    def test_quotes_std(self):
        df = self.client.quotes(["sh.600519", "sz.000001"])
        self.assertEqual(len(df), 2)

    def test_quotes_ex_single(self):
        """EX 单只快照（get_instrument_quote 逐只）。"""
        df = self.client.quotes(["us.AAPL"])
        self.assertEqual(len(df), 1)
        self.assertGreater(float(df.iloc[0]["price"]), 1)

    def test_quotes_mixed(self):
        """std + ex 混合列表：两行、无异常、列取并集。"""
        df = self.client.quotes(["sh.600519", "us.AAPL"])
        self.assertEqual(len(df), 2)
        aapl = df[df["code"] == "AAPL"].iloc[0]
        self.assertGreater(float(aapl["price"]), 1)

    def test_quotes_dimension_fixes(self):
        """58 段科创ETF / 18 段 REITs 快照量纲（历史修复回归）。"""
        fund = self.client.quotes(["sh.588000"])
        price = float(fund.iloc[0]["price"])
        self.assertGreater(price, 0)
        self.assertLess(price, 10)
        reits = self.client.quotes(["sz.180101"])
        price = float(reits.iloc[0]["price"])
        self.assertGreater(price, 0)
        self.assertLess(price, 8)

    # ---- 公司数据 ----

    def test_xdxr_and_finance(self):
        xdxr = self.client.xdxr("sh.601919")
        self.assertGreater(len(xdxr), 0)
        fin = self.client.finance("sh.601919")
        self.assertGreater(len(fin), 0)

    def test_xdxr_finance_ex_raises(self):
        """xdxr/finance 遇 EX symbol → raise（文案含"仅标准市场"）。"""
        with self.assertRaises(ValueError) as ctx:
            self.client.xdxr("us.AAPL")
        self.assertIn("仅标准市场", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            self.client.finance("us.AAPL")
        self.assertIn("仅标准市场", str(ctx.exception))

    def test_bars_range_std_raises(self):
        """bars_range 遇 std symbol → raise。"""
        with self.assertRaises(ValueError) as ctx:
            self.client.bars_range("sh.601919", "2020-01-02", "2020-01-02")
        self.assertIn("仅 EX 市场支持", str(ctx.exception))


class TestFacadeEx(unittest.TestCase):
    """扩展市场门面（7727，含自动 login）。"""

    @classmethod
    def setUpClass(cls):
        client = TdxClient(host=HQ_HOST, port=HQ_PORT, ex_host=EX_HOST, ex_port=EX_PORT)
        try:
            client._ensure_ex()
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接扩展市场: {type(e).__name__}: {e}")
        cls.client = client

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_ex_markets(self):
        df = self.client.ex_markets()
        self.assertGreater(len(df), 40)

    def test_ex_instruments(self):
        df = self.client.ex_instruments(market=28, limit=10)
        self.assertGreater(len(df), 0)
        self.assertTrue((df["market"] == 28).all())

    def test_ex_instruments_alias(self):
        """ex_instruments 的 market 别名字符串仍支持（查询主体参数）。"""
        df = self.client.ex_instruments(market="郑商所", limit=5)
        self.assertGreater(len(df), 0)
        self.assertTrue((df["market"] == 28).all())

    def test_ex_instruments_enum_member(self):
        """ex_instruments 的 market 枚举成员直传仍支持。"""
        from tdxpy.markets import ExMarket

        df = self.client.ex_instruments(market=ExMarket.ZCE, limit=5)
        self.assertGreater(len(df), 0)

    def test_bars_range(self):
        """bars_range（原 ex_history_bars_range）：EX 专属按日期区间 1 分钟线。

        main.AL8（60）路由探活：协议命令通但服务器无数据时 skip；
        郑商所.APL8 2020-01-22（原 ex_history_bars_range 等价用例）断言行数>0。
        """
        df = self.client.bars_range("main.AL8", "2020-01-22", "2020-01-22")
        if df.empty:
            self.skipTest("main(60) 市场当前服务器无 K 线数据推送（路由已验证走 EX）")
        self.assertGreater(len(df), 0)
        apl = self.client.bars_range("郑商所.APL8", "2020-01-22", "2020-01-22")
        self.assertGreater(len(apl), 0)

    def test_bars_us_market_string_alias(self):
        """点分市场段 "us"（原 ex_bars market="us" 等价用例）。"""
        df = self.client.bars("us.AAPL", period="day", count=2)
        self.assertGreater(len(df), 0)
        self.assertGreater(float(df.iloc[-1]["close"]), 1)

    def test_bars_invalid_market_raises(self):
        """点分市场段不识别 → raise（文案含 ex_market_table 指引）。"""
        with self.assertRaises(ValueError) as ctx:
            self.client.bars("mars.AAPL")
        self.assertIn("ex_market_table", str(ctx.exception))

    def test_ex_market_invalid_alias_raises(self):
        """ex_instruments 的 market 别名不识别 → raise。"""
        with self.assertRaises(ValueError) as ctx:
            self.client.ex_instruments(market="mars")
        self.assertIn("ex_market_table", str(ctx.exception))


class TestAffair(unittest.TestCase):
    """财务数据（官方 HTTP 站 data.tdx.com.cn/tdxfin，urllib 标准库）。"""

    def test_files(self):
        files = Affair.files()
        self.assertGreater(len(files), 0)
        self.assertIn("filename", files[0])
        self.assertIn("hash", files[0])
        self.assertIn("filesize", files[0])

    def test_fetch_and_parse(self):
        files = Affair.files()
        # 挑一个最小的 zip（>1KB 防空壳包；<2MB 节制带宽）
        zips = [item for item in files if item["filename"].endswith(".zip") and 1024 < item["filesize"] < 2 * 1024 * 1024]
        if not zips:
            self.skipTest("清单里没有 1KB~2MB 的 zip 包")
        small = min(zips, key=lambda item: item["filesize"])
        filename = small["filename"]

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Affair.fetch(filename, downdir=tmp)
            self.assertTrue(path.exists())
            df = Affair.parse(filename, downdir=tmp)
            self.assertGreater(len(df), 0)
            self.assertIn("基本每股收益", df.columns)
            self.assertIn("report_date", df.columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
