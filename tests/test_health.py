# -*- coding: utf-8 -*-
"""健康分引擎单测（F1.4，全离线零网络）。

覆盖 08 计划验收案例：衰减 / 回升 / 冷却窗口 / EWMA 平滑 / 排序 / 跨调用持久。
"""
import unittest

from tdxpy import client as client_mod
from tdxpy.client import TdxClient
from tdxpy.health import (
    COOLDOWN_AFTER_FAILS,
    COOLDOWN_SECONDS,
    HEALTH_CEILING,
    HEALTH_FLOOR,
    HealthState,
    health_effective_latency,
    health_is_available,
    health_rank,
    health_update,
)

T0 = 1000.0  # 固定 now，注入测试


class TestHealthEngine(unittest.TestCase):
    """纯函数层：health_update / is_available / rank。"""

    def test_health_decay_on_failure(self):
        """连续失败分数按 ×0.5 衰减，触底 0.05 不再下降。"""
        st = None
        for expected in (0.25, 0.125, 0.0625, HEALTH_FLOOR):
            st = health_update(st, False, now=T0)
            self.assertAlmostEqual(st.score, expected)
        # 继续失败不再低于 floor
        st = health_update(st, False, now=T0)
        self.assertEqual(st.score, HEALTH_FLOOR)

    def test_health_recover_on_success(self):
        """成功 +0.2 回升，封顶 1.0；成功同时清零连续失败与冷却。"""
        st = health_update(None, True, latency=0.1, now=T0)
        self.assertAlmostEqual(st.score, 0.7)
        st = health_update(st, True, latency=0.1, now=T0)
        self.assertAlmostEqual(st.score, 0.9)
        st = health_update(st, True, latency=0.1, now=T0)
        self.assertAlmostEqual(st.score, HEALTH_CEILING)
        st = health_update(st, True, latency=0.1, now=T0)
        self.assertAlmostEqual(st.score, HEALTH_CEILING)

    def test_health_cooldown_window(self):
        """连续失败 ≥3 次进入冷却；冷却期内不可用，到期恢复；成功立即解除。"""
        st = None
        for i in range(COOLDOWN_AFTER_FAILS - 1):
            st = health_update(st, False, now=T0 + i)
            self.assertEqual(st.cooldown_until, 0.0, "未达连续阈值不应进入冷却")
        st = health_update(st, False, now=T0 + 2)
        self.assertAlmostEqual(st.cooldown_until, T0 + 2 + COOLDOWN_SECONDS)
        self.assertFalse(health_is_available(st, now=T0 + 3), "冷却期内应不可用")
        self.assertTrue(health_is_available(st, now=T0 + 2 + COOLDOWN_SECONDS), "到期应恢复可用")
        # 一次成功即解除冷却并清零计数
        ok = health_update(st, True, latency=0.2, now=T0 + 3)
        self.assertEqual(ok.cooldown_until, 0.0)
        self.assertEqual(ok.consecutive_fails, 0)

    def test_health_ewma_latency(self):
        """EWMA α=0.3 平滑：首值直取，其后 0.7 旧 + 0.3 新；突发高延迟不致骤变。"""
        st = health_update(None, True, latency=0.10, now=T0)
        self.assertAlmostEqual(st.ewma_latency, 0.10)
        st = health_update(st, True, latency=0.20, now=T0)
        self.assertAlmostEqual(st.ewma_latency, 0.10 * 0.7 + 0.20 * 0.3)
        # 突发 10 倍延迟：EWMA 只温和抬升
        burst = health_update(st, True, latency=2.0, now=T0)
        self.assertLess(burst.ewma_latency, 0.7, "单次突发不应把 EWMA 抬到接近突发值")
        # latency=None 时 EWMA 保持原值（如仅记录断线失败）
        frozen = health_update(st, False, latency=None, now=T0)
        self.assertEqual(frozen.ewma_latency, st.ewma_latency)

    def test_health_ranking_order(self):
        """多节点按 effective_latency 排序；冷却节点垫底但保留（兜底）。"""
        healthy_fast = HealthState(score=0.9, ewma_latency=0.05)
        healthy_slow = HealthState(score=0.9, ewma_latency=0.50)
        cooling = HealthState(score=0.9, ewma_latency=0.01, cooldown_until=T0 + 100)
        fresh = None  # 无状态节点=初始分
        ranked = health_rank(
            [("cooling", cooling), ("fresh", fresh), ("slow", healthy_slow), ("fast", healthy_fast)],
            now=T0,
        )
        keys = [k for k, _ in ranked]
        self.assertEqual(keys[0], "fast", "健康且快者应最优")
        self.assertEqual(keys[-1], "cooling", "冷却节点应垫底但仍在榜（全池冷却可兜底）")
        self.assertIn("fresh", keys)
        # 冷却者之外的顺序按分数/延迟
        self.assertEqual(keys[1], "slow")

    def test_health_state_persistence(self):
        """类级健康字典跨 _pick/auto() 调用保持：同节点多次成功分数继续累积。"""

        class FakeAPI:
            calls = []

            def __init__(self, *a, **kw):
                pass

            def connect(self, host, port, time_out=None):
                return True

            def get_security_count(self, market):
                return 2673

            def disconnect(self):
                pass

        orig = dict(client_mod._PROBE_FLOWS)
        client_mod._PROBE_FLOWS = {"std": (FakeAPI, "get_security_count", (0,))}
        TdxClient._health.clear()
        try:
            node = ("10.0.0.1", 7709)
            TdxClient._pick([node], 1.0)
            s1 = TdxClient._health[node]
            self.assertAlmostEqual(s1.score, 0.7)
            self.assertIsNotNone(s1.ewma_latency)
            TdxClient._pick([node], 1.0)
            s2 = TdxClient._health[node]
            self.assertAlmostEqual(s2.score, 0.9, msg="跨调用健康分应继续累积而非重置")
        finally:
            client_mod._PROBE_FLOWS = orig
            TdxClient._health.clear()


if __name__ == "__main__":
    unittest.main()
