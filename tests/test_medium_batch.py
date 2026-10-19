# -*- coding: utf-8 -*-
"""MEDIUM 批次防线测试（2026-11-05）：六项上游吸收落地。

- ② _parse_symbol 数字市场段（"38.1_GDP" 宏观指标 symbol 形态）
- ① 0x1218 资金流（MacCapitalFlowCmd：帧布局 / fixture 解析 / net 派生 / 残帧防御）
- ③ 财务空壳判定（is_shell 字段 + 门面 drop_shell 过滤）
- ④ finance_all 并发门面（分片 / 失败隔离 / 空结果）
- ⑤ 批量报价校验（_validate_batch_quotes：缺失/外来/全零占位/on_missing 三态）
- ⑥ audit_price_scale（系数表 × decimal_point 交叉审计：零分歧与分歧检出）

fixture = 真实在线响应存入 tests/fixtures/mac_ext_capital_flow.bin（2026-11-05 抓取）。
运行：python tests/test_medium_batch.py -v
"""
import struct
import unittest
from unittest import mock

from tdxpy.client import TdxClient, _parse_symbol, _validate_batch_quotes
from tdxpy.parser.mac._frame import MAC_FRAME_HEADER_SIZE
from tdxpy.parser.mac.mac_ext import MacCapitalFlowCmd
from tdxpy.parser.std.get_finance_info import GetFinanceInfo

FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 依赖仅占位——单测直接调 parseResponse，不碰 socket。"""


# ---------------------------------------------------------------------------
# ② _parse_symbol 数字市场段
# ---------------------------------------------------------------------------

class TestParseSymbolNumericMarket(unittest.TestCase):
    """数字市场段 = ExMarket id 直用（MEDIUM②；宏观 38 symbol 形态前置）。"""

    def test_macro_38(self):
        self.assertEqual(_parse_symbol("38.1_GDP"), ("ex", 38, "1_GDP"))
        self.assertEqual(_parse_symbol("38.2_CPI"), ("ex", 38, "2_CPI"))

    def test_registered_ex_id(self):
        # 31 = HK_MAIN 已登记 id，数字段直用
        self.assertEqual(_parse_symbol("31.01398"), ("ex", 31, "01398"))

    def test_unregistered_id_raises(self):
        with self.assertRaises(ValueError):
            _parse_symbol("99.1_ABC")  # 99 未登记
        with self.assertRaises(ValueError):
            _parse_symbol("37.x")  # 37 未登记

    def test_id_0_1_2_conflict_raises(self):
        """复审 F4 回归锁：数字段 0/1/2 与 std 市场号语义冲突（id=1=TEMP 误路由），一律 raise。"""
        for seg in ("0", "1", "2"):
            with self.assertRaises(ValueError):
                _parse_symbol(f"{seg}.600000")

    def test_legacy_paths_unchanged(self):
        self.assertEqual(_parse_symbol("sh.600000"), ("std", 1, "600000"))
        self.assertEqual(_parse_symbol("hk.01398")[0], "ex")


# ---------------------------------------------------------------------------
# ① 0x1218 资金流
# ---------------------------------------------------------------------------

class TestMacCapitalFlowCmd(unittest.TestCase):
    """个股资金流（0x1218 + Stock_ZJLX；2026-11-05 四股票在线实测定性）。"""

    def test_frame_layout(self):
        cmd = MacCapitalFlowCmd(_FakeClient())
        cmd.setParams(1, "600519")
        body = cmd.send_pkg[MAC_FRAME_HEADER_SIZE + 2:]
        self.assertEqual(len(body), 47)
        market, = struct.unpack_from("<H", body, 0)
        code = body[2:10].rstrip(b"\x00")
        report = body[26:47].rstrip(b"\x00")
        self.assertEqual((market, code, report), (1, b"600519", b"Stock_ZJLX"))

    def test_parse_fixture(self):
        cmd = MacCapitalFlowCmd(_FakeClient())
        cmd.setParams(0, "000001")
        rows = cmd.parseResponse((FIXTURES / "mac_ext_capital_flow.bin").read_bytes())
        self.assertEqual(len(rows), 1)
        r = rows[0]
        # 行1 = 当日四值（与 2026-11-05 调查实测值一致）
        self.assertEqual(r["main_in"], 571687424.0)
        self.assertEqual(r["main_out"], 667261440.0)
        self.assertEqual(r["retail_in"], 1117462784.0)
        self.assertEqual(r["retail_out"], 1021854976.0)
        # net 派生 = in - out
        self.assertEqual(r["main_net"], round(571687424.0 - 667261440.0, 2))
        self.assertEqual(r["retail_net"], round(1117462784.0 - 1021854976.0, 2))
        # 行2 = 5 日六值（flow5d_0..5，保守命名）
        self.assertEqual(r["flow5d_0"], 5751492096.0)
        self.assertEqual(r["flow5d_5"], 380867680.0)
        self.assertNotIn("flow5d_6", r)

    def test_synthetic_body(self):
        """合成响应：net 派生 / flow5d 补齐 / 浮点化。"""
        today = [100.0, 40.0, 60.0, 30.0]
        five = [500.0, 200.0, -10.0, 20.0, -30.0, 40.0]
        payload = f"[{today},{five}]".replace("'", "").encode("ascii")
        body = bytearray(27) + payload
        struct.pack_into("<H", body, 23, len(payload))
        cmd = MacCapitalFlowCmd(_FakeClient())
        rows = cmd.parseResponse(bytes(body))
        r = rows[0]
        self.assertEqual(r["main_net"], 60.0)
        self.assertEqual(r["retail_net"], 30.0)
        self.assertEqual([r[f"flow5d_{i}"] for i in range(6)], [500.0, 200.0, -10.0, 20.0, -30.0, 40.0])

    def test_defense(self):
        cmd = MacCapitalFlowCmd(_FakeClient())
        # 过短帧
        self.assertEqual(cmd.parseResponse(b"\x00" * 20), [])
        # 坏 JSON
        bad = bytearray(27) + b"not-json"
        struct.pack_into("<H", bad, 23, 8)
        self.assertEqual(cmd.parseResponse(bytes(bad)), [])
        # JSON 只有一行（期望两行）
        one = bytearray(27) + b"[1,2,3,4]"
        struct.pack_into("<H", one, 23, 9)
        self.assertEqual(cmd.parseResponse(bytes(one)), [])
        # 行1 不足 4 值
        short = bytearray(27) + b"[[1,2],[3,4,5,6,7,8]]"
        struct.pack_into("<H", short, 23, len(short) - 27)
        self.assertEqual(cmd.parseResponse(bytes(short)), [])

    def test_row2_short_falls_back(self):
        """复审 F1 回归锁：行2 不足 6 值 → 告警并按实际输出（不炸、不丢行1）。"""
        cmd = MacCapitalFlowCmd(_FakeClient())
        payload = b"[[1,2,3,4],[9,8,7]]"  # 行2 仅 3 值
        body = bytearray(27) + payload
        struct.pack_into("<H", body, 23, len(payload))
        rows = cmd.parseResponse(bytes(body))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["main_net"], -1.0)   # 行1 正常解析
        self.assertEqual(rows[0]["flow5d_2"], 7.0)    # 行2 按实际输出
        self.assertNotIn("flow5d_3", rows[0])


# ---------------------------------------------------------------------------
# ③ 财务空壳判定
# ---------------------------------------------------------------------------

def _make_finance_body(code_bytes, ipo_date):
    """构造 0x0C1F 财务响应 body：2B num + 7B (market, code) + 数据段。"""
    data_fmt = "<fHHIIffffffffffffffffffffffffffffff"
    data = struct.pack(data_fmt, 1.0, 1, 1, 20261024, ipo_date, *([2.0] * 30))
    return struct.pack("<HB6s", 1, 0, code_bytes) + data


class TestFinanceShell(unittest.TestCase):
    """is_shell 空壳判定（判据 = code 全 \\x00 或 ipo_date<=0；在线双样本实锤）。"""

    def test_parser_shell_row(self):
        cmd = GetFinanceInfo(_FakeClient())
        row = cmd.parseResponse(_make_finance_body(b"\x00" * 6, 0))
        self.assertTrue(row["is_shell"])

    def test_parser_normal_row(self):
        cmd = GetFinanceInfo(_FakeClient())
        row = cmd.parseResponse(_make_finance_body(b"000001", 19910403))
        self.assertFalse(row["is_shell"])

    def test_facade_drop_shell_dict_normal(self):
        """真实形态：0x0C1F 单票查询返回单 dict——正常票保留。"""
        tc = TdxClient.__new__(TdxClient)
        with mock.patch.object(TdxClient, "_hq_call",
                               return_value={"code": "000001", "ipo_date": 19910403, "is_shell": False}):
            df = tc.finance("sz.000001", drop_shell=True)
        self.assertEqual(len(df), 1)

    def test_facade_drop_shell_dict_shell(self):
        """真实形态单 dict：空壳被过滤 → 空 DataFrame。"""
        tc = TdxClient.__new__(TdxClient)
        with mock.patch.object(TdxClient, "_hq_call",
                               return_value={"code": "\x00\x00\x00\x00\x00\x00", "ipo_date": 0, "is_shell": True}):
            df = tc.finance("sz.000003", drop_shell=True)
        self.assertEqual(len(df), 0)

    def test_facade_drop_shell_list_form(self):
        """list 形态（防御兼容）：混合行只留正常行。"""
        tc = TdxClient.__new__(TdxClient)
        shell = {"code": "\x00\x00\x00\x00\x00\x00", "ipo_date": 0, "is_shell": True}
        normal = {"code": "000001", "ipo_date": 19910403, "is_shell": False}
        with mock.patch.object(TdxClient, "_hq_call", return_value=[shell, normal]):
            df = tc.finance("sz.000001", drop_shell=True)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["code"], "000001")

    def test_facade_keep_shell_default(self):
        """默认不过滤（向后兼容）：空壳行原样保留。"""
        tc = TdxClient.__new__(TdxClient)
        with mock.patch.object(TdxClient, "_hq_call",
                               return_value={"code": "\x00\x00\x00\x00\x00\x00", "ipo_date": 0, "is_shell": True}):
            df = tc.finance("sz.000003")
        self.assertEqual(len(df), 1)
        self.assertTrue(df.iloc[0]["is_shell"])


# ---------------------------------------------------------------------------
# ④ finance_all 并发门面
class TestFinanceAll(unittest.TestCase):
    """并发财务：分片 / 失败隔离 / 空输入（patch 每线程 TdxClient）。"""

    def _patch_tdxclient(self, finance_map, fail_set=()):
        """patch TdxClient 三件套（__init__/finance/close）。

        ⚠️ 不要 patch ``__new__``：mock 恢复 ``object.__new__`` 后 slot 状态
        残留，会偶发炸掉后续在线测试的 TdxClient 构造（object.__new__
        TypeError，2026-11-09 全量回归实测）。__init__/finance/close 是普通
        方法属性，patch 恢复安全。"""
        finance_map = {s: dict(v) for s, v in finance_map.items()}

        for row in finance_map.values():
            row.setdefault("is_shell", False)

        import pandas as pd

        def fake_init(self, *a, **k):
            pass

        def fake_finance(self, sym, drop_shell=False):
            if sym in fail_set:
                raise ConnectionError(f"boom {sym}")
            return pd.DataFrame([dict(finance_map[sym])])

        def fake_close(self):
            pass

        return mock.patch.multiple(
            TdxClient, __init__=fake_init, finance=fake_finance, close=fake_close
        )

    def test_all_success(self):
        tc = TdxClient.__new__(TdxClient)
        fmap = {s: {"code": s.split(".")[1]} for s in
                ["sh.600000", "sz.000001", "sh.600519", "sz.300750"]}
        with self._patch_tdxclient(fmap):
            df = tc.finance_all(list(fmap), workers=2)
        self.assertEqual(len(df), 4)
        self.assertNotIn("error", df.columns)
        # 复审 R2-#1 回归锁：成功行也回填 symbol 列（对号需求不只失败行）
        self.assertTrue(df["symbol"].notna().all())
        self.assertEqual(set(df["symbol"]), set(fmap))

    def test_failure_isolation(self):
        tc = TdxClient.__new__(TdxClient)
        syms = ["sh.600000", "sz.000001", "sh.600519"]
        fmap = {s: {"code": s.split(".")[1]} for s in syms}
        with self._patch_tdxclient(fmap, fail_set={"sz.000001"}):
            df = tc.finance_all(syms, workers=2)
        self.assertEqual(len(df), 3)  # 成功 2 + 失败回填 1
        err_rows = df[df["error"].notna()] if "error" in df.columns else df.iloc[:0]
        self.assertEqual(len(err_rows), 1)
        self.assertIn("boom", str(err_rows.iloc[0]["error"]))
        self.assertEqual(err_rows.iloc[0]["symbol"], "sz.000001")

    def test_empty_input(self):
        tc = TdxClient.__new__(TdxClient)
        df = tc.finance_all([])
        self.assertTrue(df.empty)


# ---------------------------------------------------------------------------
# ⑤ 批量报价校验
# ---------------------------------------------------------------------------

class TestValidateBatchQuotes(unittest.TestCase):
    """0x122B 页校验：缺失/外来/全零占位/on_missing 三态。"""

    REQ = [(0, "000001"), (1, "600000"), (0, "300750")]

    @staticmethod
    def _page(rows):
        import pandas as pd

        return pd.DataFrame(rows)

    def test_all_matched_noop(self):
        page = self._page([
            {"market": 0, "code": "000001", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
            {"market": 1, "code": "600000", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
            {"market": 0, "code": "300750", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
        ])
        _validate_batch_quotes(self.REQ, page, on_missing="raise")  # 不抛即通过

    def test_missing_warn_not_raise(self):
        # EX 语义：6 请求回 5 属正常——缺 1 只 warn 不炸
        page = self._page([
            {"market": 0, "code": "000001", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
            {"market": 1, "code": "600000", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
        ])
        _validate_batch_quotes(self.REQ, page, on_missing="warn")  # 不抛即通过
        with self.assertRaises(ValueError):
            _validate_batch_quotes(self.REQ, page, on_missing="raise")

    def test_over_return_raises_unconditionally(self):
        # 行数 > 请求数：外来记录/服务器异常——即使 ignore 也 raise（防混入）
        page = self._page([
            {"market": 0, "code": "000001", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
            {"market": 1, "code": "600000", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
            {"market": 0, "code": "300750", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
            {"market": 1, "code": "999999", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
        ])
        with self.assertRaises(ValueError):
            _validate_batch_quotes(self.REQ, page, on_missing="ignore")

    def test_zero_placeholder_detected(self):
        # 全零占位行（close/pre_close/vol/amount 全 0）计入缺失口径
        page = self._page([
            {"market": 0, "code": "000001", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
            {"market": 1, "code": "600000", "close": 0.0, "pre_close": 0.0, "vol": 0, "amount": 0},
            {"market": 0, "code": "300750", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
        ])
        with self.assertRaises(ValueError):
            _validate_batch_quotes(self.REQ, page, on_missing="raise")

    def test_premarket_row_not_placeholder(self):
        # 盘前形态：close=0 但 pre_close>0 ——不算占位（防误报）
        page = self._page([
            {"market": 0, "code": "000001", "close": 0.0, "pre_close": 10.5, "vol": 0, "amount": 0},
            {"market": 1, "code": "600000", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
            {"market": 0, "code": "300750", "close": 10.0, "pre_close": 10.0, "vol": 1, "amount": 1},
        ])
        _validate_batch_quotes(self.REQ, page, on_missing="raise")  # 不抛即通过

    def test_ignore_skips_all(self):
        _validate_batch_quotes(self.REQ, None, on_missing="ignore")  # 空页 ignore 不炸

    def test_invalid_on_missing_raises(self):
        """复审 R2-#7 回归锁：on_missing 非法值显式 raise（不静默落 warn）。"""
        with self.assertRaises(ValueError):
            _validate_batch_quotes(self.REQ, None, on_missing="bogus")


# ---------------------------------------------------------------------------
# ⑥ audit_price_scale
# ---------------------------------------------------------------------------

class TestAuditPriceScale(unittest.TestCase):
    """系数表 × decimal_point 交叉审计：零分歧 / 分歧检出 / 未登记跳过。"""

    def _client_with_list(self, market_rows):
        """patch _hq_call：get_security_list 返回表数据，第二次调用返回 [] 终止翻页。"""
        tc = TdxClient.__new__(TdxClient)
        calls = {"n": 0}

        def _hq(method, *args, **kwargs):
            self.assertEqual(method, "get_security_list")
            rows = market_rows if calls["n"] == 0 else []
            calls["n"] += 1
            return rows

        return tc, _hq

    def test_zero_divergence(self):
        # 600000 ∈ SH_A_STOCK（coef 0.01）；dp=2 → expect 0.01 零分歧
        tc, _hq = self._client_with_list([{"code": "600000", "name": "浦发银行", "decimal_point": 2}])
        with mock.patch.object(TdxClient, "_hq_call", side_effect=_hq):
            df = tc.audit_price_scale(markets=(1,))
        self.assertTrue(df.empty)

    def test_divergence_detected(self):
        # dp=3 → expect 0.001 ≠ 0.01 → 分歧
        tc, _hq = self._client_with_list([{"code": "600000", "name": "浦发银行", "decimal_point": 3}])
        with mock.patch.object(TdxClient, "_hq_call", side_effect=_hq):
            df = tc.audit_price_scale(markets=(1,))
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]["code"], "600000")
        self.assertEqual(int(df.iloc[0]["decimal_point"]), 3)
        self.assertAlmostEqual(float(df.iloc[0]["expect"]), 0.001)

    def test_unregistered_skipped(self):
        # 未登记段（系数表盲区）跳过不算分歧
        tc, _hq = self._client_with_list([{"code": "999999", "name": "未登记", "decimal_point": 1}])
        with mock.patch.object(TdxClient, "_hq_call", side_effect=_hq):
            df = tc.audit_price_scale(markets=(0,))
        self.assertTrue(df.empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
