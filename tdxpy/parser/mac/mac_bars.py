# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.logger import logger
from tdxpy.parser.base import BaseParser
from tdxpy.parser.mac._frame import build_mac_frame

MAC_BARS_PAGE = 700  # MAC K 线单页上限（pytdxdata 同值）

# MAC 周期编码（2026-10-19 全周期在线探测 0-11 实锤，bar 间隔判别）：
#   0=5min(300s) 1=15min 2=30min 3=1h 4=day 5=week 6=month 7=1min(60s)
#   8=N分钟（times 参数=N×60s，times=1 即 1min——参数化周期钩子）
#   9=day 10=季 11=年
# 标准通道 category 0-6 与 MAC 同值（直通）；std 7（1min）→ 门面 resolve_mac_period
# 映射到 MAC 8（参数化——MAC 7 是固定 1min，times 被服务器忽略，2026-10-21 三台实测）；
# std 9（RI_K 日线）→ MAC 9；std 10/11（季/年）→ MAC 10/11。
# ⚠️ pytdxdata 原映射表 {7:0, 11:10, 10:11} 有误（其 7→0 会把 1min 变 5min、11→10 把年变季），
#    本表以在线逐周期 bar 间隔实测为准。
#: 参数化周期钩子（std category → MAC period=8 + times=N）：8=N分钟
#: （2026-10-19 在线实测 times=3/5/15/30 → 180/300/900/1800s bar 间隔）
_PARAMETRIC_PERIODS = {8: "times"}

_MAC_PERIOD = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7, 8: 8, 9: 9, 10: 10, 11: 11}

#: 分钟周期（决定是否解析 time_num 为时刻）：5min/15min/30min/1h/1min 系
_MINUTE_CATEGORIES = (0, 1, 2, 3, 7, 8)


class MacBarsCmd(BaseParser):
    """MAC 通道 K 线（0x122E，支持复权；每条 36B：ymd u32 + time u32 + 7×f32 含 amount）。"""

    def setParams(self, market, code, category=4, start=0, count=700, adjust=0, times=1):
        """
        :param market: 市场（0=SZ 1=SH）
        :param code: 6 位数字代码
        :param category: K 线种类（同标准通道 TDXParams.KLINE_*；未知值 raise 不静默回退）
        :param start: 起始偏移（0=最新一根，向前翻页递增）
        :param count: 条数（≤700）
        :param adjust: 复权（0=不复权 1=前复权 2=后复权）
        :param times: N 分钟参数（category 经 _MAC_PERIOD 映射到 8 时生效：times=N → N 分钟 bar；
                      2026-10-19 在线实测 times=3/5/15/30 → 180/300/900/1800s）
        """
        if type(code) is str:
            code = code.encode("gbk")

        if category not in _MAC_PERIOD:
            # 绝不静默回退——曾因 .get(category, 4) 把 5min 静默变日线（复审实锤）
            raise ValueError(
                f"MAC 通道未知 K 线周期 {category!r}（合法值：{sorted(_MAC_PERIOD)}，"
                "0=5min 1=15min 2=30min 3=1h 4=day 5=week 6=month 7=固定1min 8=参数化1min(times=N) 9=day 10=季 11=年）"
            )

        mac_period = _MAC_PERIOD[category]
        times_value = 1
        if mac_period == 8 and int(times) > 1:
            # MAC 8 = 参数化 1 分钟编码（times=N → N 分钟 bar）；
            # ⚠️ MAC 7 = 固定 1 分钟——times 被服务器忽略（2026-10-21 三台实测：
            # period=7 时 times=1/5 返回逐字节相同，8 时正常聚合）。门面经
            # resolve_mac_period 把 std "1min"(7) 映射到 8，勿直传 7 期望 times 生效。
            times_value = int(times)

        self.category = category
        self.is_minute = category in _MINUTE_CATEGORIES
        body = struct.pack(
            "<H22sHHIHHbbbbH4s",
            market, code, mac_period, times_value, start, count,
            adjust, 1, 1, 0, 1, 0, b"",
        )
        self.send_pkg = build_mac_frame(0x122E, body)

    def parseResponse(self, body_buf):
        body = bytes(body_buf)

        if len(body) < 33:
            logger.warning(f"0x122E 响应过短（len={len(body)}），返回空——坏帧与无数据不可区分，请留存原始响应排查")
            return []

        # 头 33B：market(2)+code(22)+category(2)@24+flag(1)@26+count(2)@27+start(4)@29
        (count,) = struct.unpack_from("<H", body, 27)
        parsable = (len(body) - 33) // 36
        if count > parsable:
            logger.warning(f"0x122E 声明 {count} 条但负载仅可解析 {parsable} 条（截断帧？），按实际条数解析")
        count = min(count, parsable)

        klines = []
        for i in range(count):
            offset = 33 + i * 36
            ymd, time_num, open_, high, low, close, amount, vol, _fs = struct.unpack_from(
                "<II7f", body, offset
            )
            if ymd < 19900101 or ymd > 20991231:
                continue

            year, month, day = ymd // 10000, (ymd % 10000) // 100, ymd % 100
            # 分钟周期从 time_num 解析时刻（bar 标签=开始时刻，与 std 通道一致——
            # 2026-10-19 逐 bar vol 对照 std 5min 完全一致实锤）；
            # 日线 time_num 无语义（实测 55000→15:15），对齐 std 日线惯例 15:00
            hour, minute = 15, 0
            if self.is_minute and time_num:
                hour = time_num // 3600
                minute = (time_num % 3600) // 60

            klines.append(
                OrderedDict(
                    [
                        ("open", open_), ("close", close), ("high", high), ("low", low),
                        ("vol", vol), ("amount", amount),
                        ("year", year), ("month", month), ("day", day),
                        ("hour", hour), ("minute", minute),
                        ("datetime", "%d-%02d-%02d %02d:%02d" % (year, month, day, hour, minute)),
                    ]
                )
            )

        return klines
