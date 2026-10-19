# -*- coding: utf-8 -*-
"""finance 金额单位 ×1000 修正单测（2026-10-19 T0.1 门禁裁决落地）。

背景：pytdx 系 20 个金额字段沿用 ×10000（万元假设）实为 ×1000（千元）——
恒等式「股本raw(万股)×每股净资产 ÷ 净资产raw = 10.0」（600519/000002 双股交叉一致）
+ 披露量级对照（茅台 3090 亿 / 平安 6.03 万亿 / 万科 9598 亿）双路裁决。

本套件以**在线披露量级区间**为断言锚点（联网自动 skip）：
量级比精确值稳健（报告期滚动不会把量级推下台阶）。
运行：python tests/test_finance_unit.py -v
"""
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

#: (symbol, 总资产区间(元), 净利润区间(元, 可为负；报告期为最新已披露季报累计), 总股本(股, ±5%))
SAMPLES = [
    ("sh.600519", "贵州茅台", (2.0e11, 5.0e11), (3.0e10, 1.2e11), 1.256e9),
    ("sz.000001", "平安银行", (4.5e12, 8.0e12), (1.5e10, 5.0e11), 1.9406e10),
    ("sz.000002", "万科A", (7.0e11, 1.4e12), (-3.0e11, 5.0e11), 1.1931e10),
]


class TestFinanceFixtureOffline(unittest.TestCase):
    """离线夹具回归（×1000 修正的防线，CI 无网可跑）。

    fixture = tests/fixtures/finance_sh600519.bin：2026-10-19 真实响应存档（茅台）。
    """

    @classmethod
    def setUpClass(cls):
        from tdxpy.parser.std.get_finance_info import GetFinanceInfo

        class _FakeClient:
            pass

        body = (FIXTURES / "finance_sh600519.bin").read_bytes()
        cls.row = GetFinanceInfo(_FakeClient()).parseResponse(body)

    def test_amount_fields_x1000(self):
        """20 个金额字段 = 千元口径（锚定 ×1000 后的具体值，防回退 ×10000）。"""
        expect = {
            "zongzichan": 309050784000.0,
            "liudongzichan": 260724656000.0,
            "gudingzichan": 22220890000.0,
            "wuxingzichan": 8578744000.0,
            "liudongfuzhai": 46645076000.0,
            "changqifuzhai": 10842758000.0,
            "jingzichan": 251253600000.0,
            "zhuyingshouru": 90703264000.0,
            "zhuyinglirun": 9473762000.0,
            "yingyelirun": 61411288000.0,
            "jingyingxianjinliu": 70690752000.0,
            "zongxianjinliu": 58387000000.0,
            "cunhuo": 61317208000.0,
            "lirunzonghe": 61438420000.0,
            "shuihoulirun": 46033328000.0,
            "jinglirun": 44516880000.0,
            "weifenpeilirun": 199683216000.0,
        }
        for k, v in expect.items():
            self.assertAlmostEqual(self.row[k], v, delta=abs(v) * 1e-6, msg=f"{k} 应为 ×1000 口径")

    def test_share_fields_x10000(self):
        """股本字段回归防线：仍为万股口径（×10000 → 股）。"""
        self.assertAlmostEqual(self.row["zongguben"], 1250081562.5, delta=1.0)
        self.assertAlmostEqual(self.row["liutongguben"], 1250081562.5, delta=1.0)

    def test_identity_offline(self):
        """恒等式：负债 + 权益 ≈ 总资产（±0.5%，茅台工商业布局）。"""
        zz = self.row["zongzichan"]
        liab_eq = self.row["liudongfuzhai"] + self.row["changqifuzhai"] + self.row["jingzichan"]
        self.assertLess(abs(liab_eq - zz) / zz, 0.005)


class TestFinanceUnitOnline(unittest.TestCase):
    """在线量级断言（×1000 口径 vs 披露值；联网自动 skip）。"""

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

    def test_amount_fields_magnitude(self):
        """总资产/净利润落披露量级区间（×1000 口径）。"""
        for sym, name, zz_rng, lr_rng, _ in SAMPLES:
            f = self.client.finance(sym)
            zz = float(f["zongzichan"].iloc[0])
            lr = float(f["jinglirun"].iloc[0])
            self.assertTrue(
                zz_rng[0] <= zz <= zz_rng[1],
                f"{name} 总资产 {zz:.3e} 未落入披露区间 {zz_rng}（×1000 口径失效？）",
            )
            self.assertTrue(
                lr_rng[0] <= lr <= lr_rng[1],
                f"{name} 净利润 {lr:.3e} 未落入披露区间 {lr_rng}",
            )

    def test_identity_assets_equals_liab_plus_equity(self):
        """财务恒等式：负债 + 权益 ≈ 总资产（茅台工商业布局，±25%）。"""
        f = self.client.finance("sh.600519")
        r = f.iloc[0]
        zz = float(r["zongzichan"])
        liab = float(r["liudongfuzhai"]) + float(r["changqifuzhai"])
        equity = float(r["jingzichan"])
        self.assertLess(abs(liab + equity - zz) / zz, 0.25)

    def test_share_fields_unchanged(self):
        """股本字段回归防线：×10000（万股→股）不受金额修正影响。"""
        for sym, name, _, _, shares in SAMPLES:
            f = self.client.finance(sym)
            zg = float(f["zongguben"].iloc[0])
            self.assertAlmostEqual(
                zg / shares, 1.0, delta=0.05,
                msg=f"{name} 总股本 {zg:.3e} 偏离披露 {shares:.3e} 超 5%",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
