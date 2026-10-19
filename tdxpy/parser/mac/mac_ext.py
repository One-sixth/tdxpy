# cython: language_level=3
"""MAC 通道扩展命令族（批次五，2026-10-20 在线破解）：板块成分/集合竞价/市场异动/个股快照。

四命令参考布局源自 pytdxdata mac_ext.py（假设来源），逐字节经在线探测裁决
（tmp_tdx_probe/probe_mac_batch2*.py 三轮）：

0x122C 板块成分报价
  请求 43B：``<I9xHIHBB``（board_code, sort_type, start, page_size, 0, 0——注意
  格式串逐字符展开是 I,9x,H,I,H,B,B，故头段 23B 而非 19B）
  + 字段位图 16B + ``<BBBB``(0,0,0,1)。board_code 须换算：881xxx→int-880000+20000
  （881248→21248 实测；399xxx→-399000+30000 / 899xxx→-899000+32000 参考同款规则）。
  响应：20B 响应位图 + ``<IH``(total, row_count) + 每行 68B ``<H22s44s``
  （market/code22/name44 GBK）+ 按位图顺序的字段流。已验证位：
  0x00 昨收 0x01 开 0x02 高 0x03 低 0x04 收 0x05 量(<I) 0x06 量比 0x07 额。
  成分跨市场（0/1/2 深沪北）；start 翻页实测有效；残尾按字段流防御。

0x123D 集合竞价
  请求 42B：``<H22sII10x``（market, code, start, count）。
  响应：36B 头（count@24 ``<I``）+ 每条 16B ``<IfIi``
  （time_sec 秒数 / price / matched 累计成交量 / unmatched 未匹配量，i32 可负）。
  实测 59 条 09:15:00→09:24:57（3 秒一条），末条 price=当日开盘价。

0x1237 市场异动
  请求 22B：``<HH2xH2xH5H``（market, start, count≤600, 1, 200, 30, 40, 50, 200；
  格式串逐字符展开含 9 个 H，头段+参数共 22B；后 5 个参数语义待探，参考实现原样传递）。响应：count@0 ``<H`` + 每条 32B：
  ``<H6sBBBHH``（market/code6/type@9/idx@11/z@13）+ 13B 数据区
  （B 未知 + B 未知 + f32 触发时点价 + f32 触发时点额(万元) + 3B 未定）
  + pad@28 + ``<BH``(hour, minute_bcd BCD)@29；尾部 GBK 逗号分隔名字表。
  ⚠️ price/amount 是**异动触发时点快照**（000530 触发价 7.02 vs 当日开盘 7.66；
  竞价时段为虚拟撮合值），非全天口径；type 0x13 竞价试买 price=0。
  类型码表（pytdxdata 还原）：0x03 主力买卖 0x04 加速拉升 0x05 加速下跌
  0x06 低位反弹 0x07 高位回落 0x08 撑杆跳高 0x09 平台跳水 0x0A 单笔冲涨跌
  0x0B 区间放量 0x0C 区间缩量 0x10 大单托盘 0x11 大单压盘 0x12 大单锁盘
  0x13 竞价试买 0x14 竞价涨跌；实测分布 0x13×137 / 0x14×463（开盘时段首页）。

0x122A 个股特征快照
  请求 40B：``<H22sI12x``（market, code, 1）。
  响应：头 8B + @8 ``<H22s44s``（market/code22/name44）+ @96
  ``<III6fIfII``（date yyyymmdd / time HHMMSS / activity 笔数? /
  pre_close/open/high/low/close/momentum 共 6f / vol <I / amount <f /
  in_vol 内盘 <I / out_vol 外盘 <I）+ @148 起 68B 尾部未定不出。
  000001 全字段与 std quotes 交叉验证逐值一致；momentum 实测 0 待定性。
"""
import struct
from collections import OrderedDict

from tdxpy.logger import logger
from tdxpy.parser.base import BaseParser
from tdxpy.parser.mac._frame import build_ex_frame
from tdxpy.parser.mac._frame import build_mac_frame

# ---------------------------------------------------------------------------
# 板块成分报价 0x122C
# ---------------------------------------------------------------------------

#: 默认请求字段位（0x00 昨收 0x01 开 0x02 高 0x03 低 0x04 收 0x05 量 0x06 量比 0x07 额）
MEMBER_BASIC_BITS = (0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07)

#: 位图字段名表（已验证 8 位；未知位输出 field_0xNN）
_MEMBER_FIELD_NAMES = {
    0x00: "pre_close",
    0x01: "open",
    0x02: "high",
    0x03: "low",
    0x04: "close",
    0x05: "vol",
    0x06: "volume_ratio",
    0x07: "amount",
}

_ROW = struct.Struct("<H22s44s")


def convert_board_code(code):
    """板块码换算：'881248'→21248（int-880000+20000，2026-10-20 在线实测）。

    str 输入按前缀换算（88xxxx→int-880000+20000，覆盖 880/881 段——0x1218
    所属板块返回 880218 等 880 段码；399xxx→-399000+30000 / 899xxx→-899000+32000）；
    int 输入原样透传（兼容调用方已换算的值）。
    """
    if isinstance(code, int):
        return code
    s = str(code).strip()
    if len(s) == 6:
        if s.startswith("88"):
            return int(s) - 880000 + 20000
        if s.startswith("399"):
            return int(s) - 399000 + 30000
        if s.startswith("899"):
            return int(s) - 899000 + 32000
    raise ValueError(f"无法识别的板块码 {code!r}（支持 88xxxx/399xxx/899xxx 或已换算 int）")


def _field_fmt(bit):
    """按位返回字段解析格式（量类 u32，其余 f32；0x2E 有符号——参考规则）。"""
    if bit in (0x05, 0x08, 0x09, 0x0A, 0x0B, 0x13, 0x14, 0x15, 0x18, 0x19, 0x1A):
        return "<I"
    if bit == 0x2E:
        return "<i"
    return "<f"


def _build_bitmap(bits, size=16):
    bm = bytearray(size)
    for b in bits:
        bm[b // 8] |= 1 << (b % 8)
    return bytes(bm)


class MacBoardMembersCmd(BaseParser):
    """板块成分报价（0x122C；MAC 通道，board_code 需 convert_board_code 换算）。"""

    def setParams(self, board_code, sort_type=0, start=0, count=80, bits=None):
        """
        :param board_code: 板块码（'881248' 或已换算 int 21248）
        :param sort_type: 排序方式（0=默认序；其余语义待探）
        :param start: 起始偏移（翻页递增，实测有效）
        :param count: 条数（单页实测 ≥80 可通）
        :param bits: 请求字段位序列（默认 MEMBER_BASIC_BITS 8 位）
        """
        bc = convert_board_code(board_code)
        bitmap = _build_bitmap(list(bits) if bits is not None else list(MEMBER_BASIC_BITS))
        body = (struct.pack("<I9xHIHBB", bc, sort_type, start, count, 0, 0)
                + bitmap + struct.pack("<BBBB", 0, 0, 0, 1))
        self.send_pkg = build_mac_frame(0x122C, body)

    def parseResponse(self, body_buf):
        return _parse_members_response(bytes(body_buf), cmd_name="0x122C")


def _parse_members_response(body, cmd_name="0x122C"):
    """0x122C/0x122B 共享响应解析（两命令响应完全同构：20B 位图 + <IH + 68B/行 + 字段流）。"""
    if len(body) < 26:
        logger.warning(f"{cmd_name} 响应过短（len={len(body)}），返回空——疑似错误帧（26B 全零形）")
        return []
    resp_bitmap = body[:20]
    total, row_count = struct.unpack_from("<IH", body, 20)
    results = []
    pos = 26
    n = 0
    truncated = False
    while n < row_count and pos + _ROW.size <= len(body):
        market, code_b, name_b = _ROW.unpack_from(body, pos)
        pos += _ROW.size
        fields = OrderedDict()
        for i in range(160):
            if not resp_bitmap[i // 8] & (1 << (i % 8)):
                continue
            fmt = _field_fmt(i)
            if pos + struct.calcsize(fmt) > len(body):
                logger.warning(f"{cmd_name} 字段流在 bit 0x{i:02x} 处越界（残帧/位图 overclaim），终止后续行解析")
                truncated = True
                break
            (v,) = struct.unpack_from(fmt, body, pos)
            name = _MEMBER_FIELD_NAMES.get(i, f"field_0x{i:02x}")
            fields[name] = int(v) if fmt == "<I" else round(float(v), 4)
            pos += struct.calcsize(fmt)
        row = OrderedDict([
            ("market", market),
            ("code", code_b.decode("gbk", "replace").rstrip("\x00")),
            ("name", name_b.decode("gbk", "replace").rstrip("\x00")),
            ("total", total),
        ])
        row.update(fields)
        results.append(row)
        n += 1
        if truncated:
            # 字段流错位后继续解行只会产出垃圾——当前行保留部分字段，后续行终止
            # （交叉复审 P1-2：响应位图 overclaim 场景的垃圾行防御）
            logger.warning(f"{cmd_name} 字段流越界，终止后续行解析（当前行保留已解出的部分字段）")
            break
    if pos < len(body):
        logger.warning(f"{cmd_name} 响应尾部 {len(body) - pos}B 未解析（残尾/未知字段），已丢弃")
    return results


# ---------------------------------------------------------------------------
# 批量报价 0x122B（批次六复活：请求位图为 20B——曾误用 0x122C 的 16B 导致请求
# 错位，服务器 fallback 默认池呈现"列表语义不生效"假象，2026-10-21 在线裁决）
# ---------------------------------------------------------------------------

class MacBatchQuotesCmd(BaseParser):
    """批量自定义字段位报价（0x122B；≤80 只/页，请求位图 20B——注意与 0x122C 的 16B 不同！）。

    响应与 0x122C 完全同构：20B 响应位图 + ``<IH``(total, rows) + 每行
    68B ``<H22s44s`` + 按位图字段流（复用 MacBoardMembersCmd 的解析）。
    2026-10-21 在线实测：请求 3 只精确返回 3 只（000001/600000/300750 字段与
    std quotes 逐值一致），1 只精确返回 1 行——列表语义完全正常。
    """

    #: 响应解析完全复用 0x122C 的实现（同构布局）
    _MEMBERS_PARSER = MacBoardMembersCmd

    def setParams(self, stocks, bits=None, channel="mac"):
        """
        :param stocks: (market, code) 列表，≤80 只/页（超出请分批调用）
        :param bits: 请求字段位（默认 0x00~0x07 八位；参考实现位图固定 20B）
        :param channel: "mac"=A 股批量报价（7709 MAC 帧）/ "ex"=港美股等扩展市场
                       批量报价（7727 EX 帧，2026-10-21 实测）
        """
        if len(stocks) > 80:
            raise ValueError(f"0x122B 单页上限 80 只，收到 {len(stocks)} 只——请分批调用")
        bitmap = _build_bitmap(list(bits) if bits is not None else list(MEMBER_BASIC_BITS), size=20)
        body = bitmap + struct.pack("<H", len(stocks))
        for market, code in stocks:
            body += struct.pack("<H22s", market, str(code).encode("ascii"))
        self.send_pkg = build_ex_frame(0x122B, body) if channel == "ex" else build_mac_frame(0x122B, body)

    def parseResponse(self, body_buf):
        # 响应布局与 0x122C 同构（20B 位图 + <IH + 68B/行 + 字段流），复用共享解析
        return _parse_members_response(bytes(body_buf), cmd_name="0x122B")


# ---------------------------------------------------------------------------
# 集合竞价 0x123D
# ---------------------------------------------------------------------------

_AUCTION_ITEM = struct.Struct("<IfIi")


class MacAuctionCmd(BaseParser):
    """集合竞价逐笔（0x123D；09:15-09:25 3 秒一条，末条价格=开盘价）。"""

    def setParams(self, market, code, start=0, count=100):
        """
        :param market: 市场号（0=深 / 1=沪）
        :param code: 6 位代码（ASCII）
        :param start: 起始偏移
        :param count: 条数（实测一天 59 条全量）
        """
        body = struct.pack("<H22sII10x", market, str(code).encode("ascii"), start, count)
        self.send_pkg = build_mac_frame(0x123D, body)

    def parseResponse(self, body_buf):
        body = bytes(body_buf)
        if len(body) < 36:
            logger.warning(f"0x123D 响应过短（len={len(body)}），返回空")
            return []
        (count,) = struct.unpack_from("<I", body, 24)
        results = []
        pos = 36
        for _ in range(count):
            if pos + _AUCTION_ITEM.size > len(body):
                logger.warning(f"0x123D 记录流越界（声称 {count} 条，实际不足），返回已解析 {len(results)} 条")
                break
            time_sec, price, matched, unmatched = _AUCTION_ITEM.unpack_from(body, pos)
            pos += _AUCTION_ITEM.size
            results.append(OrderedDict([
                ("time", f"{time_sec // 3600:02d}:{time_sec % 3600 // 60:02d}:{time_sec % 60:02d}"),
                ("price", round(float(price), 4)),
                ("matched", int(matched)),
                ("unmatched", int(unmatched)),
            ]))
        return results


# ---------------------------------------------------------------------------
# 市场异动 0x1237
# ---------------------------------------------------------------------------

_UNUSUAL_HEAD = struct.Struct("<H6sBBBHH")
_UNUSUAL_DATA = struct.Struct("<BBffi")  # 13B：B 未知 + B 未知 + f32 价 + f32 额(万) + 3B 未定（i32 容纳 3B pad）

#: 异动类型码表（pytdxdata 还原；0x13 竞价试买 price=0 实测互证）。
#: ⚠️ 全天拉取（批次六冒烟 2026-10-21）实测还有码表外类型：0x15(21)/0x16(22)/
#: 0x1D(29)/0x1E(30)（盘中异动专属，price 常为 0，语义待探）——未知码走
#: UNUSUAL_TYPES.get 兜底输出十进制字符串，不丢失记录。
UNUSUAL_TYPES = {
    0x03: "主力买卖",
    0x04: "加速拉升",
    0x05: "加速下跌",
    0x06: "低位反弹",
    0x07: "高位回落",
    0x08: "撑杆跳高",
    0x09: "平台跳水",
    0x0A: "单笔冲涨跌",
    0x0B: "区间放量",
    0x0C: "区间缩量",
    0x10: "大单托盘",
    0x11: "大单压盘",
    0x12: "大单锁盘",
    0x13: "竞价试买",
    0x14: "竞价涨跌",
}


class MacUnusualCmd(BaseParser):
    """市场异动列表（0x1237；count≤600/页，start 翻页；price/amount 为触发时点快照）。"""

    def setParams(self, market=0, start=0, count=600):
        """
        :param market: 市场号（0=深）
        :param start: 起始偏移（翻页）
        :param count: 条数（单页上限 600）
        """
        count = min(int(count), 600)
        # 尾部 5 参数（1,200,30,40,50,200）语义待探，参考实现原样传递
        body = struct.pack("<HH2xH2xH5H", market, start, count, 1, 200, 30, 40, 50, 200)
        self.send_pkg = build_mac_frame(0x1237, body)

    def parseResponse(self, body_buf):
        body = bytes(body_buf)
        if len(body) < 2:
            logger.warning(f"0x1237 响应过短（len={len(body)}），返回空")
            return []
        (count,) = struct.unpack_from("<H", body, 0)
        names_end = 2 + count * 32
        if len(body) >= names_end:
            # 名字表与记录严格按序号对齐：保留中间空串（防张冠李戴），只去首尾空
            tail = body[names_end:].decode("gbk", "ignore").strip(",")
            names = tail.split(",") if tail else []
        else:
            logger.warning(f"0x1237 记录流越界（声称 {count} 条但 body={len(body)}B），按实际可容纳数解析")
            count = max(0, (len(body) - 2) // 32)
            names = []  # 截断帧的名字区不可信（可能是被截断的记录），不解析
        results = []
        for i in range(count):
            off = 2 + i * 32
            market, code_b, _p1, utype, _p2, _idx, _z = _UNUSUAL_HEAD.unpack_from(body, off)
            b1, b2, price, amount, _pad3 = _UNUSUAL_DATA.unpack_from(body, off + 15)
            hour, min_bcd = struct.unpack_from("<BH", body, off + 29)
            results.append(OrderedDict([
                ("market", market),
                ("code", code_b.decode("gbk", "replace").rstrip("\x00")),
                ("name", names[i] if i < len(names) else ""),
                ("type", utype),
                ("type_name", UNUSUAL_TYPES.get(utype, str(utype))),
                ("price", round(float(price), 4)),
                ("amount", round(float(amount), 2)),
                ("time", f"{hour:02d}:{min_bcd // 100:02d}:{min_bcd % 100:02d}"),
            ]))
        return results


# ---------------------------------------------------------------------------
# 个股特征快照 0x122A
# ---------------------------------------------------------------------------

_SNAP_HEAD = struct.Struct("<H22s44s")
_SNAP_BODY = struct.Struct("<III6fIfII")  # date/time/activity + 6f(pre..momentum) + vol(u32)/amount(f)/in/out(u32)


class MacSymbolInfoCmd(BaseParser):
    """个股特征快照（0x122A；OHLC/量额与 std quotes 交叉验证一致，另含内外盘与 activity）。"""

    def setParams(self, market, code):
        """
        :param market: 市场号（0=深 / 1=沪）
        :param code: 6 位代码（ASCII）
        """
        body = struct.pack("<H22sI12x", market, str(code).encode("ascii"), 1)
        self.send_pkg = build_mac_frame(0x122A, body)

    def parseResponse(self, body_buf):
        body = bytes(body_buf)
        # 最小可解析长度 = 头 8B + @8 68B（市场/代码/名）+ @96 主体 52B + @76~96 间隔 20B = 148
        if len(body) < 8 + 68 + 20 + _SNAP_BODY.size:
            logger.warning(f"0x122A 响应过短（len={len(body)}），返回空")
            return []
        market, code_b, name_b = _SNAP_HEAD.unpack_from(body, 8)
        (date, t, activity, pre, opn, high, low, close, momentum,
         vol, amount, in_vol, out_vol) = _SNAP_BODY.unpack_from(body, 96)
        return [OrderedDict([
            ("market", market),
            ("code", code_b.decode("gbk", "replace").rstrip("\x00")),
            ("name", name_b.decode("gbk", "replace").rstrip("\x00")),
            ("date", int(date)),
            ("time", int(t)),
            ("activity", int(activity)),
            ("pre_close", round(float(pre), 4)),
            ("open", round(float(opn), 4)),
            ("high", round(float(high), 4)),
            ("low", round(float(low), 4)),
            ("close", round(float(close), 4)),
            ("momentum", round(float(momentum), 4)),  # 实测 0.0 待定性
            ("vol", int(vol)),
            ("amount", round(float(amount), 2)),
            ("in_vol", int(in_vol)),
            ("out_vol", int(out_vol)),
        ])]


# ---------------------------------------------------------------------------
# 个股所属板块 0x1218（head_flag=0x01 / 0x1C 实测等价）
# ---------------------------------------------------------------------------

class MacBelongBoardCmd(BaseParser):
    """个股所属板块（0x1218；响应 = 27B 头 + GBK JSON 数组的数组）。

    2026-10-20 在线实测（批次六）：000001 返回 13 个所属板块（深圳板块/跨境支付
    CIPS/…/全国性银行），JSON 每行 9 列：[类别序号, market, 板块码 880/881xxx,
    板块名, close, pre_close, flag_a, flag_b, flag_c]（后三个小整数语义未定）。
    ⚠️ 帧头 flag：参考实现用 0x01（EX flag），实测本命令在 MAC 服务器上
    0x1C 与 0x01 等价返回（又一例 flag 非硬校验）——落地统一用 0x1C（MAC 族一致）。
    """

    REPORT_NAME = b"Stock_GLHQ"

    def setParams(self, market, code):
        """
        :param market: 市场号（0=深 / 1=沪）
        :param code: 6 位代码（ASCII）
        """
        body = struct.pack("<H8s16x21s", market, str(code).encode("ascii"), self.REPORT_NAME)
        self.send_pkg = build_mac_frame(0x1218, body)

    def parseResponse(self, body_buf):
        import json

        body = bytes(body_buf)
        if len(body) < 27:
            logger.warning(f"0x1218 响应过短（len={len(body)}），返回空")
            return []
        # 头 27B：@0 u16（market echo）+ @2 "Stock_GLHQ" ASCII 回显 + pad +
        # @20 u16（=1，标志 echo？）+ @23 u16 = JSON 字节数（000001=815 与 600519=1161
        # 双样本自洽——2026-10-21 交叉复审修正：曾误读 @22 得 12032/35072 幻影漂移），
        # JSON 边界仍按 body 实际长度界定，头声称值仅作漂移告警
        json_len, = struct.unpack_from("<H", body, 23)
        json_part = body[27:]
        if json_len and json_len != len(json_part):
            logger.warning(f"0x1218 头部声称 JSON {json_len}B 与实际 {len(json_part)}B 不符，按实际解析")
        try:
            rows = json.loads(json_part.decode("gbk", "replace"))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"0x1218 JSON 解析失败（{e}），返回空")
            return []
        if not isinstance(rows, list):
            logger.warning(f"0x1218 JSON 顶层非数组（{type(rows).__name__}），返回空")
            return []
        results = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            results.append(OrderedDict([
                ("category", str(row[0])),
                ("market", int(row[1])),
                ("board_code", str(row[2])),
                ("board_name", str(row[3])),
                ("close", round(float(row[4] or 0), 4)),
                ("pre_close", round(float(row[5] or 0), 4)),
                ("flag_a", str(row[6]) if len(row) > 6 else ""),
                ("flag_b", str(row[7]) if len(row) > 7 else ""),
                ("flag_c", str(row[8]) if len(row) > 8 else ""),
            ]))
        return results


# ---------------------------------------------------------------------------
# 个股资金流 0x1218（报表名 Stock_ZJLX；与所属板块同命令异报表名）
# ---------------------------------------------------------------------------

class MacCapitalFlowCmd(BaseParser):
    """个股资金流（0x1218 + 报表名 Stock_ZJLX；响应 = 27B 头 + GBK JSON 两行数字）。

    2026-11-05 在线实测（MEDIUM 调查，000001/600519/600900/300750 四股票采样）：
    与所属板块 0x1218 同命令异报表名——请求体 21s 报表名段换 b"Stock_ZJLX" 即
    返回资金流数据；head_flag 0x1C/0x02 等价（flag 非硬校验，与所属板块一致）。
    响应 JSON 恒两行：

    - 行1（4 值）= 当日 [主力流入, 主力流出, 散户流入, 散户流出]（元）；
      main_net = main_in - main_out 即当日主力净额（与 std 0x0FFC 同量级旁证，
      eltdx docs/methods/0x0ffc 实测 sh600900 两通道同为 -1.49 亿量级）。
      量级 sanity：|net| < 当日成交额，四样本全过。
    - 行2（6 值）= 5 日累计口径（[0]/[1] ≈ 行1 对应值 ×4.4~7 实锤）；[2..5]
      精确语义未定性（eltdx 猜 [3]=large_5d/[4]=mid_5d 不可靠，[2]≠[0]-[1]），
      保守输出 flow5d_0..flow5d_5 原始值，待对照第三方数据源校准后命名。
    """

    REPORT_NAME = b"Stock_ZJLX"

    def setParams(self, market, code):
        """
        :param market: MAC 市场号（深 0 / 沪 1）
        :param code: 6 位代码（ASCII）
        """
        body = struct.pack("<H8s16x21s", market, str(code).encode("ascii"), self.REPORT_NAME)
        self.send_pkg = build_mac_frame(0x1218, body)

    def parseResponse(self, body_buf):
        import json

        body = bytes(body_buf)
        if len(body) < 27:
            logger.warning(f"0x1218(ZJLX) 响应过短（len={len(body)}），返回空")
            return []
        # 头与所属板块 0x1218 同构：@0 u16 market echo + @2 报表名回显 + @23 u16 JSON 字节数
        json_len, = struct.unpack_from("<H", body, 23)
        json_part = body[27:]
        if json_len and json_len != len(json_part):
            logger.warning(f"0x1218(ZJLX) 头部声称 JSON {json_len}B 与实际 {len(json_part)}B 不符，按实际解析")
        try:
            rows = json.loads(json_part.decode("gbk", "replace"))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"0x1218(ZJLX) JSON 解析失败（{e}），返回空")
            return []
        if not isinstance(rows, list) or len(rows) < 2:
            logger.warning(f"0x1218(ZJLX) JSON 期望两行数字数组，收到 {rows!r}，返回空")
            return []

        today, five_days = rows[0], rows[1]
        if not isinstance(today, (list, tuple)) or len(today) < 4:
            logger.warning(f"0x1218(ZJLX) 行1 期望 4 值，收到 {today!r}，返回空")
            return []

        def _f(v):
            try:
                return round(float(v), 2)
            except (TypeError, ValueError):
                return 0.0

        main_in, main_out = _f(today[0]), _f(today[1])
        retail_in, retail_out = _f(today[2]), _f(today[3])
        row = OrderedDict([
            ("main_in", main_in),
            ("main_out", main_out),
            ("main_net", round(main_in - main_out, 2)),
            ("retail_in", retail_in),
            ("retail_out", retail_out),
            ("retail_net", round(retail_in - retail_out, 2)),
        ])
        if isinstance(five_days, (list, tuple)):
            if len(five_days) < 6:
                logger.warning(f"0x1218(ZJLX) 行2 期望 6 值，收到 {len(five_days)} 值（服务器截断？），按实际输出")
            for i, v in enumerate(five_days[:6]):
                row[f"flow5d_{i}"] = _f(v)
        else:
            logger.warning(f"0x1218(ZJLX) 行2 非数组（{type(five_days).__name__}），flow5d 列缺省")
        return [row]
