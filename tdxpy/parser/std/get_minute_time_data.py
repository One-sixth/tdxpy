# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.helper import get_price, get_security_coefficient
from tdxpy.parser.base import BaseParser


class GetMinuteTimeData(BaseParser):

    def __init__(self, client, lock=None):
        super().__init__(client, lock)
        self.coefficient = 0.01

    def setParams(self, market, code):
        """
        设置参数
        :param market: 市场
        :param code: 代码
        """
        # 2026-09-16 父Agent 审查修复：coefficient 判定必须在 encode 之前用原始 str code。
        # 历史 bug：原写法在 encode 之后调用，bytes 经 str() 变为 "b'601919'"，
        # get_security_type 恒抛 NotImplementedError → 恒 fallback 0.01 →
        # ETF(0.001)/转债(0.0001) 分时价格被放大 10/100 倍。
        self.coefficient = get_security_coefficient(market=market, code=code)

        if type(code) is str:
            code = code.encode("utf-8")

        pkg = bytearray.fromhex("0c 1b 08 00 01 01 0e 00 0e 00 1d 05")
        pkg.extend(struct.pack("<H6sI", market, code, 0))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        pos = 0

        (num,) = struct.unpack("<H", body_buf[:2])

        last_price = 0

        pos += 4
        prices = []

        for _ in range(num):
            price_raw, pos = get_price(body_buf, pos)
            reversed1, pos = get_price(body_buf, pos)

            vol, pos = get_price(body_buf, pos)
            last_price = float(last_price + price_raw)

            prices.append(OrderedDict([("price", last_price * self.coefficient), ("vol", vol)]))

        return prices
