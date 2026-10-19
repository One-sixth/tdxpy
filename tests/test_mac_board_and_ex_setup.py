# -*- coding: utf-8 -*-
"""板块列表 0x1231 + EX setup 双握手单测（2026-10-20 批次四）。

fixture = 真实服务器响应存档（tests/fixtures/mac_board_1231_*.bin，2026-10-20 抓取）：
- mac_board_1231_industry3.bin  MAC 通道 0x1231 行业板块 3 条（121.37.207.165:7709，
  板块 881314 机器人 + 领涨股 688836 宇树科技，total=345）
- mac_board_1231_hk3.bin        MAC EX 通道 0x1231 港股板块 3 条（116.205.135.205:7727，
  板块 HK0232 + 领涨股 08250，total=160）

覆盖：双通道帧构造（flag 0x1C/0x01）、解析（头 4B + 160B/条）、坏帧守卫、
EX setup 握手按服务器自动选择、ExMarket 新成员（22/75/105）、在线冒烟（网络不可达自动 skip）。
运行：python tests/test_mac_board_and_ex_setup.py -v
"""
import struct
import unittest
from pathlib import Path

from tdxpy.exhq import EX_SETUP_SERVERS
from tdxpy.exhq import TdxExHq_API
from tdxpy.mac import TdxMac_API  # noqa: F401（保证模块可导入）
from tdxpy.markets import ExMarket
from tdxpy.markets import EX_MARKET_NAMES
from tdxpy.parser.mac._frame import build_ex_frame
from tdxpy.parser.mac._frame import build_mac_frame
from tdxpy.parser.mac.mac_board import MacBoardListCmd
from tdxpy.parser.setup_commands import ExLoginCmd
from tdxpy.parser.setup_commands import ExSetupCmd1

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeClient:
    """parser 构造只需占位（单测直接调 parseResponse，不走 socket）。"""


class TestMacBoardRequest(unittest.TestCase):
    """0x1231 请求帧构造（双通道）。"""

    def test_mac_request_layout(self):
        """MAC 通道：flag 0x1C + 20B body（page_size/board_type/0/0/start/1 + 8x pad）。"""
        cmd = MacBoardListCmd(_FakeClient())
        cmd.setParams(board_type=1, start=0, count=3, channel="mac")
        frame = cmd.send_pkg
        self.assertEqual(frame[0], 0x1C)
        (inner_cmd,) = struct.unpack_from("<H", frame, 10)
        self.assertEqual(inner_cmd, 0x1231)
        # 与 2026-10-20 真实抓包逐字节一致（tmp_tdx_probe/mac_board_a_req.hex）
        self.assertEqual(
            frame.hex(),
            "1c0000000001140014003112030001000000000001000000000000000000",
        )

    def test_ex_request_layout(self):
        """EX 通道：同载荷仅 flag 0x1C→0x01（含 cmd 字节锁定——交叉复审补）。"""
        cmd = MacBoardListCmd(_FakeClient())
        cmd.setParams(board_type=0, start=5, count=2, channel="ex")
        frame = cmd.send_pkg
        self.assertEqual(frame[0], 0x01)
        (inner_cmd,) = struct.unpack_from("<H", frame, 10)
        self.assertEqual(inner_cmd, 0x1231)  # 交叉复审 P2：EX 侧也锁 cmd（篡改 0x1232 必须被抓）
        (count_, board_type, b1, b2, start_, tail) = struct.unpack_from("<HHBBHH", frame, 12)
        self.assertEqual((count_, board_type, b1, b2, start_, tail), (2, 0, 0, 0, 5, 1))

    def test_default_page_cap(self):
        from tdxpy.parser.mac.mac_board import MAC_BOARD_PAGE

        self.assertLessEqual(MAC_BOARD_PAGE, 150)  # 实测一页 150 可通


class TestMacBoardParse(unittest.TestCase):
    """0x1231 解析（fixture = 双通道真实响应）。"""

    @classmethod
    def setUpClass(cls):
        cls.a_rows = MacBoardListCmd(_FakeClient()).parseResponse(
            (FIXTURES / "mac_board_1231_industry3.bin").read_bytes())
        cls.hk_rows = MacBoardListCmd(_FakeClient()).parseResponse(
            (FIXTURES / "mac_board_1231_hk3.bin").read_bytes())

    def test_a_share_rows(self):
        self.assertEqual(len(self.a_rows), 3)
        first = self.a_rows[0]
        self.assertEqual(first["code"], "881314")
        self.assertEqual(first["name"], "机器人")
        self.assertEqual(first["market"], 1)
        self.assertEqual(first["total"], 345)  # 行业板块总数（u16 头部）
        self.assertEqual(first["leader_code"], "688836")
        self.assertEqual(first["leader_name"], "宇树科技")
        self.assertGreater(first["price"], 0)
        self.assertAlmostEqual(first["leader_price"], 495.92, places=2)

    def test_hk_rows(self):
        self.assertEqual(len(self.hk_rows), 3)
        first = self.hk_rows[0]
        self.assertEqual(first["market"], 70)  # 港股板块市场 id
        self.assertEqual(first["code"], "HK0232")
        self.assertEqual(first["total"], 160)
        self.assertEqual(first["leader_market"], 48)  # 港股创业板
        self.assertEqual(first["leader_code"], "08250")

    def test_short_body_guard(self):
        cmd = MacBoardListCmd(_FakeClient())
        cmd.setParams(0, 0, 3)
        self.assertEqual(cmd.parseResponse(b""), [])
        self.assertEqual(cmd.parseResponse(b"\x01\x02"), [])

    def test_truncated_tail(self):
        """尾部残条：只解析完整 160B 记录，不抛异常（残尾有 warning——交叉复审 P2 对齐策略）。"""
        import io
        import logging

        raw = (FIXTURES / "mac_board_1231_industry3.bin").read_bytes()
        cmd = MacBoardListCmd(_FakeClient())
        cmd.setParams(0, 0, 3)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        tdx_logger = logging.getLogger("tdxpy")
        tdx_logger.addHandler(handler)
        old_level = tdx_logger.level
        tdx_logger.setLevel(logging.WARNING)
        try:
            rows = cmd.parseResponse(raw + b"\x99" * 77)
        finally:
            tdx_logger.removeHandler(handler)
            tdx_logger.setLevel(old_level)
        self.assertEqual(len(rows), 3)
        self.assertIn("非 160 倍数", stream.getvalue())  # 残尾必须告警（坏帧信号不静默）

    def test_leader_fields_complete(self):
        keys = {"leader_market", "leader_code", "leader_name",
                "leader_price", "leader_rise_speed", "leader_pre_close"}
        self.assertTrue(keys.issubset(self.a_rows[0].keys()))


class _StubExHq(TdxExHq_API):
    """绕过 BaseSocketClient.__init__ 的握手选择桩：记录 _cmd 选中的命令类。"""

    def __init__(self):
        self.ip = None
        self.picked = []

    def _cmd(self, cmd_cls):
        self.picked.append(cmd_cls)

        class _Fake:
            def call_api(self):
                return True

        return _Fake()


class TestExSetupHandshakeSelection(unittest.TestCase):
    """EX 握手按服务器自动选择（setup 清单 → 92B setup 帧；其余 → 0x2454 login）。"""

    def test_setup_servers_content(self):
        self.assertEqual(EX_SETUP_SERVERS, frozenset({"116.205.143.214", "103.221.142.82"}))

    def test_setup_server_gets_setup_cmd(self):
        api = _StubExHq()
        api.ip = "116.205.143.214"
        api.setup()
        self.assertEqual(api.picked, [ExSetupCmd1])

    def test_guotai_server_gets_setup_cmd(self):
        api = _StubExHq()
        api.ip = "103.221.142.82"
        api.setup()
        self.assertEqual(api.picked, [ExSetupCmd1])

    def test_login_server_gets_login_cmd(self):
        from tdxpy.constants import HANDSHAKE_MODE

        if HANDSHAKE_MODE == "legacy":  # 环境变量强制 legacy 时全走 setup，跳过本断言
            self.skipTest("TDXPY_HANDSHAKE_MODE=legacy")
        api = _StubExHq()
        api.ip = "139.9.191.175"
        api.setup()
        self.assertEqual(api.picked, [ExLoginCmd])

    def test_ex_setup_cmd_frame(self):
        """92B setup 帧原样性：与 pytdxdata EX_SETUP_CMD 逐字节一致（回归防线）。"""
        cmd = ExSetupCmd1(_FakeClient())
        cmd.setup()
        self.assertEqual(len(cmd.send_pkg), 92)
        expected = ("010148650001520052005424" + "1f32c6e5d53dfb41" * 8
                    + "cce16dffd5ba3fb8cbc57a054f7748ea").upper()
        self.assertEqual(cmd.send_pkg.hex().upper(), expected)


class TestExMarketNewMembers(unittest.TestCase):
    """setup 服务器扩容实测新增的 ExMarket 成员（22/75/105）。"""

    def test_resolve_by_alias(self):
        self.assertEqual(int(ExMarket("uk")), 75)
        self.assertEqual(int(ExMarket("英国")), 75)
        self.assertEqual(int(ExMarket("hk_bond")), 22)
        self.assertEqual(int(ExMarket(105)), 105)

    def test_names_registered(self):
        self.assertEqual(EX_MARKET_NAMES[22], "香港债券")
        self.assertEqual(EX_MARKET_NAMES[75], "英国股票")
        self.assertEqual(EX_MARKET_NAMES[105], "更多其它指数")


class TestExServersPool(unittest.TestCase):
    """EX_SERVERS 池扩容：setup 服务器两台已纳池。"""

    def test_setup_hosts_in_pool(self):
        from tdxpy.client import EX_SERVERS

        ips = {ip for ip, _port in EX_SERVERS}
        self.assertTrue({"116.205.143.214", "103.221.142.82"} <= ips)
        self.assertGreaterEqual(len(EX_SERVERS), 5)
        for ip, port in EX_SERVERS:
            self.assertEqual(port, 7727)


class _RecorderCmd:
    """记录 setParams 收到的参数；call_api 回放桩数据。"""

    store = {}

    def __init__(self, cls):
        _RecorderCmd.store = {"cls": cls}

    def setParams(self, *args, **kwargs):
        _RecorderCmd.store["args"] = args
        _RecorderCmd.store["kwargs"] = kwargs
        return self

    def call_api(self):
        _RecorderCmd.store["called"] = True
        return [dict(r) for r in _RecorderCmd._ROWS]

    _ROWS = [
        {"market": 1, "code": "881314", "name": "机器人", "total": 345,
         "leader_market": 1, "leader_code": "688836", "leader_name": "宇树科技",
         "leader_price": 495.92, "leader_rise_speed": 0.0, "leader_pre_close": 426.98,
         "price": 1180.87, "rise_speed": 0.0, "pre_close": 1127.42},
        {"market": 1, "code": "881287", "name": "地面兵装", "total": 345,
         "leader_market": 0, "leader_code": "000576", "leader_name": "甘化科工",
         "leader_price": 9.65, "leader_rise_speed": 0.0, "leader_pre_close": 8.77,
         "price": 1068.62, "rise_speed": 0.0, "pre_close": 1020.8},
    ]


class TestBoardApiWiring(unittest.TestCase):
    """API 层接线：get_mac_boards / get_ex_boards 的命令类选择与参数透传（离线桩）。"""

    def _stub_api(self, api_cls):
        api = api_cls.__new__(api_cls)  # 绕过 BaseSocketClient.__init__（不走 socket）
        api._cmd = _RecorderCmd
        return api

    def test_mac_api_wiring(self):
        from tdxpy.mac import TdxMac_API

        api = self._stub_api(TdxMac_API)
        rows = api.get_mac_boards(board_type=2, start=7, count=9)
        self.assertIs(_RecorderCmd.store["cls"], MacBoardListCmd)
        self.assertEqual(_RecorderCmd.store["kwargs"],
                         {"board_type": 2, "start": 7, "count": 9, "channel": "mac"})
        self.assertEqual(_RecorderCmd.store["args"], ())
        self.assertEqual(rows[0]["code"], "881314")

    def test_ex_api_wiring(self):
        from tdxpy.ex_mac import TdxMacEx_API

        api = self._stub_api(TdxMacEx_API)
        rows = api.get_ex_boards()  # 默认参数
        self.assertIs(_RecorderCmd.store["cls"], MacBoardListCmd)
        self.assertEqual(_RecorderCmd.store["kwargs"],
                         {"board_type": 0, "start": 0, "count": 100, "channel": "ex"})
        self.assertEqual(len(rows), 2)


class TestBoardFacade(unittest.TestCase):
    """门面层路由：mac_boards/mac_ex_boards 走对连接、参数透传、返回 DataFrame（离线桩）。"""

    def _stub_client(self):
        from tdxpy.client import TdxClient

        c = TdxClient.__new__(TdxClient)
        calls = []

        def fake_mac(name, *args):
            calls.append(("mac", name, args))
            return _RecorderCmd._ROWS

        def fake_mac_ex(name, *args):
            calls.append(("mac_ex", name, args))
            return _RecorderCmd._ROWS

        c._mac_call = fake_mac
        c._mac_ex_call = fake_mac_ex
        return c, calls

    def test_mac_boards_routes_to_mac_channel(self):
        c, calls = self._stub_client()
        df = c.mac_boards(1, 0, 5)
        self.assertEqual(calls, [("mac", "get_mac_boards", (1, 0, 5))])
        self.assertEqual(len(df), 2)
        for col in ("code", "name", "price", "leader_code", "leader_name", "total"):
            self.assertIn(col, df.columns)

    def test_mac_ex_boards_routes_to_mac_ex_channel(self):
        c, calls = self._stub_client()
        df = c.mac_ex_boards()
        self.assertEqual(calls, [("mac_ex", "get_ex_boards", (0, 0, 100))])
        self.assertEqual(len(df), 2)

    def test_empty_rows_give_empty_df(self):
        c, _calls = self._stub_client()
        c._mac_call = lambda name, *a: []
        df = c.mac_boards()
        self.assertEqual(len(df), 0)


class TestExSetupServerOnlineSmoke(unittest.TestCase):
    """在线冒烟：setup 服务器握手 + 市场清单 + 外盘期货日线（网络不可达自动 skip）。"""

    SETUP_HOST = "116.205.143.214"

    @classmethod
    def setUpClass(cls):
        try:
            api = TdxExHq_API()
            ok = api.connect(cls.SETUP_HOST, 7727, time_out=8)
        except Exception as e:  # noqa
            raise unittest.SkipTest(f"无法连接 {cls.SETUP_HOST}:7727: {type(e).__name__}: {e}")
        if not ok:
            raise unittest.SkipTest(f"无法连接 {cls.SETUP_HOST}:7727")
        cls.api = api

    @classmethod
    def tearDownClass(cls):
        try:
            cls.api.disconnect()
        except Exception:  # noqa
            pass

    def test_markets_from_setup_server(self):
        markets = self.api.get_markets()
        self.assertIsNotNone(markets)
        self.assertGreaterEqual(len(markets), 50)  # 实测 52 市场

    def test_comex_daily_bars(self):
        """外盘期货金矿回归：COMEX（market 16）日线有数据（2026-10-20 实测 live）。"""
        bars = self.api.get_instrument_bars(4, 16, "EHR00W", 0, 3)
        self.assertIsNotNone(bars)
        self.assertGreater(len(bars), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
