# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.helper import get_datetime
from tdxpy.parser.base import BaseParser

#: 权益类 EX 市场（28B 记录第 5 字段 = float 成交额；2026-11-04 吸收自 pytdxdata 0.7.0 实测）：
#: 港股主板 31 / 香港创业板 48 / 香港基金 49 / 新三板 44 / 美股 74。
#: 自检恒等式：amount/(close×trade) ≈ 100.0（港股主板/基金 vol 单位=手）或 ≈ 1.0
#: （创业板/新三板/美股 vol 单位=股）。其余市场（期货/期权类）第 5 字段 = int 持仓量。
#: UK_STOCK=75 为 tdxpy 独有登记（pytdxdata 未覆盖），未列入，暂按期货类解析待实测定性。
_AMOUNT_MARKETS = frozenset({31, 48, 49, 44, 74})


class GetInstrumentBars(BaseParser):
    def setParams(self, category, market, code, start, count):
        """

        :param category:
        :param market:
        :param code:
        :param start:
        :param count:
        """
        if type(code) is str:
            code = code.encode("utf-8")

        pkg = bytearray.fromhex("01 01 08 6a 01 01 16 00 16 00")
        pkg.extend(bytearray.fromhex("ff 23"))

        self.category = category
        self.market = market
        pkg.extend(struct.pack("<B9sHHIH", market, code, category, 1, start, count))

        # 这个1还不确定是什么作用，疑似和是否复权有关
        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        pos = 0
        pos += 18

        (ret_count,) = struct.unpack("<H", body_buf[pos : pos + 2])
        pos += 2

        klines = []

        for _ in range(ret_count):
            year, month, day, hour, minute, pos = get_datetime(self.category, body_buf, pos)

            if self.market in _AMOUNT_MARKETS:
                # 权益类（港股/创业板/港基/新三板/美股）：第 5 字段 = float 成交额
                open_price, high, low, close, amount, trade, price = struct.unpack(
                    "<fffffIf", body_buf[pos : pos + 28]
                )
                position = 0.0  # 权益类无持仓概念，占位保持列结构
            else:
                # 期货/期权类：第 5 字段 = int 持仓量；price 字段实为结算价（settle）
                open_price, high, low, close, position, trade, price = struct.unpack(
                    "<ffffIIf", body_buf[pos : pos + 28]
                )
                amount = 0.0  # 期货类 28B 记录无成交额字段（0 占位，语义与 EX 日线 reader 一致）

            # 28B 记录仅 offset 16-19 语义按市场分两类（权益=float 成交额 / 期货=int 持仓），
            # 其余字节布局两类完全一致（2026-11-04 勘误：此前"EX K 线无成交额"结论
            # 对权益类不成立——原实现把 amount 字节按 I 读成 position 垃圾值）。
            pos += 28

            kline = OrderedDict(
                [
                    ("open", open_price),
                    ("high", high),
                    ("low", low),
                    ("close", close),
                    ("position", position),
                    ("trade", trade),
                    ("price", price),
                    ("year", year),
                    ("month", month),
                    ("day", day),
                    ("hour", hour),
                    ("minute", minute),
                    ("datetime", "%d-%02d-%02d %02d:%02d" % (year, month, day, hour, minute)),
                    ("amount", amount),
                ]
            )

            klines.append(kline)

        return klines
