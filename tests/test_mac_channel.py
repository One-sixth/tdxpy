# -*- coding: utf-8 -*-
"""MAC 通道三命令单测（0x122E K 线 / 0x122D 分时 / 0x122F 逐笔，2026-10-19 吸收批次）。

fixture = 真实服务器响应存档（tests/fixtures/mac_*.bin，2026-10-19 抓取，sh600000）：
- mac_122e_sh600000.bin  MAC 日 K 20 根
- mac_122d_sh600000.bin  MAC 当日分时 240 点
- mac_122f_sh600000.bin  MAC 逐笔 50 笔

覆盖：MAC 帧构造（10B 头）、36B/18B 记录解析、日线 15:00 惯例、截断守卫、
门面 symbol 校验、在线冒烟（网络不可达自动 skip）。
运行：python tests/test_mac_channel.py -v
"""
import struct
import unittest
from pathlib import Path
from unittest import mock

from tdxpy.parser.mac._frame import build_mac_frame
from tdxpy.parser.mac.mac_bars import MacBarsCmd
from tdxpy.parser.mac.mac_minute import MacMinuteCmd
from tdxpy.parser.mac.mac_ticks import MacTicksCmd

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 构造只需占位（单测直接调 parseResponse，不走 socket）。"""


class TestMacFrame(unittest.TestCase):
    """MAC 请求帧构造（10B 头 + cmd + body）。"""

    def test_frame_layout(self):
        frame = build_mac_frame(0x122E, b"\x01\x02\x03")
        # <BIBHH: flag 0x1C / customize 0 / version 1 / zipsize / unzipsize
        flag, customize, version, zips, unzips = struct.unpack_from("<BIBHH", frame)
        self.assertEqual((flag, customize, version), (0x1C, 0, 1))
        inner = struct.unpack_from("<H", frame, 10)[0]
        self.assertEqual((zips, unzips), (5, 5))  # cmd 2B + body 3B
        self.assertEqual(inner, 0x122E)
        self.assertEqual(frame[12:], b"\x01\x02\x03")

    def test_bars_request_layout(self):
        cmd = MacBarsCmd(_FakeClient())
        cmd.setParams(1, "600000", 4, 0, 20, 0)
        frame = cmd.send_pkg
        flag, _, _, zips, unzips = struct.unpack_from("<BIBHH", frame)
        self.assertEqual(flag, 0x1C)
        (inner_cmd,) = struct.unpack_from("<H", frame, 10)
        self.assertEqual(inner_cmd, 0x122E)
        market, code, period, times, start, count = struct.unpack_from("<H22sHHIH", frame, 12)
        self.assertEqual((market, code.rstrip(b"\x00"), period, times, start, count),
                         (1, b"600000", 4, 1, 0, 20))
        self.assertEqual(zips, unzips)

    def test_bars_5min_not_day(self):
        """5min 回归防线（复审实锤 bug：.get(category,4) 曾把 5min 静默变 day）。"""
        cmd = MacBarsCmd(_FakeClient())
        cmd.setParams(1, "600000", 0, 0, 20, 0)  # 标准 5min=0
        frame = cmd.send_pkg
        # 帧布局：10B 头 + cmd(2) + market(2) + code(22) + period(2)@36 + times(2)@38
        period, times = struct.unpack_from("<HH", frame, 36)
        self.assertEqual((period, times), (0, 1))  # MAC 0=5min（300s gap 实测）

    def test_bars_1min_mapping(self):
        """std 1min(7) → MAC 7（times 钩子无效，恒 60s）；std 8 → MAC 8+times=N=参数化 N 分钟。"""
        cmd = MacBarsCmd(_FakeClient())
        cmd.setParams(1, "600000", 7, 0, 20, 0, times=15)
        fields = struct.unpack_from("<H22sHH", cmd.send_pkg, 12)
        self.assertEqual((fields[2], fields[3]), (7, 1))  # MAC 7 不吃 times（实测恒 60s）
        cmd = MacBarsCmd(_FakeClient())
        cmd.setParams(1, "600000", 8, 0, 20, 0, times=15)  # MAC 8 + times=15 → 15min 钩子
        fields = struct.unpack_from("<H22sHH", cmd.send_pkg, 12)
        self.assertEqual((fields[2], fields[3]), (8, 15))
        cmd = MacBarsCmd(_FakeClient())
        cmd.setParams(1, "600000", 8, 0, 20, 0)  # 默认 times=1 → 1min
        fields = struct.unpack_from("<H22sHH", cmd.send_pkg, 12)
        self.assertEqual((fields[2], fields[3]), (8, 1))

    def test_bars_unknown_period_raises(self):
        """未知周期 raise（绝不静默回退 day）。"""
        cmd = MacBarsCmd(_FakeClient())
        with self.assertRaises(ValueError):
            cmd.setParams(1, "600000", 99, 0, 20, 0)

    def test_minute_request_layout(self):
        cmd = MacMinuteCmd(_FakeClient())
        cmd.setParams(0, "000001", 20261019)
        (inner_cmd,) = struct.unpack_from("<H", cmd.send_pkg, 10)
        market, code, date_ = struct.unpack_from("<H22sI", cmd.send_pkg, 12)
        self.assertEqual(inner_cmd, 0x122D)
        self.assertEqual((market, code.rstrip(b"\x00"), date_), (0, b"000001", 20261019))

    def test_ticks_request_layout(self):
        cmd = MacTicksCmd(_FakeClient())
        cmd.setParams(1, "600000", 0, 100, 500)
        (inner_cmd,) = struct.unpack_from("<H", cmd.send_pkg, 10)
        market, code, date_, start, count = struct.unpack_from("<H22sIIH", cmd.send_pkg, 12)
        self.assertEqual(inner_cmd, 0x122F)
        self.assertEqual((market, code.rstrip(b"\x00"), date_, start, count),
                         (1, b"600000", 0, 100, 500))


class TestBarsParse(unittest.TestCase):
    """0x122E K 线解析（fixture = 真实响应）。"""

    @classmethod
    def setUpClass(cls):
        cls.cmd = MacBarsCmd(_FakeClient())
        cls.cmd.setParams(1, "600000", 4, 0, 20, 0)
        cls.rows = cls.cmd.parseResponse((FIXTURES / "mac_122e_sh600000.bin").read_bytes())

    def test_row_count(self):
        self.assertEqual(len(self.rows), 20)

    def test_first_bar(self):
        r = self.rows[0]
        self.assertEqual(r["datetime"], "2026-09-14 15:00")  # 日线 hour/minute 对齐 std 惯例 15:00
        self.assertAlmostEqual(r["open"], 9.28, places=6)
        self.assertAlmostEqual(r["close"], 9.40, places=6)
        self.assertAlmostEqual(r["amount"], 722307392.0, places=3)

    def test_last_bar_today(self):
        r = self.rows[-1]
        self.assertEqual(r["datetime"], "2026-10-19 15:00")
        self.assertAlmostEqual(r["close"], 9.78, places=6)

    def test_ascending(self):
        dts = [r["datetime"] for r in self.rows]
        self.assertEqual(dts, sorted(dts))

    def test_truncated_rejected(self):
        body = (FIXTURES / "mac_122e_sh600000.bin").read_bytes()
        # 头 33B 合法但记录流截断半条（count 声明 20，只留 10.5 条）
        cut = body[: 33 + 10 * 36 + 18]
        self.assertEqual(len(self.cmd.parseResponse(cut)), 10)  # min() 截断到可解析条数


class TestMinuteParse(unittest.TestCase):
    """0x122D 分时解析。"""

    @classmethod
    def setUpClass(cls):
        cls.cmd = MacMinuteCmd(_FakeClient())
        cls.cmd.setParams(1, "600000", 0)
        cls.rows = cls.cmd.parseResponse((FIXTURES / "mac_122d_sh600000.bin").read_bytes())

    def test_240_points(self):
        self.assertEqual(len(self.rows), 240)

    def test_first_point(self):
        p = self.rows[0]
        self.assertEqual(p["time"], "09:30")
        self.assertAlmostEqual(p["price"], 9.73, places=6)
        self.assertAlmostEqual(p["avg_price"], 9.698449, places=5)

    def test_has_avg_price_column(self):
        """MAC 分时独有均价线（与 std minute() 的差异点）。"""
        self.assertTrue(all("avg_price" in p for p in self.rows))


class TestTicksParse(unittest.TestCase):
    """0x122F 逐笔解析。"""

    @classmethod
    def setUpClass(cls):
        cls.cmd = MacTicksCmd(_FakeClient())
        cls.cmd.setParams(1, "600000", 0, 0, 50)
        cls.rows = cls.cmd.parseResponse((FIXTURES / "mac_122f_sh600000.bin").read_bytes())

    def test_row_count(self):
        self.assertEqual(len(self.rows), 50)

    def test_second_precision(self):
        """秒级时间戳（与 std ticks() 分钟粒度的差异点）。"""
        t = self.rows[0]
        self.assertEqual(t["time"], "14:54:45")
        self.assertEqual((t["hour"], t["minute"], t["second"]), (14, 54, 45))

    def test_trade_count_field(self):
        """每笔含成交笔数（MAC 独有字段）。"""
        self.assertTrue(all(r["trade_count"] >= 1 for r in self.rows))


class TestTicksSignedTradeCount(unittest.TestCase):
    """trade_count 符号语义（2026-11-04 吸收自 pytdxdata 0.6.5）。

    竞价段该字段实为负值（int32）：A 股 MAC 解析为负数；EX 通道语义未定性保持 u32。
    """

    @classmethod
    def _build_body(cls):
        # 29B 头 + count(2B@29) + 8B 间隔 + 1 条 18B 记录（<IfI?H）
        rec = struct.pack("<IfIiH", 34200, 9.73, 100, -874, 8)  # trade_count=-874（i32 字节 96 fc ff ff）
        return bytes(29) + struct.pack("<H", 1) + bytes(8) + rec

    def test_std_mac_signed(self):
        """A 股 MAC 通道：trade_count 按有符号解析（竞价段负值不读成 42 亿）。"""
        cmd = MacTicksCmd(_FakeClient())
        cmd.setParams(1, "600000", 0)
        rows = cmd.parseResponse(self._build_body())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_count"], -874)

    def test_ex_channel_unsigned(self):
        """EX 通道（ExTicksCmd）：语义未定性，覆写保持 uint32。"""
        from tdxpy.parser.mac.ex_bars import ExTicksCmd

        cmd = ExTicksCmd(_FakeClient())
        cmd.setParams(74, "AAPL", 0)
        rows = cmd.parseResponse(self._build_body())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["trade_count"], 4294966422)  # u32 环绕读数


class TestMacTicksAuctionFilter(unittest.TestCase):
    """门面 mac_ticks 竞价委托过滤（bs_flag=8 非成交，Σvol 虚高 20%~160%）。"""

    @staticmethod
    def _rows():
        return [
            {"time": "09:25:00", "price": 9.70, "vol": 500.0, "trade_count": -874, "bs_flag": 8},
            {"time": "09:30:00", "price": 9.73, "vol": 120.0, "trade_count": 1, "bs_flag": 1},
            {"time": "09:30:03", "price": 9.74, "vol": 80.0, "trade_count": 2, "bs_flag": 2},
        ]

    def _make_client(self, rows):
        from tdxpy.client import TdxClient

        c = object.__new__(TdxClient)
        c._mac_call = mock.MagicMock(return_value=rows)
        return c

    def test_default_filters_auction_orders(self):
        from tdxpy.client import TdxClient

        c = self._make_client(self._rows())
        df = c.mac_ticks("sh.600000")
        self.assertEqual(len(df), 2)
        self.assertFalse((df["bs_flag"] == 8).any())
        self.assertEqual(df["vol"].sum(), 200.0)  # 委托 500 手不计入

    def test_drop_auction_orders_false_keeps_raw(self):
        from tdxpy.client import TdxClient

        c = self._make_client(self._rows())
        df = c.mac_ticks("sh.600000", drop_auction_orders=False)
        self.assertEqual(len(df), 3)


class TestMacFacadeGuard(unittest.TestCase):
    """门面 symbol 校验（不触网）。"""

    def _make_client(self):
        from tdxpy.client import TdxClient
        c = object.__new__(TdxClient)
        return c

    def test_ex_symbol_rejected(self):
        c = self._make_client()
        with self.assertRaises(ValueError):
            c.mac_bars("us.AAPL")
        with self.assertRaises(ValueError):
            c.mac_minute("hk.01398")

    def test_std_symbol_accepted(self):
        c = self._make_client()
        market_id, code = c._std_symbol("sh.600000")
        self.assertEqual((market_id, code), (1, "600000"))


class TestOnline(unittest.TestCase):
    """在线冒烟（网络不可达自动 skip）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from tdxpy.client import TdxClient
            cls.client = TdxClient()
            cls.client._ensure_mac()
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接 MAC 通道服务器: {type(e).__name__}: {e}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.close()
        except Exception:  # noqa
            pass

    def test_mac_bars_online(self):
        df = self.client.mac_bars("sh.600000", count=10)
        self.assertEqual(len(df), 10)
        self.assertTrue(df["datetime"].is_monotonic_increasing)

    def test_mac_minute_online(self):
        df = self.client.mac_minute("sh.600000")
        self.assertGreater(len(df), 0)
        self.assertIn("avg_price", df.columns)

    def test_mac_ticks_online(self):
        df = self.client.mac_ticks("sh.600000", count=50)
        self.assertEqual(len(df), 50)
        self.assertIn("trade_count", df.columns)
        # 秒级时间戳格式
        self.assertRegex(df["time"].iloc[0], r"^\d{2}:\d{2}:\d{2}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
