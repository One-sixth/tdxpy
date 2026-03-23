# cython: language_level=3
"""市场枚举与字符串别名：让 market 参数"一眼看全、写字符串也认"。

- StdMarket：标准市场（7709）0=SZ / 1=SH / 2=BJ，支持 "sh"/"sz"/"bj" 别名
- ExMarket：扩展市场（7727，2026-09-17 实测 51 个），支持字符串别名
  （short_name / 英文成员名 / 常用中英文昵称，大小写不敏感），如
  ExMarket("us")==74、ExMarket("hk")==31、ExMarket("郑商所")==28
- ex_market_table()：离线静态 DataFrame（id/中文名/别名一览）；
  服务器实时清单请用 client.ex_markets()（新增市场以动态查询为准）
"""
from enum import IntEnum

import pandas as pd

__all__ = ["StdMarket", "ExMarket", "resolve_ex_market", "ex_market_table", "EX_MARKET_ALIASES"]


class StdMarket(IntEnum):
    """标准市场（TCP 7709）：A股/指数/基金/转债。"""

    SZ = 0
    SH = 1
    BJ = 2

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            key = value.strip().lower()
            if key in ("sz", "深圳", "深市"):
                return cls.SZ
            if key in ("sh", "上海", "沪市"):
                return cls.SH
            if key in ("bj", "北京", "北交所"):
                return cls.BJ
        return None


class ExMarket(IntEnum):
    """扩展市场（TCP 7727，自动 login）：期货/期权/外盘/港股/美股/指数/基金等。

    成员 = 2026-09-17 实测 get_markets() 返回的 51 个市场；字符串别名见
    EX_MARKET_ALIASES（short_name 与英文成员名小写天然可用）。
    """

    TEMP = 1                 # 临时股
    ZCE_OPTION = 4           # 郑州商品期权
    DCE_OPTION = 5           # 大连商品期权
    SHFE_OPTION = 6          # 上海商品期权
    CFFEX_OPTION = 7         # 中金所期权
    SSE_OPTION = 8           # 上海股票期权
    SZSE_OPTION = 9          # 深圳股票期权
    FX_BASIC = 10            # 基本汇率
    FX_CROSS = 11            # 交叉汇率
    GLOBAL_INDEX = 12        # 国际指数
    COMEX = 16               # 纽约COMEX
    NYMEX = 17               # 纽约NYMEX
    CBOT = 18                # 芝加哥CBOT
    HK_FIN_FUTURE = 23       # 香港金融期货
    HK_FIN_OPTION = 24       # 香港金融期权
    HK_STOCK_FUTURE = 25     # 香港股票期货
    HK_STOCK_OPTION = 26     # 香港股票期权
    HK_INDEX = 27            # 香港指数
    ZCE = 28                 # 郑州商品
    DCE = 29                 # 大连商品
    SHFE = 30                # 上海期货
    HK_MAIN = 31             # 香港主板
    OPEN_FUND = 33           # 开放式基金
    MONEY_FUND = 34          # 货币型基金
    MACRO = 38               # 宏观指标
    COMMODITY_INDEX = 42     # 商品指数
    B_TO_H = 43              # B股转H股
    NEEQ = 44                # 股转系统
    SH_GOLD = 46             # 上海黄金
    CFFEX = 47               # 中金所期货
    HK_GROWTH = 48           # 香港创业板
    HK_FUND = 49             # 香港基金
    BOND_PRE_ISSUE = 54      # 国债预发行
    PRIVATE_FUND = 56        # 阳光私募基金
    BROKER_PLAN = 57         # 券商集合理财
    BROKER_CASH_PLAN = 58    # 券商货币理财
    MAIN_CONTRACT = 60       # 主力期货合约
    CSI_INDEX = 62           # 中证指数
    GFEX_ARB = 65            # 广州套利期货
    GFEX = 66                # 广州期货
    GFEX_OPTION = 67         # 广州期权
    RISK_INDEX = 68          # 风控指数
    HUAZHENG_INDEX = 69      # 华证指数
    BLOCK_INDEX_EXT = 70     # 扩展板块指数
    DE_STOCK = 73            # 德国股票
    US_STOCK = 74            # 美国股票
    SG_STOCK = 78            # 新加坡股票
    MONEY_MARKET = 91        # 资金市场
    HK_GREY = 98             # 港股暗盘
    CODE_MIRROR = 100        # 代码镜像
    CNI_INDEX = 102          # 国证指数

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            key = value.strip().lower()
            market_id = EX_MARKET_ALIASES.get(key)
            if market_id is None:
                for member in cls:  # 英文成员名小写兜底（us_stock/hk_main/...）
                    if member.name.lower() == key:
                        market_id = int(member)
                        break
            if market_id is not None:
                return cls(market_id)
        return None


#: id → 中文名（2026-09-17 ex_markets 实测，供表格与别名派生）
EX_MARKET_NAMES = {
    1: "临时股", 4: "郑州商品期权", 5: "大连商品期权", 6: "上海商品期权",
    7: "中金所期权", 8: "上海股票期权", 9: "深圳股票期权", 10: "基本汇率",
    11: "交叉汇率", 12: "国际指数", 16: "纽约COMEX", 17: "纽约NYMEX",
    18: "芝加哥CBOT", 23: "香港金融期货", 24: "香港金融期权", 25: "香港股票期货",
    26: "香港股票期权", 27: "香港指数", 28: "郑州商品", 29: "大连商品",
    30: "上海期货", 31: "香港主板", 33: "开放式基金", 34: "货币型基金",
    38: "宏观指标", 42: "商品指数", 43: "B股转H股", 44: "股转系统",
    46: "上海黄金", 47: "中金所期货", 48: "香港创业板", 49: "香港基金",
    54: "国债预发行", 56: "阳光私募基金", 57: "券商集合理财", 58: "券商货币理财",
    60: "主力期货合约", 62: "中证指数", 65: "广州套利期货", 66: "广州期货",
    67: "广州期权", 68: "风控指数", 69: "华证指数", 70: "扩展板块指数",
    73: "德国股票", 74: "美国股票", 78: "新加坡股票", 91: "资金市场",
    98: "港股暗盘", 100: "代码镜像", 102: "国证指数",
}

#: id → category（与 EX_MARKET_NAMES 同源实测）
EX_MARKET_CATEGORY = {
    1: 1, 4: 12, 5: 12, 6: 12, 7: 12, 8: 12, 9: 12, 10: 4, 11: 4, 12: 5,
    16: 3, 17: 3, 18: 3, 23: 3, 24: 12, 25: 3, 26: 12, 27: 5, 28: 3, 29: 3,
    30: 3, 31: 2, 33: 8, 34: 9, 38: 10, 42: 3, 43: 1, 44: 1, 46: 11, 47: 3,
    48: 2, 49: 2, 54: 6, 56: 8, 57: 8, 58: 9, 60: 3, 62: 5, 65: 3, 66: 3,
    67: 12, 68: 5, 69: 5, 70: 5, 73: 14, 74: 13, 78: 15, 91: 7, 98: 2,
    100: 11, 102: 5,
}

#: short_name（服务器侧返回，全小写即天然别名）
_EX_SHORT_NAMES = {
    1: "tp", 4: "oz", 5: "od", 6: "os", 7: "oj", 8: "qq", 9: "sq", 10: "fe",
    11: "fx", 12: "wi", 16: "co", 17: "ny", 18: "cb", 23: "pr", 24: "pj",
    25: "ph", 26: "pq", 27: "fh", 28: "qz", 29: "qd", 30: "qs", 31: "kh",
    33: "fu", 34: "fb", 38: "hg", 42: "ti", 43: "hb", 44: "sb", 46: "sg",
    47: "cz", 48: "kg", 49: "kt", 54: "gy", 56: "ta", 57: "tb", 58: "tc",
    60: "ma", 62: "zz", 65: "eg", 66: "qg", 67: "og", 68: "tz", 69: "bz",
    70: "uz", 73: "de", 74: "us", 78: "se", 91: "fd", 98: "ap", 100: "cm",
    102: "gz",
}

#: 高频友好别名（中英文昵称 → id；short_name 与成员名小写不在此重复登记）
EX_MARKET_ALIASES = {
    # 美股 / 港股 / 国际股票
    "us": 74, "usa": 74, "美股": 74,
    "hk": 31, "港股": 31, "香港": 31, "港股主板": 31, "香港主板": 31,
    "hk_growth": 48, "港股创业板": 48, "香港创业板": 48,
    "hk_index": 27, "香港指数": 27, "恒生": 27,
    "de": 73, "德国": 73, "sg": 78, "新加坡": 78,
    # 期货交易所（中英文惯称）
    "zce": 28, "郑商所": 28, "dce": 29, "大商所": 29,
    "shfe": 30, "上期所": 30, "cffex": 47, "中金所": 47,
    "gfex": 66, "广期所": 66,
    "main": 60, "主力": 60, "主力连续": 60,
    "comex": 16, "nymex": 17, "cbot": 18,
    # 其他高频
    "黄金": 46, "沪金": 46, "新三板": 44, "股转": 44,
    "期权": 8, "50etf期权": 8,
}

#: 别名 → id 全集 = 友好别名 + short_name（构造期合成，short_name 不覆盖友好别名）
for _mid, _short in _EX_SHORT_NAMES.items():
    EX_MARKET_ALIASES.setdefault(_short, _mid)


def resolve_ex_market(market):
    """把 market 归一成 EX 市场 id（int）。

    :param market: int / ExMarket 成员 / 字符串别名（如 "us"/"hk"/"美股"/"上期所"）
    :return: int 市场 id
    :raises ValueError: 字符串别名无法识别（消息含查询指引）
    """
    if isinstance(market, ExMarket):
        return int(market)
    if isinstance(market, int) and not isinstance(market, bool):
        return int(market)  # int 直通（保持向后兼容，新市场 id 无需等枚举更新）
    if isinstance(market, str):
        key = market.strip().lower()
        market_id = EX_MARKET_ALIASES.get(key)
        if market_id is None:
            for member in ExMarket:
                if member.name.lower() == key:
                    market_id = int(member)
                    break
        if market_id is not None:
            return market_id
        raise ValueError(
            f"无法识别的 EX 市场 {market!r}。可用别名示例：us/美股=74、hk/港股=31、"
            "hk_index=27、shfe/上期所=30、main/主力=60……全表见 "
            "tdxpy.markets.ex_market_table()（离线）或 client.ex_markets()（实时）"
        )
    raise ValueError(
        f"market 须为 int / ExMarket 成员 / 字符串别名，收到 {type(market).__name__}: {market!r}"
    )


def ex_market_table():
    """离线静态市场表（market/category/name/short_name/alias/enum_member）。

    数据快照于 2026-09-17 实测；服务器新增市场以 client.ex_markets() 动态查询为准。
    """
    rows = []
    alias_by_id = {}
    for alias, mid in EX_MARKET_ALIASES.items():
        alias_by_id.setdefault(mid, []).append(alias)
    for member in ExMarket:
        mid = int(member)
        rows.append({
            "market": mid,
            "category": EX_MARKET_CATEGORY.get(mid),
            "name": EX_MARKET_NAMES.get(mid),
            "short_name": _EX_SHORT_NAMES.get(mid),
            "alias": ", ".join(sorted(alias_by_id.get(mid, []))),
            "enum_member": member.name,
        })
    return pd.DataFrame(rows).sort_values("market").reset_index(drop=True)
