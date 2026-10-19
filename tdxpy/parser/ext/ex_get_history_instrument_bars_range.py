# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.parser.base import BaseParser
from tdxpy.parser.ext.ex_get_instrument_bars import _AMOUNT_MARKETS


class GetHistoryInstrumentBarsRange(BaseParser):
    def __init__(self, *args, **kvargs):
        # 修复：原写法 super().__init__(self, *args, **kvargs) 将 self 重复传入，
        # 导致 BaseParser.client 被错绑为 parser 自身而非 socket client，
        # call_api 时无法发送/接收（2026-09-16 实测该命令恒返回空的原因之一）。
        super().__init__(*args, **kvargs)
        self.seq_id = 1

    def setParams(self, market, code, date, date2):
        """

        :param market:
        :param code:
        :param date:
        :param date2:
        """
        pkg = bytearray.fromhex("01")
        pkg.extend(struct.pack("<B", self.seq_id))
        pkg.extend(bytearray.fromhex("38 92 00 01 16 00 16 00 0D 24"))

        pkg.extend(struct.pack("<B9s", market, code.encode("utf-8")))
        pkg.extend(bytearray.fromhex("07 00"))
        pkg.extend(struct.pack("<LL", date, date2))

        self.market = market
        self.send_pkg = pkg
        self.seq_id += 1

    @staticmethod
    def _parse_date(num):
        """

        :param num:
        :return:
        """
        month = (num % 2048) // 100
        year = num // 2048 + 2004
        day = (num % 2048) % 100

        return year, month, day

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        klines = []  # noqa
        pos = 12

        # 算了，前面不解析了，没太大用
        # (market, code) = struct.unpack("<B9s", body_buf[0: 10]

        (ret_count,) = struct.unpack("H", body_buf[pos : pos + 2])
        pos = pos + 2

        for _ in range(ret_count):
            (d1, d2, open_price, high, low, close, position, trade, settlementprice) = struct.unpack(
                "<HHffffIIf", body_buf[pos : pos + 32]
            )  # noqa

            # 24B 数据段与 0x23FF 的 28B 记录逐字节同构（前缀仅日期/时间拆分方式不同），
            # 第 5 字段同样按市场分型（2026-11-05 交叉复审发现：在线恒等式实锤——
            # us.AAPL amount/(close×trade)≈1.0、hk.01398 ≈100.0（vol=手））。
            if self.market in _AMOUNT_MARKETS:
                # 32B = d1(2) + d2(2) + 24B 数据段；slot5 在数据段 offset 16 = 记录 offset 20
                (amount,) = struct.unpack("<f", body_buf[pos + 20 : pos + 24])
                position = 0.0  # 权益类无持仓概念，占位保持列结构
            else:
                amount = 0.0  # 期货/期权类：第 5 字段 = 持仓量（协议帧无成交额）

            pos += 32

            year, month, day = self._parse_date(d1)
            # d2 与 0x0B24/0x0C24 的 raw_time 同语义（自 0 点起分钟数，跨日不回绕）：
            # 美股 1 分钟线凌晨段 hour 会达 24~27，归一化 + day_offset（2026-11-05）
            day_offset, hour = divmod(d2 // 60, 24)
            minute = d2 % 60
            kline = OrderedDict(
                [
                    ("datetime", "%d-%02d-%02d %02d:%02d" % (year, month, day, hour, minute)),
                    ("year", year),
                    ("month", month),
                    ("day", day),
                    ("hour", hour),
                    ("minute", minute),
                    ("day_offset", day_offset),
                    ("open", open_price),
                    ("high", high),
                    ("low", low),
                    ("close", close),
                    ("position", position),
                    ("trade", trade),
                    ("settlementprice", settlementprice),  # noqa
                    ("amount", amount),
                ]
            )

            klines.append(kline)

        return klines
