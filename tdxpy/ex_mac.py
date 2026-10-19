# cython: language_level=3
"""MAC EX 通道 API（0x2454 login + EX 帧）：港股/美股/全球指数/国内期货扩展行情。

T0.4-EX 实测结论（2026-10-19，116.205.135.205:7727）：
  - EX 通道 = MAC 帧 + 两处改写：连接后先发 0x2454 login（80B 冻结载荷），
    此后请求帧首字节 0x1C→0x01；服务器要求 login 会话，裸 MAC 帧无响应；
  - 0x122D/122E/122F 载荷与 A 股 MAC 通道完全同构（同头/同记录布局），
    parser 直接继承复用（见 parser/mac/ex_bars.py）；
  - 市场段为 ExMarket id：HK_MAIN=31/US_STOCK=74/SHFE=30/DCE=29/ZCE=28/
    CFFEX=47/HK_INDEX=27…；代码段 gbk 22s（01398/AAPL/RBL8/IFL8…）。
  - 实测数据面：港美股指 K 线/分时/逐笔、国内期货主力 K 线/分时/逐笔全通；
    外盘期货（COMEX/NYMEX/CBOT）与期权市场空板（该服务器未提供数据面）。
"""
import secrets

from tdxpy.base_socket_client import BaseSocketClient
from tdxpy.base_socket_client import last_ack_time
from tdxpy.parser.mac._frame import build_ex_login_frame
from tdxpy.parser.mac.ex_bars import ExBarsCmd
from tdxpy.parser.mac.ex_bars import ExMinuteCmd
from tdxpy.parser.mac.ex_bars import ExTicksCmd
from tdxpy.parser.mac.mac_board import MacBoardListCmd
from tdxpy.parser.mac.mac_ext import MacBatchQuotesCmd
from tdxpy.parser.base import BaseParser


class _ExLoginCmd(BaseParser):
    """0x2454 EX 通道登录（沿用标准 BaseParser 收发路径）。

    「非空即成功」为 2026-10-19 实测假设：正确载荷与全零伪造载荷的响应形态
    相同（299B）且数据面均可用——该服务器不校验 login 载荷内容，login 本质是
    会话激活帧，拒绝响应形态不存在。若未来换服务器后登录失败，先在线重探此假设。
    """

    def setup(self):
        self.send_pkg = build_ex_login_frame()

    def parseResponse(self, body_buf):
        body = bytes(body_buf)
        return len(body) >= 2  # 实测响应 ~299B；空/过短视为登录失败


class TdxMacEx_API(BaseSocketClient):
    """MAC EX 通道客户端（第四连接；与 TdxMac_API 并存，勿混用服务器池）。"""

    def setup(self):
        # EX 通道固定 0x2454 login（不走老三条 SETUP——7727 服务器要求 login 会话）
        ok = self._cmd(_ExLoginCmd).call_api()
        if not ok:
            from tdxpy.exceptions import TdxFunctionCallError

            raise TdxFunctionCallError("EX 通道 login 失败（0x2454 响应为空）")

    def do_heartbeat(self):
        # EX 通道心跳：随机扩展市场的 K 线 count=1（市场段须 ExMarket id——
        # std id 0-2 与 ExMarket 空间不通用，_ex_symbol 同款约束）
        self.get_ex_bars(secrets.choice([31, 27, 74]), "01398", 4, 0, 1)

    @last_ack_time
    def get_ex_bars(self, market, code, category=4, start=0, count=700, adjust=0, times=1):
        """EX K 线（0x122E；market=ExMarket id；参数语义同 TdxMac_API.get_mac_bars）。"""
        cmd = self._cmd(ExBarsCmd)
        cmd.setParams(market, code, category, start, count, adjust, times)

        return cmd.call_api()

    @last_ack_time
    def get_ex_minute(self, market, code, date=0):
        """EX 单日分时（0x122D；date=yyyymmdd，0=今天）。"""
        cmd = self._cmd(ExMinuteCmd)
        cmd.setParams(market, code, date)

        return cmd.call_api()

    @last_ack_time
    def get_ex_ticks(self, market, code, date=0, start=0, count=1000):
        """EX 逐笔成交（0x122F；字段语义差异见 ExTicksCmd docstring）。"""
        cmd = self._cmd(ExTicksCmd)
        cmd.setParams(market, code, date, start, count)

        return cmd.call_api()

    @last_ack_time
    def get_ex_batch_quotes(self, members, bits=None):
        """EX 批量报价（0x122B EX 帧；港美股/指数/期货，≤80 只/页）。

        :param members: (ex_market_id, code) 列表，如 [(31, "01398"), (74, "AAPL")]；
                       代码按服务器侧原样（港股 5 位数字、指数大写如 "HSI"）
        :param bits: 请求字段位（默认 0x00~0x07 八位）
        :return: 行 dict 列表（market/code/name/pre_close/open/high/low/close/vol/
                volume_ratio/amount）。⚠️ EX 侧无行对应承诺——请求 N 只时服务器
                可丢弃无报价成员（实测 6 请求回 5），按实际返回行数为准。
        """
        cmd = self._cmd(MacBatchQuotesCmd)
        cmd.setParams(members, bits=bits, channel="ex")

        return cmd.call_api()

    @last_ack_time
    def get_ex_boards(self, board_type=0, start=0, count=100):
        """港股板块列表（0x1231 EX 帧；板块码 HK0xxx + 领涨股，2026-10-20 实测）。"""
        cmd = self._cmd(MacBoardListCmd)
        cmd.setParams(board_type=board_type, start=start, count=count, channel="ex")

        return cmd.call_api()
