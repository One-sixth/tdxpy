# -*- coding: utf-8 -*-
"""0x052B 退市股不复权日线 parser + 门面归一化单测（2026-10-19 eltdx 吸收批次）。

fixture = tests/fixtures/delisted_052b_sz000038.bin：真实服务器响应存档
（sz000038 start=0 count=20，自 eltdx 离线夹具 response.bin 剥 16B 帧头而来，
期望值与 eltdx golden 逐字节对齐验证）。

覆盖：差分累积解析、OHLC 自洽守卫、90 年代老日期、代码归一化四种写法、
批量先校验后查询纪律、记录流对齐守卫、在线冒烟（网络不可达自动 skip）。
运行：python tests/test_delisted_bars.py -v
"""
import struct
import unittest
from pathlib import Path
from unittest import mock

from tdxpy.parser.std.get_delisted_bars import GetDelistedBarsCmd

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 构造只需占位（单测直接调 parseResponse，不走 socket）。"""


def _make_parser():
    p = GetDelistedBarsCmd(_FakeClient())
    p.setParams(0, "000038")
    return p


def _synth_body(records, declared=None, trailing=b""):
    """合成 0x052B 响应 body。records = [(date, open_d, close_d, high_d, low_d, vol, amount)]。"""
    body = struct.pack("<H", len(records) if declared is None else declared)
    for date, od, cd, hd, ld, vol, amount in records:
        body += struct.pack("<I", date)
        for v in (od, cd, hd, ld, vol):
            body += _encode_varint(v)
        body += struct.pack("<I", amount) + b"\x00\x00\x01\x00"
    return body + trailing


def _encode_varint(v):
    """tdxpy get_price 的逆运算（测试合成用）：首字节低 6 位 + 0x40 符号 + 0x80 续位。"""
    sign = 0x40 if v < 0 else 0
    m = abs(v)
    out = bytearray([(m & 0x3F) | sign])
    m >>= 6
    shift = 6
    while m:
        out[-1] |= 0x80
        out.append(m & 0x7F)
        m >>= 7
        shift += 7
    return bytes(out)


class TestParseFixture(unittest.TestCase):
    """eltdx golden 夹具解析（期望值与 eltdx tests/fixtures 对齐）。"""

    @classmethod
    def setUpClass(cls):
        cls.body = (FIXTURES / "delisted_052b_sz000038.bin").read_bytes()
        cls.rows = _make_parser().parseResponse(cls.body)

    def test_row_count(self):
        self.assertEqual(len(self.rows), 20)

    def test_first_bar(self):
        r = self.rows[0]
        self.assertEqual(r["datetime"], "2023-04-24")
        self.assertAlmostEqual(r["open"], 3.320, places=9)
        self.assertAlmostEqual(r["close"], 3.500, places=9)
        self.assertAlmostEqual(r["high"], 3.520, places=9)
        self.assertAlmostEqual(r["low"], 3.320, places=9)
        self.assertAlmostEqual(r["vol"], 44137.0, places=6)  # 股→手
        self.assertAlmostEqual(r["amount"], 15186139.0, places=3)

    def test_second_bar_delta_chain(self):
        """第二根：open=前收(3500)+(-130)=3370 milli（差分跨根累积）。"""
        r = self.rows[1]
        self.assertEqual(r["datetime"], "2023-04-25")
        self.assertAlmostEqual(r["open"], 3.370, places=9)
        self.assertAlmostEqual(r["close"], 3.340, places=9)
        self.assertAlmostEqual(r["high"], 3.470, places=9)
        self.assertAlmostEqual(r["low"], 3.330, places=9)

    def test_last_bar_delisting_day(self):
        r = self.rows[-1]
        self.assertEqual(r["datetime"], "2023-07-11")
        self.assertAlmostEqual(r["close"], 0.520, places=9)

    def test_sorted_ascending(self):
        dts = [r["datetime"] for r in self.rows]
        self.assertEqual(dts, sorted(dts))


class TestGuards(unittest.TestCase):
    """坏结构守卫：全部 raise ValueError（坏数据比空结果危险，不静默）。"""

    def test_ohlc_inconsistent(self):
        # open=1000, close=1000, high=900（低于 open/close）→ 不自洽
        body = _synth_body([(20230424, 1000, 0, -100, 0, 100, 1000)])
        with self.assertRaises(ValueError):
            _make_parser().parseResponse(body)

    def test_1990s_date_passes(self):
        """1998-12-28 合法（沪深 1990 开市；在线实测 000038 有 90 年代数据，下限设 2000 会误杀）。"""
        body = _synth_body([(19981228, 5000, 0, 0, 0, 100, 1000)])
        rows = _make_parser().parseResponse(body)
        self.assertEqual(rows[0]["datetime"], "1998-12-28")

    def test_invalid_date_rejected(self):
        body = _synth_body([(20231332, 1000, 0, 0, 0, 100, 1000)])  # 13 月
        with self.assertRaises(ValueError):
            _make_parser().parseResponse(body)

    def test_negative_volume_rejected(self):
        body = _synth_body([(20230424, 1000, 0, 0, 0, -1, 1000)])
        with self.assertRaises(ValueError):
            _make_parser().parseResponse(body)

    def test_trailing_bytes_rejected(self):
        body = _synth_body([(20230424, 1000, 0, 0, 0, 100, 1000)], trailing=b"\x00")
        with self.assertRaises(ValueError):
            _make_parser().parseResponse(body)

    def test_declared_count_overrun(self):
        # 声明 3 条但只有 1 条记录（单条下限 17B：2+17 < len 不触发物理上限检查时靠截断守卫兜底）
        body = _synth_body([(20230424, 1000, 0, 0, 0, 100, 1000)], declared=3)
        with self.assertRaises(ValueError):
            _make_parser().parseResponse(body)

    def test_declared_count_exceeds_physical(self):
        # 声明 999 条但 body 只有 2B → 物理上限守卫（单条下限 17B）
        with self.assertRaises(ValueError):
            _make_parser().parseResponse(struct.pack("<H", 999))

    def test_body_too_short(self):
        with self.assertRaises(ValueError):
            _make_parser().parseResponse(b"\x01")


class TestSymbolNormalize(unittest.TestCase):
    """四种写法归一化（点分式 / 前缀式 / eltdx T 前缀 / 裸代码推断）。"""

    def test_four_writes_same_target(self):
        from tdxpy.client import _normalize_delisted_symbol
        for s in ("T000038", "sz000038", "000038", "sz.000038", " SZ.000038 "):
            market_id, code, std = _normalize_delisted_symbol(s)
            self.assertEqual((market_id, code, std), (0, "000038", "sz.000038"))

    def test_market_inference(self):
        from tdxpy.client import _normalize_delisted_symbol
        self.assertEqual(_normalize_delisted_symbol("600001")[0], 1)   # 6→SH
        self.assertEqual(_normalize_delisted_symbol("920001")[0], 2)   # 92→BJ
        self.assertEqual(_normalize_delisted_symbol("830001")[0], 2)   # 8→BJ
        self.assertEqual(_normalize_delisted_symbol("300001")[0], 0)   # 3→SZ
        self.assertEqual(_normalize_delisted_symbol("sh.600001")[2], "sh.600001")
        self.assertEqual(_normalize_delisted_symbol("bj.830001")[2], "bj.830001")

    def test_invalid_rejected(self):
        from tdxpy.client import _normalize_delisted_symbol
        for bad in ("12345", "abcdef", "００００３８", "us.AAPL", "12.3456", ""):
            with self.assertRaises(ValueError):
                _normalize_delisted_symbol(bad)


class TestBatchDiscipline(unittest.TestCase):
    """批量「先全部校验、再开始查询」纪律（吸收 eltdx：不产生半成品结果）。"""

    def _make_client(self):
        from tdxpy.client import TdxClient
        c = object.__new__(TdxClient)
        c._host, c._port = "127.0.0.1", 7709
        c._ex_host, c._ex_port = None, 7727
        c._heartbeat, c._timeout = False, 5.0
        c._hq_api, c._ex_api = object(), None
        c._lock = mock.MagicMock()
        return c

    def test_invalid_code_queries_nothing(self):
        c = self._make_client()
        with mock.patch.object(c, "_hq_call") as hq:
            with self.assertRaises(ValueError):
                c.delisted_bars(["sz.000038", "12345"], count=5)
            hq.assert_not_called()  # 校验失败 → 一次查询都没发

    def test_batch_dedup_and_keys(self):
        c = self._make_client()
        fake_rows = [{"datetime": "2023-07-11", "open": 1, "close": 1, "high": 1, "low": 1,
                      "vol": 1, "amount": 1, "year": 2023, "month": 7, "day": 11,
                      "hour": 0, "minute": 0}]
        with mock.patch.object(c, "_hq_call", return_value=fake_rows) as hq:
            res = c.delisted_bars(["T000038", "sz000038", "000038", "sz.000038", "sh.600001"], count=5)
            self.assertEqual(sorted(res.keys()), ["sh.600001", "sz.000038"])  # 4 写法去重成 1 + 1
            self.assertEqual(hq.call_count, 2)

    def test_empty_list_rejected(self):
        c = self._make_client()
        with self.assertRaises(ValueError):
            c.delisted_bars([])


class TestOnline(unittest.TestCase):
    """在线冒烟（网络不可达自动 skip）。"""

    @classmethod
    def setUpClass(cls):
        try:
            from tdxpy.client import TdxClient
            cls.client = TdxClient.auto()
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接通达信服务器: {type(e).__name__}: {e}")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.client.close()
        except Exception:  # noqa
            pass

    def test_delisted_sample(self):
        """sz.000038：1994-08-08 上市 → 2023-07-11 退市，全量 5000+ 根升序无重复。"""
        df = self.client.delisted_bars("sz.000038", all_pages=True)
        self.assertGreater(len(df), 5000)
        self.assertEqual(df["datetime"].iloc[-1], "2023-07-11")
        self.assertEqual(df["datetime"].iloc[0], "1994-08-08")
        self.assertTrue(df["datetime"].is_monotonic_increasing)
        self.assertFalse(df["datetime"].duplicated().any())
        self.assertAlmostEqual(df["close"].iloc[-1], 0.52, places=6)

    def test_delisted_batch(self):
        res = self.client.delisted_bars(["T000038", "sh.600001"], count=5)
        self.assertEqual(sorted(res.keys()), ["sh.600001", "sz.000038"])
        for df in res.values():
            self.assertEqual(len(df), 5)
            self.assertTrue(df["datetime"].is_monotonic_increasing)


if __name__ == "__main__":
    unittest.main(verbosity=2)
