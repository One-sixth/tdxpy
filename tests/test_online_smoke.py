# -*- coding: utf-8 -*-
"""在线冒烟测试：tdxpy 2026 协议升级（新式握手 + 42 字节 K 线 payload + EX 新版 login）。

需要真实通达信服务器；网络不可达时自动 skip。
服务器地址可用环境变量覆盖：TDXPY_TEST_HQ_HOST / TDXPY_TEST_EX_HOST。

运行：
    python -m unittest tests.test_online_smoke -v
    或 pytest tests/test_online_smoke.py -v
"""
import os
import unittest

from tdxpy.exhq import TdxExHq_API
from tdxpy.hq import TdxHq_API
from tdxpy.helper import time_frame

HQ_HOST = os.environ.get("TDXPY_TEST_HQ_HOST", "111.229.247.189")
HQ_PORT = 7709
EX_HOST = os.environ.get("TDXPY_TEST_EX_HOST", "139.9.191.175")
EX_PORT = 7727

CONNECT_TIMEOUT = 8


def _connect_or_skip(api, host, port):
    """连接失败（无网络/服务器不可达）时 skip 整个用例类。"""
    try:
        ok = api.connect(host, port, time_out=CONNECT_TIMEOUT)
    except Exception as e:  # noqa
        raise unittest.SkipTest(f"无法连接 {host}:{port}: {type(e).__name__}: {e}")
    if not ok:
        raise unittest.SkipTest(f"无法连接 {host}:{port}")
    return api


class TestHqOnlineSmoke(unittest.TestCase):
    """标准市场（7709）：新式握手下的 K 线复活 + 存量命令回归。"""

    @classmethod
    def setUpClass(cls):
        cls.api = _connect_or_skip(TdxHq_API(), HQ_HOST, HQ_PORT)

    @classmethod
    def tearDownClass(cls):
        cls.api.disconnect()

    def test_security_bars_day(self):
        """K 线复活：日线（新 42 字节 payload）"""
        bars = self.api.get_security_bars(4, 1, "601919", 0, 5)
        self.assertIsNotNone(bars)
        self.assertGreater(len(bars), 0)
        self.assertGreater(float(bars[-1]["close"]), 0)

    def test_security_bars_1min(self):
        """K 线复活：1 分钟线（category=7）"""
        bars = self.api.get_security_bars(7, 1, "601919", 0, 5)
        self.assertIsNotNone(bars)
        self.assertGreater(len(bars), 0)

    def test_security_bars_5min(self):
        """K 线复活：5 分钟线（category=0）"""
        bars = self.api.get_security_bars(0, 1, "601919", 0, 5)
        self.assertIsNotNone(bars)
        self.assertGreater(len(bars), 0)

    def test_index_bars(self):
        """指数 K 线（GetIndexBarsCmd 同样升级为 42 字节 payload）"""
        bars = self.api.get_index_bars(4, 1, "000001", 0, 5)
        self.assertIsNotNone(bars)
        self.assertGreater(len(bars), 0)

    def test_security_bars_adjust(self):
        """新能力：服务端复权（adjust=1 前复权 / 2 后复权）"""
        bars_qfq = self.api.get_security_bars(4, 1, "601919", 0, 5, adjust=1)
        bars_hfq = self.api.get_security_bars(4, 1, "601919", 0, 5, adjust=2)
        self.assertGreater(len(bars_qfq or []), 0)
        self.assertGreater(len(bars_hfq or []), 0)

    @unittest.skipUnless(time_frame(), "非交易时段当日分时无数据（9:30-11:30/13:00-15:00 内运行）")
    def test_minute_time_data(self):
        """回归：当日分时不受新握手影响（240 个分钟点）"""
        data = self.api.get_minute_time_data(1, "601919")
        self.assertIsNotNone(data)
        self.assertEqual(len(data), 240)

    def test_history_minute_time_data(self):
        """回归：历史分时（2020-01-02，601919 当日 240 个分钟点）"""
        data = self.api.get_history_minute_time_data(1, "601919", 20200102)
        self.assertIsNotNone(data)
        self.assertEqual(len(data), 240)

    def test_history_transaction_data(self):
        """回归：历史分笔（2020-01-02）"""
        data = self.api.get_history_transaction_data(1, "601919", 0, 30, 20200102)
        self.assertIsNotNone(data)
        self.assertGreater(len(data), 0)


class TestExHqOnlineSmoke(unittest.TestCase):
    """扩展市场（7727）：新版 login 后的 EX 命令复活。"""

    @classmethod
    def setUpClass(cls):
        cls.api = _connect_or_skip(TdxExHq_API(), EX_HOST, EX_PORT)

    @classmethod
    def tearDownClass(cls):
        cls.api.disconnect()

    def test_get_markets(self):
        """EX 复活：市场列表（新 login 后应返回 50+ 个扩展市场）"""
        markets = self.api.get_markets()
        self.assertIsNotNone(markets)
        self.assertGreater(len(markets), 0)

    def test_instrument_bars(self):
        """EX 复活：期货主连 K 线（沪铜主连 CUL8，5 分钟线）"""
        bars = self.api.get_instrument_bars(0, 30, "CUL8", 0, 5)
        self.assertIsNotNone(bars)
        self.assertGreater(len(bars), 0)
        self.assertGreater(float(bars[-1]["close"]), 0)

    def test_history_instrument_bars_range(self):
        """EX 复活：按日期范围的历史 K 线（苹果主连 2020-01-22，1 分钟粒度）"""
        bars = self.api.get_history_instrument_bars_range(28, "APL8", 20200122, 20200122)
        self.assertIsNotNone(bars)
        self.assertGreater(len(bars), 0)


if __name__ == "__main__":
    unittest.main()
