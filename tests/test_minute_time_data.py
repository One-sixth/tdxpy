# -*- coding: utf-8 -*-
"""0x051D 当日分时 parser 单测（2026-10-19 新格式破解批次，完全离线）。

fixture = 真实服务器响应存档（tests/fixtures/minute_*.bin，2026-10-19 抓取）：
- minute_051d_sh601919.bin    新格式 A 股（变长头 64B）
- minute_051d_sz000001.bin    新格式 深市 A 股（头 67B）
- minute_051d_sh510050.bin    新格式 ETF（头 68B，系数 0.001）
- minute_051d_bj821001.bin    新格式 BJ 优先股（头 71B，系数 0.0001）
- minute_legacy_sh601919.bin  旧 6B 头格式（num+preclose f32+记录流，legacy 会话响应）

覆盖：变长头定位、close 锚点自检、差分累积×系数、legacy 兜底、解析失败返回 []。
运行：python tests/test_minute_time_data.py -v
"""
import struct
import unittest
from pathlib import Path

from tdxpy.parser.std.get_minute_time_data import GetMinuteTimeData

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 构造只需占位（单测直接调 parseResponse，不走 socket）。"""


def _make_parser(market, code):
    p = GetMinuteTimeData(_FakeClient())
    p.setParams(market, code)
    return p


class TestNewFormat(unittest.TestCase):
    """新格式：变长头扫描 + close 锚点自检 + 差分累积。"""

    def test_sh601919(self):
        """A股：head 64B，close×100=1673 锚点。"""
        body = (FIXTURES / "minute_051d_sh601919.bin").read_bytes()
        (num,) = struct.unpack("<H", body[:2])
        self.assertEqual(num, 240)
        prices = _make_parser(1, "601919").parseResponse(body)
        self.assertEqual(len(prices), 240)
        self.assertAlmostEqual(prices[-1]["price"], 16.73, places=6)
        self.assertAlmostEqual(prices[0]["price"], 16.89, places=6)
        self.assertEqual(max(p["price"] for p in prices), 16.94)
        self.assertEqual(min(p["price"] for p in prices), 16.71)
        # vol 为非负整数（手）
        self.assertTrue(all(p["vol"] >= 0 for p in prices))

    def test_sz000001(self):
        """深市：head 67B（与 601919 的 64 不同——变长头不硬编码）。"""
        body = (FIXTURES / "minute_051d_sz000001.bin").read_bytes()
        prices = _make_parser(0, "000001").parseResponse(body)
        self.assertEqual(len(prices), 240)
        self.assertAlmostEqual(prices[-1]["price"], 11.65, places=6)

    def test_sh510050_etf_coefficient(self):
        """ETF 系数 0.001：close×1000=2886 锚点 → 2.886（历史 ×10 bug 回归防线）。"""
        body = (FIXTURES / "minute_051d_sh510050.bin").read_bytes()
        prices = _make_parser(1, "510050").parseResponse(body)
        self.assertEqual(len(prices), 240)
        self.assertAlmostEqual(prices[-1]["price"], 2.886, places=6)

    def test_bj821001_preferred_coefficient(self):
        """BJ 优先股系数 0.0001：close×10000=1007440 锚点 → 100.744（原 ×100 bug 回归防线）。"""
        body = (FIXTURES / "minute_051d_bj821001.bin").read_bytes()
        prices = _make_parser(2, "821001").parseResponse(body)
        self.assertEqual(len(prices), 240)
        self.assertAlmostEqual(prices[-1]["price"], 100.744, places=6)
        # 优先股当日无成交：全日横盘贴面值
        self.assertEqual(len({p["price"] for p in prices}), 1)

    def test_header_length_varies(self):
        """变长头实测：同一算法覆盖 64/67/68/71B 四种头长——证明非硬编码 skip。"""
        lens = {}
        for fn, mkt, code in [
            ("minute_051d_sh601919.bin", 1, "601919"),
            ("minute_051d_sz000001.bin", 0, "000001"),
            ("minute_051d_sh510050.bin", 1, "510050"),
            ("minute_051d_bj821001.bin", 2, "821001"),
        ]:
            body = (FIXTURES / fn).read_bytes()
            parser = _make_parser(mkt, code)
            loc = parser._locate_records(bytes(body), struct.unpack("<H", body[:2])[0])
            self.assertIsNotNone(loc, fn)
            lens[fn] = loc[0]
        self.assertEqual(lens["minute_051d_sh601919.bin"], 64)
        self.assertEqual(lens["minute_051d_sz000001.bin"], 67)
        self.assertEqual(lens["minute_051d_sh510050.bin"], 68)
        self.assertEqual(lens["minute_051d_bj821001.bin"], 71)


class TestLegacyFallback(unittest.TestCase):
    """旧 6B 头格式（legacy 会话响应）兜底解析。"""

    def test_legacy_layout(self):
        body = (FIXTURES / "minute_legacy_sh601919.bin").read_bytes()
        self.assertEqual(struct.unpack("<f", body[2:6])[0], 16.8799991607666)
        prices = _make_parser(1, "601919").parseResponse(body)
        self.assertEqual(len(prices), 240)
        self.assertAlmostEqual(prices[-1]["price"], 16.73, places=6)


class TestFailureModes(unittest.TestCase):
    """畸形/垃圾输入：解析失败必须返回 []，不许抛异常或返回垃圾价。"""

    def test_garbage_body_returns_empty(self):
        # 随机字节流：扫描与 legacy 兜底都不该命中
        garbage = bytes(range(256)) * 8
        prices = _make_parser(1, "601919").parseResponse(garbage)
        self.assertEqual(prices, [])

    def test_truncated_body_returns_empty(self):
        body = (FIXTURES / "minute_051d_sh601919.bin").read_bytes()
        prices = _make_parser(1, "601919").parseResponse(body[:200])
        self.assertEqual(prices, [])

    def test_num_mismatch_returns_empty(self):
        """num 字段被破坏（记录数对不上包长）：不命中任何 off → []。"""
        body = bytearray((FIXTURES / "minute_051d_sh601919.bin").read_bytes())
        struct.pack_into("<H", body, 0, 999)  # 假 num
        prices = _make_parser(1, "601919").parseResponse(bytes(body))
        self.assertEqual(prices, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
