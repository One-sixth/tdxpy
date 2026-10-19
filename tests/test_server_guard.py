# -*- coding: utf-8 -*-
"""服务器守护测试（guard）：探测通达信服务端毛病/格式变更，及早报警。

与 smoke 的本质区别：联网失败 → skip 但打显眼 ⚠ 横幅（服务端不可达必须可见）；
数据行为断言失败 → FAIL。锚点基线 = 2026-09-27/28 实测（MEMORY.md「守护测试」节）。

在线锚点：BJ 收盘价/分笔数/标的数/挡板消息/预留段 EMPTY（走底层 TdxHq_API，协议层最直接）。
离线锚点：vipdoc bj 段 reader（不联网；本机无 vipdoc 时 skip）。

运行：
    python tests/test_server_guard.py -v
"""
import os
import sys
import unittest
from collections import Counter
from pathlib import Path

from tdxpy.hq import TdxHq_API
from tdxpy.reader.daily_bar_reader import TdxDailyBarReader

HQ_HOST = os.environ.get("TDXPY_TEST_HQ_HOST", "111.229.247.189")
HQ_PORT = 7709
CONNECT_TIMEOUT = 8
VIPDOC_BJ_LDAY = Path(os.environ.get(
    "TDXPY_TEST_VIPDOC_BJ_LDAY",
    r"D:\Software\证券\通达信金融终端-量化模拟\vipdoc\bj\lday",
))

ANCHOR_DATE = "2026-10-15"
ANCHOR_TICK_DATE = 20260923
BJ_STOCK_920992_CLOSE = 12.46
BJ_INDEX_899050_CLOSE = 967.51
BJ_STOCK_920992_TICKS = 1990
BJ_INDEX_899050_TICKS = 2842
BJ_SECURITY_COUNT_MIN = 385
BJ_LIST_BANNER = "北交所（market=2）列表接口被服务器断连"
BJ_SEG_MIN = {"81": 2, "82": 10, "89": 2, "92": 349}
BJ_PREFERRED_821001_DATE = "2026-04-27"
# 2026-10-19 系数坑已修复（BJ_PREFERRED 0.0001）：离线/在线分时均应为在线日线口径 100.744
BJ_PREFERRED_821001_CLOSE = 100.744


def _connect_or_skip(api):
    try:
        ok = api.connect(HQ_HOST, HQ_PORT, time_out=CONNECT_TIMEOUT)
    except Exception as e:  # noqa
        api.disconnect()
        print(f"\n⚠️⚠️⚠️ guard 无法连接 {HQ_HOST}:{HQ_PORT}——服务端不可达或网络异常: {e}", file=sys.stderr)
        raise unittest.SkipTest(f"无法连接 {HQ_HOST}:{HQ_PORT}: {e}")
    if not ok:
        api.disconnect()
        print(f"\n⚠️⚠️⚠️ guard 无法连接 {HQ_HOST}:{HQ_PORT}——服务端不可达或网络异常", file=sys.stderr)
        raise unittest.SkipTest(f"无法连接 {HQ_HOST}:{HQ_PORT}")
    return api


def _close_on_date(api, fetch, date):
    """在返回的 bar 序列里找 date 当日 close；找不到返回 None。"""
    for b in fetch() or []:
        if str(b["datetime"]).startswith(date):
            return float(b["close"])
    return None


def _tick_total(api, code, date, market=2, page=800, max_pages=10):
    """历史分笔全天笔数：start=0 取最新段、递增往回翻，页长累加。"""
    total, start = 0, 0
    for _ in range(max_pages):
        rows = api.get_history_transaction_data(market, code, start, page, date) or []
        if not rows:
            break
        total += len(rows)
        start += page
        if len(rows) < page:
            break
    return total


class TestBjGuard(unittest.TestCase):
    """北交所在线锚点守护：数据面消失/价格漂移/分页行为变化都会 FAIL。"""

    @classmethod
    def setUpClass(cls):
        cls.api = _connect_or_skip(TdxHq_API())

    @classmethod
    def tearDownClass(cls):
        cls.api.disconnect()

    def test_bj_stock_920992_close(self):
        c = _close_on_date(self.api, lambda: self.api.get_security_bars(4, 2, "920992", 0, 10), ANCHOR_DATE)
        self.assertIsNotNone(c, f"{ANCHOR_DATE} 的 920992 bar 未返回——BJ 数据面消失？")
        self.assertAlmostEqual(c, BJ_STOCK_920992_CLOSE, places=2, msg="920992 close 漂移——服务端格式/数据变更？")

    def test_bj_index_899050_close(self):
        c = _close_on_date(self.api, lambda: self.api.get_index_bars(4, 2, "899050", 0, 10), ANCHOR_DATE)
        self.assertIsNotNone(c, f"{ANCHOR_DATE} 的 899050 bar 未返回——BJ 指数数据面消失？")
        self.assertAlmostEqual(c, BJ_INDEX_899050_CLOSE, places=2, msg="899050 close 漂移——服务端格式/数据变更？")

    def test_bj_security_count(self):
        n = self.api.get_security_count(2)
        self.assertIsNotNone(n, "get_security_count(2) 返回 None")
        self.assertGreaterEqual(n, BJ_SECURITY_COUNT_MIN, "BJ 标的数跌破基线——服务器/数据面缩水？")

    def test_bj_list_bulwark(self):
        with self.assertWarns(UserWarning) as cm:
            result = self.api.get_security_list(2, 0)
        self.assertIsNone(result, "list(2,0) 应被挡板拦截返回 None——若返回数据说明服务器行为变了，需更新认知")
        self.assertIn(BJ_LIST_BANNER, str(cm.warning), "挡板警告消息变化——是否挡板被移除/消息文案被改？")

    def test_bj_stock_920992_ticks(self):
        total = _tick_total(self.api, "920992", ANCHOR_TICK_DATE)
        self.assertEqual(total, BJ_STOCK_920992_TICKS, "920992 全天分笔数漂移——数据面/分页行为变化？")

    def test_bj_index_899050_ticks(self):
        total = _tick_total(self.api, "899050", ANCHOR_TICK_DATE)
        self.assertEqual(total, BJ_INDEX_899050_TICKS, "899050 全天分笔数漂移——数据面/分页行为变化？")

    def test_bj_finance_info(self):
        r = self.api.get_finance_info(2, "920992")
        self.assertIsNotNone(r, "finance_info(2, 920992) 返回 None——BJ 财务数据面消失？")
        self.assertEqual(r["market"], 2, "finance_info market 回显变化")
        self.assertGreater(r["ipo_date"], 0, "ipo_date 无值——财务字段缺失？")

    def test_bj_reserved_segment_bars_empty(self):
        """83/43 预留段应无 K 线数据面——哪天有数据此用例红，提醒更新段表认知。"""
        for code in ("831010", "430047"):
            bars = self.api.get_security_bars(4, 2, code, 0, 5)
            self.assertFalse(bars, f"{code}（预留段）意外返回了数据——段表认知需更新")


class TestBjVipdocGuard(unittest.TestCase):
    """离线 vipdoc reader 守护（不联网；本机无 vipdoc 时 skip）。"""

    @classmethod
    def setUpClass(cls):
        if not VIPDOC_BJ_LDAY.is_dir():
            raise unittest.SkipTest(f"vipdoc 路径不存在: {VIPDOC_BJ_LDAY}")
        cls.reader = TdxDailyBarReader()

    def _close_on(self, filename, date):
        df = self.reader.get_df(str(VIPDOC_BJ_LDAY / filename))
        row = df[df.index == date]
        if not len(row):
            self.fail(f"{filename} 无 {date} 行（rows={len(df)}）——客户端缓存被重灌？")
        return float(row["close"].iloc[0])

    def test_vipdoc_920992_close(self):
        self.assertAlmostEqual(
            self._close_on("bj920992.day", ANCHOR_DATE), BJ_STOCK_920992_CLOSE, places=2,
            msg="920992 离线读数与在线锚点漂移——reader 系数表或缓存格式变化？")

    def test_vipdoc_899050_close(self):
        self.assertAlmostEqual(
            self._close_on("bj899050.day", ANCHOR_DATE), BJ_INDEX_899050_CLOSE, places=2,
            msg="899050 离线读数与在线锚点漂移——reader 系数表或缓存格式变化？")

    def test_vipdoc_821001_preferred_close(self):
        """82 段优先股（821xxx）离线读数 = 在线日线口径 100.744（2026-10-19 系数修复后）。"""
        c = self._close_on("bj821001.day", BJ_PREFERRED_821001_DATE)
        self.assertAlmostEqual(
            c, BJ_PREFERRED_821001_CLOSE, places=2,
            msg="821001 离线读数漂移——BJ_PREFERRED 系数或缓存格式变化？")

    def test_vipdoc_810011_bond_close(self):
        """810 段定向可转债（官方编制指引 810=定转）：vipdoc raw×10000 → 100.0 面值。"""
        c = self._close_on("bj810011.day", "2026-04-07")
        self.assertAlmostEqual(c, 100.0, places=2,
                               msg="810011 离线读数漂移——BJ_BOND 系数或缓存格式变化？")

    def test_vipdoc_bj_segment_distribution(self):
        """BJ 段分布下限守护（92×349/82×10/89×2/81×2，2026-09-27 基线，只增不减）。"""
        c = Counter(p.name[2:4] for p in VIPDOC_BJ_LDAY.glob("bj*.day"))
        for seg, minimum in BJ_SEG_MIN.items():
            self.assertGreaterEqual(
                c.get(seg, 0), minimum,
                f"bj {seg} 段文件数跌破基线（{c.get(seg, 0)} < {minimum}）——客户端缓存缩水？")


if __name__ == "__main__":
    unittest.main()
