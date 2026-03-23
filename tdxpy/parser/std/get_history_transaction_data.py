# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.helper import get_price, get_security_coefficient, get_time
from tdxpy.parser.base import BaseParser
from tdxpy.logger import logger


class GetHistoryTransactionData(BaseParser):

    def __init__(self, client, lock=None):
        super().__init__(client, lock)
        self.coefficient = 0.01

    def setParams(self, market, code, start, count, date):
        """

        :param market:
        :param code:
        :param start:
        :param count:
        :param date:
        """
        # 2026-10-19 修复：coefficient 判定必须在 encode 之前用原始 str code。
        # 同 get_minute_time_data 的历史 bug 修法——原写法硬编码 /100（A 股 0.01），
        # ETF(0.001)/转债(0.0001) 分笔价格被放大 10/100 倍。
        self.coefficient = get_security_coefficient(market=market, code=code)

        if type(code) is str:
            code = code.encode("utf-8")

        # date 传 str/bytes 时转 int（YYYYMMDD，int() 直接接受 bytes 数字串）。
        # 旧写法 `if type(date) is (type(date) is str) or (type(date) is bytes)`
        # 是类型与 bool 的比较，str 分支永不触发（2026-10-19 清理死代码）。
        if type(date) is str or type(date) is bytes:
            date = int(date)

        pkg = bytearray.fromhex("0c 01 30 01 00 01 12 00 12 00 b5 0f")
        pkg.extend(struct.pack("<IH6sHH", date, market, code, start, count))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        pos = 0
        (num,) = struct.unpack("<H", body_buf[:2])
        logger.debug(f'num => {num}')

        pos += 2
        ticks = []

        # skip 4 bytes
        pos += 4
        last_price = 0

        for _ in range(num):
            hour, minute, pos = get_time(body_buf, pos)  # noqa

            price_raw, pos = get_price(body_buf, pos)
            vol, pos = get_price(body_buf, pos)

            buy_or_sell, pos = get_price(body_buf, pos)
            _, pos = get_price(body_buf, pos)

            last_price = last_price + price_raw

            tick = OrderedDict(
                [
                    ("time", "%02d:%02d" % (hour, minute)),
                    ("price", float(last_price) * self.coefficient),
                    ("vol", vol),
                    ("buyorsell", buy_or_sell),
                ]
            )

            logger.debug(f'tick => {tick}')
            ticks.append(tick)

        return ticks
