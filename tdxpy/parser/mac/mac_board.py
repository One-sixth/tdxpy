# cython: language_level=3
"""MAC 通道板块列表（0x1231）：A 股板块（MAC 7709）与港股板块（MAC EX 7727）共用命令。

2026-10-20 双通道在线实测（tmp_tdx_probe/probe_mac_board.py）：
  - MAC 通道（老三条 SETUP 会话）：board_type=0 全部 total=128 / =1 行业 total=345；
    板块码 881xxx（market=1），领涨股 market=0/1；
  - MAC EX 通道（0x2454 login 会话 + EX 帧）：港股板块 total=160，板块码 HK0xxx
    （market=70），领涨股 market=31/48；
  - 响应布局（两通道同构）：头 4B（u16 count_all=2×本页记录数（段计数）+
    u16 total=该类板块总数）+ 每条 160B = 板块段 80B + 领涨股段 80B，
    各 `<H6s16s44sfff`（market/code6/pad16/name44/price/rise/pre；GBK 文本；
    rise 实测恒 0——字段保留待定性）。
"""
import struct
from collections import OrderedDict

from tdxpy.logger import logger
from tdxpy.parser.base import BaseParser
from tdxpy.parser.mac._frame import build_ex_frame
from tdxpy.parser.mac._frame import build_mac_frame

#: 单页条数上限（服务器实测一页 150 可通；345 个行业板块分 3~4 页拉全）
MAC_BOARD_PAGE = 100

_SEG = struct.Struct("<H6s16s44sfff")
_HEAD = struct.Struct("<HH")  # count_all(段计数) + total(该类板块总数)


def _decode_segment(seg):
    market, code_b, _pad, name_b, price, rise, pre = _SEG.unpack(seg)
    return OrderedDict([
        ("market", market),
        ("code", code_b.decode("gbk", "replace").rstrip("\x00")),
        ("name", name_b.decode("gbk", "replace").rstrip("\x00")),
        ("price", round(float(price), 4)),
        ("rise_speed", round(float(rise), 4)),
        ("pre_close", round(float(pre), 4)),
    ])


class MacBoardListCmd(BaseParser):
    """板块列表（0x1231；MAC 与 MAC EX 通道通用，帧 flag 由 channel 参数决定）。"""

    def setParams(self, board_type=0, start=0, count=MAC_BOARD_PAGE, channel="mac"):
        """
        :param board_type: 板块类别（实测 0=全部 / 1=行业；其余类别语义待探）
        :param start: 起始偏移（翻页递增）
        :param count: 条数（每条 160B；单页实测 ≥150 可通）
        :param channel: "mac"=A 股板块（7709 MAC 帧）/ "ex"=港股板块（7727 EX 帧）
        """
        body = struct.pack("<HHBBHH8x", count, board_type, 0, 0, start, 1)
        self.send_pkg = build_ex_frame(0x1231, body) if channel == "ex" else build_mac_frame(0x1231, body)

    def parseResponse(self, body_buf):
        body = bytes(body_buf)

        if len(body) < 4:
            logger.warning(f"0x1231 响应过短（len={len(body)}），返回空——坏帧与无数据不可区分，请留存原始响应排查")
            return []

        # 头 4B：count_all（段计数，=2×记录数）+ total（该类板块总数）。
        # 刻意不信任 count_all——以 body 长度按 160B 步进解析为准（残帧防御）；
        # 头部声称值与实际不符时按实际解析，失配属协议漂移信号。
        count_all, total = _HEAD.unpack_from(body)
        n = (len(body) - 4) // 160
        if (len(body) - 4) % 160:
            logger.warning(
                f"0x1231 响应尾部 {(len(body) - 4) % 160}B 非 160 倍数（截断/坏帧？），"
                f"已按完整记录解析 {n} 条并丢弃残尾（count_all={count_all} total={total}）")
        results = []
        pos = 4
        for _ in range(n):
            board = _decode_segment(body[pos:pos + 80])
            leader = _decode_segment(body[pos + 80:pos + 160])
            board["total"] = total
            board["leader_market"] = leader["market"]
            board["leader_code"] = leader["code"]
            board["leader_name"] = leader["name"]
            board["leader_price"] = leader["price"]
            board["leader_rise_speed"] = leader["rise_speed"]
            board["leader_pre_close"] = leader["pre_close"]
            results.append(board)
            pos += 160
        return results
