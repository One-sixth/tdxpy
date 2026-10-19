# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.logger import logger
from tdxpy.parser.base import BaseParser
from tdxpy.parser.mac._frame import build_mac_frame


class MacMinuteCmd(BaseParser):
    """MAC 通道单日分时（0x122D；35B 头 + 18B/条：minutes+price+avg+vol+momentum）。

    与标准通道 get_minute_time_data（0x051D 当日）互补：可查任意历史交易日分时，
    含均价线；当日数据建议用 std 通道 minute()。
    """

    #: 单日点数上限（A 股一天 240 分钟）；EX 通道子类覆写为 None（港股 330/
    #: 美股 390/期货含夜盘 345，一天点数随市场而变，按响应实际条数解析）
    _MAX_POINTS = 240

    def setParams(self, market, code, date=0):
        """
        :param market: 市场（0=SZ 1=SH）
        :param code: 6 位数字代码
        :param date: 查询日期 yyyymmdd（0=今天）
        """
        if type(code) is str:
            code = code.encode("gbk")

        body = struct.pack("<H22sI5H", market, code, date, 1, 0, 0, 0, 0)
        self.send_pkg = build_mac_frame(0x122D, body)

    def parseResponse(self, body_buf):
        body = bytes(body_buf)

        if len(body) < 35:
            logger.warning(f"0x122D 响应过短（len={len(body)}），返回空——坏帧与无数据不可区分，请留存原始响应排查")
            return []

        # 头 35B：market(2)+code(22)+date(4)+reserved(1)+ref_price f32+count(2)
        (count,) = struct.unpack_from("<H", body, 33)
        parsable = (len(body) - 35) // 18
        if self._MAX_POINTS is not None:
            parsable = min(self._MAX_POINTS, parsable)
        if count > parsable:
            logger.warning(f"0x122D 声明 {count} 点但负载仅可解析 {parsable} 点（截断帧？），按实际点数解析")
        count = min(count, parsable)

        points = []
        for i in range(count):
            offset = 35 + i * 18
            minutes, price, avg, vol, _momentum = struct.unpack_from("<HffIf", body, offset)
            points.append(
                OrderedDict(
                    [
                        ("hour", minutes // 60 % 24),
                        ("minute", minutes % 60),
                        ("price", price),
                        ("avg_price", avg),
                        ("vol", float(vol)),
                        ("time", "%02d:%02d" % (minutes // 60 % 24, minutes % 60)),
                    ]
                )
            )

        return points
