# cython: language_level=3
"""统一门面 TdxClient：以"更好用、更易用、更易理解"为唯一标准的重塑层。

symbol 语法（2026-09-17 点分式定版，唯一格式）："{市场}.{代码}"，只按第一个 "." 拆分。

- 市场段：strip + lower 后查表——
  1. 标准市场 sh/sz/bj（含 "上海"/"深圳"/"北京"/"北交所" 等中文昵称）→ 7709 通道；
  2. 扩展市场 EX_MARKET_ALIASES 别名（us/美股/hk/main/上期所/zce…，
     全表 tdxpy.markets.ex_market_table() 离线查、client.ex_markets() 实时查）→ 7727 通道；
  3. 都不命中 → raise（教学式错误消息）。
- 代码段：strip 后原样传递（不改大小写），允许数字/字母/下划线
  （601919 / 01398 / AAPL / A_IXIC / cu2510），允许代码段内再含点。
- hk 市场段先细分：5 位数字=主板 31（hk.01398）、"08" 开头=创业板 48（hk.08001）、
  非数字=香港指数 27（hk.HSI，服务器侧大写）。
- 指数自动路由：bars() 识别标准市场指数代码段（sh 000/880/999、sz 399、bj 899）
  自动转指数命令——个股命令对指数返回 12 字节尾部 bar，按个股格式解析会错位出垃圾数据。
- 旧写法一律报错：裸代码（"601919"）、旧式无点前缀（"sh601919"）、
  品种定位方法的 market 参数（已物理删除；ex_instruments 的 market 是查询主体，保留）。

公开方法路由矩阵：

| 方法 | std 7709 | ex 7727 |
|---|---|---|
| bars | get_security_bars（指数段自动 get_index_bars） | get_instrument_bars（700/页） |
| bars_range | raise（EX 专属） | get_history_instrument_bars_range |
| minute | get_minute_time_data | 同名 EX 命令 |
| minute_history | get_history_minute_time_data | 同名 EX 命令 |
| ticks | get_history_transaction_data（std 2000/页，自适应降档） | 同名 EX 命令（1800/页） |
| quotes | get_security_quotes 批量（≤80/次） | get_instrument_quote 逐只 |
| xdxr / finance | 支持 | raise（协议级无此数据） |
| ex_markets / ex_instruments | — | EX 市场清单 / 合约列表 |

其余要点：

- period 用字符串："1min"/"5min"/"15min"/"30min"/"1h"/"day"/"week"/"month"。
- adjust 服务端复权："qfq"/"hfq"/None（仅标准市场；EX 无服务端复权，须 None）。
- all_pages=True 自动翻页拉全量（page_size ≤ 单次上限，max_pages 防失控）。
- ticks(date=None) 内部走历史分笔接口传当天日期——绕开当日分笔接口的
  交易时段限制（time_frame），盘后同样可用。
- 标准市场（7709）与扩展市场（7727）双连接懒加载：用到 EX 品种才连 EX。
- TdxClient.auto() 并发测速选优；断线自动重建连接重试一次。
"""

import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd

from tdxpy.exceptions import CODE_POOL_EXHAUSTED, TdxConnectionError
from tdxpy.exceptions import TdxFunctionCallError
from tdxpy.exhq import TdxExHq_API
from tdxpy.health import health_rank, health_update
from tdxpy.helper import get_security_coefficient
from tdxpy.hq import TdxHq_API
from tdxpy.ex_mac import TdxMacEx_API
from tdxpy.logger import logger
from tdxpy.mac import TdxMac_API
from tdxpy.markets import EX_MARKET_NAMES, ExMarket, StdMarket, resolve_ex_market

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 字符串周期 → 底层 K 线 category
PERIOD_MAP = {
    "1min": 7,
    "5min": 0,
    "15min": 1,
    "30min": 2,
    "1h": 3,
    "day": 4,
    "week": 5,
    "month": 6,
}
PERIOD_ALIASES = {
    "1": "1min", "1m": "1min", "min1": "1min", "1min": "1min",
    "5": "5min", "5m": "5min", "min5": "5min", "5min": "5min",
    "15": "15min", "15m": "15min", "15min": "15min",
    "30": "30min", "30m": "30min", "30min": "30min",
    "60": "1h", "60m": "1h", "h": "1h", "1h": "1h", "hour": "1h",
    "d": "day", "1d": "day", "daily": "day", "day": "day",
    "w": "week", "1w": "week", "weekly": "week", "week": "week",
    "mo": "month", "mon": "month", "1mo": "month", "monthly": "month", "month": "month",
}

#: 字符串复权 → 底层 adjust 字段
ADJUST_MAP = {"none": 0, "qfq": 1, "hfq": 2}

#: 实测可用的标准市场服务器（7709，2026-10-19 T0.3 门禁：a-share servers.txt 43 IP
#: 全量 TCP + 业务命令（security_count，全员一致 27631）双验证通过，按探测延迟排序）
STD_SERVERS = [
    ("116.205.171.132", 7709),
    ("116.205.163.254", 7709),
    ("175.178.128.227", 7709),
    ("159.75.29.111", 7709),
    ("129.204.230.128", 7709),
    ("111.230.186.52", 7709),
    ("110.41.2.72", 7709),
    ("101.33.225.16", 7709),
    ("124.71.9.153", 7709),
    ("43.139.18.171", 7709),
    ("43.139.95.83", 7709),
    ("175.178.112.197", 7709),
    ("119.97.185.59", 7709),
    ("123.60.84.66", 7709),
    ("123.60.70.228", 7709),
    ("124.71.187.122", 7709),
    ("123.60.164.122", 7709),
    ("116.205.183.150", 7709),
    ("150.158.160.2", 7709),
    ("81.71.32.47", 7709),
    ("101.35.121.35", 7709),
    ("101.42.164.241", 7709),
    ("101.42.240.54", 7709),
    ("101.43.159.194", 7709),
    ("110.41.147.114", 7709),
    ("111.229.247.189", 7709),
    ("111.231.113.208", 7709),
    ("118.25.98.114", 7709),
    ("120.53.8.251", 7709),
    ("121.36.225.169", 7709),
    ("122.51.120.217", 7709),
    ("122.51.232.182", 7709),
    ("123.60.73.44", 7709),
    ("124.223.163.242", 7709),
    ("124.70.133.119", 7709),
    ("124.70.199.56", 7709),
    ("124.71.187.72", 7709),
    ("152.136.191.169", 7709),
    ("49.232.15.141", 7709),
    ("62.234.50.143", 7709),
    ("81.70.151.186", 7709),
    ("82.156.174.84", 7709),
    ("82.156.214.79", 7709),
]

#: 实测可用的扩展市场服务器（7727）。握手模式随服务器而异（exhq.EX_SETUP_SERVERS
#: 清单内的走 92B setup 帧，其余走 0x2454 login——连接时自动选择；交叉复测：
#: 握手误配不致命，清单固化设计语义）：
#: - login 模式：139.9.191.175 / 121.37.232.167 / 43.139.173.246
#: - setup 模式：116.205.143.214（通达信扩展市场 52 市场清单更广——独有「更多其它
#:   指数 105」，2026-10-20 实测）、103.221.142.82（国泰君安 60 市场——英国股票 75/
#:   香港债券 22；经典 EX 命令含外盘期货在 5 台服务器数据面一致）
EX_SERVERS = [
    ("139.9.191.175", 7727),
    ("121.37.232.167", 7727),
    ("43.139.173.246", 7727),
    ("116.205.143.214", 7727),
    ("103.221.142.82", 7727),
]

#: 实测可用的 MAC 通道服务器（7709，老三条 SETUP 会话；T0.4 2026-10-19 实测
#: 600000 日线三台全通。标准服务器对 MAC 帧无响应，勿与 STD_SERVERS 混用）
MAC_SERVERS = [
    ("121.37.207.165", 7709),
    ("123.60.47.136", 7709),
    ("139.9.52.158", 7709),
]

#: 实测可用的 MAC EX 通道服务器（7727，0x2454 login 会话 + 帧首字节 0x01）；
#: T0.4-EX 2026-10-19 实测港美股指/国内期货 K 线分时逐笔全通。
#: ⚠️ 与 EX_SERVERS 共享 121.37.232.167 但协议不同（EX_SERVERS 走 exhq setup
#: 握手、本池走 0x2454 login，两池连接独立不可互换）。
#: pytdxdata EX 清单另两台（116.205.143.214/103.221.142.82）为 setup 握手模式，未纳池
MAC_EX_SERVERS = [
    ("116.205.135.205", 7727),
    ("121.37.232.167", 7727),
]

#: 实时五档单次上限
MAX_QUOTES_PER_CALL = 80

#: 分笔/K线单页上限（std）。K 线服务器硬上限 800（超限「整请求失败」），
#: 因此 K 线/退市股必须 ≤800；分笔上限更宽（2000），见 TICK_PAGE_SIZE。
MAX_PAGE_SIZE = 800

#: std 历史分笔单页上限（服务器实测硬上限 2000，超限会「静默截断」）
TICK_PAGE_SIZE = 2000

#: EX 分笔单页上限
EX_PAGE_SIZE = 1800

#: EX K 线单页上限
EX_BARS_PAGE_SIZE = 700

#: MAC 逐笔 bs_flag=8 = 集合竞价委托（非成交；vol 为累计值，2026-11-04 定性自 pytdxdata 0.6.5）
_BS_AUCTION_ORDER = 8


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 点分式 symbol 解析（2026-09-17 定版：唯一格式 "{市场}.{代码}"）
# ---------------------------------------------------------------------------

#: 港股 EX 市场 id（ExMarket 枚举成员，IntEnum 兼容 int 比较）
HK_EX_MARKET_MAIN = ExMarket.HK_MAIN
HK_EX_MARKET_GROWTH = ExMarket.HK_GROWTH
HK_EX_MARKET_INDEX = ExMarket.HK_INDEX

#: 标准市场指数代码段（market_id → 代码前缀元组）：bars() 据此自动路由 index_bars。
#: 个股命令 get_security_bars 对指数返回 12 字节尾部 bar，按个股 8 字节解析会错位。
INDEX_CODE_PREFIXES = {
    1: ("000", "880", "999"),  # SH：上证系列指数 / 通达信板块指数 / 上证综指旧码
    0: ("399",),               # SZ：深证系列指数
    2: ("899",),               # BJ：北证系列指数（899050 北证50）
}


def _is_index_code(market_id, code):
    """判断标准市场代码是否为指数段（bars() 自动路由 get_index_bars 用）。"""
    return str(code).startswith(INDEX_CODE_PREFIXES.get(int(market_id), ()))


#: 退市股代码前缀 → 市场 id（8 字符写法显式市场段）
_DELISTED_PREFIX_MARKET = {"sz": 0, "sh": 1, "bj": 2, "t": None}


def _normalize_delisted_symbol(symbol):
    """退市股代码归一化 → (market_id, code, 标准点分 symbol)。

    兼容四种写法（0x052B 门面 delisted_bars 用，规则与 eltdx NormalizedCode::parse 对齐）：
      "sz.000038"（点分式）/ "sz000038"（前缀式）/ "T000038"（eltdx t 前缀=按数字推断）
      / "000038"（裸 6 位数字，按首位推断：92→BJ、6/9→SH、0/1/2/3→SZ、8→BJ）
    """
    text = str(symbol).strip().lower()

    if "." in text:  # 点分式：复用门面统一解析（只认 std 市场）
        kind, market_id, code = _parse_symbol(text)
        if kind != "std":
            raise ValueError(f"退市股日线仅支持标准市场（sh/sz/bj），收到 {symbol!r}")
        return market_id, code, f"{StdMarket(market_id).name.lower()}.{code}"

    if len(text) == 8 and text[:2] in ("sz", "sh", "bj"):  # 前缀式
        market_id = _DELISTED_PREFIX_MARKET[text[:2]]
        code = text[2:]
    elif len(text) == 7 and text[0] == "t":  # eltdx t 前缀：剥掉后按数字推断
        code = text[1:]
        market_id = None
    elif len(text) == 6 and text.isdigit():
        code = text
        market_id = None
    else:
        raise ValueError(
            f"无法解析退市股代码 {symbol!r}：支持 'sz.000038'/'sz000038'/'T000038'/'000038' 四种写法"
        )

    if not (len(code) == 6 and code.isdigit()):
        raise ValueError(f"退市股代码段须为 6 位数字，{symbol!r} 解析出 {code!r}")

    if market_id is None:  # 按首位数字推断市场（eltdx 同规则）
        head = code[0]
        if code.startswith("92") or head == "8":
            market_id = 2
        elif head in ("6", "9"):
            market_id = 1
        elif head in ("0", "1", "2", "3"):
            market_id = 0
        else:
            raise ValueError(
                f"无法推断 {code!r} 的市场归属（首位 {head!r} 不在任何已知代码段）。"
                "请显式指定市场，如 'sz.000038'"
            )

    market_name = {0: "sz", 1: "sh", 2: "bj"}[market_id]
    return market_id, code, f"{market_name}.{code}"


def _resolve_hk(code):
    """点分式 hk 市场段的代码段 → (ex_market_id, ex_code)。

    规则：5 位数字 "08" 开头=创业板(48，如 hk.08001)、其余 5 位数字=主板(31，如 hk.01398)；
    非数字（如 HSI/HSCEI）=香港指数(27，upper 后返回——香港指数代码服务器侧大写)。
    """
    code = str(code).strip()
    if not code:
        raise ValueError("hk 市场段后须跟代码：'hk.01398'（主板）/ 'hk.08001'（创业板）/ 'hk.HSI'（指数）")
    if code.isdigit():
        if len(code) != 5:
            raise ValueError(f"港股代码须为 5 位数字（如 hk.01398），收到 {code!r}")
        if code.startswith("08"):
            return HK_EX_MARKET_GROWTH, code
        return HK_EX_MARKET_MAIN, code
    return HK_EX_MARKET_INDEX, code.upper()


def _parse_symbol(symbol):
    """点分式 symbol → (kind, market_id, code)。

    格式 "{市场}.{代码}"，只按第一个 "." 拆分。市场段 strip+lower 后先查
    StdMarket（sh/sz/bj 及中文昵称 → kind="std"），再查 ExMarket 别名表
    （us/美股/hk/main/上期所… → kind="ex"）；都不命中时纯数字段按 ExMarket
    id 直用（仅已登记 id，如 "38.1_GDP"），仍不命中 raise（教学式文案）。
    "hk" 市场段先过 _resolve_hk 细分（主板 31 / 创业板 48 / 香港指数 27）。

    :param symbol: "sh.601919"/"us.AAPL"/"hk.01398"/"上期所.cu2510" 等
    :return: (kind, market_id, code)；kind ∈ {"std", "ex"}；
             market_id：std ∈ {0,1,2}，ex ∈ ExMarket id
    :raises TypeError: symbol 非 str
    :raises ValueError: 裸代码 / 旧式无点前缀 / 市场段无法识别 / 代码段为空
    """
    if not isinstance(symbol, str):
        raise TypeError(f"symbol 必须是字符串（如 'sh.601919' 或 'us.AAPL'），收到 {type(symbol).__name__}")

    symbol = symbol.strip()
    if "." not in symbol:
        if symbol[:2].isalpha():
            head = symbol[:2].lower()
            raise ValueError(
                f"旧式无点前缀已移除，请改用点分式 '{head}.{symbol[2:]}'，如 'sh.601919'/'us.AAPL'"
            )
        raise ValueError(
            "裸代码已不支持（市场归属有歧义）。请用 '市场.代码' 格式，"
            "如 'sh.601919'/'us.AAPL'；市场别名全表见 tdxpy.markets.ex_market_table()"
        )

    market_part, code = symbol.split(".", 1)
    code = code.strip()
    if not code:
        raise ValueError(f"symbol 代码段为空：{symbol!r}。请用 '市场.代码' 格式，如 'sh.601919'")

    market_key = market_part.strip().lower()

    try:  # 标准市场优先（sh/sz/bj 及中文昵称）
        return "std", int(StdMarket(market_key)), code
    except ValueError:
        pass

    if market_key == "hk":  # 港股细分：主板 31 / 创业板 48 / 香港指数 27
        market_id, hk_code = _resolve_hk(code)
        return "ex", int(market_id), hk_code

    try:  # 扩展市场别名（short_name / 英文成员名小写 / 中英文昵称）
        ex_id = int(ExMarket(market_key))
    except ValueError:
        # 纯数字市场段 = ExMarket id 直用（如 "38.1_GDP" 宏观指标；2026-11-05
        # MEDIUM②：IntEnum 不收 str，数字段此前直接 raise——仅接受已登记 id 且
        # id>=3：0/1/2 属 std 市场号语义空间，id=1(TEMP 临时股) 与 std 沪市 1
        # 冲突，直用会静默误路由（交叉复审 F4），故数字段一律排除）
        ex_id = None
        if market_key.isdigit():
            if int(market_key) < 3:
                raise ValueError(
                    f"数字市场段 {market_key} 与标准市场号 0/1/2 语义冲突（id=1 为 EX 临时股，"
                    f"误路由会静默出错）。std 市场请用 sh/sz/bj，EX 市场请用别名或 >=3 的 id"
                )
            try:
                ex_id = int(ExMarket(int(market_key)))
            except ValueError:
                pass

    if ex_id is None:
        raise ValueError(
            f"无法识别的市场 '{market_part.strip()}'。可用别名见 tdxpy.markets.ex_market_table()（离线）"
            f"或 client.ex_markets()（实时）"
        )

    # hk 泛别名（港股/香港/hk→31、港股创业板→48）遇非数字代码 → 香港指数（与 hk 细分规则一致，如 "港股.HSI"）
    if ex_id in (int(HK_EX_MARKET_MAIN), int(HK_EX_MARKET_GROWTH)) and not code.isdigit():
        ex_id = int(HK_EX_MARKET_INDEX)
        code = code.upper()  # 香港指数代码服务器侧大写（与 _resolve_hk 一致）

    return "ex", ex_id, code


def normalize_period(period):
    """字符串周期 → category int。支持 "1min"/"5min"/"day" 及常用别名。"""
    if isinstance(period, int):
        if period in PERIOD_MAP.values():
            return period
        raise ValueError(f"未知周期编号 {period}，合法值：{sorted(set(PERIOD_MAP.values()))}")
    key = str(period).strip().lower()
    key = PERIOD_ALIASES.get(key, key)
    if key in PERIOD_MAP:
        return PERIOD_MAP[key]
    raise ValueError(f"未知周期 {period!r}，合法值：{sorted(PERIOD_MAP)}（含常见别名）")


def resolve_mac_period(period, times=None):
    """period/times → (mac_category, times_value)（MAC/MAC EX 通道 K 线专用）。

    与标准通道的编码差异（2026-10-21 三台 MAC 服务器实测勘误）：
    MAC 侧 ``7`` = 固定 1 分钟（times 被服务器忽略），``8`` = 参数化 1 分钟
    （times=N → N 分钟 bar，实测 2/3/4/5/7/10/15/20/30/45/60/120 全通）。
    标准通道 "1min"=7 直传 MAC 7 会**静默丢失 times**（曾致门面参数化周期失效）。

    规则：
    - "Nmin"（N≥2，如 "10min"）→ (8, N)——参数化 N 分钟别名；
    - "1min"（std cat=7）→ (8, times 或 1)——映射到 MAC 参数化编码；
    - 其余周期（std cat 0-6 与 MAC 0-6 同值）→ 直传，times>1 raise
      （times 仅对 1 分钟系生效，不静默丢弃）。

    :raises ValueError: period 与 times 冲突 / times 用于非 1 分钟系周期

    ⚠️ int/str 输入不对称是刻意的：int 输入 = std category（合法值仅 0-7，
    int 8 不是合法 std 值会 raise）；"Nmin" 别名仅存在于 str 路径（"8min"
    是新周期别名）。需要 MAC 参数化编码请传 "1min"+times 或 "Nmin"。
    """
    if isinstance(period, int):
        category = normalize_period(period)
        if times is not None and int(times) > 1 and category != 7:
            raise ValueError(f"times 仅对 1 分钟系周期生效，period={period} 传 times={times} 会被丢弃")
        if category == 7:
            return 8, int(times) if times is not None else 1
        return category, 1
    key = str(period).strip().lower()
    m = re.fullmatch(r"(\d{1,3})min", key)
    if m and int(m.group(1)) >= 2 and key not in PERIOD_MAP:
        # 仅接管 PERIOD_MAP 之外的新周期（10min/2min/45min…）；"5min" 等标准名
        # 保持原映射（MAC 0/1/2 固定周期），不做语义变更
        n = int(m.group(1))
        if times is not None and int(times) != n:
            raise ValueError(f"period={period!r} 与 times={times} 冲突，二者只能传一个")
        if n > 240:
            raise ValueError(f"N 分钟参数 n={n} 超出合理范围 [1, 240]")
        return 8, n
    category = normalize_period(period)
    if category == 7:  # std 1min → MAC 参数化 1min 编码
        return 8, int(times) if times is not None else 1
    if times is not None and int(times) > 1:
        raise ValueError(f"times 仅对 1 分钟系周期生效（period={period!r} 传 times={times} 会被 MAC 服务器忽略）")
    return category, 1


def normalize_adjust(adjust):
    """None/"qfq"/"hfq" → 底层 adjust 字段（0=不复权 1=前复权 2=后复权）。"""
    if adjust is None:
        return 0
    if isinstance(adjust, int) and adjust in (0, 1, 2):
        return adjust
    key = str(adjust).strip().lower()
    if key in ADJUST_MAP:
        return ADJUST_MAP[key]
    raise ValueError(f"adjust 仅支持 None/'qfq'/'hfq'，收到 {adjust!r}")


def normalize_date(date):
    """'2020-01-02' / '20200102' / 20200102 / date → int(YYYYMMDD)。"""
    if isinstance(date, int):
        return date
    if hasattr(date, "strftime"):
        return int(date.strftime("%Y%m%d"))
    text = str(date).strip().replace("-", "").replace("/", "")
    if not text.isdigit() or len(text) != 8:
        raise ValueError(f"日期须为 'YYYY-MM-DD' / 'YYYYMMDD' / 8 位整数，收到 {date!r}")
    return int(text)


def _validate_batch_quotes(requested, page, on_missing="warn", ctx="0x122B"):
    """批量报价页校验（2026-11-05 MEDIUM⑤）：行数越界 raise / 外来记录告警 / 缺失与全零占位。

    EX 语义是"允许少于 N"（服务器可丢弃无报价成员，实测 6 请求回 5）——缺失默认
    只告警不炸；行数 > 请求数属服务器异常，无条件 raise（防外来记录混入）。

    :param requested: 请求对 [(market_id, code), ...]
    :param page: 该页返回 DataFrame（可能为空）
    :param on_missing: "warn"（默认，logger 告警）/ "raise"（缺失即 ValueError）/ "ignore"
    :param ctx: 日志上下文（门面名/命令名）
    """
    if on_missing not in ("warn", "raise", "ignore"):
        raise ValueError(f"on_missing 仅接受 'warn'/'raise'/'ignore'，收到 {on_missing!r}")
    if page is None or len(page) == 0:
        returned, zero_keys = set(), set()
    else:
        # 行数越界（> 请求数）= 外来记录/服务器异常，无条件 raise（含 ignore——
        # ignore 只关缺失/占位告警，不关防混入硬校验）
        if len(page) > len(requested):
            raise ValueError(
                f"{ctx} 返回 {len(page)} 行 > 请求 {len(requested)} 只（外来记录/服务器异常）"
            )
        keys = [(int(m), str(c)) for m, c in zip(page["market"], page["code"])]
        returned = set(keys)
        # 全零占位行（close/pre_close/vol/amount 全 0 = 无真实报价；pre_close 也 0
        # 才算——盘前/停牌 close=0 但 pre_close 有值，不误报）
        zero_mask = pd.Series(True, index=page.index)
        for col in ("close", "pre_close", "vol", "amount"):
            if col in page.columns:
                zero_mask &= page[col].fillna(0) == 0
        zero_keys = {k for k, z in zip(keys, zero_mask) if z}
    missing = [(int(m), str(c)) for m, c in requested if (int(m), str(c)) not in returned]
    if on_missing == "raise" and (missing or zero_keys):
        detail = f"缺失 {missing[:8]}{'…' if len(missing) > 8 else ''}"
        if zero_keys:
            detail += f"；全零占位 {sorted(zero_keys)[:8]}{'…' if len(zero_keys) > 8 else ''}"
        raise ValueError(f"{ctx} {detail}")
    if on_missing != "warn":
        return  # "ignore"：只保留防混入硬校验，缺失/占位告警关闭
    if missing:
        logger.warning(
            f"{ctx} 请求 {len(requested)} 只返回 {len(returned)} 行，缺失："
            f"{missing[:8]}{'…' if len(missing) > 8 else ''}"
        )
    if zero_keys:
        logger.warning(f"{ctx} 全零占位行（无真实报价）：{sorted(zero_keys)[:8]}{'…' if len(zero_keys) > 8 else ''}")


def _page_stall_guard(prev_key, page, key_fn, ctx):
    """翻页停滞守卫（2026-11-05 LOW 批次）：新页首键与上页首键相同 → 服务器对同一
    pos 反复返回同页（状态机卡死），继续翻只会静默重复直到 max_pages——raise 真相。

    正常翻页 pos 单调推进，相邻两页首行键必不同；键取 None（缺字段）时跳过检测。

    :param prev_key: 上一页首行键（None=上页为空/首次）
    :param page: 本页行列表（非空才检测）
    :param key_fn: 行 → 键（bars 用 datetime；ticks 用 (时间, price, vol) 三元组——
                   同分钟多笔合法，单字段撞键不判停滞）
    :param ctx: 日志上下文（门面名）
    :return: 本页首行键（供下一轮传 prev_key；空页原样返回 prev_key）
    """
    if not page:
        return prev_key
    key = key_fn(page[0])
    if key is None:
        return prev_key
    if prev_key is not None and key == prev_key:
        raise RuntimeError(
            f"{ctx} 翻页停滞：服务器对同一 pos 重复返回同页（首行键 {key!r}），"
            "数据源异常——已拉取数据不完整，请检查服务器或稍后重试"
        )
    return key


def _paginate_adaptive(fetch, page_size, *, start=0, hard_limit=None, max_pages=None,
                       stall_key=None, ctx="", max_requests=20000):
    """自适应翻页（2026-11-09）：无论调用方把 page_size 设多大，都不因「哨兵误判」丢数。

    背景：服务器对每类命令有硬上限（2026-11-09 实测：std K 线 800、std 分笔 2000、
    EX 分笔 1800、EX K 线 700、MAC 逐笔 1000、MAC K 线 700），超限表现为
    「静默截断」（分笔/列表/MAC/EX）或「整请求失败」（std K 线，由 `_hq_call` 抛
    `TdxFunctionCallError`，不会走到本函数）。而翻页普遍用
    「返回数 < 请求页大小 ⇒ 已到末页」判断结束——若请求页大于服务器上限，哨兵会
    立即误判末页，静默丢掉后续数据。

    两层防护：
    1. **静态钳制**：page_size 超过该命令已知上限（hard_limit）时直接降到上限，
       零额外请求。
    2. **运行时自适应**：每次「返回数 < 有效页大小」时，把页大小降到实际返回数
       继续翻，由「空页」判定结束。这样可吸收**任意次**欠填（真末页、服务器上限
       低于常量、服务器抖动短页），代价是序列末尾多 1 次空请求。
       （早期实现用一次性 probed 标志，第二次短页会被误判末页 —— 已废弃。）

    :param fetch: (pos, count) -> list | None（None = 该次请求失败或无更多数据）
    :param page_size: 调用方请求的页大小
    :param start: 起始偏移（默认 0）
    :param hard_limit: 该命令服务器硬上限（None=未知，不做静态钳制）
    :param max_pages: 数据页数上限（None=不限）；达到即静默返回已拉到的部分
    :param stall_key: 停滞守卫的取键函数（None=不检测）
    :param ctx: 日志上下文
    :param max_requests: 请求次数熔断（防「服务器恒返回极少条数」把一次查询拖成
        数千次请求；正触发时 raise，不静默返回）
    :return: (rows, effective_page_size, requests)
    """
    eff = min(int(page_size), hard_limit) if hard_limit else int(page_size)
    rows: list = []
    pos = int(start)
    pages = 0
    requests = 0
    prev_key = None

    while max_pages is None or pages < max_pages:
        if requests >= max_requests:
            raise RuntimeError(
                f"{ctx} 翻页请求数超过 {max_requests}（有效页已降到 {eff}）——"
                "疑似服务器异常（恒返回极少条数），已中止以免拖垮调用方")
        page = fetch(pos, eff)
        requests += 1
        if page is None:
            # None = 请求失败，或 std 分笔越界查询的正常末页形态（实测 start 超范围返回 None）。
            # 二者无法区分，保持既有语义：停止并返回已拉部分。
            break
        if not page:
            break
        if stall_key is not None:
            prev_key = _page_stall_guard(prev_key, page, stall_key, ctx=ctx)
        rows.extend(page)
        pos += len(page)
        pages += 1
        if len(page) < eff:
            # 真末页 / 服务器上限低于 hard_limit / 抖动欠填 都可能，统一降档继续
            eff = len(page)
            continue

    return rows, eff, requests


def _rows_to_df(rows):
    # get_finance_info 的 parser 返回单个 OrderedDict（非 list）——直接 list(dict)
    # 只会拿到字段名（值全丢，2026-10-19 tick 全量任务实测）。dict 分支包成单行表。
    if isinstance(rows, dict):
        return pd.DataFrame([rows])
    return pd.DataFrame(list(rows or []))


def _index_board_code(code):
    """指数代码 → MAC 板块成分码（0x122C；映射移植自 pytdxdata _convert_board_code）。

    000xxx（沪指数）→ 31000+int（000300→31300）；399xxx（深指数）→ int-399000+30000
    （399001→30001）；899xxx（北证）→ int-899000+32000（899050→32050）；880xxx
    （通达信板块指数）→ int-880000+20000。
    """
    if code.startswith("399"):
        return int(code) - 399000 + 30000
    if code.startswith("899"):
        return int(code) - 899000 + 32000
    if code.startswith(("000", "880")):
        return 31000 + int(code) if code.startswith("000") else int(code) - 880000 + 20000
    raise ValueError(
        f"非指数代码段: {code!r}（index_trade_stats 支持 000xxx/399xxx/899xxx/880xxx）"
    )


def _sorted_df(rows):
    """bars/ticks 行 → DataFrame，按 datetime 升序排序（kline/分笔时间序一致）。"""
    df = pd.DataFrame(list(rows or []))
    if not df.empty and "datetime" in df.columns:
        df = df.sort_values("datetime").reset_index(drop=True)
    return df


_PROBE_FLOWS = {
    # kind → (API 类, 业务命令方法名或 None, 命令参数)
    # mac 无显式查询方法——connect 内置的老三条 SETUP 即 3 条真实业务命令往返
    "std": (TdxHq_API, "get_security_count", (0,)),
    "ex": (TdxExHq_API, "get_instrument_count", ()),
    "mac": (TdxMac_API, None, ()),
}


def _business_ping(host, port, timeout, kind="std", detail=False):
    """三段业务探测（F1.3）：TCP 连接 → 协议握手（connect 内置 setup）→ 轻量业务命令。

    探测不再是纯 TCP——必须真实完成协议握手并成功执行一条轻量业务命令
    （std=证券数量、ex=品种数量、mac=老三条 SETUP），防"能建 TCP 但返回
    空/垃圾响应"的假活服务器被误选为最快。

    :param kind: 通道类型 "std"/"ex"/"mac"（决定 API 类与探测命令）
    :param detail: True 时返回 ``(latency_or_None, stage)`` 供测试分类断言；
        stage ∈ {"tcp", "connect", "probe", "ok"}——tcp=TCP 建连失败、
        connect=握手/setup 失败、probe=业务命令失败
    :return: 成功返回总耗时（秒）；失败返回 None
    """
    api_cls, method, args = _PROBE_FLOWS.get(kind, _PROBE_FLOWS["std"])
    stage = "tcp"
    api = None
    try:
        t0 = time.monotonic()
        api = api_cls(multithread=False, heartbeat=False)
        stage = "connect"
        if not api.connect(host, port, time_out=timeout):
            return (None, stage) if detail else None
        stage = "probe"
        if method is not None:
            count = getattr(api, method)(*args)
            if not count or int(count) <= 0:
                return (None, stage) if detail else None
        latency = time.monotonic() - t0
        return (latency, "ok") if detail else latency
    except Exception:  # noqa: BLE001——探测对任何失败一律静默降级为不可达
        return (None, stage) if detail else None
    finally:
        if api is not None:
            try:
                api.disconnect()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# 门面
# ---------------------------------------------------------------------------

class TdxClient:
    """通达信数据统一门面（标准市场 + 扩展市场，点分式 symbol）。

    用法::

        client = TdxClient.auto()            # 自动测速选优
        client.bars("sh.601919", period="1min", count=100)
        client.bars("us.AAPL")               # 美股（EX 7727 自动路由）
        client.ticks("sh.601919", date="2020-01-02")
        client.quotes(["sh.600519", "us.AAPL"])   # 混合列表实时快照

        with TdxClient(host="111.229.247.189") as client:
            client.bars("上期所.cu2510", period="1min")
    """

    STD_SERVERS = STD_SERVERS
    EX_SERVERS = EX_SERVERS
    MAC_SERVERS = MAC_SERVERS
    MAC_EX_SERVERS = MAC_EX_SERVERS

    _health = {}  # 类级健康分字典 {(host, port): HealthState}——跨实例/跨 auto() 共享

    def __init__(self, host=None, port=7709, ex_host=None, ex_port=7727,
                 mac_host=None, mac_port=7709, mac_ex_host=None, mac_ex_port=7727,
                 heartbeat=True, timeout=10.0, f10_category_ttl=3600.0):
        """
        :param host: 标准市场服务器；None 时首次调用自动测速选优
        :param ex_host: 扩展市场服务器；None 时首次调用 EX 品种自动测速选优
        :param mac_host: MAC 通道服务器；None 时首次调用 MAC 品种自动测速选优
        :param mac_ex_host: MAC EX 通道服务器；None 时首次调用 MAC EX 品种自动测速选优
        :param heartbeat: 是否开启协议心跳保活
        :param timeout: 连接/读写超时（秒）
        :param f10_category_ttl: F10 分类列表缓存秒数（分类变化频率低，默认 1h）
        """
        self._host = host
        self._port = port
        self._ex_host = ex_host
        self._ex_port = ex_port
        self._mac_host = mac_host
        self._mac_port = mac_port
        self._mac_ex_host = mac_ex_host
        self._mac_ex_port = mac_ex_port
        self._heartbeat = heartbeat
        self._timeout = timeout
        self._f10_category_ttl = f10_category_ttl
        self._f10_category_cache = {}  # {(market_id, code): (monotonic_ts, entries)}
        self._hq_api = None
        self._ex_api = None
        self._mac_api = None
        self._mac_ex_api = None
        self._lock = threading.Lock()  # 懒加载双重检查锁（多线程首次并发调用只建一个连接）

    # -- 构造方式 -----------------------------------------------------------

    @classmethod
    def auto(cls, timeout=2.0, heartbeat=True, servers=None, ex_servers=None, call_timeout=10.0):
        """并发测速选优后创建客户端。

        ⚠️ 内部用 ThreadPoolExecutor 并发测速——**只应在主线程调用**；
        在已开线程池的 worker 线程里再调 auto() 会嵌套卡死（2026-10-19 实测
        CPU 0% 零进度）。多 worker 场景：主线程 auto() 选优后取
        `client._host/_port` 传给 worker，worker 直接 `TdxClient(host, port)` 构造。

        :param timeout: 单服务器探测超时（秒）
        :param servers: 覆盖默认标准市场服务器池 [(host, port), ...]
        :param ex_servers: 覆盖默认扩展市场服务器池
        """
        best = cls._pick(servers or cls.STD_SERVERS, timeout, kind="std")
        best_ex = cls._pick(ex_servers or cls.EX_SERVERS, timeout, kind="ex")
        return cls(host=best[0], port=best[1], ex_host=best_ex[0], ex_port=best_ex[1],
                   heartbeat=heartbeat, timeout=call_timeout)

    @classmethod
    def _pick(cls, servers, timeout, kind="std"):
        """并发业务探测选优（三段判定 + 健康分排序），返回最优 (host, port)。

        F1.3：每台服务器须通过 TCP→握手→轻量业务命令三段判定才算可达；
        F1.4：结果写入类级健康分（冷却期节点让位健康节点，全池冷却时
        仍兜底选延迟最优者）。
        """
        with ThreadPoolExecutor(max_workers=len(servers)) as pool:
            latencies = list(pool.map(lambda s: _business_ping(s[0], s[1], timeout, kind), servers))
        now = time.monotonic()
        candidates = []
        for (host, port), latency in zip(servers, latencies):
            # 读-改-写非原子：并发 _pick 间可能 lost update——benign（健康分
            # 自纠正，下次探测重新记账；CPython GIL 下单条 dict 赋值原子，
            # 字典结构不会损坏），不加锁。
            state = health_update(cls._health.get((host, port)), latency is not None, latency, now)
            cls._health[(host, port)] = state
            if latency is not None:
                candidates.append(((host, port), state))
        ranked = health_rank(candidates, now)
        if not ranked:
            raise TdxConnectionError(
                f"所有服务器均不可达：{servers}", code=CODE_POOL_EXHAUSTED
            )
        return ranked[0][0]

    # -- 连接管理 ------------------------------------------------------------
    def _ensure_hq(self):
        if self._hq_api is None:
            with self._lock:
                if self._hq_api is None:
                    if self._host is None:
                        self._host, self._port = self._pick(self.STD_SERVERS, 2.0)
                    # 门面承诺并发安全：底层 API 显式开锁（multithread=True），
                    # 即使 heartbeat=False 连接锁也存在，多线程共享连接不撕裂。
                    # raise_exception=True：断线时命令抛 TdxFunctionCallError，
                    # _hq_call 才能捕获并重建连接（默认 False 会吞成 None，重试失效）。
                    api = TdxHq_API(multithread=True, heartbeat=self._heartbeat, raise_exception=True)
                    if not api.connect(self._host, self._port, time_out=self._timeout):
                        raise TdxConnectionError(f"无法连接标准市场 {self._host}:{self._port}")
                    self._hq_api = api
        return self._hq_api

    def _ensure_ex(self):
        if self._ex_api is None:
            with self._lock:
                if self._ex_api is None:
                    if self._ex_host is None:
                        self._ex_host, self._ex_port = self._pick(self.EX_SERVERS, 2.0, kind="ex")
                    api = TdxExHq_API(multithread=True, heartbeat=self._heartbeat, raise_exception=True)
                    if not api.connect(self._ex_host, self._ex_port, time_out=self._timeout):
                        raise TdxConnectionError(f"无法连接扩展市场 {self._ex_host}:{self._ex_port}")
                    self._ex_api = api
        return self._ex_api

    def _ensure_mac(self):
        """MAC 通道懒加载（第三连接；老三条 SETUP + MAC 服务器池，T0.4 实测门禁结论）。

        MAC 服务器池小（3 台）且测速扇出无谓，直接按列表顺序首台可用即用。
        MAC 通道固定不开心跳（标准心跳命令在 MAC 专用机上语义未验证）。
        """
        if self._mac_api is None:
            with self._lock:
                if self._mac_api is None:
                    errors = []
                    hosts = [(self._mac_host, self._mac_port)] if self._mac_host else self.MAC_SERVERS
                    for host, port in hosts:
                        api = TdxMac_API(multithread=True, heartbeat=False, raise_exception=True)
                        try:
                            if api.connect(host, port, time_out=self._timeout):
                                self._mac_host, self._mac_port = host, port
                                self._mac_api = api
                                self._health[(host, port)] = health_update(
                                    self._health.get((host, port)), True, None)
                                return self._mac_api
                        except Exception as e:  # noqa
                            errors.append(f"{host}:{e}")
                        self._health[(host, port)] = health_update(
                            self._health.get((host, port)), False, None)
                    raise TdxConnectionError(
                        "无法连接 MAC 通道服务器（老三条 SETUP + MAC 帧）："
                        + "; ".join(errors or [f"{self._mac_host}:{self._mac_port}"])
                    )
        return self._mac_api

    def _ensure_mac_ex(self):
        """MAC EX 通道懒加载（第四连接；0x2454 login + EX 帧，T0.4-EX 实测门禁结论）。

        EX 服务器池小（2 台 login 模式机）且测速扇出无谓，直接按列表顺序
        首台可用即用。通道固定不开心跳（do_heartbeat 走 EX 帧随机 K 线）。
        """
        if self._mac_ex_api is None:
            with self._lock:
                if self._mac_ex_api is None:
                    errors = []
                    hosts = ([(self._mac_ex_host, self._mac_ex_port)]
                             if self._mac_ex_host else self.MAC_EX_SERVERS)
                    for host, port in hosts:
                        api = TdxMacEx_API(multithread=True, heartbeat=False, raise_exception=True)
                        try:
                            if api.connect(host, port, time_out=self._timeout):
                                self._mac_ex_host, self._mac_ex_port = host, port
                                self._mac_ex_api = api
                                self._health[(host, port)] = health_update(
                                    self._health.get((host, port)), True, None)
                                return self._mac_ex_api
                        except Exception as e:  # noqa
                            errors.append(f"{host}:{e}")
                        self._health[(host, port)] = health_update(
                            self._health.get((host, port)), False, None)
                    raise TdxConnectionError(
                        "无法连接 MAC EX 通道服务器（0x2454 login + EX 帧）："
                        + "; ".join(errors or [f"{self._mac_ex_host}:{self._mac_ex_port}"])
                    )
        return self._mac_ex_api

    def _close_hq(self):
        # 与 _ensure_hq 同一把锁：避免 close 与首次连接并发时
        # 新连接引用被清掉导致 socket 泄漏（2026-09-20 复审加固）
        with self._lock:
            if self._hq_api is not None:
                try:
                    self._hq_api.disconnect()
                except Exception:  # noqa
                    pass
                self._hq_api = None

    def _close_ex(self):
        with self._lock:
            if self._ex_api is not None:
                try:
                    self._ex_api.disconnect()
                except Exception:  # noqa
                    pass
                self._ex_api = None

    def _close_mac(self):
        with self._lock:
            if self._mac_api is not None:
                try:
                    self._mac_api.disconnect()
                except Exception:  # noqa
                    pass
                self._mac_api = None

    def _close_mac_ex(self):
        with self._lock:
            if self._mac_ex_api is not None:
                try:
                    self._mac_ex_api.disconnect()
                except Exception:  # noqa
                    pass
                self._mac_ex_api = None

    def close(self):
        """关闭标准市场、扩展市场、MAC 通道与 MAC EX 通道连接。"""
        self._close_hq()
        self._close_ex()
        self._close_mac()
        self._close_mac_ex()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _hq_call(self, method, *args, **kwargs):
        """调用标准市场命令；连接类异常时重建连接重试一次。

        底层 raise_exception=True：断线时命令抛 TdxFunctionCallError（original 为
        连接类异常），一并捕获触发重建——满足"断线自动重建连接重试一次"承诺。
        """
        last_exc = None
        for _attempt in range(2):
            api = self._ensure_hq()
            try:
                return getattr(api, method)(*args, **kwargs)
            except (TdxConnectionError, TdxFunctionCallError, ConnectionError, socket.timeout, OSError) as exc:
                last_exc = exc
                self._close_hq()
        raise last_exc

    def _ex_call(self, method, *args, **kwargs):
        last_exc = None
        for _attempt in range(2):
            api = self._ensure_ex()
            try:
                return getattr(api, method)(*args, **kwargs)
            except (TdxConnectionError, TdxFunctionCallError, ConnectionError, socket.timeout, OSError) as exc:
                last_exc = exc
                self._close_ex()
        raise last_exc

    def _mac_call(self, method, *args, **kwargs):
        """调用 MAC 通道命令；连接类异常时重建连接重试一次（语义同 _hq_call）。"""
        last_exc = None
        for _attempt in range(2):
            api = self._ensure_mac()
            try:
                return getattr(api, method)(*args, **kwargs)
            except (TdxConnectionError, TdxFunctionCallError, ConnectionError, socket.timeout, OSError) as exc:
                last_exc = exc
                self._close_mac()
        raise last_exc

    def _mac_ex_call(self, method, *args, **kwargs):
        """调用 MAC EX 通道命令；连接类异常时重建连接重试一次（语义同 _hq_call）。"""
        last_exc = None
        for _attempt in range(2):
            api = self._ensure_mac_ex()
            try:
                return getattr(api, method)(*args, **kwargs)
            except (TdxConnectionError, TdxFunctionCallError, ConnectionError, socket.timeout, OSError) as exc:
                last_exc = exc
                self._close_mac_ex()
        raise last_exc

    def _std_symbol(self, symbol):
        """MAC 门面共用：解析点分式 symbol 并限定标准市场（sh/sz/bj）。"""
        kind, market_id, code = _parse_symbol(symbol)
        if kind != "std":
            raise ValueError(f"MAC 通道仅支持标准市场（sh/sz/bj），收到 {symbol!r}")
        return market_id, code

    def _ex_symbol(self, symbol):
        """MAC EX 门面共用：解析点分式 symbol 并限定扩展市场（ExMarket id 空间）。

        ⚠️ std 市场（sh/sz/bj）id 0-2 与 ExMarket id 空间**不通用**（ExMarket
        1=TEMP 临时股，0/2 无定义）——std symbol 直传会被 EX 服务器按 ExMarket
        语义错解释，实测 sh.600000/sz.000001/bj.899050 全部返回空。
        A 股数据请走 MAC 通道（mac_bars/mac_minute/mac_ticks）。
        """
        kind, market_id, code = _parse_symbol(symbol)
        if kind == "std":
            raise ValueError(
                f"MAC EX 通道仅支持扩展市场（ex），收到标准市场 symbol {symbol!r}。"
                "A 股（sh/sz/bj）请用 mac_bars/mac_minute/mac_ticks"
            )
        return market_id, code

    # -- K 线 -----------------------------------------------------------------

    def bars(self, symbol, period="day", start=0, count=100, adjust=None,
             all_pages=False, page_size=MAX_PAGE_SIZE, max_pages=None):
        """K 线万能源（std 个股/债券/基金/指数 + EX 期货/期权/外盘/港股/美股）。

        :param symbol: 点分式 "{市场}.{代码}"，如 "sh.601919"/"sh.000001"（上证指数，
                       指数段自动路由指数命令）/"us.AAPL"/"hk.01398"/"main.AL8"
        :param period: "1min"/"5min"/"15min"/"30min"/"1h"/"day"/"week"/"month"
        :param start: 起始偏移（0=最新，向前翻页递增）
        :param count: 条数（std 单次 ≤800）
        :param adjust: None/"qfq"/"hfq"（服务端复权；仅标准市场，EX 须 None）
        :param all_pages: True 时从 start 自动翻页拉全量
        :param page_size: 翻页单页条数（std ≤800，EX ≤700）
        :param max_pages: 翻页页数上限（防失控；None=不限）
        :return: pd.DataFrame（datetime/open/close/high/low/vol/amount，按时间升序）
        """
        kind, market_id, code = _parse_symbol(symbol)
        category = normalize_period(period)

        if kind == "ex":
            if adjust is not None:
                market_name = EX_MARKET_NAMES.get(market_id, str(market_id))
                raise ValueError(f"EX 市场（{market_name}）不支持服务端复权，adjust 须为 None")
            # 页大小上限由 _paginate_adaptive 的 hard_limit 统一钳制

            def fetch(pos, cnt):
                return self._ex_call("get_instrument_bars", category, market_id, code, pos, cnt)
        else:
            adjust_id = normalize_adjust(adjust)
            command = "get_index_bars" if _is_index_code(market_id, code) else "get_security_bars"

            def fetch(pos, cnt):
                return self._hq_call(command, category, market_id, code, pos, cnt, adjust=adjust_id)

        if not all_pages:
            return _sorted_df(fetch(start, count))

        frames, _, _ = _paginate_adaptive(
            fetch, page_size, start=start,
            hard_limit=EX_BARS_PAGE_SIZE if kind == "ex" else MAX_PAGE_SIZE,
            max_pages=max_pages,
            stall_key=lambda r: r.get("datetime"),
            ctx=f"bars({symbol})",
        )
        return _sorted_df(frames)

    def bars_range(self, symbol, start_date, end_date):
        """EX 专属按日期区间的 1 分钟 K 线（原 ex_history_bars_range，EX 专属）。

        单次 ~1500 根上限，区间过大请分段；仅当前在市的合约/主连（品种+L8）有效，
        已摘牌历史合约查不到。标准市场请用 bars() 的 start/count 翻页。

        :param symbol: 点分式 EX symbol，如 "main.AL8"/"上期所.cu2510"/"zce.AP510"
        :param start_date: "2020-01-22" / "20200122" / 20200122
        :param end_date: 同 start_date
        """
        kind, market_id, code = _parse_symbol(symbol)
        if kind == "std":
            raise ValueError("日期区间查询仅 EX 市场支持；标准市场请用 bars() 的 start/count 翻页")
        rows = self._ex_call("get_history_instrument_bars_range", market_id, code,
                             normalize_date(start_date), normalize_date(end_date))
        return _sorted_df(rows)

    def delisted_bars(self, symbol, start=0, count=800, all_pages=False,
                      page_size=MAX_PAGE_SIZE, max_pages=200):
        """退市股不复权日线（0x052B，2026-10 自 eltdx 移植）。

        :param symbol: 单只代码或代码列表，兼容四种写法：
                       "sz.000038"（点分式，与其他门面统一）/ "sz000038"（前缀式）
                       / "T000038"（eltdx 写法）/ "000038"（裸代码按首位推断市场，
                       92/8→BJ、6/9→SH、0/1/2/3→SZ）
        :param start: 起始偏移（0=最新一根，向前翻页递增，语义同 bars()）
        :param count: 条数（all_pages=False 时生效，≤800）
        :param all_pages: True 时从 start 正序自动翻页拉到数据尽头
        :param page_size: 翻页单页条数（≤800）
        :param max_pages: 翻页页数上限（防失控；None=不限）
        :return: 单只 → pd.DataFrame（datetime/open/close/high/low/vol/amount，时间升序）；
                 列表 → dict{标准点分代码: DataFrame}。
                 批量先全部校验归一化（含去重）再开始查询——不产生半成品结果
        """
        if isinstance(symbol, str):
            market_id, code, std_symbol = _normalize_delisted_symbol(symbol)
            return self._delisted_bars_one(market_id, code, start, count, all_pages, page_size, max_pages)

        if not isinstance(symbol, (list, tuple, set)):
            raise TypeError(f"symbol 须为字符串或字符串序列，收到 {type(symbol).__name__}")

        # 先全部校验归一化去重，再开始查询（吸收 eltdx 纪律：避免查到一半发现无效代码）
        normalized = {}
        for item in symbol:
            market_id, code, std_symbol = _normalize_delisted_symbol(item)
            normalized[std_symbol] = (market_id, code)

        if not normalized:
            raise ValueError("symbol 列表为空")

        return {
            std_symbol: self._delisted_bars_one(market_id, code, start, count, all_pages, page_size, max_pages)
            for std_symbol, (market_id, code) in normalized.items()
        }

    def _delisted_bars_one(self, market_id, code, start, count, all_pages, page_size, max_pages):
        """单只退市股查询（归一化之后）。"""
        page_size = int(page_size)  # 上限钳制由 _paginate_adaptive 的 hard_limit 统一处理

        if not all_pages:
            if not isinstance(count, int) or isinstance(count, bool) or not 0 < count <= MAX_PAGE_SIZE:
                raise ValueError(f"count 须为 1~{MAX_PAGE_SIZE} 的整数，收到 {count!r}")
            return _sorted_df(self._hq_call("get_delisted_bars", market_id, code, start, count))

        if max_pages is not None and (
            isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages <= 0
        ):
            raise ValueError("max_pages 须为正整数或 None")

        frames, _, _ = _paginate_adaptive(
            lambda pos, cnt: self._hq_call("get_delisted_bars", market_id, code, pos, cnt),
            page_size, start=start, hard_limit=MAX_PAGE_SIZE, max_pages=max_pages,
            stall_key=lambda r: r.get("datetime"), ctx=f"delisted_bars({code})",
        )
        # 语义对齐 bars()：达到 max_pages 静默返回已拉到的部分数据（不 raise）
        return _sorted_df(frames)

    # -- 分时/分笔 ------------------------------------------------------------

    def minute(self, symbol):
        """当日分时（240 个分钟点；price/vol；EX 品种含 open_interest 持仓量，期货含夜盘）。

        :param symbol: 点分式，如 "sh.601919"/"sz.127089"/"hk.01398"/"上期所.cu2510"
        """
        kind, market_id, code = _parse_symbol(symbol)
        if kind == "ex":
            return _rows_to_df(self._ex_call("get_minute_time_data", market_id, code))
        return _rows_to_df(self._hq_call("get_minute_time_data", market_id, code))

    def minute_history(self, symbol, date=None):
        """指定交易日分时。date 缺省=今天；支持 "2020-01-02"/20200102。

        :param symbol: 点分式，如 "sh.601919"/"hk.01398"/"main.AL8"
        :param date: None=今天；"YYYY-MM-DD" / "YYYYMMDD" / 8 位整数
        """
        kind, market_id, code = _parse_symbol(symbol)
        trade_date = normalize_date(date) if date is not None else int(datetime.now().strftime("%Y%m%d"))
        if kind == "ex":
            return _rows_to_df(self._ex_call("get_history_minute_time_data", market_id, code, trade_date))
        return _rows_to_df(self._hq_call("get_history_minute_time_data", market_id, code, trade_date))

    def ticks(self, symbol, date=None, all=True):
        """逐笔成交。

        :param symbol: 点分式，如 "sh.601919"/"hk.01398"/"上期所.cu2510"
        :param date: None=当天（内部走历史分笔接口传今天日期，盘后/非交易时段可用）
        :param all: True 自动翻页拉全天并按时间排序（std 2000/页，ex 1800/页）；False 只取第一页

        :return: pd.DataFrame。列结构分市场：
            - std: ``time(HH:MM) / price / vol / buyorsell``
            - ex:  ``date(datetime) / hour / minute / price / volume / zengcang /
              natrue_name / nature_name / direction / nature``
            无数据日期（服务器未存该日分笔）返回同列结构的空 DataFrame。

        .. note:: EX 分笔服务器仅保留数月级深度（2026-09 实测：CUL8 2026-06 仍有
           18851 条、2024-01 已为空），更早日期请用 bars_range 的 1 分钟 K 线
           （深度更深）。非交易日返回空属正常。
        """
        kind, market_id, code = _parse_symbol(symbol)
        trade_date = normalize_date(date) if date is not None else int(datetime.now().strftime("%Y%m%d"))

        # 空结果补标准列：无数据日期（服务器未存该日分笔）返回空但列结构与有数据时
        # 一致，下游按列访问不 KeyError（2026-10-19 tick 全量任务实测踩坑）。
        _STD_TICK_COLS = ["time", "price", "vol", "buyorsell"]
        _EX_TICK_COLS = ["date", "hour", "minute", "price", "volume", "zengcang",
                         "natrue_name", "nature_name", "direction", "nature"]

        if kind == "ex":
            if not all:
                rows = self._ex_call("get_history_transaction_data", market_id, code, trade_date, 0, EX_PAGE_SIZE)
                return _sorted_df(rows) if rows else pd.DataFrame(columns=_EX_TICK_COLS)
            rows, _, _ = _paginate_adaptive(
                lambda pos, cnt: self._ex_call(
                    "get_history_transaction_data", market_id, code, trade_date, pos, cnt),
                EX_PAGE_SIZE, hard_limit=EX_PAGE_SIZE,
                stall_key=lambda r: (r.get("date"), r.get("price"), r.get("volume")),
                ctx=f"ticks(ex,{code})",
            )
            if not rows:
                return pd.DataFrame(columns=_EX_TICK_COLS)
            # 分页拼接后页间逆序（start=0 取最新段，递增翻页往回翻）——统一按时间重排。
            # EX tick 字段为 date(datetime 含秒)；std 为 time(HH:MM 字符串)。
            rows.sort(key=lambda r: r["date"] if "date" in r else r["time"])
            return _sorted_df(rows)

        if not all:
            rows = self._hq_call("get_history_transaction_data", market_id, code, 0, MAX_PAGE_SIZE, trade_date)
            return _sorted_df(rows) if rows else pd.DataFrame(columns=_STD_TICK_COLS)
        rows, _, _ = _paginate_adaptive(
            lambda pos, cnt: self._hq_call(
                "get_history_transaction_data", market_id, code, pos, cnt, trade_date),
            TICK_PAGE_SIZE, hard_limit=TICK_PAGE_SIZE,
            stall_key=lambda r: (r.get("time"), r.get("price"), r.get("vol")),
            ctx=f"ticks(std,{code})",
        )
        if not rows:
            return pd.DataFrame(columns=_STD_TICK_COLS)
        # 同上：分页拼接页间逆序，统一按 time 重排（time 为 HH:MM 带前导零，字符串序=时间序）。
        # ⚠️ 同一分钟内的笔序依赖服务器返回顺序（分笔实为 3 秒快照聚合，HH:MM 粒度
        # 内无更细时间戳），跨页同分钟笔序不保证稳定——协议口径如此，非实现缺陷。
        rows.sort(key=lambda r: r["time"])
        return _sorted_df(rows)

    # -- MAC 通道（第三连接：老三条 SETUP + MAC 帧 + MAC 服务器池）-------------

    def mac_bars(self, symbol, period="day", start=0, count=700, adjust=None, times=None):
        """MAC 通道 K 线（0x122E；支持复权、含 amount，秒级分时周期可用）。

        与 bars() 的差异：走 MAC 服务器池（独立连接，老三条 SETUP 会话）；
        页上限 700；数据面与标准通道互相独立（部分品种两边都有，MAC 侧更新更快）。

        :param symbol: 点分式标准市场 symbol，如 "sh.600000"/"sz.000001"
        :param period: "1min"/"5min"/"day" 等（同 bars()）；另支持参数化 N 分钟
                      别名 "Nmin"（N=2~240，如 "10min" → 10 分钟 bar）；详见 times
        :param start: 起始偏移（0=最新一根，向前翻页递增）
        :param count: 条数（≤700）
        :param adjust: None/0=不复权，1="qfq"，2="hfq"
        :param times: 参数化 N 分钟（MAC 协议钩子，仅 1 分钟系生效）：period="1min"
                      且 times=N → N 分钟 bar（实测 2~120 全通）；None=默认 1 分钟。
                      例：mac_bars(sym, period="1min", times=5) ≡ mac_bars(sym, period="5min")。
                      ⚠️ MAC 侧编码 7=固定 1min（times 被忽略）、8=参数化 1min——
                      门面经 resolve_mac_period 自动映射，勿绕过门面直传 std cat=7
        """
        market_id, code = self._std_symbol(symbol)
        category, times_value = resolve_mac_period(period, times)
        adjust_id = normalize_adjust(adjust)
        return _sorted_df(self._mac_call("get_mac_bars", market_id, code, category, start, count, adjust_id, times_value))

    def mac_minute(self, symbol, date=None):
        """MAC 通道单日分时（0x122D；240 点含均价 avg_price，可查历史交易日）。

        与 minute_history() 的差异：走 MAC 通道（独立数据面），含均价线。

        :param symbol: 点分式标准市场 symbol，如 "sh.600000"
        :param date: None=今天；"YYYY-MM-DD" / "YYYYMMDD" / 8 位整数
        """
        market_id, code = self._std_symbol(symbol)
        trade_date = normalize_date(date) if date is not None else 0
        return _rows_to_df(self._mac_call("get_mac_minute", market_id, code, trade_date))

    def mac_ticks(self, symbol, date=None, start=0, count=1000, drop_auction_orders=True):
        """MAC 通道逐笔成交（0x122F；秒级精度，含成交笔数 trade_count 与买卖方向 bs_flag）。

        与 ticks() 的差异：走 MAC 通道（独立数据面），时间戳精确到秒、每笔含成交笔数；
        深度实测达 2000 年（26 年，远超 std 历史分笔的 2006 年末）；
        老数据（约 2015 前）trade_count 恒 0（当年未记录该字段）。

        :param symbol: 点分式标准市场 symbol，如 "sh.600000"
        :param date: None=今天；"YYYY-MM-DD" / "YYYYMMDD" / 8 位整数
        :param start: 起始偏移（0=最新一笔，向前翻页递减；单页 ≤1000）
        :param count: 条数（≤1000）
        :param drop_auction_orders: True（默认）剔除集合竞价委托记录（bs_flag==8，
                非成交、vol 为累计值，计入会虚高 Σvol 20%~160%，2026-11-04 吸收自
                pytdxdata 0.6.5 实测）；过滤后行数可少于 count。False 保留原始记录
        """
        market_id, code = self._std_symbol(symbol)
        trade_date = normalize_date(date) if date is not None else 0
        rows = self._mac_call("get_mac_ticks", market_id, code, trade_date, start, count)

        if drop_auction_orders and rows:
            # 逐笔无自动翻页（单页调用），此处过滤不影响任何翻页终止语义
            rows = [r for r in rows if r["bs_flag"] != _BS_AUCTION_ORDER]

        return _rows_to_df(rows)

    # -- MAC EX 通道（第四连接：0x2454 login + EX 帧 + EX 服务器池）-------------
    # 港股/美股/全球指数/国内期货扩展行情；symbol 仅扩展市场（ExMarket id 空间）。

    def mac_ex_bars(self, symbol, period="day", start=0, count=700, adjust=None, times=None):
        """MAC EX 通道 K 线（0x122E；港股/美股/指数/期货，含复权与 amount）。

        与 bars()/mac_bars() 的差异：走 MAC EX 服务器池（独立连接，0x2454 login
        会话 + 帧首字节 0x01）；symbol 仅扩展市场
        （"hk.01398"/"us.AAPL"/"hk.HSI"/"上期所.RBL8"…）——std 市场（sh/sz/bj）
        id 与 ExMarket 空间不通用，A 股请走 mac_bars/mac_minute/mac_ticks。
        实测深度：美股日线 ~6945 根（1999-03 起）、港股 ~3500 根（2012-07 起）、
        期货主力连续 ~3171 根（2013-09 起）；页上限 700。

        :param symbol: 点分式 symbol，仅扩展市场（ex）
        :param period: "1min"/"5min"/"day" 等（同 bars()）；period="1min"+times=N
                       → 参数化 N 分钟（与 mac_bars 同钩子）
        :param adjust: None/0=不复权，1="qfq"，2="hfq"
        """
        market_id, code = self._ex_symbol(symbol)
        category, times_value = resolve_mac_period(period, times)
        adjust_id = normalize_adjust(adjust)
        return _sorted_df(self._mac_ex_call("get_ex_bars", market_id, code, category, start, count, adjust_id, times_value))

    def mac_ex_minute(self, symbol, date=None):
        """MAC EX 通道单日分时（0x122D；港股 330 点/期货 345 点含夜盘/含均价线）。

        :param symbol: 点分式 symbol，仅扩展市场（ex；std 请走 mac_minute）
        :param date: None=今天；"YYYY-MM-DD" / "YYYYMMDD" / 8 位整数
        """
        market_id, code = self._ex_symbol(symbol)
        trade_date = normalize_date(date) if date is not None else 0
        return _rows_to_df(self._mac_ex_call("get_ex_minute", market_id, code, trade_date))

    def mac_ex_ticks(self, symbol, date=None, start=0, count=1000):
        """MAC EX 通道逐笔成交（0x122F；美股/指数为分钟级聚合笔）。

        ⚠️ 字段语义差异（2026-10-19 实测）：期货逐笔 bs_flag 出现 >10000 值、
        trade_count 有 u32 环绕值——与 A 股方向语义不同，未定性前勿直接套用；
        期货逐笔深度仅 ~1 个月；港股逐笔无数据（分时可用）。

        :param symbol: 点分式 symbol，仅扩展市场（ex；std 请走 mac_ticks）
        :param date: None=今天；"YYYY-MM-DD" / "YYYYMMDD" / 8 位整数
        :param start: 起始偏移（0=最新一笔，向前翻页递减）
        :param count: 条数（≤1000）
        """
        market_id, code = self._ex_symbol(symbol)
        trade_date = normalize_date(date) if date is not None else 0
        return _rows_to_df(self._mac_ex_call("get_ex_ticks", market_id, code, trade_date, start, count))

    def mac_boards(self, board_type=0, start=0, count=100):
        """A 股板块列表（0x1231 MAC 通道；板块 + 领涨股快照，2026-10-20 实测）。

        板块码 881xxx（market=1）；board_type 实测：0=全部（total=128）/
        1=行业（total=345）；每行含 leader_*（领涨股市场/代码/名称/价格）。
        翻页：start 递增（count≤页上限 150 实测可通）。

        :param board_type: 板块类别（0=全部 / 1=行业；其余类别语义待探）
        :param start: 起始偏移
        :param count: 条数
        """
        return _rows_to_df(self._mac_call("get_mac_boards", board_type, start, count))

    def mac_ex_boards(self, board_type=0, start=0, count=100):
        """港股板块列表（0x1231 MAC EX 通道；板块码 HK0xxx，market=70）。

        每行含 leader_*（领涨股市场 31/48/代码/名称/价格）；翻页同 mac_boards。
        """
        return _rows_to_df(self._mac_ex_call("get_ex_boards", board_type, start, count))

    def mac_ex_quotes(self, symbols, bits=None, on_missing="warn"):
        """EX 批量报价（0x122B EX 帧；港股/美股/指数/期货，≤80 只/页自动分批）。

        :param symbols: 点分式 symbol 或其列表，如 "hk.01398"/"us.AAPL"/"hk.HSI"
                       （市场别名见 ex_markets()；std 市场不支持——走 mac_quotes）
        :param bits: 请求字段位（默认 0x00~0x07 八位全字段）
        :param on_missing: 缺失处理（2026-11-05 MEDIUM⑤）：默认 "warn" 告警缺失
                          名单与全零占位行；"raise" 缺失即 ValueError；"ignore"。
                          ⚠️ EX 侧无行对应承诺——服务器可丢弃无报价成员（实测
                          6 请求回 5），返回行数 ≤ 请求数属正常（告警不炸）
        :return: DataFrame（market/code/name/pre_close/open/high/low/close/vol/
                volume_ratio/amount）

        例：mac_ex_quotes(["hk.01398", "hk.00700", "us.AAPL", "hk.HSI"])
        """
        if isinstance(symbols, str):
            symbols = [symbols]
        if not symbols:
            return _rows_to_df([])
        pairs = [self._ex_symbol(s) for s in symbols]  # (ex_market_id, code)
        pages = []
        for i in range(0, len(pairs), 80):
            batch = pairs[i:i + 80]
            page = _rows_to_df(self._mac_ex_call("get_ex_batch_quotes", batch, bits))
            _validate_batch_quotes(batch, page, on_missing=on_missing, ctx="mac_ex_quotes/0x122B")
            pages.append(page)
        if len(pages) == 1:
            return pages[0]
        return pd.concat(pages, ignore_index=True)


    def mac_members(self, board, sort_type=0, start=0, count=80):
        """板块成分报价（0x122C MAC 通道；成分跨深沪北市场，2026-10-20 实测）。

        :param board: 板块码 '881248'（自动换算 881xxx→2xxxx；也可传换算后 int）
        :param sort_type: 排序方式（0=默认序，其余语义待探）
        :param start: 翻页偏移
        :param count: 条数
        """
        return _rows_to_df(self._mac_call("get_mac_board_members", board, sort_type, start, count))

    def mac_auction(self, symbol, start=0, count=100):
        """集合竞价逐笔（0x123D MAC 通道；09:15-09:25 3 秒一条，末条价格=开盘价）。

        :param symbol: 标准市场 symbol（sh.600000 / sz.000001）
        """
        market_id, code = self._std_symbol(symbol)
        return _rows_to_df(self._mac_call("get_mac_auction", market_id, code, start, count))

    def mac_unusual(self, market=0, start=0, count=600, all_pages=False, max_pages=50):
        """市场异动列表（0x1237 MAC 通道；type/type_name/触发时点价格）。

        price/amount 为异动触发时点快照（竞价时段为虚拟撮合值），非全天口径。

        :param all_pages: True=循环翻页拉全（start 递增 600/页，直到短页或空；
                          上限 max_pages 页防失控）。异动按时间序，盘后拉全即全天异动
        """
        rows = _rows_to_df(self._mac_call("get_mac_unusual", market, start, count))
        if not all_pages:
            return rows
        pages = [rows]
        page_size = min(int(count), 600)
        for _ in range(int(max_pages) - 1):
            start += page_size
            more = _rows_to_df(self._mac_call("get_mac_unusual", market, start, count))
            if more.empty or len(more) < page_size:
                if not more.empty:
                    pages.append(more)
                break
            pages.append(more)
        import pandas as pd
        return pd.concat(pages, ignore_index=True) if pages else rows

    def index_trade_stats(self, index, date=None, workers=4, max_members=3000, skip_errors=True):
        """指数每分钟成交量/成交笔数（成分股逐笔聚合；2026-11-05 LOW 批次立项）。

        为什么绕成分聚合：协议里**指数逐笔的 trade_count 是"当时均价×1000"，不是笔数**
        （pytdxdata b84a6c8 定性）——指数自身没有真实笔数，只能用成分股逐笔累加。

        实现走 MAC 通道：0x122C 板块成分（指数→板块码映射，pytdxdata 实测 sh000300
        300 只/sh000016 50 只等全数返回）+ 0x122F 逐笔翻页聚合。并发模式同
        finance_all（每 worker 独立 TdxClient + 轮转分片 + 单只失败隔离）。

        ⚠️ 口径注意（实测 2026-11-05）：
        - vol 单位 = **手**（股票段逐笔 raw 单位沪深同构，全天 Σ 与日线 vol 比值
          实测 0.010001）；成分含非股票（指数成分通常全为股票）时单位可能异构
        - 北交所成分逐笔 trade_count 恒 0（服务器不存）→ cum_trades 列全 0
        - 综合指数（000xxx）成员名单不完整（服务器板块成分缺近期上市新股，
          pytdxdata 实测 sh000001 返回 2230/实有 2318 只、成交额低估 ~4.6%）；
          样本指数（000016/000300/000905 等）实测无此问题

        :param index: 指数 symbol（"sh.000300"/"sz.399001"/"bj.899050"/"sh.880xxx"）
        :param date: None=最近交易日；"YYYY-MM-DD"/YYYYMMDD（回溯深度同 mac_ticks）
        :param workers: 并发连接数（默认 4，建议 2~6）
        :param max_members: 成分拉取上限（翻页防失控）
        :param skip_errors: True 单只成分逐笔失败忽略（计入告警）；False 直接 raise
        :return: pd.DataFrame[minute, vol, trade_count, cum_vol, cum_trades]
                （minute="HH:MM" 升序；vol/trade_count 为每分钟增量，cum_* 为累计）
        """
        from concurrent.futures import ThreadPoolExecutor

        kind, market_id, code = _parse_symbol(index)
        if kind != "std":
            raise ValueError(f"index_trade_stats 仅支持标准市场指数，收到 {index!r}")
        board = _index_board_code(code)

        # 1) 成分全量（mac_members 翻页；total 列可交叉核对成分数）
        pages, start = [], 0
        while start < int(max_members):
            page = self.mac_members(board, start=start, count=80)
            if page is None or not len(page):
                break
            pages.append(page)
            start += len(page)
            if len(page) < 80:
                break
        if not pages:
            raise ValueError(f"{index} 无成分返回（board={board}）")
        members = pd.concat(pages, ignore_index=True)
        if code in ("000001", "000002", "000003", "000008", "000009", "000010",
                    "399106", "399107", "399108"):
            logger.warning(  # 已知综合指数（服务器成分缺近期新股，pytdxdata 实测低估 ~4.6%）
                f"index_trade_stats({index}): 综合指数成分可能不完整（服务器缺近期新股，"
                f"本次返回 {len(members)} 只）——Σvol/笔数系统性低估，精确汇总请用全市场股票列表"
            )

        # 2) 并发逐笔聚合（finance_all 模式；tick 翻页含停滞守卫）
        _MKT_NAMES = {0: "sz", 1: "sh", 2: "bj"}
        symbols = [_MKT_NAMES.get(int(m), f"m{int(m)}") + "." + str(c)
                   for m, c in zip(members["market"], members["code"])]
        workers = max(1, min(int(workers), len(symbols)))

        failed = []

        def _worker(chunk):
            client = TdxClient()
            agg = {}  # "HH:MM" -> [vol, trades]
            try:
                for sym in chunk:
                    try:
                        pos, prev_key = 0, None
                        while True:
                            # drop_auction_orders=False：翻页终止/推进必须按原始行数
                            # （过滤后行数判短页会提前 break 静默丢数据——复审 R2 MEDIUM），
                            # 竞价委托由下方自过滤剔除
                            page = client.mac_ticks(sym, date=date, start=pos, count=1000,
                                                    drop_auction_orders=False)
                            rows = page.to_dict("records") if len(page) else []
                            if not rows:
                                break
                            prev_key = _page_stall_guard(
                                prev_key, rows,
                                lambda r: (r.get("time"), r.get("price"), r.get("vol")),
                                ctx=f"index_trade_stats({sym})")
                            for r in rows:
                                if r.get("bs_flag") == _BS_AUCTION_ORDER:
                                    continue  # 竞价委托 vol 为累计值（批次八教训），不计增量
                                slot = agg.setdefault(str(r["time"])[:5], [0.0, 0])
                                slot[0] += float(r["vol"])
                                slot[1] += int(r["trade_count"])
                            if len(rows) < 1000:
                                break
                            pos += len(rows)
                    except Exception as exc:
                        if not skip_errors:
                            raise
                        failed.append(f"{sym}: {type(exc).__name__}: {exc}")
            finally:
                client.close()
            return agg

        chunks = [symbols[i::workers] for i in range(workers)]
        merged = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for agg in pool.map(_worker, chunks):
                for k, (v, t) in agg.items():
                    slot = merged.setdefault(k, [0.0, 0])
                    slot[0] += v
                    slot[1] += t

        if failed:
            logger.warning(f"index_trade_stats({index}): {len(failed)}/{len(symbols)} 只成分逐笔失败"
                           f"（前 3：{failed[:3]}）")
        if not merged:
            return _rows_to_df([])
        rows = [(k, v[0], v[1]) for k, v in sorted(merged.items())]
        out = pd.DataFrame(rows, columns=["minute", "vol", "trade_count"])
        out["cum_vol"] = out["vol"].cumsum()
        out["cum_trades"] = out["trade_count"].cumsum()
        return out

    def mac_quotes(self, symbols, bits=None, on_missing="warn"):
        """批量自定义字段位报价（0x122B MAC 通道；≤80 只/批自动分批，2026-10-21 复活）。

        与 quotes()（std 五档）的区别：本方法走 MAC 通道、支持 bits 自定义字段位、
        无五档盘口；字段（默认八位）= pre_close/open/high/low/close/vol/volume_ratio/amount。

        :param symbols: 标准市场 symbol 或列表（["sh.600000", "sz.000001"]）
        :param bits: 字段位列表（如 [0x00, 0x04, 0x38]；0x38 主力净流入语义未验证）
        :param on_missing: 缺失处理（同 mac_ex_quotes）：默认 "warn"/"raise"/"ignore"
        """
        if isinstance(symbols, str):
            symbols = [symbols]
        if not symbols:
            return _rows_to_df([])
        pairs = [self._std_symbol(s) for s in symbols]
        pages = []
        for i in range(0, len(pairs), 80):
            batch = pairs[i:i + 80]
            page = _rows_to_df(self._mac_call("get_mac_batch_quotes", batch, bits))
            _validate_batch_quotes(batch, page, on_missing=on_missing, ctx="mac_quotes/0x122B")
            pages.append(page)
        import pandas as pd
        if len(pages) == 1:
            return pages[0]
        return pd.concat(pages, ignore_index=True)

    def mac_belong_boards(self, symbol):
        """个股所属板块（0x1218 MAC 通道；板块码 880/881xxx + 板块指数点位，GBK JSON）。

        :param symbol: 标准市场 symbol（sh.600000 / sz.000001）
        """
        market_id, code = self._std_symbol(symbol)
        return _rows_to_df(self._mac_call("get_mac_belong_boards", market_id, code))

    def mac_capital_flow(self, symbol):
        """个股资金流（0x1218 + 报表名 Stock_ZJLX；当日主力/散户四值 + 5 日六值，2026-11-05 实测复活）。

        列结构：main_in/main_out/main_net（当日主力流入/流出/净额，元）；
        retail_in/retail_out/retail_net（当日散户）；flow5d_0..flow5d_5 为
        5 日累计口径原始值（[0]/[1]≈当日值×4.4~7，[2..5] 语义待第三方校准）。
        std 通道资金流（0x0FFC）已下线，本命令是 MAC 通道替代（当日主力净额
        与 0x0FFC 同量级旁证）。

        :param symbol: 标准市场 symbol（sh.600000 / sz.000001）
        """
        market_id, code = self._std_symbol(symbol)
        return _rows_to_df(self._mac_call("get_mac_capital_flow", market_id, code))

    def mac_snapshot(self, symbol):
        """个股特征快照（0x122A MAC 通道；OHLC/量额同 std 口径，另含内外盘/activity）。

        :param symbol: 标准市场 symbol（sh.600000 / sz.000001）
        """
        market_id, code = self._std_symbol(symbol)
        return _rows_to_df(self._mac_call("get_mac_snapshot", market_id, code))

    # -- 行情/公司 ------------------------------------------------------------

    def quotes(self, symbols):
        """实时快照（支持混合列表；std 批量五档 ≤80 只/次，EX 逐只单档）。

        EX 行字段已对齐标准快照命名（pre_close→last_close、zongliang→vol、
        xianliang→cur_vol），另保留 EX 特有字段（kaicang 开仓/chicang 持仓/
        neipan 内盘/waipan 外盘）；std 行的 EX 特有列与 EX 行的 std 特有列
        （servertime/s_vol/b_vol/amount 等）互为 NaN。

        :param symbols: 单个 symbol 或列表（std/ex 可混），如 ["sh.600519", "us.AAPL"]
        :return: pd.DataFrame（列取并集，缺失填 NaN）；全部无记录返回空 DataFrame
        """
        if isinstance(symbols, str):
            symbols = [symbols]

        std_pairs, ex_items = [], []
        for item in symbols:
            kind, market_id, code = _parse_symbol(item)
            if kind == "std":
                std_pairs.append((market_id, code))
            else:
                ex_items.append((market_id, code))

        frames = []
        for offset in range(0, len(std_pairs), MAX_QUOTES_PER_CALL):
            page = self._hq_call("get_security_quotes", std_pairs[offset:offset + MAX_QUOTES_PER_CALL])
            if page:
                frames.append(pd.DataFrame(list(page)))
        for market_id, code in ex_items:
            rows = self._ex_call("get_instrument_quote", market_id, code)
            for row in rows or []:
                record = dict(row)
                record["last_close"] = record.pop("pre_close", None)
                if "zongliang" in record:
                    record["vol"] = record.pop("zongliang")
                if "xianliang" in record:
                    record["cur_vol"] = record.pop("xianliang")
                frames.append(pd.DataFrame([record]))

        if not frames:
            return _rows_to_df([])
        return pd.concat(frames, ignore_index=True, sort=False)

    def xdxr(self, symbol):
        """除权除息事件历史（分红/送股/配股/股本变动）。仅标准市场。

        :param symbol: 点分式，如 "sh.601919"/"sz.000001"
        """
        kind, market_id, code = _parse_symbol(symbol)
        if kind == "ex":
            raise ValueError("除权除息数据仅标准市场（sh/sz/bj）存在，EX 协议无此命令")
        return _rows_to_df(self._hq_call("get_xdxr_info", market_id, code))

    def finance(self, symbol, drop_shell=False):
        """个股即时财务数据（股本/EPS/资产负债等）。仅标准市场。

        退市/无效票服务器返回全零空壳行（code 全 \\x00、ipo_date=0）——响应行
        附 is_shell 标记（bool）；drop_shell=True 时过滤空壳行并告警。

        :param symbol: 点分式，如 "sh.601919"/"sz.000001"
        :param drop_shell: True=过滤空壳行（默认 False 保持原始行，向后兼容）
        """
        kind, market_id, code = _parse_symbol(symbol)
        if kind == "ex":
            raise ValueError("财务数据仅标准市场（sh/sz/bj）存在，EX 协议无此命令")
        rows = self._hq_call("get_finance_info", market_id, code)
        if drop_shell:
            # parser 返回单 dict（0x0C1F 单票查询）——dict/list 两形态都要处理
            if isinstance(rows, dict):
                if rows.get("is_shell"):
                    logger.warning(f"finance({symbol}) 过滤空壳行 1 条")
                    rows = []
            else:
                kept = [r for r in rows if isinstance(r, dict) and not r.get("is_shell")]
                if len(kept) != len(rows):
                    logger.warning(f"finance({symbol}) 过滤空壳行 {len(rows) - len(kept)} 条")
                rows = kept
        return _rows_to_df(rows)

    # -- F10 公司资料（0x9B0F/0x9C07，修复 filename GBK 编码往返 bug 后的门面） -

    def _f10_entries(self, kind, market_id, code, symbol):
        """取 F10 分类条目（含 filename_raw），带实例级 TTL 缓存。"""
        if kind == "ex":
            raise ValueError("F10 公司资料仅标准市场（sh/sz）存在，EX 协议无此命令")
        if market_id == StdMarket.BJ.value:
            raise ValueError(
                f"F10 公司资料仅验证过沪/深市场，不支持北证：{symbol}（请用 sh./sz. 前缀）"
            )
        key = (market_id, code)
        cached = self._f10_category_cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._f10_category_ttl:
            return cached[1]
        entries = self._hq_call("get_company_info_category", market_id, code)
        if not entries:
            raise ValueError(f"F10 分类列表为空：{symbol}（该股可能无公司资料）")
        self._f10_category_cache[key] = (now, entries)
        return entries

    def f10_categories(self, symbol):
        """F10 分类列表（公司概况/财务分析/股东研究等）。仅 sh/sz。

        :param symbol: 点分式，如 "sh.600519"/"sz.000001"
        :return: DataFrame（name/filename/start/length/size_kb；分类列表实例级缓存 TTL 1h）
        """
        kind, market_id, code = _parse_symbol(symbol)
        entries = self._f10_entries(kind, market_id, code, symbol)
        rows = [{k: v for k, v in e.items() if k != "filename_raw"} for e in entries]
        df = _rows_to_df(rows)
        df["size_kb"] = (df["length"] / 1024).round(1)
        return df

    def f10_content(self, symbol, name):
        """按分类名取 F10 正文（GBK 文本）。仅 sh/sz。

        :param symbol: 点分式，如 "sh.600519"
        :param name: 分类名（`f10_categories` 返回的 name，如 "公司概况"）
        :return: str 正文
        """
        kind, market_id, code = _parse_symbol(symbol)
        entries = self._f10_entries(kind, market_id, code, symbol)
        for e in entries:
            if e["name"].strip() == name.strip():
                # filename_raw 原样回传（GBK 原始字节），禁止经 str 编码往返
                return self._hq_call(
                    "get_company_info_content", market_id, code,
                    e["filename_raw"], e["start"], e["length"],
                )
        available = "、".join(e["name"] for e in entries)
        raise ValueError(f"F10 分类 {name!r} 不存在：{symbol}。可用分类：{available}")

    def f10_all(self, symbol):
        """F10 全量正文（逐分类拉取，单分类失败告警跳过）。仅 sh/sz。

        :param symbol: 点分式，如 "sh.600519"
        :return: dict {name: str}
        """
        kind, market_id, code = _parse_symbol(symbol)
        entries = self._f10_entries(kind, market_id, code, symbol)
        out = {}
        for e in entries:
            try:
                out[e["name"]] = self._hq_call(
                    "get_company_info_content", market_id, code,
                    e["filename_raw"], e["start"], e["length"],
                )
            except (
                TdxFunctionCallError, TdxConnectionError, ConnectionError, socket.timeout, OSError
            ) as exc:  # 对齐 _hq_call 的捕获集：重试耗尽可能抛裸 OSError/timeout（复审 B-M1）
                logger.warning(f"f10_all({symbol}) 分类 {e['name']!r} 拉取失败跳过：{exc}")
        return out

    def finance_all(self, symbols, workers=6, drop_shell=False):
        """并发批量财务数据（多连接分片 + 线程池；全市场量级采集用）。

        每线程独立 TdxClient（自动选优建连，类级健康分缓存共享），分片内串行
        复用连接；单只失败不炸线程，失败行保留 symbol 并附 error 说明。
        服务器 ~4s/请求限速为服务端策略，只能靠并发摊——workers 建议克制（5~10）。

        :param symbols: symbol 列表（["sh.600000", ...]，仅标准市场；单字符串亦可）
        :param workers: 并发连接数（默认 6，建议 5~10）
        :param drop_shell: 透传 finance()（True=过滤空壳行）
        :return: pd.DataFrame（成功行为完整财务字段 + is_shell；失败行仅 symbol/error 列）
        """
        from concurrent.futures import ThreadPoolExecutor

        if isinstance(symbols, str):
            symbols = [symbols]
        symbols = [s for s in symbols if s]
        if not symbols:
            return _rows_to_df([])
        workers = max(1, min(int(workers), len(symbols)))

        def _worker(chunk):
            client = TdxClient()
            frames = []
            try:
                for sym in chunk:
                    try:
                        df = client.finance(sym, drop_shell=drop_shell)
                        if len(df):
                            df = df.copy()
                            df["symbol"] = sym  # 成功行也回填输入 symbol（复审 R2-#1：对号需求不只失败行）
                            frames.append(df)
                        else:
                            frames.append(pd.DataFrame([{"symbol": sym, "error": "empty response"}]))
                    except Exception as exc:  # 失败隔离：单只异常不炸线程
                        frames.append(pd.DataFrame([{"symbol": sym, "error": f"{type(exc).__name__}: {exc}"}]))
            finally:
                client.close()
            return frames

        # 轮转分片（均衡各线程负载）；类级 _health 分选缓存跨实例共享，选优无重复开销
        chunks = [symbols[i::workers] for i in range(workers)]
        all_frames = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for frames in pool.map(_worker, chunks):
                all_frames.extend(frames)
        if not all_frames:
            return _rows_to_df([])
        return pd.concat(all_frames, ignore_index=True, sort=False)

    def audit_price_scale(self, markets=(0, 1), max_pages=200):
        """系数表 × 协议 decimal_point 交叉审计（2026-11-05 MEDIUM⑥；独立按需调用，不污染解析热路径）。

        全量拉取证券列表，逐 code 对照 SECURITY_COEFFICIENT 价格系数与协议上报
        decimal_point（期望 coef == 10**-decimal_point）；不一致汇总告警并返回报告。
        pytdxdata 全量 51982 只实测协议值与静态规则零分歧——本审计为系数表漂移
        的保险机制（未登记代码段系系数表盲区，跳过不算分歧）。

        :param markets: 审计市场（默认 (0 深, 1 沪)）
        :param max_pages: 单市场翻页上限（防失控）
        :return: pd.DataFrame（分歧行：market/code/name/decimal_point/coef_price/expect；
                 空表 = 零分歧）
        """
        mismatches = []
        audited = 0
        skipped_unregistered = 0
        for market in markets:
            start = 0
            for _ in range(int(max_pages)):
                rows = self._hq_call("get_security_list", int(market), start)
                if not rows:
                    break
                for r in rows:
                    code = str(r.get("code", "")).strip()
                    if not code:
                        continue
                    dp = r.get("decimal_point")
                    if dp is None:
                        continue
                    try:
                        coef = get_security_coefficient(market=int(market), code=code)
                    except NotImplementedError:
                        skipped_unregistered += 1
                        continue
                    expect = 10.0 ** (-int(dp))
                    audited += 1
                    if abs(coef - expect) > expect * 0.01:  # 1% 相对容差
                        mismatches.append({
                            "market": int(market), "code": code, "name": r.get("name", ""),
                            "decimal_point": int(dp), "coef_price": coef, "expect": expect,
                        })
                start += len(rows)
        if mismatches:
            logger.warning(
                f"audit_price_scale: 审计 {audited} 只，发现 {len(mismatches)} 处系数表与协议 "
                f"decimal_point 分歧（未登记跳过 {skipped_unregistered}）——详见返回报告"
            )
        else:
            logger.info(f"audit_price_scale: 审计 {audited} 只零分歧（未登记跳过 {skipped_unregistered}）")
        return pd.DataFrame(mismatches)

    def security_count(self, market=1):
        """市场证券总数（sh=1 / sz=0）。"""
        return self._hq_call("get_security_count", int(StdMarket(market)))

    # -- 扩展市场 -------------------------------------------------------------

    def ex_markets(self):
        """扩展市场列表（51~60 个随服务器；期货/期权/外盘/港股等）。"""
        rows = self._ex_call("get_markets")
        return pd.DataFrame([dict(item) for item in rows or []])

    def ex_instruments(self, market, limit=600):
        """扩展市场指定市场的商品列表（内部二分定位市场边界后翻页）。

        :param market: int / ExMarket 成员 / 字符串别名（"hk"/"us"/"美股"/"上期所"等；
                       查询主体参数，非品种定位，故保留）
        :param limit: 最多返回条数
        """
        market = resolve_ex_market(market)
        total = self._ex_call("get_instrument_count")
        if not total:
            return pd.DataFrame()

        # 二分找 market 起始偏移
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi) // 2
            batch = self._ex_call("get_instrument_info", mid, 1)
            if not batch:
                hi = mid
                continue
            if batch[0]["market"] < market:
                lo = mid + 1
            else:
                hi = mid

        items, pos = [], lo
        while pos < total and len(items) < limit:
            batch = self._ex_call("get_instrument_info", pos, 1000)
            if not batch:
                break
            for item in batch:
                if item["market"] == market:
                    items.append(item)
                    # 2026-09-16 父Agent 审核修复：limit 必须在 batch 内逐条检查，
                    # 否则单批 1000 条会整批收满（limit=5 返回 306 条）
                    if len(items) >= limit:
                        return pd.DataFrame([dict(x) for x in items])
                elif item["market"] > market:
                    return pd.DataFrame([dict(x) for x in items])
            pos += 1000
        return pd.DataFrame([dict(x) for x in items[:limit]])
