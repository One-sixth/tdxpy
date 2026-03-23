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
| ticks | get_history_transaction_data（800/页） | 同名 EX 命令（1800/页） |
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

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pandas as pd

from tdxpy.exceptions import TdxConnectionError
from tdxpy.exceptions import TdxFunctionCallError
from tdxpy.exhq import TdxExHq_API
from tdxpy.hq import TdxHq_API
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

#: 实测可用的标准市场服务器（7709）
STD_SERVERS = [
    ("111.229.247.189", 7709),
    ("116.205.163.254", 7709),
    ("159.75.29.111", 7709),
    ("180.153.18.170", 7709),
    ("119.147.212.81", 7709),
]

#: 实测可用的扩展市场服务器（7727，连接后须 login，exhq.setup 已自动处理）
EX_SERVERS = [
    ("139.9.191.175", 7727),
    ("121.37.232.167", 7727),
    ("43.139.173.246", 7727),
]

#: 实时五档单次上限
MAX_QUOTES_PER_CALL = 80

#: 分笔/K线单页上限
MAX_PAGE_SIZE = 800

#: EX 分笔单页上限
EX_PAGE_SIZE = 1800

#: EX K 线单页上限
EX_BARS_PAGE_SIZE = 700


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
    （us/美股/hk/main/上期所… → kind="ex"）；都不命中 raise（教学式文案）。
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
        ex_id = None

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


def _rows_to_df(rows):
    # get_finance_info 的 parser 返回单个 OrderedDict（非 list）——直接 list(dict)
    # 只会拿到字段名（值全丢，2026-10-19 tick 全量任务实测）。dict 分支包成单行表。
    if isinstance(rows, dict):
        return pd.DataFrame([rows])
    return pd.DataFrame(list(rows or []))


def _sorted_df(rows):
    """bars/ticks 行 → DataFrame，按 datetime 升序排序（kline/分笔时间序一致）。"""
    df = pd.DataFrame(list(rows or []))
    if not df.empty and "datetime" in df.columns:
        df = df.sort_values("datetime").reset_index(drop=True)
    return df


def _ping(host, port, timeout):
    """TCP 连接耗时（秒）；不可达返回 None。轻量探测，不含协议握手。"""
    try:
        t0 = time.monotonic()
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return time.monotonic() - t0
    except OSError:
        return None


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

    def __init__(self, host=None, port=7709, ex_host=None, ex_port=7727, heartbeat=True, timeout=10.0):
        """
        :param host: 标准市场服务器；None 时首次调用自动测速选优
        :param ex_host: 扩展市场服务器；None 时首次调用 EX 品种自动测速选优
        :param heartbeat: 是否开启协议心跳保活
        :param timeout: 连接/读写超时（秒）
        """
        self._host = host
        self._port = port
        self._ex_host = ex_host
        self._ex_port = ex_port
        self._heartbeat = heartbeat
        self._timeout = timeout
        self._hq_api = None
        self._ex_api = None
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
        best = cls._pick(servers or cls.STD_SERVERS, timeout)
        best_ex = cls._pick(ex_servers or cls.EX_SERVERS, timeout)
        return cls(host=best[0], port=best[1], ex_host=best_ex[0], ex_port=best_ex[1],
                   heartbeat=heartbeat, timeout=call_timeout)

    @classmethod
    def _pick(cls, servers, timeout):
        """并发探测，返回最快的 (host, port)。"""
        with ThreadPoolExecutor(max_workers=len(servers)) as pool:
            latencies = list(pool.map(lambda s: _ping(s[0], s[1], timeout), servers))
        ranked = [item for item in zip(servers, latencies) if item[1] is not None]
        if not ranked:
            raise TdxConnectionError(f"所有服务器均不可达：{servers}")
        ranked.sort(key=lambda item: item[1])
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
                        self._ex_host, self._ex_port = self._pick(self.EX_SERVERS, 2.0)
                    api = TdxExHq_API(multithread=True, heartbeat=self._heartbeat, raise_exception=True)
                    if not api.connect(self._ex_host, self._ex_port, time_out=self._timeout):
                        raise TdxConnectionError(f"无法连接扩展市场 {self._ex_host}:{self._ex_port}")
                    self._ex_api = api
        return self._ex_api

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

    def close(self):
        """关闭标准市场与扩展市场连接。"""
        self._close_hq()
        self._close_ex()

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
            page_size = min(page_size, EX_BARS_PAGE_SIZE)

            def fetch(pos, cnt):
                return self._ex_call("get_instrument_bars", category, market_id, code, pos, cnt)
        else:
            adjust_id = normalize_adjust(adjust)
            page_size = min(page_size, MAX_PAGE_SIZE)
            command = "get_index_bars" if _is_index_code(market_id, code) else "get_security_bars"

            def fetch(pos, cnt):
                return self._hq_call(command, category, market_id, code, pos, cnt, adjust=adjust_id)

        if not all_pages:
            return _sorted_df(fetch(start, count))

        frames, pos, pages = [], start, 0
        while max_pages is None or pages < max_pages:
            rows = fetch(pos, page_size)
            if not rows:
                break
            frames.extend(rows)
            pos += len(rows)
            pages += 1
            if len(rows) < page_size:
                break
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
        :param all: True 自动翻页拉全天并按时间排序（std 800/页，ex 1800/页）；False 只取第一页

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
            rows, pos = [], 0
            while True:
                page = self._ex_call("get_history_transaction_data", market_id, code, trade_date, pos, EX_PAGE_SIZE)
                if not page:
                    break
                rows.extend(page)
                if len(page) < EX_PAGE_SIZE:
                    break
                pos += len(page)
            if not rows:
                return pd.DataFrame(columns=_EX_TICK_COLS)
            # 分页拼接后页间逆序（start=0 取最新段，递增翻页往回翻）——统一按时间重排。
            # EX tick 字段为 date(datetime 含秒)；std 为 time(HH:MM 字符串)。
            rows.sort(key=lambda r: r["date"] if "date" in r else r["time"])
            return _sorted_df(rows)

        if not all:
            rows = self._hq_call("get_history_transaction_data", market_id, code, 0, MAX_PAGE_SIZE, trade_date)
            return _sorted_df(rows) if rows else pd.DataFrame(columns=_STD_TICK_COLS)
        rows, pos = [], 0
        while True:
            page = self._hq_call("get_history_transaction_data", market_id, code, pos, MAX_PAGE_SIZE, trade_date)
            if not page:
                break
            rows.extend(page)
            if len(page) < MAX_PAGE_SIZE:
                break
            pos += len(page)
        if not rows:
            return pd.DataFrame(columns=_STD_TICK_COLS)
        # 同上：分页拼接页间逆序，统一按 time 重排（time 为 HH:MM 带前导零，字符串序=时间序）。
        # ⚠️ 同一分钟内的笔序依赖服务器返回顺序（分笔实为 3 秒快照聚合，HH:MM 粒度
        # 内无更细时间戳），跨页同分钟笔序不保证稳定——协议口径如此，非实现缺陷。
        rows.sort(key=lambda r: r["time"])
        return _sorted_df(rows)

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

    def finance(self, symbol):
        """个股即时财务数据（股本/EPS/资产负债等）。仅标准市场。

        :param symbol: 点分式，如 "sh.601919"/"sz.000001"
        """
        kind, market_id, code = _parse_symbol(symbol)
        if kind == "ex":
            raise ValueError("财务数据仅标准市场（sh/sz/bj）存在，EX 协议无此命令")
        return _rows_to_df(self._hq_call("get_finance_info", market_id, code))

    def security_count(self, market=1):
        """市场证券总数（sh=1 / sz=0）。"""
        return self._hq_call("get_security_count", int(StdMarket(market)))

    # -- 扩展市场 -------------------------------------------------------------

    def ex_markets(self):
        """扩展市场列表（51 个：期货/期权/外盘/港股等）。"""
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
