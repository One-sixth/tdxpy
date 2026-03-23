# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.constants import HANDSHAKE_MODE
from tdxpy.helper import get_datetime
from tdxpy.helper import get_price
from tdxpy.helper import get_volume
from tdxpy.parser.base import BaseParser


class GetSecurityBarsCmd(BaseParser):
    def setParams(self, category, market, code, start, count, adjust=0):
        """
        构建请求
        :param category:
        :param market:
        :param code:
        :param start:
        :param count:
        :param adjust: 服务端复权模式：0=不复权 1=前复权 2=后复权（仅新协议 42 字节 payload 支持）
        """
        if type(code) is str:
            code = code.encode("utf-8")

        self.category = category

        if HANDSHAKE_MODE == "legacy":
            # pytdx 传统 26 字节 payload：现代服务器（2026-09 实测）已拒绝该格式，返回空数据
            values = (
                0x10C,
                0x01016408,
                0x1C,
                0x1C,
                0x052D,
                market,
                code,
                category,
                1,
                start,
                count,
                0,
                0,
                0,
            )  # I + I +  H total 10 zero

            self.send_pkg = struct.pack("<HIHHHH6sHHHHIIH", *values)
        else:
            # 2026+ 新协议 42 字节 payload（须搭配 NewHandshakeCmd 新式握手）：
            # market(H) code(6s) category(H) parameter(H=1) start(H) count(H) adjust(H) anchor_date(I) + 20 字节 0 保留区
            # 帧内两个 len 字段从 0x1C 改 0x2C。2026-09 真实服务器实测 day/1min/5min 全部正常返回。
            values = (
                0x10C,
                0x01016408,
                0x2C,
                0x2C,
                0x052D,
                market,
                code,
                category,
                1,
                start,
                count,
                adjust,
                0,
            )

            self.send_pkg = struct.pack("<HIHHHH6sHHHHHI", *values) + b"\x00" * 20

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        pos = 0

        (ret_count,) = struct.unpack("<H", body_buf[0:2])
        pos += 2

        klines = []

        pre_diff_base = 0

        for _ in range(ret_count):
            year, month, day, hour, minute, pos = get_datetime(self.category, body_buf, pos)

            price_open_diff, pos = get_price(body_buf, pos)
            price_close_diff, pos = get_price(body_buf, pos)

            price_high_diff, pos = get_price(body_buf, pos)
            price_low_diff, pos = get_price(body_buf, pos)

            (vol_raw,) = struct.unpack("<I", body_buf[pos : pos + 4])
            vol = get_volume(vol_raw)

            pos += 4

            (db_vol_raw,) = struct.unpack("<I", body_buf[pos : pos + 4])
            db_vol = get_volume(db_vol_raw)

            pos += 4

            open_ = self._cal_price1000(price_open_diff, pre_diff_base)
            price_open_diff = price_open_diff + pre_diff_base

            close = self._cal_price1000(price_open_diff, price_close_diff)
            high = self._cal_price1000(price_open_diff, price_high_diff)
            low = self._cal_price1000(price_open_diff, price_low_diff)

            pre_diff_base = price_open_diff + price_close_diff

            # 为了避免python处理浮点数的时候，浮点数运算不精确问题，这里引入了多余的代码
            kline = OrderedDict(
                [
                    ("open", open_),
                    ("close", close),
                    ("high", high),
                    ("low", low),
                    ("vol", vol),
                    ("amount", db_vol),
                    ("year", year),
                    ("month", month),
                    ("day", day),
                    ("hour", hour),
                    ("minute", minute),
                    ("datetime", "%d-%02d-%02d %02d:%02d" % (year, month, day, hour, minute)),
                ]
            )

            klines.append(kline)

        return klines
