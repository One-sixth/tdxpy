# -*- coding: utf-8 -*-
"""离线单元测试：系数段表 + 点分式 symbol 解析（2026-09-17 点分式统一改造批次）。

不联网，随时可跑：
    python tests/test_routing_and_coefficient.py -v
    或 pytest tests/test_routing_and_coefficient.py -v
"""
import unittest

from tdxpy.client import (
    HK_EX_MARKET_GROWTH,
    HK_EX_MARKET_INDEX,
    HK_EX_MARKET_MAIN,
    _is_index_code,
    _parse_symbol,
    _resolve_hk,
)
from tdxpy.helper import get_security_coefficient, get_security_type
from tdxpy.markets import ExMarket, StdMarket, ex_market_table, resolve_ex_market


class TestSecurityType(unittest.TestCase):
    """get_security_type 段表（2026-09-17 补段：沪 52/53/55/56/58、深 18、北交所）。"""

    def test_stock(self):
        self.assertEqual(get_security_type(1, "600519"), "SH_A_STOCK")
        self.assertEqual(get_security_type(0, "000001"), "SZ_A_STOCK")
        self.assertEqual(get_security_type(2, "920992"), "BJ_STOCK")
        self.assertEqual(get_security_type(2, "830799"), "BJ_STOCK")

    def test_fund_fixed_segments(self):
        # 沪市基金全段（原 bug：58 段科创ETF 不在表 → 回退 0.01 → ×10）
        for code in ("510300", "510050", "588000", "563360", "522000", "530000", "550000"):
            self.assertEqual(get_security_type(1, code), "SH_FUND", code)
        # 深市基金（原 bug：18 段 REITs 不在表）
        self.assertEqual(get_security_type(0, "159915"), "SZ_FUND")
        self.assertEqual(get_security_type(0, "180101"), "SZ_FUND")

    def test_bond_and_index(self):
        self.assertEqual(get_security_type(1, "110090"), "SH_BOND")
        self.assertEqual(get_security_type(0, "127089"), "SZ_BOND")
        self.assertEqual(get_security_type(1, "000001"), "SH_INDEX")
        self.assertEqual(get_security_type(0, "399001"), "SZ_INDEX")
        self.assertEqual(get_security_type(2, "899050"), "BJ_INDEX")

    def test_unknown_raises(self):
        with self.assertRaises(NotImplementedError):
            get_security_type(1, "777777")


class TestCoefficient(unittest.TestCase):
    """get_security_coefficient：值正确 + 未知段 raise（不静默回退 0.01）。"""

    def test_values(self):
        self.assertEqual(get_security_coefficient(1, "600519"), 0.01)
        self.assertEqual(get_security_coefficient(1, "588000"), 0.001)  # 原 bug ×10
        self.assertEqual(get_security_coefficient(0, "180101"), 0.001)  # 原 bug ×10
        self.assertEqual(get_security_coefficient(1, "110090"), 0.0001)
        self.assertEqual(get_security_coefficient(0, "127089"), 0.0001)
        self.assertEqual(get_security_coefficient(2, "920992"), 0.01)

    def test_unknown_raises_not_fallback(self):
        with self.assertRaises(NotImplementedError):
            get_security_coefficient(1, "777777")


class TestParseSymbol(unittest.TestCase):
    """_parse_symbol 点分式解析（2026-09-17 symbol 统一改造）。"""

    def test_std_markets(self):
        self.assertEqual(_parse_symbol("sh.601919"), ("std", 1, "601919"))
        self.assertEqual(_parse_symbol("SZ.000001"), ("std", 0, "000001"))
        self.assertEqual(_parse_symbol("bj.920992"), ("std", 2, "920992"))

    def test_std_chinese_alias(self):
        self.assertEqual(_parse_symbol("上海.601919"), ("std", 1, "601919"))
        self.assertEqual(_parse_symbol("深圳.000001"), ("std", 0, "000001"))
        self.assertEqual(_parse_symbol("北交所.920992"), ("std", 2, "920992"))

    def test_ex_markets(self):
        self.assertEqual(_parse_symbol("us.AAPL"), ("ex", 74, "AAPL"))
        self.assertEqual(_parse_symbol("美股.AAPL"), ("ex", 74, "AAPL"))
        self.assertEqual(_parse_symbol("main.AL8"), ("ex", 60, "AL8"))
        self.assertEqual(_parse_symbol("上期所.cu2510"), ("ex", 30, "cu2510"))

    def test_hk_routing(self):
        # hk 市场段先细分：5 位数字=主板 31、"08" 开头=创业板 48、非数字=香港指数 27
        self.assertEqual(_parse_symbol("hk.01398"), ("ex", 31, "01398"))
        self.assertEqual(_parse_symbol("hk.08001"), ("ex", 48, "08001"))
        self.assertEqual(_parse_symbol("hk.HSI"), ("ex", 27, "HSI"))

    def test_case_insensitive(self):
        # 市场段大小写不敏感；代码段原样传递（不改大小写）
        self.assertEqual(_parse_symbol("SH.601919"), ("std", 1, "601919"))
        self.assertEqual(_parse_symbol("Us.AAPL"), ("ex", 74, "AAPL"))
        self.assertEqual(_parse_symbol("us.aapl"), ("ex", 74, "aapl"))

    def test_strip(self):
        self.assertEqual(_parse_symbol("  sh.601919  "), ("std", 1, "601919"))
        self.assertEqual(_parse_symbol("sh. 601919 "), ("std", 1, "601919"))
        self.assertEqual(_parse_symbol(" us . AAPL "), ("ex", 74, "AAPL"))

    def test_code_with_dot(self):
        # 只按第一个点拆分：代码段允许再含点（解析层不校验服务器有无此代码）
        self.assertEqual(_parse_symbol("sh.600000.01"), ("std", 1, "600000.01"))

    def test_hk_index_code_uppered(self):
        # 例外：hk 指数代码段 upper（香港指数代码服务器侧大写）
        self.assertEqual(_parse_symbol("hk.hscei"), ("ex", 27, "HSCEI"))

    def test_bare_code_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_symbol("601919")
        msg = str(ctx.exception)
        self.assertIn("裸代码", msg)
        self.assertIn("sh.601919", msg)  # 教学式提示

    def test_old_prefix_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_symbol("sh601919")
        msg = str(ctx.exception)
        self.assertIn("旧式无点前缀", msg)
        self.assertIn("sh.601919", msg)  # 消息中给出点分式改写提示

    def test_unknown_market_raises(self):
        with self.assertRaises(ValueError) as ctx:
            _parse_symbol("xx.123")
        msg = str(ctx.exception)
        self.assertIn("无法识别的市场", msg)
        self.assertIn("ex_market_table", msg)

    def test_empty_code_raises(self):
        with self.assertRaises(ValueError):
            _parse_symbol("sh.")

    def test_non_str_raises(self):
        with self.assertRaises(TypeError):
            _parse_symbol(601919)


class TestResolveHkDetail(unittest.TestCase):
    """_resolve_hk 港股代码段细分规则（点分市场段 "hk" 专用）。"""

    def test_main_board(self):
        self.assertEqual(_resolve_hk("01398"), (HK_EX_MARKET_MAIN, "01398"))
        self.assertEqual(_resolve_hk("00700"), (HK_EX_MARKET_MAIN, "00700"))

    def test_growth_board(self):
        self.assertEqual(_resolve_hk("08001"), (HK_EX_MARKET_GROWTH, "08001"))

    def test_index(self):
        self.assertEqual(_resolve_hk("HSI"), (HK_EX_MARKET_INDEX, "HSI"))
        self.assertEqual(_resolve_hk("hscei"), (HK_EX_MARKET_INDEX, "HSCEI"))

    def test_bad_code(self):
        with self.assertRaises(ValueError):
            _resolve_hk("1234")  # 非 5 位数字
        with self.assertRaises(ValueError):
            _resolve_hk("")


class TestIndexCodeRouting(unittest.TestCase):
    """_is_index_code 指数段判断（同码不同市场语义不同）。"""

    def test_index_segments(self):
        self.assertTrue(_is_index_code(1, "000001"))  # 上证指数
        self.assertTrue(_is_index_code(1, "880011"))
        self.assertTrue(_is_index_code(1, "999999"))
        self.assertTrue(_is_index_code(0, "399001"))
        self.assertTrue(_is_index_code(2, "899050"))

    def test_stock_segments(self):
        self.assertFalse(_is_index_code(0, "000001"))  # 深市 000001=平安银行
        self.assertFalse(_is_index_code(1, "600519"))
        self.assertFalse(_is_index_code(0, "300536"))
        self.assertFalse(_is_index_code(1, "510300"))  # 基金不是指数
        self.assertFalse(_is_index_code(9, "000001"))  # 未知市场无前缀表 → False


class TestExMarketEnum(unittest.TestCase):
    """ExMarket 枚举与字符串别名（2026-09-17 market 三态支持）。"""

    def test_enum_members(self):
        self.assertEqual(int(ExMarket.HK_MAIN), 31)
        self.assertEqual(int(ExMarket.US_STOCK), 74)
        self.assertEqual(int(ExMarket.HK_INDEX), 27)
        self.assertEqual(len(ExMarket), 51)  # 实测市场数
        self.assertEqual(ExMarket("us"), ExMarket.US_STOCK)  # 枚举直接吃字符串

    def test_alias_resolution(self):
        cases = {
            "us": 74, "US": 74, " usa ": 74, "美股": 74,
            "hk": 31, "港股": 31, "香港主板": 31, "kh": 31, "hk_main": 31,
            "hk_index": 27, "恒生": 27, "fh": 27,
            "上期所": 30, "shfe": 30, "qs": 30,
            "主力": 60, "main": 60, "ma": 60,
            "us_stock": 74,  # 英文成员名小写兜底
        }
        for alias, expected in cases.items():
            self.assertEqual(resolve_ex_market(alias), expected, alias)

    def test_int_passthrough(self):
        self.assertEqual(resolve_ex_market(74), 74)
        self.assertEqual(resolve_ex_market(ExMarket.US_STOCK), 74)
        self.assertEqual(resolve_ex_market(999), 999)  # int 直通（新市场 id 兼容）

    def test_invalid_raises_with_hint(self):
        with self.assertRaises(ValueError) as ctx:
            resolve_ex_market("mars")
        msg = str(ctx.exception)
        self.assertIn("ex_market_table", msg)
        with self.assertRaises(ValueError):
            resolve_ex_market(1.5)
        with self.assertRaises(ValueError):
            resolve_ex_market(True)  # bool 防呆
        with self.assertRaises(ValueError):
            ExMarket("mars")

    def test_std_market(self):
        self.assertEqual(int(StdMarket("sh")), 1)
        self.assertEqual(int(StdMarket("SZ")), 0)
        self.assertEqual(int(StdMarket("北交所")), 2)
        self.assertEqual(int(StdMarket.SH), 1)

    def test_table(self):
        df = ex_market_table()
        self.assertGreaterEqual(len(df), 51)
        for col in ("market", "category", "name", "short_name", "alias", "enum_member"):
            self.assertIn(col, df.columns)
        us = df[df["market"] == 74].iloc[0]
        self.assertEqual(us["enum_member"], "US_STOCK")
        self.assertIn("us", us["alias"].split(", "))


    def test_hk_generic_alias_falls_to_index(self):
        """泛别名（港股/香港/hk_growth）遇非数字代码 → 香港指数（父 Agent 复审补）。"""
        self.assertEqual(_parse_symbol("港股.HSI"), ("ex", 27, "HSI"))
        self.assertEqual(_parse_symbol("香港.hscei"), ("ex", 27, "HSCEI"))
        self.assertEqual(_parse_symbol("hk_growth.08001"), ("ex", 48, "08001"))  # 数字代码保留创业板


if __name__ == "__main__":
    unittest.main(verbosity=2)
