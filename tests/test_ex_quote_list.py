# -*- coding: utf-8 -*-
"""EX 期货/港股行情列表（0x0B24 get_instrument_quote_list）单测（全离线）。

背景：`extract_futures` 曾误写 `return datalist`（应为 `return pos`），调用方把
返回的 list 当偏移用，第二个块开始 `body_buf[pos:pos+10]` 抛
`can only concatenate list (not int)`——表现为期货市场（category=3）列表全部失败。
本测试用真实服务器响应存档（tests/fixtures/ex_quote_list_*.bin）做回归：
多块解析必须按 300 字节/块推进、代码互不重复、字段可读。
"""
import pathlib
import struct
import unittest

from tdxpy.parser.ext.ex_get_instrument_quote_list import GetInstrumentQuoteList

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 构造只需占位（单测直接调 setParams/parseResponse，不走 socket）。"""


def parse_fixture(market, category, count):
    parser = GetInstrumentQuoteList(_FakeClient())
    parser.setParams(market, category, 0, count)
    body = (FIXTURES / f"ex_quote_list_{market}_c{count}.bin").read_bytes()
    return parser.parseResponse(body)


class TestFuturesQuoteList(unittest.TestCase):
    """期货（category=3）多块解析回归——修复前此处抛 ValidationException。"""

    def _check(self, market, expected_prefixes):
        rows = parse_fixture(market, 3, 5)
        self.assertEqual(len(rows), 5, f"market={market} 应有 5 条")
        codes = [r["code"] for r in rows]
        self.assertEqual(len(set(codes)), 5, f"代码不得重复（块偏移错会重叠）：{codes}")
        for r in rows:
            self.assertTrue(str(r["code"]).strip(), "代码为空")
            self.assertEqual(r["market"], market)
            # 字段健全性：涨跌停必须为正且 高 >= 低；成交/持仓非负
            self.assertGreater(r["ZuiGao"], 0)
            self.assertGreaterEqual(r["ZuiGao"], r["ZuiDi"])
            self.assertGreaterEqual(r["ZongLiang"], 0)
            self.assertGreaterEqual(r["ChiCangLiang"], 0)
            # 买价只做上界检查：实测无盘口合约买价为 0（47 市场 IC500），
            # 且有挂单价低于跌停价的真实样本（30 市场 AD2703），故不断言「在涨跌停区间内」。
            self.assertGreaterEqual(r["MaiRuJia"], 0)
            self.assertLessEqual(r["MaiRuJia"], r["ZuiGao"] + 1e-6)
        for code, prefix in zip(codes, expected_prefixes):
            self.assertTrue(code.startswith(prefix), f"{code} 期望前缀 {prefix}")

    def test_zce_market28(self):
        self._check(28, ["AP"] * 5)

    def test_shfe_market30(self):
        self._check(30, ["AD"] * 5)

    def test_cffex_market47(self):
        self._check(47, ["IC"] * 5)

    def test_block_stride_300(self):
        """响应体长度 = 2B 数量 + N×300B，parser 必须完整消费最后一个块（无越界无剩余）。"""
        body = (FIXTURES / "ex_quote_list_30_c5.bin").read_bytes()
        (num,) = struct.unpack("<H", body[:2])
        self.assertEqual(len(body), 2 + num * 300)
        rows = parse_fixture(30, 3, 5)
        self.assertEqual(len(rows), num)


class TestHongkongQuoteList(unittest.TestCase):
    """港股（category=2）分支原本正确，保留回归。"""

    def test_hk_market31(self):
        rows = parse_fixture(31, 2, 5)
        self.assertEqual(len(rows), 5)
        codes = [r["code"] for r in rows]
        self.assertEqual(len(set(codes)), 5)
        for r in rows:
            self.assertEqual(r["market"], 31)
            self.assertTrue(str(r["code"]).strip())
            self.assertGreater(r["ZuiGao"], 0)
            self.assertGreaterEqual(r["ZuiGao"], r["ZuiDi"])


class TestUnsupportedCategory(unittest.TestCase):
    def test_category_not_supported(self):
        parser = GetInstrumentQuoteList(_FakeClient())
        parser.setParams(31, 5, 0, 5)
        body = struct.pack("<H", 1) + bytes(300)
        with self.assertRaises(NotImplementedError):
            parser.parseResponse(body)

    def test_empty_response(self):
        parser = GetInstrumentQuoteList(_FakeClient())
        parser.setParams(28, 3, 0, 5)
        self.assertEqual(parser.parseResponse(struct.pack("<H", 0)), [])


class TestMalformedBody(unittest.TestCase):
    """块长校验（2026-11-09 新增）：畸形响应必须报错，不得静默返回错位/垃圾数据。"""

    @staticmethod
    def _fixture():
        return (FIXTURES / "ex_quote_list_30_c5.bin").read_bytes()

    def test_single_block(self):
        """num=1 只含一块（300B）时正常解析。"""
        body = self._fixture()
        one = struct.pack("<H", 1) + body[2:302]
        parser = GetInstrumentQuoteList(_FakeClient())
        parser.setParams(30, 3, 0, 1)
        rows = parser.parseResponse(one)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["code"].strip())
        self.assertGreater(rows[0]["ZuiGao"], 0)

    def test_truncated_last_block(self):
        """末块被截断（少 150B）→ ValueError，不得静默返回 5 条。"""
        body = self._fixture()
        parser = GetInstrumentQuoteList(_FakeClient())
        parser.setParams(30, 3, 0, 5)
        with self.assertRaises(ValueError):
            parser.parseResponse(body[: len(body) - 150])

    def test_num_overstated(self):
        """num 声明 7 但实际只有 5 块 → ValueError，不得读出空 code 的垃圾条目。"""
        body = self._fixture()
        parser = GetInstrumentQuoteList(_FakeClient())
        parser.setParams(30, 3, 0, 7)
        with self.assertRaises(ValueError):
            parser.parseResponse(struct.pack("<H", 7) + body[2:])

    def test_header_only(self):
        """仅 1 字节（连数量头都不完整）→ ValueError。"""
        parser = GetInstrumentQuoteList(_FakeClient())
        parser.setParams(30, 3, 0, 5)
        with self.assertRaises(ValueError):
            parser.parseResponse(b"\x05")


class TestSendPkg(unittest.TestCase):
    """请求包布局回归（0x0B24 + market/category/start/count/1）。"""

    def test_send_pkg_layout(self):
        parser = GetInstrumentQuoteList(_FakeClient())
        parser.setParams(28, 3, 10, 5)
        pkg = bytes(parser.send_pkg)
        # 12B 固定头 + struct.pack("<BHHHH", market, 0, start, count, 1) = 21B
        self.assertEqual(len(pkg), 21)
        self.assertEqual(pkg[:4], bytes.fromhex("01c1060b"))
        self.assertEqual(pkg[12], 28)                                  # market (B)
        self.assertEqual(struct.unpack("<H", pkg[15:17])[0], 10)        # start
        self.assertEqual(struct.unpack("<H", pkg[17:19])[0], 5)         # count
        self.assertEqual(struct.unpack("<H", pkg[19:21])[0], 1)         # 尾随 1


if __name__ == "__main__":
    unittest.main()
