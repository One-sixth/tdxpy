# -*- coding: utf-8 -*-
"""经典 EX 通道解析防线（0x23FF K 线市场分型 + 0x0B24 分时跨日，2026-11-04 吸收批次）。

覆盖（吸收自 pytdxdata 0.6.5/0.7.0 实测）：
- 0x23FF 28B 记录第 5 字段按市场分两类：权益类（31/48/49/44/74）= float 成交额；
  期货/期权类 = int 持仓量。权益类恒等式 amount/(close×trade×100) ≈ 1（主板/基金 vol=手）。
- 0x0B24 分时 raw_time 跨日不回绕：hour 取模归一化 + day_offset 跨日偏移。

fixture：ex_bars_023ff_hk01398.bin = 真实服务器响应存档（2026-11-04 抓取，
116.205.143.214，hk01398 日线 2 根；2026-11-03 记录 amount=1906065792.0）。
运行：python tests/test_ext_kline_amount.py -v
"""
import struct
import unittest
from pathlib import Path

from tdxpy.parser.ext.ex_get_history_instrument_bars_range import GetHistoryInstrumentBarsRange
from tdxpy.parser.ext.ex_get_instrument_bars import GetInstrumentBars
from tdxpy.parser.ext.ex_get_minute_time_data import GetMinuteTimeData
from tdxpy.parser.ext.ex_get_history_minute_time_data import GetHistoryMinuteTimeData

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 构造只需占位（单测直接调 parseResponse，不走 socket）。"""


class TestExKlineAmountMarkets(unittest.TestCase):
    """0x23FF K 线：权益类 amount 回填 + 期货类 position 语义。"""

    @classmethod
    def setUpClass(cls):
        cls.body = (FIXTURES / "ex_bars_023ff_hk01398.bin").read_bytes()

    def _parse(self, market):
        cmd = GetInstrumentBars(_FakeClient())
        cmd.setParams(4, market, "01398", 0, 2)
        return cmd.parseResponse(self.body)

    def test_equity_amount_filled(self):
        """港股主板（31）：amount 回填真实成交额（不再恒 0.0 占位）。"""
        rows = self._parse(31)
        by_dt = {r["datetime"]: r for r in rows}
        r = by_dt["2026-11-03 15:00"]
        self.assertAlmostEqual(r["amount"], 1906065792.0, places=2)
        self.assertEqual(r["position"], 0.0)  # 权益类无持仓概念
        self.assertEqual(r["trade"], 2478716)

    def test_equity_identity_hk(self):
        """港股主板恒等式：amount/(close×trade×100) ≈ 1（vol 单位=手，VWAP 与 close 有正常偏差）。"""
        for r in self._parse(31):
            self.assertAlmostEqual(r["amount"] / (r["close"] * r["trade"] * 100), 1.0, places=1)

    def test_futures_branch_position(self):
        """期货类市场（30 上期所）：同字节按持仓量解析，amount 保持 0.0 占位。"""
        by_dt = {r["datetime"]: r for r in self._parse(30)}
        r = by_dt["2026-11-03 15:00"]
        # slot5 的 u32 解读值（HK 11-03 字节 83 38 e3 4e = 1323513987）
        self.assertEqual(r["position"], 1323513987)
        self.assertEqual(r["amount"], 0.0)


class TestExMinuteDayOffset(unittest.TestCase):
    """0x0B24 分时：raw_time 跨日归一化（美股夜盘 hour 21~27 → %24 + day_offset）。"""

    @classmethod
    def _parse(cls, raw_times):
        # 12B 头（market+code9s+num）+ N×18B 记录（<HffII）
        body = struct.pack("<B9sH", 74, b"AAPL", len(raw_times))
        for t in raw_times:
            body += struct.pack("<HffII", t, 352.5, 352.0, 1000, 0)
        cmd = GetMinuteTimeData(_FakeClient())
        cmd.setParams(74, "AAPL")
        return cmd.parseResponse(body)

    def test_normal_session_no_offset(self):
        """A 股/港股正常时段：day_offset=0，hour 语义不变（向后兼容）。"""
        rows = self._parse([570, 809])  # 09:30 / 13:29
        self.assertEqual((rows[0]["hour"], rows[0]["day_offset"]), (9, 0))
        self.assertEqual((rows[1]["hour"], rows[1]["day_offset"]), (13, 0))

    def test_us_overnight_wraps(self):
        """美股夜盘：27:59 → hour=3 + day_offset=1（修复前 hour=27 非法时钟）。"""
        rows = self._parse([1290, 1679])  # 21:30 / 27:59
        self.assertEqual((rows[0]["hour"], rows[0]["day_offset"]), (21, 0))
        self.assertEqual((rows[1]["hour"], rows[1]["day_offset"]), (3, 1))


class TestExBarsRangeAmountMarkets(unittest.TestCase):
    """bars_range（0x0D24）32B 记录：数据段与 0x23FF 同构，第 5 字段同样按市场分型。

    2026-11-05 交叉复审发现（在线恒等式实锤：us.AAPL≈1.0、hk.01398≈100.0）。
    """

    @staticmethod
    def _build_body():
        # 12B 假头 + count(2B@12) + 1 条 32B 记录（<HHffffIIf）
        # d1=46159 → 2026-11-03（year=num//2048+2004=2026、(num%2048)=1103 → month=11 day=03）
        # d2=570 → 09:30
        rec = struct.pack("<HHffffIIf", 46159, 570, 7.83, 7.84, 7.545, 7.74, 1323513987, 2478716, 47075000.0)
        return bytes(12) + struct.pack("<H", 1) + rec

    def _parse(self, market):
        cmd = GetHistoryInstrumentBarsRange(_FakeClient())
        cmd.setParams(market, "01398", 20261103, 20261103)
        return cmd.parseResponse(self._build_body())

    def test_equity_branch(self):
        """权益类（31）：slot5 按 float 读回填 amount，position 置 0.0。"""
        r = self._parse(31)[0]
        self.assertAlmostEqual(r["amount"], 1906065792.0, places=2)
        self.assertEqual(r["position"], 0.0)
        self.assertAlmostEqual(r["amount"] / (r["close"] * r["trade"] * 100), 1.0, places=1)
        self.assertEqual(r["datetime"], "2026-11-03 09:30")

    def test_futures_branch(self):
        """期货类（30）：slot5 保持 u32 持仓量，amount 恒 0.0，settlementprice 原样。"""
        r = self._parse(30)[0]
        self.assertEqual(r["position"], 1323513987)
        self.assertEqual(r["amount"], 0.0)
        self.assertAlmostEqual(r["settlementprice"], 47075000.0, places=2)


class TestExHistoryMinuteDayOffset(unittest.TestCase):
    """minute_history EX 路径（0x0C24）：与 0x0B24 同型跨日归一化（2026-11-05 交叉复审发现）。"""

    @staticmethod
    def _parse(raw_times):
        # 20B 头（<B9s8sH）+ N×18B 记录（<HffII）
        body = struct.pack("<B9s8sH", 74, b"AAPL", b"", len(raw_times))
        for t in raw_times:
            body += struct.pack("<HffII", t, 352.5, 352.0, 1000, 0)
        cmd = GetHistoryMinuteTimeData(_FakeClient())
        cmd.setParams(74, "AAPL", 20261104)
        return cmd.parseResponse(body)

    def test_us_overnight_wraps(self):
        """美股夜盘跨日：27:59 → hour=3 + day_offset=1（修复前 hour=27 非法时钟）。"""
        rows = self._parse([1350, 1739])  # 22:30 / 28:59
        self.assertEqual((rows[0]["hour"], rows[0]["day_offset"]), (22, 0))
        self.assertEqual((rows[1]["hour"], rows[1]["day_offset"]), (4, 1))

    def test_normal_session_no_offset(self):
        """正常时段：day_offset 恒 0，向后兼容。"""
        rows = self._parse([570, 809])
        self.assertEqual((rows[0]["hour"], rows[0]["day_offset"]), (9, 0))
        self.assertEqual((rows[1]["hour"], rows[1]["day_offset"]), (13, 0))


if __name__ == "__main__":
    unittest.main()
