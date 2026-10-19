# -*- coding: utf-8 -*-
"""可达性探测单测（F1.3：_business_ping 三段业务探测 + _pick 剔除坏节点）。

离线用例全程 mock API 类；最后一个用例为在线真实池验证（无网络自动 skip）。

故障阶段语义（与 BaseSocketClient.connect 结构对齐）：
- "connect"：TCP 建连/协议握手 setup 失败（两者在同一 connect 调用内完成，
  外部不可分，故合并为"连接建立失败"阶段）；
- "probe"：连接建立成功但轻量业务命令返回空/异常——假活服务器。
"""
import unittest
from unittest import mock

from tdxpy import client as client_mod
from tdxpy.client import TdxClient, _business_ping


def _fake_flow(connect_result=True, connect_exc=None, probe_value=2673, probe_exc=None):
    """构造 (FakeAPI, method, args) 探针流：可编程 connect/probe 行为。"""

    class FakeAPI:
        def __init__(self, *a, **kw):
            pass

        def connect(self, host, port, time_out=None):
            if connect_exc is not None:
                raise connect_exc
            return connect_result

        def get_security_count(self, market):
            if probe_exc is not None:
                raise probe_exc
            return probe_value

        def disconnect(self):
            pass

    return (FakeAPI, "get_security_count", (0,))


class TestBusinessPing(unittest.TestCase):
    """_business_ping 三段判定。"""

    def setUp(self):
        self.orig = dict(client_mod._PROBE_FLOWS)

    def tearDown(self):
        client_mod._PROBE_FLOWS = self.orig

    def test_ping_business_command_gate(self):
        """业务命令返回空/垃圾 → 判定不可达（防假活服务器）。"""
        client_mod._PROBE_FLOWS = {"std": _fake_flow(probe_value=0)}
        self.assertIsNone(_business_ping("h", 7709, 1.0))
        self.assertEqual(_business_ping("h", 7709, 1.0, detail=True)[1], "probe")

        client_mod._PROBE_FLOWS = {"std": _fake_flow(probe_value=None)}
        self.assertIsNone(_business_ping("h", 7709, 1.0))

        # 正常计数 → 可达且返回正耗时
        client_mod._PROBE_FLOWS = {"std": _fake_flow(probe_value=2673)}
        latency = _business_ping("h", 7709, 1.0)
        self.assertIsNotNone(latency)
        self.assertGreaterEqual(latency, 0.0)

    def test_ping_timeout_classification(self):
        """三类故障分类：连接建立超时/拒绝 → "connect"；业务命令超时 → "probe"。"""
        import socket as _socket

        # TCP 建连超时（connect 内抛 timeout）→ connect 阶段
        client_mod._PROBE_FLOWS = {"std": _fake_flow(connect_exc=_socket.timeout())}
        self.assertEqual(_business_ping("h", 7709, 1.0, detail=True), (None, "connect"))

        # 服务器拒绝/握手返回失败（connect 返回 False）→ connect 阶段
        client_mod._PROBE_FLOWS = {"std": _fake_flow(connect_result=False)}
        self.assertEqual(_business_ping("h", 7709, 1.0, detail=True), (None, "connect"))

        # 业务命令超时 → probe 阶段（假活：连得上、问不出）
        client_mod._PROBE_FLOWS = {"std": _fake_flow(probe_exc=_socket.timeout())}
        self.assertEqual(_business_ping("h", 7709, 1.0, detail=True), (None, "probe"))

    def test_auto_excludes_bad_node(self):
        """_pick 剔除探测失败节点、选中可用节点；失败节点健康分被记录降权。"""
        TdxClient._health.clear()
        bad, good = ("10.9.9.9", 7709), ("10.0.0.2", 7709)
        lat = {bad: None, good: 0.12}

        def fake_ping(host, port, timeout, kind="std", detail=False):
            v = lat[(host, port)]
            return (v, "ok" if v is not None else "connect") if detail else v

        with mock.patch.object(client_mod, "_business_ping", fake_ping):
            best = TdxClient._pick([bad, good], 1.0)
        self.assertEqual(best, good)
        self.assertIsNone(TdxClient._health[bad].ewma_latency, "坏节点不应有延迟记录")
        self.assertLess(TdxClient._health[bad].score, 0.5, "坏节点应被降权")
        self.assertIsNotNone(TdxClient._health[good].ewma_latency)
        TdxClient._health.clear()

    def test_auto_real_pool(self):
        """在线：真实服务器池三段探测选优，选中节点能出 K 线。无网络自动 skip。"""
        try:
            best = TdxClient._pick(list(TdxClient.STD_SERVERS)[:5], 3.0)
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"真实服务器池不可达：{type(e).__name__}: {e}")
        api = TdxClient(host=best[0], port=best[1])
        try:
            df = api.bars("sh.601919", count=5)
            self.assertGreater(len(df), 0)
        finally:
            api.close()


if __name__ == "__main__":
    unittest.main()
