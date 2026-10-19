# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.helper import get_price, get_security_coefficient
from tdxpy.logger import logger
from tdxpy.parser.base import BaseParser


class GetMinuteTimeData(BaseParser):
    """当日分时（0x051D）。

    2026-10-19 新格式破解（new 式握手会话下响应升级，实测 13 品种）：
      [num u16][变长头部 58~75B][记录流]
      - 头部：[2:4]=00 00、[4]=market u8、[5:11]=code ASCII6、[11:]=varint 摘要流
        （含 close×系数锚点、vol 总量、amount 等；除锚点外字段语义未定）
      - 记录流与 0x0FB4 完全同构：num 组 (price_diff, rev1, vol) varint 三元组，
        price 差分累积 ×coefficient
    解析算法（不依赖头部固定长度）：扫描记录起点 off——三元组流精确对齐到包尾、
    全部 vol ≥ 0、且差分累积收盘价（cum[-1]）出现在头部 varint 流中（锚点自检）。
    legacy 模式（老三条 SETUP 会话）下服务器仍返回旧格式（头部 6B：num+preclose f32），
    由 _try_legacy_layout 兜底解析。
    """

    def __init__(self, client, lock=None):
        super().__init__(client, lock)
        self.coefficient = 0.01

    def setParams(self, market, code):
        """
        设置参数
        :param market: 市场
        :param code: 代码
        """
        # 2026-09-16 修复：coefficient 判定必须在 encode 之前用原始 str code
        # （bytes 经 str() 变 "b'601919'" → 恒 NotImplementedError → 恒 fallback 0.01）
        self.coefficient = get_security_coefficient(market=market, code=code)

        if type(code) is str:
            code = code.encode("utf-8")

        pkg = bytearray.fromhex("0c 1b 08 00 01 01 0e 00 0e 00 1d 05")
        pkg.extend(struct.pack("<H6sI", market, code, 0))

        self.send_pkg = pkg

    # ------------------------------------------------------------------
    # 记录流解码
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_trips(body, num, off):
        """从 off 起解码 num 组 (price_diff, rev1, vol)；任何异常/不对齐返回 None。"""
        pos, trips = off, []
        try:
            for _ in range(num):
                p, pos = get_price(body, pos)
                r, pos = get_price(body, pos)
                v, pos = get_price(body, pos)
                trips.append((p, r, v))
        except (IndexError, struct.error):
            return None
        if pos != len(body):
            return None
        return trips

    @staticmethod
    def _header_varints(body, limit):
        """收集头部区 [12, limit) 的 varint 值集合（锚点自检用）。"""
        vals, pos = [], 12
        while pos < limit:
            try:
                v, pos = get_price(body, pos)
            except (IndexError, struct.error):
                break
            vals.append(v)
        return vals

    def _locate_records(self, body, num):
        """扫描记录起点：精确对齐 + vol≥0 + close 锚点自检（cum[-1] ∈ 头部值集合）。

        头部含负值/大数摘要字段，锚点取正的收盘 raw 值；810/82 段固收品种
        价格贴面值、差分恒 0 时 cum[-1] 即绝对价 raw（如 1007440），同样命中锚点。
        返回 (off, trips)；无合格 off 返回 None。
        """
        for off in range(12, 130):
            trips = self._decode_trips(body, num, off)
            if trips is None:
                continue
            if any(v < 0 for _, _, v in trips):
                continue
            cum, last = [], 0
            for p, _, _ in trips:
                last += p
                cum.append(last)
            close_raw = cum[-1] if cum else None
            if close_raw is None or close_raw <= 0:
                continue
            if close_raw in self._header_varints(body, off):
                return off, trips
        return None

    # ------------------------------------------------------------------
    # 旧格式（legacy 会话）兜底
    # ------------------------------------------------------------------

    def _try_legacy_layout(self, body):
        """旧格式：num u16@0 + preclose f32@[2:6] + 记录流@6。

        判据：记录流从 6 起精确对齐到包尾。f32 合理范围（0.01~1e5）仅辅助，
        不作为主判据（6B 头版本无 market/code 字段，preclose 可能落到其他语义）。
        """
        if len(body) < 8:
            return None
        preclose = struct.unpack("<f", body[2:6])[0]
        if not (0.01 <= preclose <= 1e5):
            return None
        (num,) = struct.unpack("<H", body[:2])
        return self._decode_trips(body, num, 6)

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    def parseResponse(self, body_buf):
        num = struct.unpack("<H", body_buf[:2])[0]
        body = bytes(body_buf)

        trips = None
        located = self._locate_records(body, num)
        if located:
            _, trips = located
        else:
            # 新式握手老服务器（或未来回滚）兼容：旧 6B 头布局
            trips = self._try_legacy_layout(body)

        if trips is None:
            logger.error(
                f"0x051D 响应解析失败：len={len(body)} num={num}——"
                "记录起点扫描与旧格式兜底均未命中（服务器格式再变？请存原始响应排查）"
            )
            return []

        prices, last = [], 0
        for p, _, vol in trips:
            last += p
            prices.append(OrderedDict([("price", float(last) * self.coefficient), ("vol", vol)]))

        return prices
