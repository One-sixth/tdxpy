# cython: language_level=3
"""MAC 通道 API（0x12xx 命令族）：老三条 SETUP 握手 + MAC 帧 + MAC 服务器池。

T0.4 实测结论（2026-10-19）：
  - MAC 数据面只活在「老三条 SETUP 会话 + MAC 专用服务器」组合上；
    tdxpy 标准池服务器与 0x000D 新式握手会话下 MAC 帧均无响应。
  - 响应帧头 16B 与标准同构（magic 0x0074CBB1），BaseParser 收发复用；
    请求帧为 10B MAC 头（flag 0x1C），由 parser/mac/_frame.py 构造。
"""
import secrets

from tdxpy.base_socket_client import BaseSocketClient
from tdxpy.base_socket_client import last_ack_time
from tdxpy.parser.mac.mac_board import MacBoardListCmd
from tdxpy.parser.mac.mac_bars import MacBarsCmd
from tdxpy.parser.mac.mac_ext import MacAuctionCmd
from tdxpy.parser.mac.mac_ext import MacBatchQuotesCmd
from tdxpy.parser.mac.mac_ext import MacBelongBoardCmd
from tdxpy.parser.mac.mac_ext import MacBoardMembersCmd
from tdxpy.parser.mac.mac_ext import MacCapitalFlowCmd
from tdxpy.parser.mac.mac_ext import MacSymbolInfoCmd
from tdxpy.parser.mac.mac_ext import MacUnusualCmd
from tdxpy.parser.mac.mac_minute import MacMinuteCmd
from tdxpy.parser.mac.mac_ticks import MacTicksCmd
from tdxpy.parser.setup_commands import SetupCmd1
from tdxpy.parser.setup_commands import SetupCmd2
from tdxpy.parser.setup_commands import SetupCmd3


class TdxMac_API(BaseSocketClient):
    """MAC 通道客户端（独立连接；与 TdxHq_API 并存，勿混用服务器池）。"""

    def setup(self):
        # MAC 通道固定老三条 SETUP（新式 0x000D 握手会话下 MAC 帧无响应——T0.4 实测）
        self._cmd(SetupCmd1).call_api()
        self._cmd(SetupCmd2).call_api()
        self._cmd(SetupCmd3).call_api()

    def do_heartbeat(self):
        # MAC 通道没有标准轻量命令可安全当心跳（标准命令在 MAC 专用机上返回空），
        # 用随机参数的 K 线查询（count=1，开销极小）
        self.get_mac_bars(secrets.randbelow(2), "600000", 4, 0, 1)

    @last_ack_time
    def get_mac_bars(self, market, code, category=4, start=0, count=700, adjust=0, times=1):
        """MAC K 线（0x122E；支持复权，含 amount；≤700/页）。

        :param times: N 分钟参数（category=8 时生效：times=N → N 分钟 bar，
                      2026-10-19 在线实测 3/5/15/30 全通——参数化周期钩子）
        """
        cmd = self._cmd(MacBarsCmd)
        cmd.setParams(market, code, category, start, count, adjust, times)

        return cmd.call_api()

    @last_ack_time
    def get_mac_minute(self, market, code, date=0):
        """MAC 单日分时（0x122D；date=yyyymmdd，0=今天；240 点含均价）。"""
        cmd = self._cmd(MacMinuteCmd)
        cmd.setParams(market, code, date)

        return cmd.call_api()

    @last_ack_time
    def get_mac_ticks(self, market, code, date=0, start=0, count=2000):
        """MAC 逐笔成交（0x122F；秒级精度，含成交笔数与买卖方向）。"""
        cmd = self._cmd(MacTicksCmd)
        cmd.setParams(market, code, date, start, count)

        return cmd.call_api()

    @last_ack_time
    def get_mac_boards(self, board_type=0, start=0, count=100):
        """板块列表（0x1231；A 股板块码 881xxx + 领涨股，2026-10-20 实测）。

        :param board_type: 板块类别（实测 0=全部 total=128 / 1=行业 total=345）
        """
        cmd = self._cmd(MacBoardListCmd)
        cmd.setParams(board_type=board_type, start=start, count=count, channel="mac")

        return cmd.call_api()

    @last_ack_time
    def get_mac_board_members(self, board_code, sort_type=0, start=0, count=80, bits=None):
        """板块成分报价（0x122C；board_code '881248' 或换算后 int，跨市场成分）。

        :param bits: 请求字段位（默认 昨收/开/高/低/收/量/量比/额 8 位）
        """
        cmd = self._cmd(MacBoardMembersCmd)
        cmd.setParams(board_code, sort_type=sort_type, start=start, count=count, bits=bits)

        return cmd.call_api()

    @last_ack_time
    def get_mac_auction(self, market, code, start=0, count=100):
        """集合竞价逐笔（0x123D；09:15-09:25 3 秒一条，末条价格=开盘价）。"""
        cmd = self._cmd(MacAuctionCmd)
        cmd.setParams(market, code, start=start, count=count)

        return cmd.call_api()

    @last_ack_time
    def get_mac_unusual(self, market=0, start=0, count=600):
        """市场异动列表（0x1237；count≤600/页；price/amount 为触发时点快照）。"""
        cmd = self._cmd(MacUnusualCmd)
        cmd.setParams(market=market, start=start, count=count)

        return cmd.call_api()

    @last_ack_time
    def get_mac_snapshot(self, market, code):
        """个股特征快照（0x122A；OHLC/量额与 std 一致，另含内外盘/activity）。"""
        cmd = self._cmd(MacSymbolInfoCmd)
        cmd.setParams(market, code)

        return cmd.call_api()

    @last_ack_time
    def get_mac_belong_boards(self, market, code):
        """个股所属板块（0x1218；GBK JSON，含板块码 880/881xxx + 板块指数点位）。"""
        cmd = self._cmd(MacBelongBoardCmd)
        cmd.setParams(market, code)

        return cmd.call_api()

    @last_ack_time
    def get_mac_capital_flow(self, market, code):
        """个股资金流（0x1218 + 报表名 Stock_ZJLX；当日主力/散户四值 + 5 日六值）。"""
        cmd = self._cmd(MacCapitalFlowCmd)
        cmd.setParams(market, code)

        return cmd.call_api()

    @last_ack_time
    def get_mac_batch_quotes(self, stocks, bits=None):
        """批量自定义字段位报价（0x122B；≤80 只/页，请求位图 20B，2026-10-21 复活实测）。

        :param stocks: (market, code) 列表
        :param bits: 字段位（默认 昨收/开/高/低/收/量/量比/额 八位）
        """
        cmd = self._cmd(MacBatchQuotesCmd)
        cmd.setParams(stocks, bits=bits)

        return cmd.call_api()
