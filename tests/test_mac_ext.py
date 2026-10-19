# -*- coding: utf-8 -*-
"""MAC 扩展命令族单测（批次五，2026-10-20）：0x122C 成分 / 0x123D 竞价 / 0x1237 异动 / 0x122A 快照。

fixture = 真实在线响应存入 tests/fixtures/mac_ext_*.bin（2026-10-20 抓取）：
- mac_ext_members.bin   板块成分（881248 诊断服务 6 只，8 字段位图全返）
- mac_ext_auction.bin   集合竞价（sz.000001 当日 59 条 09:15-09:24:57）
- mac_ext_unusual.bin   市场异动（裁剪自 600 条响应：头 2B + 前 4 条 + 前 4 个名字）
- mac_ext_snapshot.bin  个股快照（sz.000001 216B，OHLC/量额与 std quotes 交叉验证一致）

覆盖：MAC 帧构造（10B 头 + cmd + body）、四命令解析、board_code 换算、
字段流位图步进、残帧防御、facade 路由参数透传。
运行：python tests/test_mac_ext.py -v
"""
import struct
import unittest
from pathlib import Path
from unittest import mock

from tdxpy.parser.mac._frame import MAC_FRAME_HEADER_SIZE
from tdxpy.parser.mac._frame import build_mac_frame
from tdxpy.parser.mac.mac_ext import MEMBER_BASIC_BITS
from tdxpy.parser.mac.mac_ext import MacAuctionCmd
from tdxpy.parser.mac.mac_ext import MacBatchQuotesCmd
from tdxpy.parser.mac.mac_ext import MacBelongBoardCmd
from tdxpy.parser.mac.mac_ext import MacBoardMembersCmd
from tdxpy.parser.mac.mac_ext import MacSymbolInfoCmd
from tdxpy.parser.mac.mac_ext import MacUnusualCmd
from tdxpy.parser.mac.mac_ext import convert_board_code

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 依赖仅占位——单测直接调 parseResponse，不碰 socket。"""


class TestBoardCodeConvert(unittest.TestCase):
    """板块码换算表（2026-10-20 在线实测 881248→21248 全响应 626B）。"""

    def test_88(self):
        self.assertEqual(convert_board_code("881248"), 21248)
        self.assertEqual(convert_board_code("881001"), 21001)
        self.assertEqual(convert_board_code("881101"), 21101)
        self.assertEqual(convert_board_code("880218"), 20218)  # 880 段（0x1218 所属板块返回）

    def test_399_899(self):
        self.assertEqual(convert_board_code("399001"), 30001)
        self.assertEqual(convert_board_code("899050"), 32050)

    def test_int_passthrough(self):
        self.assertEqual(convert_board_code(21248), 21248)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            convert_board_code("600000")
        with self.assertRaises(ValueError):
            convert_board_code("88")


class TestMembersCmd(unittest.TestCase):
    """0x122C 板块成分。"""

    def test_frame_layout(self):
        cmd = MacBoardMembersCmd(_FakeClient())
        cmd.setParams("881248")
        frame = cmd.send_pkg
        flag, customize, version, zips, unzips = struct.unpack_from("<BIBHH", frame)
        self.assertEqual((flag, customize, version), (0x1C, 0, 1))
        inner_cmd = struct.unpack_from("<H", frame, MAC_FRAME_HEADER_SIZE)[0]
        self.assertEqual(inner_cmd, 0x122C)
        body = frame[MAC_FRAME_HEADER_SIZE + 2:]
        # 43B：board_code(u32) + 9x + sort(H) + start(I) + count(H) + 0(B) + 0(B)
        #      + bitmap16 + 尾 4B（与参考实现逐字节同构，在线实测 626B 响应）
        self.assertEqual(len(body), 43)
        bc, = struct.unpack_from("<I", body, 0)
        self.assertEqual(bc, 21248)  # 换算后
        sort_type, = struct.unpack_from("<H", body, 13)
        start, = struct.unpack_from("<I", body, 15)
        count, zero1, zero2 = struct.unpack_from("<HBB", body, 19)
        self.assertEqual((sort_type, start, count, zero1, zero2), (0, 0, 80, 0, 0))
        bitmap = body[23:39]
        self.assertEqual(bitmap[0], 0xFF)  # bits 0-7 全置位
        self.assertEqual(bitmap[1:], bytes(15))
        self.assertEqual(body[39:], b"\x00\x00\x00\x01")

    def test_parse_fixture(self):
        cmd = MacBoardMembersCmd(_FakeClient())
        cmd.setParams("881248")
        rows = cmd.parseResponse((FIXTURES / "mac_ext_members.bin").read_bytes())
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["total"], 6)
        # 首行：000710 贝瑞基因（与 0x1237 异动列表交叉印证）
        r0 = rows[0]
        self.assertEqual((r0["market"], r0["code"], r0["name"]), (0, "000710", "贝瑞基因"))
        self.assertAlmostEqual(r0["pre_close"], 10.07, places=3)
        self.assertAlmostEqual(r0["open"], 10.0, places=3)
        self.assertAlmostEqual(r0["high"], 10.98, places=3)
        self.assertAlmostEqual(r0["low"], 9.65, places=3)
        self.assertAlmostEqual(r0["close"], 10.42, places=3)
        self.assertEqual(r0["vol"], 536923)  # <I 整型
        self.assertAlmostEqual(r0["volume_ratio"], 0.9161, places=3)
        self.assertAlmostEqual(r0["amount"], 550006976.0, places=1)
        # 跨市场成分（最后一只 688315 科创板 market=1）
        self.assertEqual((rows[-1]["market"], rows[-1]["code"]), (1, "688315"))

    def test_custom_bits(self):
        cmd = MacBoardMembersCmd(_FakeClient())
        cmd.setParams("881248", bits=[0x04, 0x05])
        bitmap = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2 + 23:MAC_FRAME_HEADER_SIZE + 2 + 39]
        self.assertEqual(bitmap[0], (1 << 4) | (1 << 5))
        self.assertEqual(bitmap[1], 0)

    def test_error_frame(self):
        cmd = MacBoardMembersCmd(_FakeClient())
        cmd.setParams("881248")
        # 在线实测 board_code 未换算时返回 26B 全零形错误帧 → 返回空不抛
        bad = bytes.fromhex("ff00" + "00" * 24)
        self.assertEqual(cmd.parseResponse(bad), [])

    def test_truncated_field_stream(self):
        cmd = MacBoardMembersCmd(_FakeClient())
        cmd.setParams("881248")
        full = (FIXTURES / "mac_ext_members.bin").read_bytes()
        # 截断首行字段流（保留 20B 位图 + 6B 头 + 68B 行头 + 10B 字段）
        cut = full[:26 + 68 + 10]
        rows = cmd.parseResponse(cut)
        self.assertEqual(len(rows), 1)
        self.assertIn("pre_close", rows[0])  # 前几个字段已解出
        self.assertNotIn("amount", rows[0])  # 尾部字段被截断丢弃

    def test_overclaim_bitmap_stops_row_loop(self):
        """响应位图 overclaim（声称字段流比实际长）→ 当前行保留、后续行终止（复审 P1-2）。"""
        cmd = MacBoardMembersCmd(_FakeClient())
        cmd.setParams("881248")
        # 手工构造：位图 160 位全置（overclaim）+ 声称 2 行但只有 1 行字段流
        body = (b"\xff" * 20                                  # 响应位图全 1
                + struct.pack("<IH", 2, 2)                    # total=2 rows=2
                + struct.pack("<H22s44s", 0, b"000001", "平安银行".encode("gbk").ljust(44, b"\x00"))
                + struct.pack("<8f", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0)   # 行 1 仅 8 字段
                + struct.pack("<H22s44s", 1, b"600000", "浦发银行".encode("gbk").ljust(44, b"\x00")))
        rows = cmd.parseResponse(body)
        # 行 1 解完 8 字段后第 9 位越界 → 保留行 1（partial），行 2 终止不产出
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["code"], "000001")
        self.assertAlmostEqual(rows[0]["pre_close"], 1.0, places=3)


class TestAuctionCmd(unittest.TestCase):
    """0x123D 集合竞价。"""

    def test_frame_layout(self):
        cmd = MacAuctionCmd(_FakeClient())
        cmd.setParams(0, "000001", start=0, count=100)
        body = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2:]
        self.assertEqual(len(body), 42)
        market, = struct.unpack_from("<H", body, 0)
        code = body[2:24].rstrip(b"\x00")
        start, count = struct.unpack_from("<II", body, 24)
        self.assertEqual((market, code, start, count), (0, b"000001", 0, 100))

    def test_parse_fixture(self):
        cmd = MacAuctionCmd(_FakeClient())
        cmd.setParams(0, "000001")
        rows = cmd.parseResponse((FIXTURES / "mac_ext_auction.bin").read_bytes())
        self.assertEqual(len(rows), 59)
        # 首条 09:15:00，价格 11.65（昨收），matched 326，unmatched -118
        r0 = rows[0]
        self.assertEqual((r0["time"], r0["price"], r0["matched"], r0["unmatched"]),
                         ("09:15:00", 11.65, 326, -118))
        # 末条 09:24:57，价格 11.62 = 当日开盘价（std quotes 交叉验证）
        rlast = rows[-1]
        self.assertEqual(rlast["time"], "09:24:57")
        self.assertAlmostEqual(rlast["price"], 11.62, places=3)
        self.assertEqual(rlast["matched"], 3994)
        # matched 累计递增（同分钟内）
        self.assertGreaterEqual(rows[-1]["matched"], rows[0]["matched"])

    def test_short_response(self):
        cmd = MacAuctionCmd(_FakeClient())
        cmd.setParams(0, "000001")
        self.assertEqual(cmd.parseResponse(b"\x00" * 20), [])


class TestUnusualCmd(unittest.TestCase):
    """0x1237 市场异动。"""

    def test_frame_layout(self):
        cmd = MacUnusualCmd(_FakeClient())
        cmd.setParams(0, start=0, count=600)
        body = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2:]
        self.assertEqual(len(body), 22)
        market, start, count = struct.unpack_from("<HHH", body, 0)
        market2, = struct.unpack_from("<H", body, 6)  # count 在 @6
        self.assertEqual((market, start, market2), (0, 0, 600))
        flag, = struct.unpack_from("<H", body, 10)
        tail = struct.unpack_from("<5H", body, 12)
        self.assertEqual((flag, tail), (1, (200, 30, 40, 50, 200)))

    def test_count_capped(self):
        cmd = MacUnusualCmd(_FakeClient())
        cmd.setParams(0, count=5000)
        body = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2:]
        count = struct.unpack_from("<H", body, 6)[0]  # <HH2xH2x...：count 在 body@6
        self.assertEqual(count, 600)

    def test_parse_fixture(self):
        cmd = MacUnusualCmd(_FakeClient())
        cmd.setParams(0)
        rows = cmd.parseResponse((FIXTURES / "mac_ext_unusual.bin").read_bytes())
        self.assertEqual(len(rows), 4)
        # 首条：000056 *ST皇庭 竞价涨跌，触发时点价 2.09（=当日收盘/开盘价，std 交叉验证）
        r0 = rows[0]
        self.assertEqual((r0["code"], r0["name"]), ("000056", "*ST皇庭"))
        self.assertEqual(r0["type"], 0x14)
        self.assertEqual(r0["type_name"], "竞价涨跌")
        self.assertAlmostEqual(r0["price"], 2.09, places=3)
        self.assertAlmostEqual(r0["amount"], 2879.0, places=1)
        self.assertEqual(r0["time"], "09:15:00")
        # 第 2 条：000530 冰山冷热 7.02（触发时点价 f32=7.01999…，当日开盘 7.66——快照口径实锤）
        self.assertEqual((rows[1]["code"], rows[1]["name"]), ("000530", "冰山冷热"))
        self.assertAlmostEqual(rows[1]["price"], 7.02, places=3)

    def test_type_names(self):
        from tdxpy.parser.mac.mac_ext import UNUSUAL_TYPES
        self.assertEqual(UNUSUAL_TYPES[0x13], "竞价试买")
        self.assertEqual(UNUSUAL_TYPES[0x14], "竞价涨跌")

    def test_empty_name_keeps_alignment(self):
        """名字表中间空名必须保留占位，防止后续名字张冠李戴（复审 P2-1 回归锁）。"""
        cmd = MacUnusualCmd(_FakeClient())
        cmd.setParams(0)
        # 构造 3 条记录 + 名字表 "甲,,乙,"（中间空名占位）
        body = struct.pack("<H", 3) + bytes(3 * 32)
        body += "甲,,乙,".encode("gbk")
        rows = cmd.parseResponse(body)
        self.assertEqual([r["name"] for r in rows], ["甲", "", "乙"])


class TestSnapshotCmd(unittest.TestCase):
    """0x122A 个股特征快照。"""

    def test_frame_layout(self):
        cmd = MacSymbolInfoCmd(_FakeClient())
        cmd.setParams(0, "000001")
        body = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2:]
        self.assertEqual(len(body), 40)
        market, = struct.unpack_from("<H", body, 0)
        code = body[2:24].rstrip(b"\x00")
        flag, = struct.unpack_from("<I", body, 24)
        self.assertEqual((market, code, flag), (0, b"000001", 1))

    def test_parse_fixture(self):
        cmd = MacSymbolInfoCmd(_FakeClient())
        cmd.setParams(0, "000001")
        rows = cmd.parseResponse((FIXTURES / "mac_ext_snapshot.bin").read_bytes())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["market"], r["code"], r["name"]), (0, "000001", "平安银行"))
        self.assertEqual(r["date"], 20261020)
        self.assertEqual(r["time"], 153306)
        # 以下字段与 std quotes 逐值交叉验证（2026-10-20 实测）
        self.assertAlmostEqual(r["pre_close"], 11.65, places=3)
        self.assertAlmostEqual(r["open"], 11.62, places=3)
        self.assertAlmostEqual(r["high"], 11.68, places=3)
        self.assertAlmostEqual(r["low"], 11.38, places=3)
        self.assertAlmostEqual(r["close"], 11.44, places=3)
        self.assertEqual(r["vol"], 1245096)
        self.assertAlmostEqual(r["amount"], 1429559808.0, delta=4096)
        self.assertEqual(r["in_vol"], 673775)
        self.assertEqual(r["out_vol"], 571322)
        # 内外盘之和 ≈ 总量（±1 容差）
        self.assertAlmostEqual(r["in_vol"] + r["out_vol"], r["vol"], delta=1)

    def test_short_response(self):
        cmd = MacSymbolInfoCmd(_FakeClient())
        cmd.setParams(0, "000001")
        self.assertEqual(cmd.parseResponse(b"\x00" * 30), [])

    def test_guard_window_136_to_147(self):
        """长度落在 [136,148) 空窗也必须返回空而非抛 struct.error（复审 P1-1 回归锁）。"""
        cmd = MacSymbolInfoCmd(_FakeClient())
        cmd.setParams(0, "000001")
        for size in (136, 140, 147):
            with self.subTest(size=size):
                rows = cmd.parseResponse(b"\x00" * size)
                self.assertEqual(rows, [])


class TestBatchQuotesCmd(unittest.TestCase):
    """0x122B 批量报价（批次六复活：请求位图 20B——曾误用 16B 导致列表语义失效假象）。"""

    def test_frame_layout(self):
        cmd = MacBatchQuotesCmd(_FakeClient())
        cmd.setParams([(0, "000001"), (1, "600000")])
        body = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2:]
        # 20B 位图 + u16 count + 2×24B（market+code22）= 70B
        self.assertEqual(len(body), 70)
        self.assertEqual(body[:20], bytes([0xFF]) + bytes(19))  # 位图 20B！
        count, = struct.unpack_from("<H", body, 20)
        self.assertEqual(count, 2)
        mkt, code = struct.unpack_from("<H22s", body, 22)
        self.assertEqual((mkt, code.rstrip(b"\x00")), (0, b"000001"))
        mkt2, code2 = struct.unpack_from("<H22s", body, 46)
        self.assertEqual((mkt2, code2.rstrip(b"\x00")), (1, b"600000"))

    def test_page_cap_80(self):
        cmd = MacBatchQuotesCmd(_FakeClient())
        with self.assertRaises(ValueError):
            cmd.setParams([(0, "000001")] * 81)

    def test_parse_fixture_same_shape_as_members(self):
        """响应与 0x122C 同构——共享解析器直接吃 0x122B 真实响应。"""
        cmd = MacBatchQuotesCmd(_FakeClient())
        cmd.setParams([(0, "000001"), (1, "600000"), (0, "300750")])
        rows = cmd.parseResponse((FIXTURES / "mac_ext_batch_quotes.bin").read_bytes())
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["code"] for r in rows], ["000001", "600000", "300750"])
        self.assertEqual([r["name"] for r in rows], ["平安银行", "浦发银行", "宁德时代"])
        # 字段与 std quotes / 0x122A 交叉验证一致（2026-10-20 收盘口径）
        r0 = rows[0]
        self.assertAlmostEqual(r0["pre_close"], 11.65, places=3)
        self.assertAlmostEqual(r0["close"], 11.44, places=3)
        self.assertEqual(r0["vol"], 1245096)  # <I 整型
        self.assertAlmostEqual(r0["volume_ratio"], 1.063, places=3)
        self.assertAlmostEqual(r0["amount"], 1429559808.0, delta=4096)
        r2 = rows[2]  # 300750 宁德时代
        self.assertAlmostEqual(r2["close"], 259.08, places=2)
        self.assertAlmostEqual(r2["volume_ratio"], 1.66, places=2)

    def test_custom_bits_20b_bitmap(self):
        """自定义字段位写进 20B 位图（16B 位图曾致请求错位——布局回归锁）。"""
        cmd = MacBatchQuotesCmd(_FakeClient())
        cmd.setParams([(1, "600519")], bits=[0x00, 0x04, 0x38])
        body = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2:]
        self.assertEqual(len(body), 20 + 2 + 24)
        bm = body[:20]
        self.assertEqual(bm[0], (1 << 0) | (1 << 4))
        self.assertEqual(bm[7], 1 << 0)  # 0x38 → byte 7 bit 0
        self.assertEqual(sum(bm[1:7]), 0)

    def test_ex_frame_channel(self):
        """channel="ex" → EX 帧头（首字节 0x01 vs MAC 0x1C），body 与 mac 通道一致。

        2026-10-21 实测：同一 0x122B 请求走 EX 帧到 MAC EX 服务器（7727）即得
        港美股批量报价（工商银行/汇丰/腾讯/恒指/AAPL 全通）。
        """
        cmd_ex = MacBatchQuotesCmd(_FakeClient())
        cmd_ex.setParams([(31, "01398"), (74, "AAPL")], channel="ex")
        cmd_mac = MacBatchQuotesCmd(_FakeClient())
        cmd_mac.setParams([(31, "01398"), (74, "AAPL")], channel="mac")
        self.assertEqual(cmd_ex.send_pkg[0], 0x01)   # EX 帧 flag
        self.assertEqual(cmd_mac.send_pkg[0], 0x1C)  # MAC 帧 flag
        self.assertEqual(cmd_ex.send_pkg[1:], cmd_mac.send_pkg[1:])  # 仅 flag 位不同


class TestBelongBoardCmd(unittest.TestCase):
    """0x1218 个股所属板块（批次六，2026-10-20）。"""

    def test_frame_layout(self):
        cmd = MacBelongBoardCmd(_FakeClient())
        cmd.setParams(0, "000001")
        body = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2:]
        self.assertEqual(len(body), 47)
        market, = struct.unpack_from("<H", body, 0)
        code = body[2:10].rstrip(b"\x00")
        report = body[26:47].rstrip(b"\x00")
        self.assertEqual((market, code, report), (0, b"000001", b"Stock_GLHQ"))

    def test_parse_fixture(self):
        cmd = MacBelongBoardCmd(_FakeClient())
        cmd.setParams(0, "000001")
        rows = cmd.parseResponse((FIXTURES / "mac_ext_belong.bin").read_bytes())
        self.assertEqual(len(rows), 13)  # 000001 平安银行所属 13 板块
        r0 = rows[0]
        self.assertEqual((r0["category"], r0["market"], r0["board_code"], r0["board_name"]),
                         ("3", 1, "880218", "深圳板块"))
        self.assertAlmostEqual(r0["close"], 2940.39, places=2)
        self.assertAlmostEqual(r0["pre_close"], 2925.85, places=2)
        # 尾行：股份制银行 881388（881 段码也在所属板块里出现）
        rlast = rows[-1]
        self.assertEqual((rlast["board_code"], rlast["board_name"]), ("881388", "股份制银行"))
        # 板块码可喂回 convert_board_code（880/881 段统一换算）
        self.assertEqual(convert_board_code(rlast["board_code"]), 21388)

    def test_short_and_bad_json(self):
        cmd = MacBelongBoardCmd(_FakeClient())
        cmd.setParams(0, "000001")
        self.assertEqual(cmd.parseResponse(b"\x00" * 20), [])
        # 27B 头 + 坏 JSON → 返回空不抛
        bad = bytearray(27) + b"not-json".ljust(8, b"\x00")
        struct.pack_into("<H", bad, 22, 8)
        self.assertEqual(cmd.parseResponse(bytes(bad)), [])
        # JSON 顶层非数组 → 返回空不抛（复审 P2-4 回归锁）
        bad2 = bytearray(27) + b'{"a":1}'.ljust(8, b"\x00")
        struct.pack_into("<H", bad2, 22, 7)
        self.assertEqual(cmd.parseResponse(bytes(bad2)), [])

    def test_json_len_offset_23(self):
        """@23 u16 = JSON 字节数（000001=815/600519=1161 双样本自洽——复审 P1-1 回归锁）。"""
        cmd = MacBelongBoardCmd(_FakeClient())
        cmd.setParams(0, "000001")
        full = (FIXTURES / "mac_ext_belong.bin").read_bytes()
        json_len, = struct.unpack_from("<H", full, 23)
        self.assertEqual(json_len, 815)
        self.assertEqual(len(full) - 27, 815)  # 头 27B + JSON 815B = 842B 闭合


class TestFacadeRouting(unittest.TestCase):
    """TdxClient 门面路由参数透传（patch TdxClient._mac_call 断言透传参数）。"""

    def _make(self):
        from tdxpy.client import TdxClient
        tc = TdxClient.__new__(TdxClient)
        patcher = mock.patch.object(TdxClient, "_mac_call", return_value=[])
        self.mac_call = patcher.start()
        self.addCleanup(patcher.stop)
        return tc

    def test_mac_members_routes(self):
        tc = self._make()
        tc.mac_members("881248", sort_type=1, start=10, count=5)
        self.mac_call.assert_called_with("get_mac_board_members", "881248", 1, 10, 5)

    def test_mac_auction_routes_std_symbol(self):
        tc = self._make()
        with mock.patch.object(type(tc), "_std_symbol", return_value=(0, "000001")):
            tc.mac_auction("sz.000001", start=3, count=50)
        self.mac_call.assert_called_with("get_mac_auction", 0, "000001", 3, 50)

    def test_mac_unusual_routes(self):
        tc = self._make()
        tc.mac_unusual(market=0, start=600, count=600)
        self.mac_call.assert_called_with("get_mac_unusual", 0, 600, 600)

    def test_mac_snapshot_routes_std_symbol(self):
        tc = self._make()
        with mock.patch.object(type(tc), "_std_symbol", return_value=(1, "600000")):
            tc.mac_snapshot("sh.600000")
        self.mac_call.assert_called_with("get_mac_snapshot", 1, "600000")

    def test_mac_belong_routes_std_symbol(self):
        tc = self._make()
        with mock.patch.object(type(tc), "_std_symbol", return_value=(0, "000001")):
            tc.mac_belong_boards("sz.000001")
        self.mac_call.assert_called_with("get_mac_belong_boards", 0, "000001")

    def test_mac_unusual_all_pages(self):
        """all_pages 循环翻页：满页续拉、短页即停、页拼接（批次六）。"""
        from tdxpy.client import TdxClient

        tc = TdxClient.__new__(TdxClient)
        calls = []
        fake_pages = [
            [{"code": f"p1_{i}"} for i in range(600)],
            [{"code": f"p2_{i}"} for i in range(600)],
            [{"code": f"p3_{i}"} for i in range(123)],  # 短页→停
        ]

        def fake_call(method, *args):
            calls.append(args)
            return fake_pages[len(calls) - 1]

        with mock.patch.object(TdxClient, "_mac_call", side_effect=fake_call):
            df = tc.mac_unusual(all_pages=True)
        self.assertEqual(len(df), 600 + 600 + 123)
        # start 递增：0 → 600 → 1200（args = (market, start, count)）
        self.assertEqual([c[1] for c in calls], [0, 600, 1200])

    def test_mac_unusual_all_pages_empty_first(self):
        from tdxpy.client import TdxClient

        tc = TdxClient.__new__(TdxClient)
        with mock.patch.object(TdxClient, "_mac_call", return_value=[]):
            df = tc.mac_unusual(all_pages=True)
        self.assertTrue(df.empty)

    def test_mac_unusual_all_pages_count_gt_600(self):
        """count>600：page_size 截断为 600，start 按 600 递增（复审 P2-3 边界锁）。"""
        from tdxpy.client import TdxClient

        tc = TdxClient.__new__(TdxClient)
        calls = []
        fake_pages = [
            [{"code": f"a{i}"} for i in range(600)],
            [{"code": f"b{i}"} for i in range(10)],  # 短页→停
        ]

        def fake_call(method, *args):
            calls.append(args)
            return fake_pages[len(calls) - 1]

        with mock.patch.object(TdxClient, "_mac_call", side_effect=fake_call):
            df = tc.mac_unusual(count=1000, all_pages=True)
        self.assertEqual(len(df), 610)
        # page_size=600（截断），传给服务器的 count 仍是 1000
        self.assertEqual([c[1] for c in calls], [0, 600])
        self.assertEqual([c[2] for c in calls], [1000, 1000])

    def test_mac_auction_rejects_ex_symbol(self):
        from tdxpy.client import TdxClient
        tc = TdxClient.__new__(TdxClient)
        with self.assertRaises(ValueError):
            TdxClient.mac_auction(tc, "us.AAPL")
        with self.assertRaises(ValueError):
            TdxClient.mac_snapshot(tc, "hk.01398")

    def test_mac_ex_quotes_routes(self):
        """EX 批量报价门面：symbol 解析 + 透传 (ex_id, code) 对。"""
        from tdxpy.client import TdxClient
        tc = TdxClient.__new__(TdxClient)
        with mock.patch.object(TdxClient, "_mac_ex_call", return_value=[]) as ex_call:
            tc.mac_ex_quotes(["hk.01398", "us.AAPL", "hk.HSI"])
        ex_call.assert_called_once_with(
            "get_ex_batch_quotes", [(31, "01398"), (74, "AAPL"), (27, "HSI")], None)

    def test_mac_ex_quotes_batches_over_80(self):
        """超 80 只自动分批（2×80 → 2 次调用）。"""
        from tdxpy.client import TdxClient
        tc = TdxClient.__new__(TdxClient)
        syms = [f"hk.{i:05d}" for i in range(1, 161)]
        with mock.patch.object(TdxClient, "_mac_ex_call", return_value=[]) as ex_call:
            tc.mac_ex_quotes(syms)
        self.assertEqual(ex_call.call_count, 2)

    def test_mac_ex_quotes_rejects_std(self):
        """std 市场走 mac_quotes，进 EX 批量报价直接拒绝（ExMarket id 空间不同）。"""
        from tdxpy.client import TdxClient
        tc = TdxClient.__new__(TdxClient)
        with self.assertRaises(ValueError):
            tc.mac_ex_quotes("sh.600000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
