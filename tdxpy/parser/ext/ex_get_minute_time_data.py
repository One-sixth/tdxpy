# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.parser.base import BaseParser


class GetMinuteTimeData(BaseParser):
    def setParams(self, market, code):
        """

        :param market:
        :param code:
        """
        pkg = bytearray.fromhex("01 07 08 00 01 01 0c 00 0c 00 0b 24")
        pkg.extend(struct.pack("<B9s", market, code.encode("utf-8")))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        pos = 0

        market, code, num = struct.unpack("<B9sH", body_buf[pos : pos + 12])
        pos += 12

        result = []

        for _ in range(num):
            raw_time, price, avg_price, volume, amount = struct.unpack("<HffII", body_buf[pos : pos + 18])
            pos += 18

            # raw_time = 自 0 点起的连续分钟数，跨日不回绕（美股夜盘 21:30~次日 03:59
            # → 1290~1679 分钟，hour 21~27；期货夜盘同理）。2026-11-04 吸收自
            # pytdxdata 0.6.5：hour 取模归一化 + day_offset 记录跨日偏移
            # （A 股/港股正常时段 day_offset 恒 0，行为向后兼容）。
            day_offset, hour = divmod(raw_time // 60, 24)
            minute = raw_time % 60

            result.append(
                OrderedDict(
                    [
                        ("hour", hour),
                        ("minute", minute),
                        ("day_offset", day_offset),
                        ("price", price),
                        ("avg_price", avg_price),
                        ("volume", volume),
                        ("open_interest", amount),
                    ]
                )
            )

        return result
