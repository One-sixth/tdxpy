# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.parser.base import BaseParser
from tdxpy.parser.mac._frame import build_mac_frame

MAC_TICKS_PAGE = 1000  # 实测单页上限（请求 2000 时服务器仍只回 1000，2026-10-19 探测）


class MacTicksCmd(BaseParser):
    """MAC 通道逐笔成交（0x122F，秒级精度；29B 头 + 每条 18B：time+price+vol+笔数+bs）。

    trade_count 按有符号 int32 解析（2026-11-04 吸收自 pytdxdata 0.6.5 实测）：
    集合竞价段该字段实为负值，按 uint32 会读出 42 亿量级脏数据。
    EX 通道（ExTicksCmd）语义未定性（期货侧有 u32 环绕值），覆写保持 uint32。
    """

    #: trade_count 解析格式符：A 股实测为有符号 int32；EX 子类覆写为 "I"（未定性保守）
    _TRADE_COUNT_FMT = "i"

    def setParams(self, market, code, date=0, start=0, count=2000):
        """
        :param market: 市场（0=SZ 1=SH）
        :param code: 6 位数字代码
        :param date: 查询日期 yyyymmdd（0=今天）
        :param start: 起始偏移
        :param count: 条数（≤2000）
        """
        if type(code) is str:
            code = code.encode("gbk")

        body = struct.pack("<H22sIIH10x", market, code, date, start, count)
        self.send_pkg = build_mac_frame(0x122F, body)

    def parseResponse(self, body_buf):
        body = bytes(body_buf)

        if len(body) < 31:
            logger.warning(f"0x122F 响应过短（len={len(body)}），返回空——坏帧与无数据不可区分，请留存原始响应排查")
            return []

        (count,) = struct.unpack_from("<H", body, 29)
        parsable = (len(body) - 39) // 18
        if count > parsable:
            logger.warning(f"0x122F 声明 {count} 笔但负载仅可解析 {parsable} 笔（截断帧？），按实际笔数解析")
        count = min(count, parsable)

        records = []
        for i in range(count):
            offset = 39 + i * 18
            time_sec, price, volume, trade_count, bs_flag = struct.unpack_from(
                "<IfI" + self._TRADE_COUNT_FMT + "H", body, offset
            )
            hour, minute, second = time_sec // 3600, time_sec % 3600 // 60, time_sec % 60
            records.append(
                OrderedDict(
                    [
                        ("time", "%02d:%02d:%02d" % (hour, minute, second)),
                        ("price", price),
                        ("vol", float(volume)),
                        ("trade_count", int(trade_count)),
                        ("bs_flag", bs_flag),
                        ("hour", hour), ("minute", minute), ("second", second),
                    ]
                )
            )

        return records
