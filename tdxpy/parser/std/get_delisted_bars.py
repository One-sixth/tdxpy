# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.helper import get_price, get_volume
from tdxpy.parser.base import BaseParser

# 请求帧头（12B，与 0x051D 同族新式帧：prefix + msg_id u32 + control + len u16×2 + cmd u16）
#   len 字段 = data 长度 + 2（cmd 占 2B），见 eltdx RequestFrame::encode 同构布局
_DELISTED_FRAME_HEAD = bytearray.fromhex("0c 1b 08 00 01 01 0e 00 0e 00 2b 05")


class GetDelistedBarsCmd(BaseParser):
    """退市股不复权日线（0x052B，自 eltdx delisted_bars.rs 移植，字节布局经其离线夹具逐字节验证）。

    请求 data：[market u16le][code 6s ASCII][start u16le][count u16le]
      start 语义与 0x052D 一致：0=最新一根，向前（更老方向）翻页递增；
      返回的 count 根内部按日期升序（eltdx 夹具 start=0 count=20 = 最后 20 根，实测同）。
    响应：[count u16le] + N 条变长记录（与 0x052D 同族的差分累积布局）：
      [date u32le yyyymmdd][open_delta varint][close_delta varint][high_delta varint]
      [low_delta varint][vol varint（股）][amount u32le][4B 未知后缀]
    差分累积（milli 口径）：open=前收+open_delta，close=open+close_delta，
      high/low=open+high/low_delta；首根前收=0（open_delta 即绝对价）。
    输出：价格 /1000 元；vol 股 → /100 手；amount 经 get_volume 浮点解码（元）。
    """

    def setParams(self, market, code, start=0, count=800):
        """
        :param market: 市场（0=SZ 1=SH 2=BJ）
        :param code: 6 位数字代码
        :param start: 起始偏移（0=最早一根，正序向后）
        :param count: 条数（≤800）
        """
        if type(code) is str:
            code = code.encode("utf-8")

        data = struct.pack("<H6sHH", market, code, start, count)

        pkg = bytearray(_DELISTED_FRAME_HEAD)
        struct.pack_into("<H", pkg, 6, len(data) + 2)
        struct.pack_into("<H", pkg, 8, len(data) + 2)
        pkg.extend(data)

        self.send_pkg = bytes(pkg)

    def parseResponse(self, body_buf):
        """
        :param body_buf:
        :return: OrderedDict 列表（datetime/open/close/high/low/vol/amount/year/month/day/hour/minute），
                 布局与 get_security_bars 输出对齐（hour/minute 恒 0，日线）
        """
        body = bytes(body_buf)

        if len(body) < 2:
            raise ValueError(f"0x052B 响应过短：len={len(body)}")

        (ret_count,) = struct.unpack_from("<H", body)

        if ret_count > (len(body) - 2) // 17:
            # 单条记录下限 17B（date4 + 4×1B varint + vol 1B + amount4 + suffix4），
            # 声明条数超物理上限 → 帧结构坏
            raise ValueError(
                f"0x052B 响应声明条数 {ret_count} 超出负载物理上限（len={len(body)}）"
            )

        klines = []
        last_close_milli = 0
        pos = 2

        try:
            for _ in range(ret_count):
                (date_raw,) = struct.unpack_from("<I", body, pos)
                pos += 4

                year, month, day = date_raw // 10000, date_raw % 10000 // 100, date_raw % 100
                # 年份下限 1990：沪深交易所 1990-12-19 开市，老退市股有 90 年代数据
                # （实测 000038 出现 1998-12-28，下限设 2000 会误杀——在线踩过的坑）
                if not (1990 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
                    raise ValueError(f"0x052B 非法日期 raw={date_raw:#x}")

                open_delta, pos = get_price(body, pos)
                close_delta, pos = get_price(body, pos)
                high_delta, pos = get_price(body, pos)
                low_delta, pos = get_price(body, pos)

                open_milli = last_close_milli + open_delta
                close_milli = open_milli + close_delta
                high_milli = open_milli + high_delta
                low_milli = open_milli + low_delta

                if (
                    low_milli > min(open_milli, close_milli)
                    or high_milli < max(open_milli, close_milli)
                    or low_milli > high_milli
                ):
                    raise ValueError(
                        f"0x052B OHLC 不自洽 raw={date_raw} "
                        f"o={open_milli} c={close_milli} h={high_milli} l={low_milli}"
                    )

                vol_shares, pos = get_price(body, pos)
                if vol_shares < 0 or vol_shares > 0xFFFFFFFF:
                    raise ValueError(f"0x052B 非法成交量 {vol_shares} raw={date_raw:#x}")

                (amount_raw,) = struct.unpack_from("<I", body, pos)
                pos += 4
                pos += 4  # 4B 未知后缀（eltdx 同样跳过）

                last_close_milli = close_milli

                klines.append(
                    OrderedDict(
                        [
                            ("open", open_milli / 1000),
                            ("close", close_milli / 1000),
                            ("high", high_milli / 1000),
                            ("low", low_milli / 1000),
                            ("vol", vol_shares / 100),  # 股 → 手
                            ("amount", get_volume(amount_raw)),
                            ("year", year),
                            ("month", month),
                            ("day", day),
                            ("hour", 0),
                            ("minute", 0),
                            ("datetime", "%d-%02d-%02d" % (year, month, day)),
                        ]
                    )
                )
        except (IndexError, struct.error) as e:
            raise ValueError(f"0x052B 记录流截断（pos={pos} len={len(body)}）：{e}") from e

        if pos != len(body):
            raise ValueError(
                f"0x052B 记录流未对齐包尾：解析至 {pos}，包长 {len(body)}（尾部 {len(body) - pos}B）"
            )

        return klines
