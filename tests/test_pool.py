# -*- coding: utf-8 -*-
"""服务器池调度器 + 时段自适应限流单测（全离线零网络）。

覆盖 11 提案验收：三阶段乘数、上限 200、set_rps 重算、wait 计时、
轮转均匀、健康分跳过、全池冷却兜底、建连失败换台、report 写回、stats 快照。
"""
import threading
import time
import unittest
from datetime import datetime, timezone

from tdxpy.exceptions import TdxConnectionError
from tdxpy.health import COOLDOWN_AFTER_FAILS, COOLDOWN_SECONDS, HealthState, health_update
from tdxpy.pool import (
    DEFAULT_BASE_RPS,
    MAX_RATE_LIMIT,
    MINUTE_BASE_RPS,
    PHASE_CLOSED,
    PHASE_PRE_POST,
    PHASE_TRADING,
    PhaseRateLimiter,
    ServerPool,
    get_phase,
)

# 固定日期：2026-11-07 周六 / 11-08 周日 / 11-09 周一
MON = datetime(2026, 11, 9)
SAT = datetime(2026, 11, 7)

SERVERS_3 = [("h%d" % i, 7709) for i in range(3)]


class FakeApi:
    """假客户端：池只存引用、判 None，不调其方法。"""


def fake_factory_factory(ok_servers):
    """构造假工厂：host 在 ok_servers 里则给 FakeApi，否则 None（建连失败）。"""

    def factory(host, port, kind):
        return FakeApi() if host in ok_servers else None

    return factory


class TestGetPhase(unittest.TestCase):
    """交易时段判定（简化版边界）。"""

    def test_boundaries(self):
        cases = [
            (datetime(2026, 11, 9, 9, 29), PHASE_PRE_POST),
            (datetime(2026, 11, 9, 9, 30), PHASE_TRADING),
            (datetime(2026, 11, 9, 12, 0), PHASE_TRADING),  # 午休视为交易中
            (datetime(2026, 11, 9, 15, 0), PHASE_TRADING),
            (datetime(2026, 11, 9, 15, 1), PHASE_PRE_POST),
            (SAT.replace(hour=10), PHASE_CLOSED),
            (datetime(2026, 11, 8, 10), PHASE_CLOSED),
        ]
        for now, expected in cases:
            self.assertEqual(get_phase(now), expected, msg=str(now))

    def test_aware_utc_converted(self):
        """UTC 1:30 = 东八区 9:30 → trading。"""
        utc = datetime(2026, 11, 9, 1, 30, tzinfo=timezone.utc)
        self.assertEqual(get_phase(utc), PHASE_TRADING)


class TestPhaseRateLimiter(unittest.TestCase):
    """乘数表 / 上限 200 / set_rps / wait 计时。"""

    def test_phase_multipliers(self):
        """通用档与分时档按乘数表取整。"""
        cases = [
            (MON.replace(hour=10), PHASE_TRADING, DEFAULT_BASE_RPS, MINUTE_BASE_RPS),
            (MON.replace(hour=8), PHASE_PRE_POST, 30, 15),  # 15*2 / int(10*1.5)
            (SAT.replace(hour=10), PHASE_CLOSED, 60, 30),  # 15*4 / 10*3
        ]
        for now, phase, rps, minute_rps in cases:
            lim = PhaseRateLimiter()
            self.assertEqual(lim.phase(now), phase)
            self.assertEqual(lim.effective_rps(now=now), rps)
            self.assertEqual(lim.effective_rps(is_minute=True, now=now), minute_rps)

    def test_rate_cap_200(self):
        """effective = min(int(base*mult), 200)，分时同上限。"""
        lim = PhaseRateLimiter(base_rps=1000, minute_base_rps=1000)
        self.assertEqual(lim.effective_rps(now=SAT.replace(hour=10)), MAX_RATE_LIMIT)
        self.assertEqual(
            lim.effective_rps(is_minute=True, now=SAT.replace(hour=10)), MAX_RATE_LIMIT
        )

    def test_forced_phase(self):
        """phase 强制指定时无视时钟。"""
        lim = PhaseRateLimiter(phase=PHASE_CLOSED)
        self.assertEqual(lim.phase(MON.replace(hour=10)), PHASE_CLOSED)
        self.assertEqual(lim.effective_rps(now=MON.replace(hour=10)), 60)

    def test_invalid_phase_raises(self):
        with self.assertRaises(ValueError):
            PhaseRateLimiter(phase="weekend")

    def test_set_rps_recalc(self):
        """set_rps 后下次 wait 按新基准重算。"""
        lim = PhaseRateLimiter()
        lim.set_rps(base_rps=60)
        self.assertEqual(lim.effective_rps(now=MON.replace(hour=10)), 60)
        lim.set_rps(minute_base_rps=20)
        self.assertEqual(lim.effective_rps(is_minute=True, now=MON.replace(hour=10)), 20)

    def test_wait_spacing(self):
        """同台连续两次 wait：第二次被卡到基准间隔（真睡短间隔）。"""
        lim = PhaseRateLimiter(base_rps=200, phase=PHASE_TRADING)  # interval = 5ms
        lim.wait()
        t0 = time.monotonic()
        delay = lim.wait()
        gap = time.monotonic() - t0
        self.assertGreaterEqual(delay, 0.0)
        self.assertGreaterEqual(gap, 0.004)  # ≈5ms 间隔（容忍时钟精度）

    def test_wait_zero_on_first_call(self):
        """首次调用零等待。"""
        lim = PhaseRateLimiter(base_rps=200, phase=PHASE_TRADING)
        self.assertEqual(lim.wait(), 0.0)


class TestServerPool(unittest.TestCase):
    """轮转 / 健康分跳过 / 兜底 / report / stats（假工厂全离线）。"""

    def setUp(self):
        self.health = {}  # 独立健康字典，不污染 TdxClient._health

    def _pool(self, servers=SERVERS_3, ok=None, **kwargs):
        ok = servers if ok is None else ok
        return ServerPool(
            servers=servers,
            health_dict=self.health,
            client_factory=fake_factory_factory({h for h, _ in ok}),
            **kwargs,
        )

    def test_round_robin_uniform(self):
        """3 台 × 9 次：每台恰好 3 次，无饿死（只看 std 分片）。"""
        pool = self._pool(rate_limit=1000)
        for _ in range(9):
            pool.next_client()
        stats = pool.stats(kind="std")
        self.assertEqual({entry["req"] for entry in stats.values()}, {3})
        self.assertEqual(set(stats), set(SERVERS_3))

    def test_rate_limited_per_server(self):
        """单台池连续调用被 limiter 卡间隔（限流不被绕过）。"""
        pool = self._pool(servers=[("h0", 7709)], rate_limit=200)
        pool.next_client()
        t0 = time.monotonic()
        pool.next_client()
        self.assertGreaterEqual(time.monotonic() - t0, 0.004)

    def test_unhealthy_server_skipped(self):
        """冷却期服务器让位健康节点。"""
        pool = self._pool(rate_limit=1000)
        self.health[SERVERS_3[0]] = health_update(
            None, False, now=time.monotonic()
        )
        self.health[SERVERS_3[0]] = health_update(
            self.health[SERVERS_3[0]], False, now=time.monotonic()
        )
        self.health[SERVERS_3[0]] = health_update(
            self.health[SERVERS_3[0]], False, now=time.monotonic()
        )  # 连续 3 败 → 冷却
        for _ in range(6):
            server, api = pool.next_client()
            self.assertNotEqual(server, SERVERS_3[0])

    def test_all_cooldown_fallback(self):
        """全池冷却仍兜底交付（health_rank 最优），不死锁。"""
        pool = self._pool(rate_limit=1000)
        now = time.monotonic()
        for s in SERVERS_3:
            st = None
            for _ in range(COOLDOWN_AFTER_FAILS):
                st = health_update(st, False, now=now)
            self.health[s] = st
        server, api = pool.next_client()
        self.assertIn(server, SERVERS_3)
        self.assertIsInstance(api, FakeApi)

    def test_connect_fail_rotates_to_next(self):
        """建连失败写回健康分并换下一台交付。"""
        pool = self._pool(ok=SERVERS_3[1:], rate_limit=1000)  # h0 建连必败
        server, api = pool.next_client()
        self.assertNotEqual(server, SERVERS_3[0])
        self.assertEqual(self.health[SERVERS_3[0]].consecutive_fails, 1)
        self.assertEqual(pool.stats()[SERVERS_3[0]]["fail"], 1)

    def test_all_connect_fail_raises(self):
        pool = self._pool(ok=[], rate_limit=1000)
        with self.assertRaises(TdxConnectionError):
            pool.next_client()

    def test_report_counts_and_cooldown(self):
        """report 成功计 req + latency；连败 3 次进冷却被跳过。"""
        pool = self._pool(rate_limit=1000)
        server, _ = pool.next_client()
        pool.report(server, ok=True, latency=0.12)
        self.assertEqual(pool.stats()[server]["req"], 1)
        self.assertEqual(pool.stats()[server]["last_latency_ms"], 120.0)
        for _ in range(COOLDOWN_AFTER_FAILS):
            pool.report(server, ok=False)
        self.assertEqual(pool.stats()[server]["fail"], 3)
        self.assertGreater(self.health[server].cooldown_until, 0)
        # 冷却后其他台接管
        for _ in range(3):
            self.assertNotEqual(pool.next_client()[0], server)

    def test_report_recover_clears_cooldown(self):
        """成功一次即解除冷却并恢复参与。"""
        pool = self._pool(rate_limit=1000)
        server, _ = pool.next_client()
        for _ in range(COOLDOWN_AFTER_FAILS):
            pool.report(server, ok=False)
        pool.report(server, ok=True, latency=0.05)
        self.assertEqual(self.health[server].cooldown_until, 0)
        self.assertEqual(self.health[server].consecutive_fails, 0)

    def test_stats_snapshot_isolated(self):
        """stats() 返回快照，外部改动不影响池内计数。"""
        pool = self._pool(rate_limit=1000)
        pool.next_client()
        snap = pool.stats()
        snap[SERVERS_3[0]]["req"] = 999
        self.assertEqual(pool.stats()[SERVERS_3[0]]["req"], 1)

    def test_invalid_kind_raises(self):
        pool = self._pool()
        with self.assertRaises(ValueError):
            pool.next_client(kind="mac")
        with self.assertRaises(ValueError):
            pool.next_client(kind="whatever")

    def test_is_minute_uses_minute_tier(self):
        """is_minute=True 走分时档（闭市 3.0× → 30 rps 而非 60）。"""
        pool = self._pool(
            servers=[("h0", 7709)],
            ok=[("h0", 7709)],
            rate_limit=DEFAULT_BASE_RPS,
            phase=PHASE_CLOSED,
        )
        limiter = pool._shards["std"].limiters[("h0", 7709)]
        self.assertEqual(limiter.effective_rps(is_minute=True), 30)
        self.assertEqual(limiter.effective_rps(is_minute=False), 60)

    def test_ex_shard_isolated(self):
        """std/ex 分片各自独立轮转与统计。"""
        ex_servers = [("ex0", 7727), ("ex1", 7727)]
        pool = ServerPool(
            servers=SERVERS_3,
            ex_servers=ex_servers,
            rate_limit=1000,
            health_dict=self.health,
            client_factory=fake_factory_factory(
                {h for h, _ in SERVERS_3} | {h for h, _ in ex_servers}
            ),
        )
        pool.next_client(kind="ex")
        pool.next_client(kind="ex")
        stats = pool.stats(kind="ex")
        self.assertEqual(stats[ex_servers[0]]["req"], 1)
        self.assertEqual(stats[ex_servers[1]]["req"], 1)
        self.assertEqual(pool.stats(kind="std")[SERVERS_3[0]]["req"], 0)

    def test_ex_connect_fail_raises(self):
        pool = ServerPool(
            servers=SERVERS_3,
            ex_servers=[("exbad", 7727)],
            health_dict=self.health,
            client_factory=fake_factory_factory(set()),
        )
        with self.assertRaises(TdxConnectionError):
            pool.next_client(kind="ex")

    def test_default_pool_from_class_servers(self):
        """servers=None 时取 TdxClient.STD_SERVERS 前 size 台。"""
        from tdxpy.client import TdxClient

        pool = ServerPool(
            size=10,
            rate_limit=1000,
            health_dict=self.health,
            client_factory=fake_factory_factory({h for h, _ in TdxClient.STD_SERVERS[:10]}),
        )
        server, _ = pool.next_client()
        self.assertIn(server, TdxClient.STD_SERVERS[:10])
        self.assertEqual(len(pool.stats(kind="std")), 10)


class TestThreadSafety(unittest.TestCase):
    """多线程并发 next_client：总交付数正确、无崩溃（粗粒度冒烟）。"""

    def test_concurrent_next_client(self):
        health = {}
        servers = [("h%d" % i, 7709) for i in range(4)]
        pool = ServerPool(
            servers=servers,
            rate_limit=2000,  # 0.5ms 间隔，让限流几乎不阻塞
            health_dict=health,
            client_factory=fake_factory_factory({h for h, _ in servers}),
        )
        delivered = []
        lock = threading.Lock()

        def worker():
            for _ in range(10):
                server, api = pool.next_client()
                with lock:
                    delivered.append(server)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(delivered), 40)
        self.assertTrue(all(isinstance(s, tuple) for s in delivered))
        # 每台都有交付（4 台轮转 40 次，健康池不应饿死任何一台）
        served = {s for s in delivered}
        self.assertEqual(served, set(servers))


class TestReviewRegressions(unittest.TestCase):
    """三路对抗复审回归锁（2026-11-09 批次十一复审）：竞态懒建/异常换台/空表/timeout 路由/重复元素。"""

    def setUp(self):
        self.health = {}

    def test_lazy_connect_no_leak(self):
        """HIGH-1：并发懒建同一台只建一次连接（双检锁；修复前 8 线程建 8 个泄漏 7 个）。"""
        calls = []
        lock = threading.Lock()

        def slow_factory(host, port, kind):
            with lock:
                calls.append(host)
            time.sleep(0.05)  # 放大竞态窗口
            return FakeApi()

        pool = ServerPool(
            servers=[("h0", 7709)],
            rate_limit=2000,
            health_dict=self.health,
            client_factory=slow_factory,
        )
        threads = [threading.Thread(target=pool.next_client) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 1, f"并发懒建应只建一次连接，实际 {len(calls)} 次")

    def test_connect_exception_rotates(self):
        """HIGH-2：工厂抛异常（默认工厂 raise_exception=True 的真实语义）→ 写健康分换台。"""
        from tdxpy.exceptions import TdxConnectionError

        def raising_factory(host, port, kind):
            if host == "h0":
                raise TdxConnectionError("connection timeout error")
            return FakeApi()

        pool = ServerPool(
            servers=SERVERS_3,
            rate_limit=1000,
            health_dict=self.health,
            client_factory=raising_factory,
        )
        server, api = pool.next_client()
        self.assertNotEqual(server, SERVERS_3[0])
        self.assertEqual(self.health[SERVERS_3[0]].consecutive_fails, 1, "异常路径也应写健康分")
        self.assertEqual(pool.stats()[SERVERS_3[0]]["fail"], 1)

    def test_empty_servers_rejected(self):
        """空表构造时 ValueError（而非 next_client 裸 IndexError，复审 C-M1）。"""
        with self.assertRaises(ValueError):
            ServerPool(servers=[], health_dict=self.health, client_factory=fake_factory_factory(set()))
        with self.assertRaises(ValueError):
            ServerPool(ex_servers=[], health_dict=self.health, client_factory=fake_factory_factory(set()))

    def test_timeout_routed_to_default_factory(self):
        """MEDIUM-4：timeout 参数路由到默认工厂（修复前硬编码 10.0 死参数）。"""
        import tdxpy.pool as pool_mod

        captured = {}

        def spy(host, port, kind, timeout=10.0):
            captured["timeout"] = timeout
            return FakeApi()

        original = pool_mod._default_client_factory
        pool_mod._default_client_factory = spy
        try:
            pool = ServerPool(
                servers=[("h0", 7709)],
                rate_limit=2000,
                timeout=3.5,
                health_dict=self.health,
            )
            pool.next_client()
        finally:
            pool_mod._default_client_factory = original
        self.assertEqual(captured["timeout"], 3.5)

    def test_duplicate_servers_rotation(self):
        """MEDIUM-3：servers 含重复元素时轮转仍按位置均匀（修复前 servers.index 脱钩）。"""
        from collections import Counter

        servers = [("A", 1), ("B", 1), ("A", 1)]  # 位置 0/2 都是 A
        pool = ServerPool(
            servers=servers,
            rate_limit=2000,
            health_dict=self.health,
            client_factory=fake_factory_factory({"A", "B"}),
        )
        got = Counter(pool.next_client()[0] for _ in range(30))
        self.assertEqual(got[("A", 1)], 20, "A 占位置 0/2 应得 20 次")
        self.assertEqual(got[("B", 1)], 10, "B 占位置 1 应得 10 次")


if __name__ == "__main__":
    unittest.main()
