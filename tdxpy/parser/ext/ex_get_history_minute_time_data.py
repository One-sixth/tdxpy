# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.parser.base import BaseParser


class GetHistoryMinuteTimeData(BaseParser):
    def setParams(self, market, code, date):
        pkg = bytearray.fromhex("01 01 30 00 01 01 10 00 10 00 0c 24")
        pkg.extend(struct.pack("<IB9s", date, market, code.encode("utf-8")))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        pos = 0

        market, code, _, num = struct.unpack("<B9s8sH", body_buf[pos : pos + 20])

        pos += 20

        result = []

        for _ in range(num):
            raw_time, price, avg_price, volume, amount = struct.unpack("<HffII", body_buf[pos : pos + 18])

            pos += 18

            # raw_time = 自 0 点起的连续分钟数，跨日不回绕（美股夜盘 21:30~次日 04:59
            # → hour 22~28）。与 0x0B24（ex_get_minute_time_data）同型修复（2026-11-05
            # 交叉复审发现）：hour 取模归一化 + day_offset 跨日偏移；A 股/港股正常时段
            # day_offset 恒 0，行为向后兼容。
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
