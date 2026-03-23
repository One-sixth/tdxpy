# cython: language_level=3
from tdxpy.base_socket_client import BaseSocketClient
from tdxpy.base_socket_client import last_ack_time
from tdxpy.constants import HANDSHAKE_MODE
from tdxpy.parser.ext.ex_get_history_instrument_bars_range import GetHistoryInstrumentBarsRange
from tdxpy.parser.ext.ex_get_history_minute_time_data import GetHistoryMinuteTimeData
from tdxpy.parser.ext.ex_get_history_transaction_data import GetHistoryTransactionData
from tdxpy.parser.ext.ex_get_instrument_bars import GetInstrumentBars
from tdxpy.parser.ext.ex_get_instrument_count import GetInstrumentCount
from tdxpy.parser.ext.ex_get_instrument_info import GetInstrumentInfo
from tdxpy.parser.ext.ex_get_instrument_quote import GetInstrumentQuote
from tdxpy.parser.ext.ex_get_instrument_quote_list import GetInstrumentQuoteList
from tdxpy.parser.ext.ex_get_markets import GetMarkets
from tdxpy.parser.ext.ex_get_minute_time_data import GetMinuteTimeData
from tdxpy.parser.ext.ex_get_transaction_data import GetTransactionData
from tdxpy.parser.setup_commands import ExLoginCmd
from tdxpy.parser.setup_commands import ExSetupCmd1

"""
In [7]: 0x7e
Out[7]: 126

In [5]: len(body)
Out[5]: 8066

In [6]: len(body)/126
Out[6]: 64.01587301587301

In [7]: len(body)%126
Out[7]: 2

In [8]: (len(body)-2)/126
Out[8]: 64.0
"""


class TdxExHq_API(BaseSocketClient):  # noqa
    def setup(self):
        if HANDSHAKE_MODE == "legacy":
            self._cmd(ExSetupCmd1).call_api()
        else:
            # 2026+ 现代服务器实测：老 ExSetupCmd1 已失效（服务器不响应，或会话降级导致
            # 后续 EX 命令全部返回空/被踢）。须用新版 login（cmd 0x2454 + 80 字节 body），
            # login 后 EX 全部命令恢复正常，业务命令构包无需改动。
            self._cmd(ExLoginCmd).call_api()

    @last_ack_time
    def get_markets(self):
        """
        获得市场列表
        :return:
        """
        cmd = self._cmd(GetMarkets)
        return cmd.call_api()

    @last_ack_time
    def get_instrument_count(self):
        """
        获取证券数量
        :return:
        """
        cmd = self._cmd(GetInstrumentCount)
        return cmd.call_api()

    @last_ack_time
    def get_instrument_quote(self, market, code):
        """
        获取行情
        :param market:
        :param code:
        :return:
        """
        cmd = self._cmd(GetInstrumentQuote)
        cmd.setParams(market, code)
        return cmd.call_api()

    @last_ack_time
    def get_instrument_bars(self, category, market, code, start=0, count=700):
        """
        获取时间线
        :param category:
        :param market:
        :param code:
        :param start:
        :param count:
        :return:
        """
        cmd = self._cmd(GetInstrumentBars)
        cmd.setParams(category, market, code, start=start, count=count)
        return cmd.call_api()

    @last_ack_time
    def get_minute_time_data(self, market, code):
        """
        分钟线

        :param market:
        :param code:
        :return:
        """
        cmd = self._cmd(GetMinuteTimeData)
        cmd.setParams(market, code)
        return cmd.call_api()

    @last_ack_time
    def get_history_minute_time_data(self, market, code, date):
        """
        分钟线历史
        :param market:
        :param code:
        :param date:
        :return:
        """
        cmd = self._cmd(GetHistoryMinuteTimeData)
        cmd.setParams(market, code, date=date)
        return cmd.call_api()

    @last_ack_time
    def get_transaction_data(self, market, code, start=0, count=1800):
        """
        分笔数据
        :param market:
        :param code:
        :param start:
        :param count:
        :return:
        """
        cmd = self._cmd(GetTransactionData)
        cmd.setParams(market, code, start=start, count=count)
        return cmd.call_api()

    @last_ack_time
    def get_history_transaction_data(self, market, code, date, start=0, count=1800):
        """
        分笔历史数据
        :param market:
        :param code:
        :param date:
        :param start:
        :param count:
        :return:
        """
        cmd = self._cmd(GetHistoryTransactionData)
        cmd.setParams(market, code, date, start=start, count=count)
        return cmd.call_api()

    @last_ack_time
    def get_history_instrument_bars_range(self, market, code, start, end):
        """

        :param market:
        :param code:
        :param start:
        :param end:
        :return:
        """
        cmd = self._cmd(GetHistoryInstrumentBarsRange)
        cmd.setParams(market, code, start, end)
        return cmd.call_api()

    @last_ack_time
    def get_instrument_info(self, start, count=100):
        """

        :param start:
        :param count:
        :return:
        """
        cmd = self._cmd(GetInstrumentInfo)
        cmd.setParams(start, count)
        return cmd.call_api()

    @last_ack_time
    def get_instrument_quote_list(self, market, category, start=0, count=80):
        """

        :param market:
        :param category:
        :param start:
        :param count:
        :return:
        """
        cmd = self._cmd(GetInstrumentQuoteList)
        cmd.setParams(market, category, start, count)
        return cmd.call_api()

    def do_heartbeat(self):
        self.get_instrument_count()
