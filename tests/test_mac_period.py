# -*- coding: utf-8 -*-
"""resolve_mac_period 映射单测（全离线）。

背景：MAC 侧编码 7=固定 1min（times 被服务器忽略）、8=参数化 1min（times=N）。
std "1min"=7 直传 MAC 7 会静默丢失 times（2026-10-21 实测勘误，门面须映射到 8）。
"""
import unittest

from tdxpy.client import resolve_mac_period


class TestResolveMacPeriod(unittest.TestCase):
    def test_1min_maps_to_mac8(self):
        """std "1min"(7) 必须映射到 MAC 参数化编码 8——防 times 静默丢失回归。"""
        self.assertEqual(resolve_mac_period("1min"), (8, 1))
        self.assertEqual(resolve_mac_period("1min", times=5), (8, 5))
        self.assertEqual(resolve_mac_period(7, times=30), (8, 30))

    def test_nmin_alias(self):
        """"Nmin" 别名 → (8, N)。"""
        self.assertEqual(resolve_mac_period("10min"), (8, 10))
        self.assertEqual(resolve_mac_period("2min"), (8, 2))
        self.assertEqual(resolve_mac_period("120MIN"), (8, 120))
        self.assertEqual(resolve_mac_period("3min", times=3), (8, 3))  # 一致不冲突

    def test_fixed_periods_passthrough(self):
        """0-6 周期 std/MAC 同值直传；times>1 raise（不静默丢弃）。"""
        for p in ("5min", "15min", "30min", "1h", "day", "week", "month"):
            cat, times = resolve_mac_period(p)
            self.assertEqual(times, 1)
            self.assertIn(cat, (0, 1, 2, 3, 4, 5, 6))
        with self.assertRaises(ValueError):
            resolve_mac_period("day", times=5)

    def test_conflicts_raise(self):
        """period 与 times 冲突 / 非法 N / 非法周期 → ValueError。"""
        with self.assertRaises(ValueError):
            resolve_mac_period("10min", times=5)
        with self.assertRaises(ValueError):
            resolve_mac_period("0min")
        with self.assertRaises(ValueError):
            resolve_mac_period("999min")
        with self.assertRaises(ValueError):
            resolve_mac_period("fortnight")

    def test_times_none_defaults(self):
        """times=None 时 Nmin 别名与 1min 映射默认 times=1 语义正确。"""
        self.assertEqual(resolve_mac_period("7min"), (8, 7))
        self.assertEqual(resolve_mac_period("1min"), (8, 1))
        self.assertEqual(resolve_mac_period("day"), (4, 1))

    def test_int_str_asymmetry(self):
        """int/str 不对称边界锁（交叉复审 P2-1）：int=std category 0-7，Nmin 别名仅 str。"""
        with self.assertRaises(ValueError):
            resolve_mac_period(8)  # int 8 不是合法 std category
        self.assertEqual(resolve_mac_period("8min"), (8, 8))  # str "8min" 是新别名
        with self.assertRaises(ValueError):
            resolve_mac_period("7min", times=5)  # 别名分支要求 times==N
        self.assertEqual(resolve_mac_period(7, times=5), (8, 5))  # int 7 + times=参数化倍数


if __name__ == "__main__":
    unittest.main()
