# -*- coding: utf-8 -*-
"""自适应翻页（_paginate_adaptive）离线单元测试。

背景：服务器对每类命令有硬上限（std K 线 800 / std 分笔 2000 / EX 分笔 1800 /
EX K 线 700 / MAC 逐笔 1000 / MAC K 线 700），超限表现为「静默截断」（分笔/列表/
MAC/EX）或「整请求失败」（std K 线，由 _hq_call 抛 TdxFunctionCallError，不进本函数）。
旧翻页哨兵 `len(page) < page_size ⇒ 末页` 在请求页超上限时会立即误判，静默丢数。

本测试用假服务器穷举「数据量 × 硬上限 × 服务器实际上限 × 请求页大小」，并单独钉住
「连续短页」「中途 None」「请求熔断」等情形。
"""
import unittest

from tdxpy.client import _paginate_adaptive


def make_fetch(total, server_limit):
    """假服务器：每次最多返回 min(请求数, 实际上限, 剩余)，pos 超过总量返回空。"""
    def fetch(pos, count):
        if pos >= total:
            return []
        n = min(count, server_limit, total - pos)
        return list(range(pos, pos + n))
    return fetch


def make_jitter_fetch(total, server_limit, jitters):
    """假服务器 + 抖动：jitters = [(pos, 该次实际返回条数)]，模拟服务器欠填。"""
    def fetch(pos, count):
        if pos >= total:
            return []
        rem = total - pos
        for jp, jl in jitters:
            if pos == jp and rem > jl:
                return list(range(pos, pos + jl))
        return list(range(pos, pos + min(count, server_limit, rem)))
    return fetch


TOTALS = [0, 1, 94, 799, 800, 801, 1999, 2000, 2001, 4000, 4094]
#: (hard_limit, server_actual_limit) —— 含「常量=真实」「真实<常量」「无上限表」三类
LIMIT_PAIRS = [
    (800, 800), (1000, 1000), (2000, 2000),      # 常量与真实一致
    (2000, 1000), (2000, 800), (1800, 1000),     # 真实上限 < 常量（自适应兜底的核心场景）
    (None, 2000), (None, 1000),                  # 无上限表（纯运行时自适应）
]
SIZES = [800, 1000, 2000, 2500, 5000, 10000]


class TestFullScanMatrix(unittest.TestCase):
    """穷举矩阵：任何「数据量 × 上限组合 × 请求页」都必须完整取回。"""

    def test_matrix(self):
        combos = 0
        for total in TOTALS:
            for hard_limit, server_limit in LIMIT_PAIRS:
                for size in SIZES:
                    combos += 1
                    with self.subTest(total=total, hard_limit=hard_limit,
                                      server_limit=server_limit, size=size):
                        fetch = make_fetch(total, server_limit)
                        rows, eff, reqs = _paginate_adaptive(fetch, size, hard_limit=hard_limit)
                        self.assertEqual(rows, list(range(total)),
                                         f"丢数/乱序：拿到 {len(rows)}/{total}")
                        if hard_limit:
                            self.assertLessEqual(eff, hard_limit, "有效页不得超硬上限")
        self.assertEqual(combos, len(TOTALS) * len(LIMIT_PAIRS) * len(SIZES))

    def test_no_duplicates_any_combo(self):
        for total in (800, 4094, 6000):
            for hard_limit, server_limit in LIMIT_PAIRS:
                with self.subTest(total=total, server_limit=server_limit):
                    rows, _, _ = _paginate_adaptive(
                        make_fetch(total, server_limit), 5000, hard_limit=hard_limit)
                    self.assertEqual(len(rows), len(set(rows)))


class TestShortPageRobustness(unittest.TestCase):
    """短页/抖动：降档必须能吸收任意次欠填（旧一次性格标志会在此丢数）。"""

    def test_single_short_page(self):
        rows, _, _ = _paginate_adaptive(
            make_jitter_fetch(6000, 2000, [(2000, 1500)]), 2000, hard_limit=2000)
        self.assertEqual(rows, list(range(6000)))

    def test_consecutive_short_pages(self):
        """连续两次欠填（旧实现丢 1500）。"""
        rows, _, _ = _paginate_adaptive(
            make_jitter_fetch(6000, 2000, [(2000, 1500), (3500, 1000)]), 2000, hard_limit=2000)
        self.assertEqual(rows, list(range(6000)), "连续短页不得丢数")

    def test_many_short_pages(self):
        jitters = [(2000, 1500), (3500, 1200), (4700, 300), (5000, 200)]
        rows, _, _ = _paginate_adaptive(
            make_jitter_fetch(6000, 2000, jitters), 2000, hard_limit=2000)
        self.assertEqual(rows, list(range(6000)))

    def test_server_limit_below_hard_limit(self):
        """真实上限(1000) < 硬上限常量(2000)：必须降档拿全。"""
        rows, eff, reqs = _paginate_adaptive(make_fetch(4094, 1000), 2000, hard_limit=2000)
        self.assertEqual(rows, list(range(4094)))
        self.assertEqual(eff, 94)  # 末页欠填后降到剩余条数


class TestEdgeCases(unittest.TestCase):
    def test_empty(self):
        rows, _, reqs = _paginate_adaptive(make_fetch(0, 2000), 5000)
        self.assertEqual(rows, [])
        self.assertEqual(reqs, 1, "无数据应 1 次请求即结束")

    def test_single_row(self):
        rows, _, _ = _paginate_adaptive(make_fetch(1, 2000), 5000)
        self.assertEqual(rows, [0])

    def test_exact_one_page(self):
        rows, _, _ = _paginate_adaptive(make_fetch(2000, 2000), 5000)
        self.assertEqual(rows, list(range(2000)))

    def test_exact_multiple_of_page(self):
        """数据量恰为有效页整数倍：末页满页后需要一次空请求确认。"""
        rows, _, reqs = _paginate_adaptive(make_fetch(4000, 2000), 5000)
        self.assertEqual(rows, list(range(4000)))
        self.assertGreaterEqual(reqs, 3)

    def test_limit_plus_one(self):
        rows, _, _ = _paginate_adaptive(make_fetch(2001, 2000), 5000)
        self.assertEqual(rows, list(range(2001)))

    def test_start_offset(self):
        rows, _, _ = _paginate_adaptive(make_fetch(4094, 2000), 5000, start=1000)
        self.assertEqual(rows, list(range(1000, 4094)))

    def test_max_pages_truncates_without_losing_content(self):
        """max_pages 达上限 → 静默返回已拉部分（既有契约），且内容必须是前缀而非乱序。"""
        rows, _, _ = _paginate_adaptive(make_fetch(10000, 2000), 2000,
                                        hard_limit=2000, max_pages=2)
        self.assertEqual(rows, list(range(4000)), "必须是前 4000 条的连续前缀")

    def test_none_terminates(self):
        """fetch 返回 None（std 分笔越界/失败的正常形态）→ 停止并返回已拉部分。"""
        def fetch(pos, count):
            if pos >= 1500:
                return None
            return list(range(pos, pos + min(count, 500)))

        rows, _, reqs = _paginate_adaptive(fetch, 2000, hard_limit=2000)
        self.assertEqual(rows, list(range(1500)))
        self.assertGreaterEqual(reqs, 1)

    def test_stall_guard_still_raises(self):
        """停滞守卫保留：服务器反复返回同一页应 raise。"""
        def fetch(pos, count):
            return [{"datetime": "2026-11-06"}] * count

        with self.assertRaises(RuntimeError):
            _paginate_adaptive(fetch, 10, hard_limit=10,
                               stall_key=lambda r: r.get("datetime"), ctx="test")

    def test_request_circuit_breaker(self):
        """服务器恒返回 1 条 → 请求数熔断 raise（不静默、不无限循环）。"""
        def fetch(pos, count):
            if pos >= 100000:
                return []
            return [pos]

        with self.assertRaises(RuntimeError):
            _paginate_adaptive(fetch, 2000, hard_limit=2000, max_requests=50, ctx="test")


class TestRequestCost(unittest.TestCase):
    """请求次数特征（弱断言，避免绑定实现细节）。"""

    def test_oversized_page_not_more_expensive(self):
        _, _, req_auto = _paginate_adaptive(make_fetch(4094, 2000), 10000)
        _, _, req_base = _paginate_adaptive(make_fetch(4094, 2000), 800)
        self.assertLessEqual(req_auto, req_base)

    def test_end_probe_at_most_one_extra(self):
        """正常页大小下，末页确认至多多 1 次请求。"""
        _, _, req_probe = _paginate_adaptive(make_fetch(4094, 2000), 2000, hard_limit=2000)
        data_pages = (4094 + 1999) // 2000
        self.assertLessEqual(req_probe - data_pages, 1)


if __name__ == "__main__":
    unittest.main()
