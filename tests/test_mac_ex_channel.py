# -*- coding: utf-8 -*-
"""MAC EX 通道三命令单测（0x122E K 线 / 0x122D 分时 / 0x122F 逐笔，2026-10-19 批次三）。

fixture = 真实服务器响应存档（tests/fixtures/mac_ex_*.bin，2026-10-19 抓取，
116.205.135.205:7727）：
- mac_ex_122e_usAAPL.bin   EX 日 K 20 根（美股 AAPL，market 74）
- mac_ex_122d_hk01398.bin  EX 单日分时 330 点（港股汇丰，market 31——A 股 240 cap 回归防线）
- mac_ex_122f_hkHSI.bin    EX 逐笔 50 笔（恒指，market 27，分钟级聚合笔）

覆盖：EX 帧构造（flag 0x01 + 0x2454 login 帧）、parser 继承复用（载荷与 A 股
MAC 同构）、330 点 uncapped 解析、门面 symbol 通吃校验、在线冒烟（网络不可达自动 skip）。
运行：python tests/test_mac_ex_channel.py -v
"""
import struct
import unittest
from pathlib import Path
from unittest import mock

from tdxpy.ex_mac import _ExLoginCmd
from tdxpy.parser.mac._frame import EX_LOGIN_BODY
from tdxpy.parser.mac._frame import build_ex_frame
from tdxpy.parser.mac._frame import build_ex_login_frame
from tdxpy.parser.mac.ex_bars import ExBarsCmd
from tdxpy.parser.mac.ex_bars import ExMinuteCmd
from tdxpy.parser.mac.ex_bars import ExTicksCmd

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 构造只需占位（单测直接调 parseResponse，不走 socket）。"""


class TestExFrame(unittest.TestCase):
    """EX 请求帧构造（与 MAC 帧同构，仅 flag 0x1C→0x01）。"""

    def test_frame_layout(self):
        frame = build_ex_frame(0x122E, b"\x01\x02\x03")
        flag, customize, version, zips, unzips = struct.unpack_from("<BIBHH", frame)
        self.assertEqual((flag, customize, version), (0x01, 0, 1))
        (inner_cmd,) = struct.unpack_from("<H", frame, 10)
        self.assertEqual(inner_cmd, 0x122E)
        self.assertEqual((zips, unzips), (5, 5))
        self.assertEqual(frame[12:], b"\x01\x02\x03")

    def test_login_frame(self):
        frame = build_ex_login_frame()
        flag, _, _, zips, unzips = struct.unpack_from("<BIBHH", frame)
        self.assertEqual(flag, 0x01)
        (inner_cmd,) = struct.unpack_from("<H", frame, 10)
        self.assertEqual(inner_cmd, 0x2454)
        self.assertEqual((zips, unzips), (2 + len(EX_LOGIN_BODY),) * 2)  # cmd 2B + login body 80B
        self.assertEqual(frame[12:], EX_LOGIN_BODY)
        self.assertEqual(len(EX_LOGIN_BODY), 80)

    def test_login_cmd_parse(self):
        cmd = _ExLoginCmd(_FakeClient())
        cmd.setup()
        self.assertEqual(cmd.send_pkg, build_ex_login_frame())
        self.assertTrue(cmd.parseResponse(b"\x00" * 33))  # 非空响应=成功
        self.assertFalse(cmd.parseResponse(b""))  # 空=失败

    def test_ex_bars_request_layout(self):
        """EX K 线请求帧：flag 0x01 + 载荷与 A 股 MAC 同构（market 74=AAPL）。"""
        cmd = ExBarsCmd(_FakeClient())
        cmd.setParams(74, "AAPL", 4, 0, 20, 0)
        frame = cmd.send_pkg
        flag, _, _, zips, unzips = struct.unpack_from("<BIBHH", frame)
        self.assertEqual(flag, 0x01)
        (inner_cmd,) = struct.unpack_from("<H", frame, 10)
        self.assertEqual(inner_cmd, 0x122E)
        market, code, period, times, start, count = struct.unpack_from("<H22sHHIH", frame, 12)
        self.assertEqual((market, code.rstrip(b"\x00"), period, times, start, count),
                         (74, b"AAPL", 4, 1, 0, 20))
        self.assertEqual(zips, unzips)

    def test_ex_minute_request_layout(self):
        cmd = ExMinuteCmd(_FakeClient())
        cmd.setParams(31, "01398", 20261019)
        flag, _, _, _, _ = struct.unpack_from("<BIBHH", cmd.send_pkg)
        (inner_cmd,) = struct.unpack_from("<H", cmd.send_pkg, 10)
        market, code, date_ = struct.unpack_from("<H22sI", cmd.send_pkg, 12)
        self.assertEqual((flag, inner_cmd), (0x01, 0x122D))
        self.assertEqual((market, code.rstrip(b"\x00"), date_), (31, b"01398", 20261019))

    def test_ex_ticks_request_layout(self):
        cmd = ExTicksCmd(_FakeClient())
        cmd.setParams(27, "HSI", 0, 0, 50)
        flag, _, _, _, _ = struct.unpack_from("<BIBHH", cmd.send_pkg)
        (inner_cmd,) = struct.unpack_from("<H", cmd.send_pkg, 10)
        market, code, date_, start, count = struct.unpack_from("<H22sIIH", cmd.send_pkg, 12)
        self.assertEqual((flag, inner_cmd), (0x01, 0x122F))
        self.assertEqual((market, code.rstrip(b"\x00"), date_, start, count), (27, b"HSI", 0, 0, 50))

    def test_parser_inheritance_reuse(self):
        """EX parser 继承 A 股 MAC 解析逻辑（载荷同构零重复的结构保证）。"""
        from tdxpy.parser.mac.mac_bars import MacBarsCmd
        from tdxpy.parser.mac.mac_minute import MacMinuteCmd
        from tdxpy.parser.mac.mac_ticks import MacTicksCmd

        self.assertTrue(issubclass(ExBarsCmd, MacBarsCmd))
        self.assertTrue(issubclass(ExMinuteCmd, MacMinuteCmd))
        self.assertTrue(issubclass(ExTicksCmd, MacTicksCmd))
        # A 股 MAC 分时保留 240 cap；EX 放开（市场间一天点数不同）
        self.assertEqual(MacMinuteCmd._MAX_POINTS, 240)
        self.assertIsNone(ExMinuteCmd._MAX_POINTS)
        # 未知周期守卫同样继承（绝不静默回退）
        cmd = ExBarsCmd(_FakeClient())
        with self.assertRaises(ValueError):
            cmd.setParams(74, "AAPL", 99, 0, 20, 0)

    def test_payload_byte_identical_with_mac(self):
        """继承复用契约：同参数下 EX 帧与 A 股 MAC 帧**仅 flag 与市场段不同**。

        契约语义：帧结构（头 9B + cmd + 载荷布局）逐字节同构——同 market/code
        调用时应得到除 flag 外完全一致的帧。市场段是入参本身，不同市场自然
        不同；若有人改 setParams 重写方式（重排载荷/改字段布局），本断言抓住。
        """
        from tdxpy.parser.mac.mac_bars import MacBarsCmd
        from tdxpy.parser.mac.mac_minute import MacMinuteCmd
        from tdxpy.parser.mac.mac_ticks import MacTicksCmd

        cases = [
            (ExBarsCmd, MacBarsCmd, (74, "AAPL", 4, 0, 20, 0)),
            (ExMinuteCmd, MacMinuteCmd, (31, "01398", 0)),
            (ExTicksCmd, MacTicksCmd, (27, "HSI", 0, 0, 50)),
        ]
        for ex_cls, mac_cls, args in cases:
            ex_cmd = ex_cls(_FakeClient())
            ex_cmd.setParams(*args)
            mac_cmd = mac_cls(_FakeClient())
            mac_cmd.setParams(*args)  # 同参数（同 market id/code）应产生同构帧
            self.assertEqual(ex_cmd.send_pkg[0], 0x01)
            self.assertEqual(mac_cmd.send_pkg[0], 0x1C)
            # flag 之外全部逐字节一致（帧头其余 9B + cmd + 载荷）
            self.assertEqual(ex_cmd.send_pkg[1:], mac_cmd.send_pkg[1:])

    def test_240_cap_behavior(self):
        """A 股 240 cap 行为回归（合成 250 点响应：Mac 砍到 240，Ex 不砍）。

        防线场景：若有人删掉 parseResponse 的 _MAX_POINTS 分支，A 股路径行为
        已变而常量断言仍绿——本用例锁行为不锁常量。
        """
        from tdxpy.parser.mac.mac_minute import MacMinuteCmd

        header = struct.pack("<H22sI", 1, "600000".encode("gbk"), 0)  # 前 28B
        records = b"".join(
            struct.pack("<HffIf", m, 10.0 + m % 10, 10.0, 100, 0.0)
            for m in range(9 * 60, 9 * 60 + 250)
        )
        body = header + b"\x00" + struct.pack("<f", 10.0) + struct.pack("<H", 250) + records
        mac_rows = MacMinuteCmd(_FakeClient())
        mac_rows.setParams(1, "600000", 0)
        self.assertEqual(len(mac_rows.parseResponse(body)), 240)  # cap 生效
        ex_rows = ExMinuteCmd(_FakeClient())
        ex_rows.setParams(31, "01398", 0)
        self.assertEqual(len(ex_rows.parseResponse(body)), 250)   # uncapped

    def test_times_hook_layout(self):
        """times 参数化钩子（QUICK_REFERENCE 宣传的 period='1min'+times=N）离线断言。"""
        cmd = ExBarsCmd(_FakeClient())
        cmd.setParams(74, "AAPL", 8, 0, 20, 0, times=5)  # MAC 8 + times=5
        period, times = struct.unpack_from("<HH", cmd.send_pkg, 12 + 2 + 22)
        self.assertEqual((period, times), (8, 5))


class TestExBarsParse(unittest.TestCase):
    """0x122E EX K 线解析（fixture = 美股 AAPL 真实响应）。"""

    @classmethod
    def setUpClass(cls):
        cls.cmd = ExBarsCmd(_FakeClient())
        cls.cmd.setParams(74, "AAPL", 4, 0, 20, 0)
        cls.rows = cls.cmd.parseResponse((FIXTURES / "mac_ex_122e_usAAPL.bin").read_bytes())

    def test_row_count(self):
        self.assertEqual(len(self.rows), 20)

    def test_first_bar(self):
        r = self.rows[0]
        self.assertEqual(r["datetime"], "2026-09-22 15:00")
        self.assertAlmostEqual(r["open"], 340.1350, places=4)
        self.assertAlmostEqual(r["close"], 339.7500, places=4)
        self.assertAlmostEqual(r["amount"], 13880993792.0, delta=1.0)  # 美股有 amount（美元成交额）

    def test_ascending(self):
        dts = [r["datetime"] for r in self.rows]
        self.assertEqual(dts, sorted(dts))


class TestExMinuteParse(unittest.TestCase):
    """0x122D EX 分时解析（fixture = 港股 01398 真实响应，330 点 uncapped）。"""

    @classmethod
    def setUpClass(cls):
        cls.cmd = ExMinuteCmd(_FakeClient())
        cls.cmd.setParams(31, "01398", 0)
        cls.rows = cls.cmd.parseResponse((FIXTURES / "mac_ex_122d_hk01398.bin").read_bytes())

    def test_330_points_not_capped(self):
        """港股一天 330 分钟——A 股 240 cap 会错砍（回归防线）。"""
        self.assertEqual(len(self.rows), 330)

    def test_first_point(self):
        p = self.rows[0]
        self.assertEqual(p["time"], "09:30")
        self.assertAlmostEqual(p["price"], 7.7300, places=4)
        self.assertAlmostEqual(p["avg_price"], 7.7229, places=4)

    def test_has_avg_price_column(self):
        self.assertTrue(all("avg_price" in p for p in self.rows))


class TestExTicksParse(unittest.TestCase):
    """0x122F EX 逐笔解析（fixture = 恒指真实响应，分钟级聚合笔）。"""

    @classmethod
    def setUpClass(cls):
        cls.cmd = ExTicksCmd(_FakeClient())
        cls.cmd.setParams(27, "HSI", 0, 0, 50)
        cls.rows = cls.cmd.parseResponse((FIXTURES / "mac_ex_122f_hkHSI.bin").read_bytes())

    def test_row_count(self):
        self.assertEqual(len(self.rows), 50)

    def test_first_tick(self):
        t = self.rows[0]
        self.assertEqual(t["time"], "15:59:00")
        self.assertAlmostEqual(t["price"], 24173.6191, places=3)
        self.assertAlmostEqual(t["vol"], 30319.0, places=3)

    def test_minute_aggregate_semantics(self):
        """美股/指数逐笔为分钟级聚合（trade_count=0 与 A 股秒级笔的差异点）。"""
        self.assertEqual(self.rows[0]["trade_count"], 0)


class TestMacExFacadeGuard(unittest.TestCase):
    """门面 symbol 解析（不触网）：std/ex 通吃。"""

    def _make_client(self):
        from tdxpy.client import TdxClient
        c = object.__new__(TdxClient)
        return c

    def test_ex_symbols_accepted(self):
        c = self._make_client()
        self.assertEqual(c._ex_symbol("us.AAPL"), (74, "AAPL"))
        self.assertEqual(c._ex_symbol("hk.01398"), (31, "01398"))
        self.assertEqual(c._ex_symbol("hk.HSI"), (27, "HSI"))
        self.assertEqual(c._ex_symbol("上期所.RBL8"), (30, "RBL8"))

    def test_std_symbols_rejected(self):
        """std id 与 ExMarket 空间不通用（实测 sh.600000 直传被错解释返回空）——教学式拒绝。"""
        c = self._make_client()
        for sym in ("sh.600000", "sz.000001"):
            with self.assertRaises(ValueError) as ctx:
                c._ex_symbol(sym)
            # 教学式错误信息须指路 MAC 通道（可行动性保护）
            self.assertIn("mac_bars", str(ctx.exception))


class TestOnline(unittest.TestCase):
    """在线冒烟（网络不可达自动 skip）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from tdxpy.client import TdxClient
            cls.client = TdxClient()
            cls.client._ensure_mac_ex()
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接 MAC EX 通道服务器: {type(e).__name__}: {e}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.close()
        except Exception:  # noqa
            pass

    def test_ex_bars_online(self):
        df = self.client.mac_ex_bars("us.AAPL", count=10)
        # 在线用例不锁精确条数（非交易日/数据延迟可能少于请求值），锁行为
        self.assertGreaterEqual(len(df), 1)
        self.assertTrue(df["datetime"].is_monotonic_increasing)

    def test_ex_minute_online(self):
        df = self.client.mac_ex_minute("hk.01398")
        self.assertGreater(len(df), 0)
        self.assertTrue(all("avg_price" in df.columns for _ in [0]))

    def test_ex_ticks_online(self):
        df = self.client.mac_ex_ticks("hk.HSI", count=10)
        self.assertGreaterEqual(len(df), 1)

    def test_ex_bars_futures_online(self):
        """期货主力连续（上期所.RBL8）在线可达。"""
        df = self.client.mac_ex_bars("上期所.RBL8", count=5)
        self.assertGreaterEqual(len(df), 1)


class TestMacExTicksNoFilter(unittest.TestCase):
    """mac_ex_ticks 不做竞价委托过滤的防线（2026-11-05）。

    文档明确宣称的行为：EX 侧 bs_flag/trade_count 语义未定性（期货 bs_flag>10000），
    不套用 A 股"bs_flag=8 竞价委托"过滤——防止未来误加过滤。
    """

    def _make_client(self, rows):
        from tdxpy.client import TdxClient

        c = object.__new__(TdxClient)
        c._mac_ex_call = mock.MagicMock(return_value=rows)
        return c

    def test_bs_flag_8_kept_on_ex_channel(self):
        from tdxpy.client import TdxClient

        rows = [
            {"time": "22:30:00", "price": 352.5, "vol": 100.0, "trade_count": 0, "bs_flag": 8},
            {"time": "22:31:00", "price": 352.6, "vol": 50.0, "trade_count": 0, "bs_flag": 1},
        ]
        c = self._make_client(rows)
        df = c.mac_ex_ticks("us.AAPL")
        self.assertEqual(len(df), 2)  # EX 通道不过滤：bs_flag=8 保留



if __name__ == "__main__":
    unittest.main()
