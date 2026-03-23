# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.helper import get_datetime
from tdxpy.parser.base import BaseParser


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
            open_price, high, low, close, position, trade, price = struct.unpack("<ffffIIf", body_buf[pos : pos + 28])
            # EX K 线记录无成交额字段（28 字节 = OHLC + position + trade + price）——
            # 原实现从 pos+16 读 float 实为 position 字节，输出垃圾值（9.76e-42），
            # 2026-09-25 修正为 0 占位（与 EX 日线 reader「无成交额」语义一致）。
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
                    ("amount", 0.0),
                ]
            )

            klines.append(kline)

        return klines
