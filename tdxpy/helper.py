# cython: language_level=3
import struct
from datetime import datetime

from tdxpy.constants import SECURITY_COEFFICIENT
from tdxpy.logger import logger
from tdxpy.markets import StdMarket


def get_price(data, pos):
    """
    分析了一下，貌似是类似utf-8的编码方式保存有符号数字

    :param data:
    :param pos:
    :return:
    """

    pos_byte = 6

    bdata = index_bytes(data, pos)
    int_data = bdata & 0x3F

    if bdata & 0x40:
        sign = True
    else:
        sign = False

    if bdata & 0x80:
        while True:
            pos += 1

            bdata = index_bytes(data, pos)

            int_data += (bdata & 0x7F) << pos_byte
            pos_byte += 7

            if bdata & 0x80:
                pass
            else:
                break

    pos += 1

    if sign:
        int_data = -int_data

    return int_data, pos


def get_volume(vol):
    """
    获取交易量
    :param vol:
    :return:
    """
    if vol == 0:
        # 0 字节直接归零：否则按位运算算出 2^-127 ≈ 5.877e-39 的浮点垃圾
        # （指数基日等无成交记录的 bar 实测触发，2026-09-25 修复）
        return 0.0

    logpoint = vol >> (8 * 3)

    hleax = (vol >> (8 * 2)) & 0xFF  # [2]
    lheax = (vol >> 8) & 0xFF  # [1]
    lleax = vol & 0xFF  # [0]

    dw_ecx = logpoint * 2 - 0x7F
    dw_edx = logpoint * 2 - 0x86

    dw_esi = logpoint * 2 - 0x8E
    dw_eax = logpoint * 2 - 0x96

    if dw_ecx < 0:
        tmp_eax = -dw_ecx
    else:
        tmp_eax = dw_ecx

    dbl_xmm6 = pow(2.0, tmp_eax)

    if dw_ecx < 0:
        dbl_xmm6 = 1.0 / dbl_xmm6

    if hleax > 0x80:
        dwtmpeax = dw_edx + 1
        tmpdbl_xmm3 = pow(2.0, dwtmpeax)

        dbl_xmm0 = pow(2.0, dw_edx) * 128.0
        dbl_xmm0 += (hleax & 0x7F) * tmpdbl_xmm3
        dbl_xmm4 = dbl_xmm0

    else:
        if dw_edx >= 0:
            dbl_xmm0 = pow(2.0, dw_edx) * hleax
        else:
            dbl_xmm0 = (1 / pow(2.0, dw_edx)) * hleax

        dbl_xmm4 = dbl_xmm0

    dbl_xmm3 = pow(2.0, dw_esi) * lheax
    dbl_xmm1 = pow(2.0, dw_eax) * lleax

    if hleax & 0x80:
        dbl_xmm3 *= 2.0
        dbl_xmm1 *= 2.0

    dbl_ret = dbl_xmm6 + dbl_xmm4 + dbl_xmm3 + dbl_xmm1

    return dbl_ret


def get_datetime(category, buffer, pos):
    """
    获取日期时间
    :param category:
    :param buffer:
    :param pos:
    :return:
    """
    minute = 0
    hour = 15

    if category < 4 or category == 7 or category == 8:
        zip_day, minutes = struct.unpack("<HH", buffer[pos: pos + 4])

        month = int((zip_day % 2048) / 100)
        year = (zip_day >> 11) + 2004
        day = (zip_day % 2048) % 100

        minute = minutes % 60
        hour = int(minutes / 60)
    else:
        (zip_day,) = struct.unpack("<I", buffer[pos: pos + 4])

        month = int((zip_day % 10000) / 100)
        year = int(zip_day / 10000)
        day = zip_day % 100

    pos += 4

    return year, month, day, hour, minute, pos


def get_time(buffer, pos):
    """
    获取时间
    :param buffer:
    :param pos:
    :return:
    """
    (minutes,) = struct.unpack("<H", buffer[pos: pos + 2])

    hour = int(minutes / 60)
    minute = minutes % 60

    pos += 2

    return hour, minute, pos


def index_bytes(data, pos):
    """
    索引比特
    :param data:
    :param pos:
    :return:
    """
    return data[pos]


# def get_security_coefficient(market, name):
#     return SECURITY_COEFFICIENT[get_security_type(market, name)]


# TODO 增加 get_coefficient 函数
def get_security_coefficient(market=None, code=None):
    """按 (market, code) 查快照/分时价格系数。

    未知代码段直接 raise NotImplementedError（绝不静默回退 0.01——
    静默回退曾导致 58 段科创ETF/18 段REITs 快照价格 ×10，2026-09-17 修复）。
    """
    security_type = get_security_type(market=market, code=code)
    return SECURITY_COEFFICIENT[security_type][0]


def get_security_type(market, code):
    """
    获取股票类型, A股, B股, 指数等

    :param market: 市场（StdMarket 成员 / "sh"/"sz"/"bj" / 0/1/2 / 中文昵称）
    :param code: 代码
    :return:
    """
    try:
        market_id = int(StdMarket(market))
    except ValueError:
        raise NotImplementedError(
            f"未知市场 {market!r}：支持 StdMarket 成员 / 'sh'/'sz'/'bj' / 0/1/2"
        )

    code = str(code)
    code_head = str(code)[:2]

    if market_id == StdMarket.SZ:
        if code_head in ["00", "30"]:
            return "SZ_A_STOCK"

        if code_head in ["20"]:
            return "SZ_B_STOCK"

        if code_head in ["39"]:
            return "SZ_INDEX"

        if code_head in ["15", "16", "18"]:  # 18=REITs（1801xx，2026-09-17 实测 raw×1000）
            return "SZ_FUND"

        if code_head in ["10", "11", "12", "13", "14"]:
            return "SZ_BOND"

    if market_id == StdMarket.SH:
        if code_head in ["60", "68"]:  # 688XXX科创板
            return "SH_A_STOCK"

        if code_head in ["90"]:
            return "SH_B_STOCK"

        if code_head in ["00", "88", "99"]:
            return "SH_INDEX"

        if code_head in ["50", "51", "52", "53", "55", "56", "58"]:  # 沪市基金全段（50/51 ETF、56 中证系、58 科创ETF；52/53/55 LOF/封基）
            return "SH_FUND"

        if code_head in ["01", "10", "11", "12", "13", "14", "20"]:
            return "SH_BOND"

    if market_id == StdMarket.BJ:
        if code_head in ["43", "83", "87", "88", "92"]:  # 北交所股票段（92 现役；43/83/87/88 老三板/历史，防御保留）
            return "BJ_STOCK"

        # 2026-10-19 官方规测定性（北证公告〔2024〕20号《证券代码、证券简称编制指引》）：
        #   810 首三位 = 向特定对象发行可转换公司债券（简称后四位"定转"）；vipdoc 实测 810011/810014 价格贴面值 100、raw×10000
        #   820 首三位（老版"首两位 82"）= 优先股；vipdoc 现役文件全为 821xxx，同为固收类精度
        #   80/811+ 号段 = 政府债券（北交所《启用政府债券代码号段的通知》）——不登记，raise 防御
        if str(code)[:3] == "810":
            return "BJ_BOND"

        if code_head == "82":
            return "BJ_PREFERRED"

        if code_head in ["89"]:  # 89 段指数（899050 北证50 等，系数同 SH_INDEX）
            return "BJ_INDEX"

    logger.debug("Unknown security exchange !")
    raise NotImplementedError(
        f"未登记的品种代码段：market={market!r} code={code!r}（价格系数未知，拒绝猜测）。"
        "请在 tdxpy.helper.get_security_type 与 tdxpy.constants.SECURITY_COEFFICIENT 登记该代码段"
    )


def dump(buf):
    from pprint import pprint

    try:
        from hexdump import hexdump

        pprint(hexdump(buf))
    except ImportError:
        pprint(buf)


def time_frame(current_time=None):
    """
    判断时间是否在交易时间段内
    :param current_time: 要检查的时间, 如果空则默认当前时间
    :return: 在交易时间段内返回 True， 否则返回 False
    """
    current_time = current_time or datetime.now()

    start_time = datetime.strptime(str(current_time.date()) + '9:30', '%Y-%m-%d%H:%M')
    end_time = datetime.strptime(str(current_time.date()) + '11:30', '%Y-%m-%d%H:%M')

    if start_time < current_time < end_time:
        return True

    start_time = datetime.strptime(str(current_time.date()) + '13:00', '%Y-%m-%d%H:%M')
    end_time = datetime.strptime(str(current_time.date()) + '15:00', '%Y-%m-%d%H:%M')

    if start_time < current_time < end_time:
        return True

    return False
