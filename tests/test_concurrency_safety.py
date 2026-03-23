# -*- coding: utf-8 -*-
"""并发安全测试：多线程共享连接必须串行化。

背景（2026-09-20 修复）：TdxExHq_API 全部命令曾漏传 lock=self.lock，
多线程共享连接时 socket 流撕裂/响应错位（见 pattern_investigate 调查报告）。
修复后命令统一经 BaseSocketClient._cmd 工厂创建，锁自动注入。

本测试用「响应自带 code/market 回显字段 == 请求值」作最强判据：
错位响应 = 别家标的真实数据，code 字段必对不上（HARD_CODE_MISMATCH）。

需要真实通达信服务器；网络不可达时自动 skip。
服务器地址可用环境变量覆盖：TDXPY_TEST_HQ_HOST / TDXPY_TEST_EX_HOST。

运行：
    python tests/test_concurrency_safety.py -v
"""
import os
import threading
import unittest

from tdxpy.exceptions import TdxFunctionCallError
from tdxpy.exhq import TdxExHq_API
from tdxpy.hq import TdxHq_API

HQ_HOST = os.environ.get("TDXPY_TEST_HQ_HOST", "111.229.247.189")
HQ_PORT = 7709
EX_HOST = os.environ.get("TDXPY_TEST_EX_HOST", "139.9.191.175")
EX_PORT = 7727

CONNECT_TIMEOUT = 8

#: 标准市场真值（market, code, 期望回显 code）
STD_TRUTH = [
    (1, "600000", "600000"),
    (1, "600519", "600519"),
    (0, "000001", "000001"),
    (0, "300750", "300750"),
    (1, "688981", "688981"),
]

#: 扩展市场真值（market, code, 期望回显 code）——CUL8 大连商品主力连续
EX_TRUTH = [
    (29, "CUL8", "CUL8"),
    (29, "CUL8", "CUL8"),
    (30, "cu2510", "cu2510"),
    (28, "SR511", "SR511"),
    (30, "au2512", "au2512"),
]


class TestConcurrencySafety(unittest.TestCase):
    """多线程共享连接（multithread=True）下命令必须全部正确。"""

    def test_hq_shared_connection_safe(self):
        """标准市场 get_security_quotes：4 线程 × 15 次共享连接，回显零错位。"""
        api = TdxHq_API(multithread=True)
        try:
            if not api.connect(HQ_HOST, HQ_PORT, time_out=CONNECT_TIMEOUT):
                raise unittest.SkipTest(f"无法连接标准市场 {HQ_HOST}:{HQ_PORT}")
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接标准市场: {type(e).__name__}: {e}")

        results = []
        err_lock = threading.Lock()

        def worker(tid):
            local_err = []
            for i in range(15):
                market, code, _ = STD_TRUTH[(tid + i) % len(STD_TRUTH)]
                quotes = api.get_security_quotes([(market, code)])
                if not quotes:
                    local_err.append((tid, i, code, "empty"))
                    continue
                q = quotes[0]
                if str(q.get("code")) != code or int(q.get("market")) != market:
                    local_err.append((tid, i, code, q.get("code"), q.get("market")))
            with err_lock:
                results.extend(local_err)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        api.disconnect()

        self.assertEqual(results, [], f"std 共享连接错位 {len(results)} 次: {results[:5]}")

    def test_ex_shared_connection_safe(self):
        """扩展市场 get_instrument_quote：4 线程 × 15 次共享连接，回显零错位。

        ⚠️ 修复前必挂：TdxExHq_API 曾全部漏传锁，此用例 62 次并发可复现
        4 HARD + 50 撕裂（pattern_investigate E-multi A 实测）。
        """
        api = TdxExHq_API(multithread=True)
        try:
            if not api.connect(EX_HOST, EX_PORT, time_out=CONNECT_TIMEOUT):
                raise unittest.SkipTest(f"无法连接扩展市场 {EX_HOST}:{EX_PORT}")
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接扩展市场: {type(e).__name__}: {e}")

        results = []
        err_lock = threading.Lock()

        def worker(tid):
            local_err = []
            for i in range(15):
                market, code, _ = EX_TRUTH[(tid + i) % len(EX_TRUTH)]
                try:
                    quotes = api.get_instrument_quote(market, code)
                except Exception as e:  # noqa 传输撕裂会抛异常
                    local_err.append((tid, i, code, type(e).__name__, str(e)))
                    continue
                if not quotes:
                    local_err.append((tid, i, code, "empty"))
                    continue
                q = quotes[0]
                if str(q.get("code")) != code or int(q.get("market")) != market:
                    local_err.append((tid, i, code, q.get("code"), q.get("market")))
            with err_lock:
                results.extend(local_err)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        api.disconnect()

        self.assertEqual(results, [], f"ex 共享连接错位/撕裂 {len(results)} 次: {results[:5]}")

    def test_facade_lazy_init_single_connection(self):
        """TdxClient 多线程首次并发调用：全部成功且只建一个连接（双重检查锁）。"""
        from tdxpy.client import TdxClient

        try:
            client = TdxClient(host=HQ_HOST, port=HQ_PORT, ex_host=EX_HOST, ex_port=EX_PORT,
                               heartbeat=False)
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"创建门面失败: {type(e).__name__}: {e}")

        errs = []

        def worker():
            try:
                client._ensure_hq()
                client._ensure_ex()
            except Exception as e:  # noqa
                errs.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errs, [], f"并发懒加载失败 {len(errs)} 次: {errs[:3]}")
        # 双重检查锁保证：并发下也只建一个连接对象，且可复用
        self.assertIsNotNone(client._hq_api)
        self.assertIsNotNone(client._ex_api)
        self.assertIs(client._ensure_hq(), client._hq_api)
        self.assertIs(client._ensure_ex(), client._ex_api)
        client.close()

    def test_auto_retry_recovers_after_socket_kill(self):
        """auto_retry=True + 底层 socket 被杀 → 自动重连重发恢复。

        回归 2026-09-20 复审发现的三个 retry 下沉缺陷：
        #1 retry 分支曾误把 socket 当 API 访问 auto_retry → AttributeError；
        #2 重连 setup 再进 call_api 曾死锁（非重入锁），本用例会卡死超时；
        #3 重连后 parser 仍绑定旧 socket，重发必败。
        """
        api = TdxHq_API(multithread=True, auto_retry=True, raise_exception=True)
        try:
            if not api.connect(HQ_HOST, HQ_PORT, time_out=CONNECT_TIMEOUT):
                raise unittest.SkipTest(f"无法连接标准市场 {HQ_HOST}:{HQ_PORT}")
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接标准市场: {type(e).__name__}: {e}")

        # 模拟断线：直接关闭底层 socket（不通知 API），下次查询必然失败 → 触发重连重发
        api.client.close()

        quotes = api.get_security_quotes([(1, "600000")])
        self.assertTrue(quotes, "auto_retry 重连后应返回数据")
        self.assertEqual(str(quotes[0].get("code")), "600000",
                         "auto_retry 重连后回显 code 应正确（非别家标的）")
        self.assertIsNotNone(api.client, "重连后 API 应持有新 socket")
        try:
            api.disconnect()
        except Exception:  # noqa 收尾清理：断开失败不影响测试结论
            pass

    def test_auto_retry_raises_when_exhausted(self):
        """auto_retry=True 但服务器持续不可达：raise_exception=True → TdxFunctionCallError。

        验证重试耗尽路径：不 AttributeError、不吞异常、original_exception 是连接类异常。
        """
        api = TdxHq_API(multithread=True, auto_retry=True, raise_exception=True)
        try:
            if not api.connect(HQ_HOST, HQ_PORT, time_out=CONNECT_TIMEOUT):
                raise unittest.SkipTest(f"无法连接标准市场 {HQ_HOST}:{HQ_PORT}")
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接标准市场: {type(e).__name__}: {e}")

        # 模拟断线并指向不可达端口：重连必然失败，重试耗尽
        api.client.close()
        api.port = 1

        with self.assertRaises(TdxFunctionCallError) as ctx:
            api.get_security_quotes([(1, "600000")])
        self.assertIsNotNone(ctx.exception.original_exception,
                             "original_exception 不应为空（保持原始异常链）")
        try:
            api.disconnect()
        except Exception:  # noqa 收尾清理：断开失败不影响测试结论
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
