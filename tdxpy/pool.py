# ----------------------------------------------------------------------
# tdxpy/pool.py —— 服务器池调度器 + 交易时段自适应限流
# ----------------------------------------------------------------------
"""靠服务器池的轮转调度器（吸收 tdxrs downloader ``ServerPool`` 经验）。

职责边界（与 tdxrs 版的差异全部有据）：

- **只负责「选谁」**：轮转 + 健康分跳过 + 限流；连接懒建与异常后置空重建
  是「交付可用客户端」的副作用，不主动剔除服务器（健康分自然降级）。
- **连接对象是轻量单连接客户端**（``TdxHq_API``/``TdxExHq_API``，
  ``multithread=True`` 级别），**不是 N 个 TdxClient 门面实例**——门面是
  单实例多通道设计，N 个实例会带 N 套心跳线程和用不到的通道。
- **锁粒度**：池锁只包「选服务器 + idx 自增 + 取引用」三步，随即释放；
  ``PhaseRateLimiter.wait`` 在 limiter 自己的锁内执行（同一服务器
  check-then-sleep 原子，不同服务器并行）。两线程同刻取到同一服务器时
  由 limiter 锁串行，限流不被绕过。
- **健康分复用**：读写 ``TdxClient._health`` 类级字典（可注入独立字典），
  冷却节点让位健康节点；全池冷却时按 ``health_rank`` 兜底选最优，不死锁。

用法::

    pool = ServerPool(rate_limit=15)
    for symbol in all_symbols:
        server, api = pool.next_client(kind="std")
        df = api.get_security_bars(category, market, code, start, count)
        pool.report(server, ok=True, latency=elapsed)
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from tdxpy.client import EX_SERVERS, STD_SERVERS, TdxClient
from tdxpy.exceptions import CODE_POOL_EXHAUSTED, TdxConnectionError
from tdxpy.health import health_is_available, health_rank, health_update
from tdxpy.hq import TdxHq_API
from tdxpy.exhq import TdxExHq_API

__all__ = [
    "ServerPool",
    "PhaseRateLimiter",
    "get_phase",
    "PHASE_TRADING",
    "PHASE_PRE_POST",
    "PHASE_CLOSED",
    "PHASE_MULTIPLIERS",
    "DEFAULT_BASE_RPS",
    "MINUTE_BASE_RPS",
    "MAX_RATE_LIMIT",
]

# -- 交易时段自适应限流参数（同 tdxrs：src/net/utils.rs） -------------------

TZ_CN = timezone(timedelta(hours=8))
"""判定用的东八区时区。"""

PHASE_TRADING = "trading"
"""交易时段（工作日 9:30–15:00，简化版不考假期、午休视为交易中）。"""

PHASE_PRE_POST = "pre_post"
"""盘前盘后（工作日非交易时段）。"""

PHASE_CLOSED = "closed"
"""休市（周末）。"""

PHASE_MULTIPLIERS = {
    PHASE_TRADING: (1.0, 1.0),
    PHASE_PRE_POST: (2.0, 1.5),
    PHASE_CLOSED: (4.0, 3.0),
}
"""各时段乘数表 ``{phase: (通用乘数, 分时乘数)}``，同 tdxrs 参数。"""

DEFAULT_BASE_RPS = 15
"""通用档基准速率（per-server，trading 时段值），req/s。"""

MINUTE_BASE_RPS = 10
"""分时档基准速率（per-server），req/s——分时有独立基准。"""

MAX_RATE_LIMIT = 200
"""有效速率上限 req/s（``effective = min(int(base × mult), 200)``）。"""


def get_phase(now=None):
    """交易时段判定（简化版，同 tdxrs：不考假期，午休视为交易中）。

    :param now: 判定时刻；aware datetime 转 UTC+8 后判定，naive 视为
        UTC+8 直用，None 取当前东八区时刻
    :return: ``"trading"`` / ``"pre_post"`` / ``"closed"``
    """
    if now is None:
        now = datetime.now(TZ_CN)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TZ_CN)
    else:
        now = now.astimezone(TZ_CN)
    if now.weekday() >= 5:
        return PHASE_CLOSED
    minute_of_day = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minute_of_day <= 15 * 60:
        return PHASE_TRADING
    return PHASE_PRE_POST


class PhaseRateLimiter:
    """per-server 交易时段自适应限流器。

    ``wait`` 的 check-then-sleep 在自身锁内原子——同一服务器的并发请求
    被串行化到基准速率，不同服务器各持独立实例互不阻塞（这正是池锁只包
    选服务器三步、wait 放池锁外的原因）。
    """

    def __init__(self, base_rps=DEFAULT_BASE_RPS, minute_base_rps=MINUTE_BASE_RPS, phase=None):
        """
        :param base_rps: 通用档基准 req/s（trading 时段值）
        :param minute_base_rps: 分时档基准 req/s
        :param phase: 强制指定时段（``"trading"/"pre_post"/"closed"``）；
            None 自动按 ``get_phase`` 检测
        """
        if phase is not None and phase not in PHASE_MULTIPLIERS:
            raise ValueError(
                f"phase 必须是 {sorted(PHASE_MULTIPLIERS)} 之一，收到 {phase!r}"
            )
        self._base_rps = float(base_rps)
        self._minute_base_rps = float(minute_base_rps)
        self._forced_phase = phase
        self._lock = threading.Lock()
        self._last = 0.0  # 上次放行时刻（time.monotonic() 秒）；0 = 从未放行

    def effective_rps(self, is_minute=False, now=None):
        """当前生效速率 req/s：``min(int(base × phase_mult), 200)``。

        :param is_minute: True 按分时档（独立基准 + 独立乘数列）
        :param now: 判定时刻（仅自动检测 phase 时有意义）；None 取当前
        :return: float req/s
        """
        phase = self._forced_phase or get_phase(now)
        mult = PHASE_MULTIPLIERS[phase][1 if is_minute else 0]
        base = self._minute_base_rps if is_minute else self._base_rps
        return min(int(base * mult), MAX_RATE_LIMIT)

    def phase(self, now=None):
        """当前生效时段名（强制值或自动检测值）。"""
        return self._forced_phase or get_phase(now)

    def set_rps(self, base_rps=None, minute_base_rps=None):
        """运行时调基准速率（下次 wait 起生效），对齐 tdxrs ``set_rps()``。

        :param base_rps: 新通用档基准；None 不变
        :param minute_base_rps: 新分时档基准；None 不变
        """
        with self._lock:
            if base_rps is not None:
                self._base_rps = float(base_rps)
            if minute_base_rps is not None:
                self._minute_base_rps = float(minute_base_rps)

    def wait(self, is_minute=False):
        """阻塞到允许发请求（按当前时段速率卡间隔）。

        :param is_minute: True 按分时档限流
        :return: 本次实际等待秒数（>=0，便于日志/测试断言）
        """
        with self._lock:
            interval = 1.0 / self.effective_rps(is_minute)
            elapsed = time.monotonic() - self._last
            delay = interval - elapsed
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()
            return max(delay, 0.0)


class _Shard:
    """单通道分片状态（std / ex 各一份，互不共享轮转与限流）。"""

    __slots__ = ("servers", "idx", "clients", "limiters", "stats", "lock")

    def __init__(self, servers, limiter_kwargs):
        self.servers = list(servers)
        self.idx = 0
        self.clients = {}  # (host, port) -> api | None（None = 待懒建/待重建）
        self.limiters = {s: PhaseRateLimiter(**limiter_kwargs) for s in self.servers}
        self.stats = {
            s: {"req": 0, "fail": 0, "last_latency_ms": None} for s in self.servers
        }
        self.lock = threading.Lock()  # 池锁：只包选服务器 + idx 自增 + 取引用


def _default_client_factory(host, port, kind, timeout=10.0):
    """默认连接工厂：轻量单连接客户端（对齐 ``_ensure_hq`` 的建连参数）。

    ``raise_exception=True``：连接失败时 connect **抛 TdxConnectionError**
    （而非返回 False——base_socket_client.py 只在 raise_exception=False 才
    走 return False）；命令级断线异常也靠它触发调用方记账。建连失败换台
    由 :meth:`ServerPool.next_client` 的循环捕获实现，本工厂只管建连。
    """
    if kind == "ex":
        api = TdxExHq_API(multithread=True, heartbeat=True, raise_exception=True)
    else:
        api = TdxHq_API(multithread=True, heartbeat=True, raise_exception=True)
    if not api.connect(host, port, time_out=timeout):
        return None  # 仅 raise_exception=False 的底层路径会到这里（防御保留）
    return api


class ServerPool:
    """服务器池轮转调度器（健康分跳过 + 时段自适应限流）。

    交付形态对齐 tdxrs ``next_client``：返回 ``(server, api)`` 元组，
    ``server`` 用于 :meth:`report` 回执与健康分定位。
    """

    KINDS = ("std", "ex")

    def __init__(
        self,
        servers=None,
        rate_limit=DEFAULT_BASE_RPS,
        phase=None,
        size=10,
        ex_servers=None,
        timeout=10.0,
        health_dict=None,
        client_factory=None,
    ):
        """
        :param servers: std 服务器表 ``[(host, port), ...]``；
            None 取 ``TdxClient.STD_SERVERS`` 前 *size* 台
        :param rate_limit: per-server 通用档基准 req/s（trading 时段值）
        :param phase: 强制时段；None 自动检测
        :param size: 服务器表为 None 时各取前多少台（默认 10）
        :param ex_servers: ex 服务器表；None 取 ``EX_SERVERS`` 前 *size* 台
        :param timeout: 建连超时秒
        :param health_dict: 健康分字典；None 用 ``TdxClient._health``（类级
            共享，与 auto() 测速互通）；测试可注入独立 dict
        :param client_factory: ``fn(host, port, kind) -> api | None``；
            None 用默认轻量连接工厂（测试注入假工厂）
        """
        if size <= 0:
            raise ValueError(f"size 必须 >= 1，收到 {size}")
        if servers is not None and not servers:
            raise ValueError("servers 不能为空列表（传 None 用内置服务器表，或至少给一台）")
        if ex_servers is not None and not ex_servers:
            raise ValueError("ex_servers 不能为空列表（传 None 用内置服务器表，或至少给一台）")
        limiter_kwargs = {"base_rps": rate_limit, "phase": phase}
        std = servers if servers is not None else STD_SERVERS[:size]
        ex = ex_servers if ex_servers is not None else EX_SERVERS[:size]
        self._timeout = timeout
        self._health_dict = health_dict  # None = 方法内惰性取 TdxClient._health
        self._client_factory = client_factory  # None = 用默认工厂（可吃 timeout 参数）
        self._shards = {
            "std": _Shard(std, limiter_kwargs),
            "ex": _Shard(ex, limiter_kwargs),
        }

    # -- 内部工具 ------------------------------------------------------------

    def _health(self):
        return TdxClient._health if self._health_dict is None else self._health_dict

    def _make_client(self, server, kind):
        """按工厂造连接：None=默认工厂（吃 pool 的 timeout），否则按注入签名调用。"""
        host, port = server
        if self._client_factory is None:
            return _default_client_factory(host, port, kind, timeout=self._timeout)
        return self._client_factory(host, port, kind)

    def _ensure_client(self, shard, server, kind):
        """确保该服务器有可用连接（懒建；异常置空后下次轮到自动重建）。

        快路径无锁读；未命中进 ``shard.lock`` 二次检查后建连——并发懒建
        竞态防护（复审 HIGH-1：读-建-写无锁时 8 线程同刻 miss 会建 8 个
        连接、7 个未 close 即被覆盖泄漏）。建连是网络 IO，持分片锁会短暂
        阻塞同分片选路，仅发生在懒建/重建瞬间，可接受；不与其他锁嵌套。
        """
        api = shard.clients.get(server)
        if api is not None:
            return api
        with shard.lock:
            api = shard.clients.get(server)  # 二次检查：等锁期间别的线程已建好
            if api is not None:
                return api
            api = self._make_client(server, kind)
            shard.clients[server] = api  # 失败也记 None：下次轮到重新建连
            return api

    # -- 对外 API ------------------------------------------------------------

    def next_client(self, kind="std", is_minute=False):
        """轮转取下一个可用服务器的客户端（已连接、可直接发命令）。

        选路顺序：从轮转起点找第一个健康分可用服务器 → limiter 限流等待
        （池锁外）→ 交付已连接客户端。建连失败（返回 None 或抛异常，默认
        工厂 raise_exception=True 时为后者）写回健康分并尝试下一台，绕满
        一圈全失败 raise；全池冷却时按 ``health_rank`` 全列表兜底重试。

        :param kind: ``"std"``（7709 行情）/ ``"ex"``（7727 扩展行情）分片；
            mac 通道不入池
        :param is_minute: True 时本次按分时档限流（独立基准 10 req/s）
        :return: ``((host, port), api)``
        :raises ValueError: kind 非法（mac 不入池）
        :raises TdxConnectionError: 该分片所有服务器均不可用
        """
        if kind not in self.KINDS:
            raise ValueError(
                f"kind 必须是 {self.KINDS} 之一（mac 通道不入池），收到 {kind!r}"
            )
        shard = self._shards[kind]
        now = time.monotonic()
        health = self._health()

        # 池锁内：选服务器 + idx 自增 + 取 limiter 引用，三步即出
        with shard.lock:
            n = len(shard.servers)
            rotated = [shard.servers[(shard.idx + i) % n] for i in range(n)]
            cand = [(s, i) for i, s in enumerate(rotated) if health_is_available(health.get(s), now)]
            if cand:
                chosen = [s for s, _ in cand]
                sel_offset = cand[0][1]  # 被选元素在 rotated 的偏移（记录偏移而非 index 查找：重复元素时 servers/rotated.index 都会错位）
            else:
                # 全池冷却：兜底按 health_rank 取全排序列表（复审 MEDIUM-6：
                # 只取最优一台会反复撞同一坏台；全列表让建连失败自然轮到次优）
                ranked = health_rank([(s, health.get(s)) for s in rotated], now)
                chosen = [r[0] for r in ranked] if ranked else list(rotated)
                sel_offset = rotated.index(chosen[0]) if chosen else 0  # 兜底近似：全冷却+重复元素组合极边缘
            shard.idx = (shard.idx + sel_offset + 1) % n  # 推进到被选位置之后（offset 必须叠加起点 idx，否则轮转卡死）
            limiter = shard.limiters[chosen[0]]

        # 限流在池锁外、limiter 自己锁内（同服务器原子、跨服务器并行）
        limiter.wait(is_minute)

        # 依次尝试候选：建连失败/异常写回健康分换下一台，绕满候选全败 raise
        last_err_server = None
        last_err = None

        for server in chosen:
            try:
                api = self._ensure_client(shard, server, kind)
            except Exception as exc:  # noqa: BLE001 —— 建连抛异常（默认工厂 raise_exception=True 的 TdxConnectionError/OSError 等）记账换台
                last_err_server, last_err = server, exc
                self.report(server, ok=False, kind=kind)
                continue
            if api is not None:
                with shard.lock:
                    shard.stats[server]["req"] += 1  # 交付即计请求（同限流粒度）
                return server, api
            last_err_server = server
            self.report(server, ok=False, kind=kind)
        raise TdxConnectionError(
            f"ServerPool：{kind} 分片所有服务器均不可用（末次失败 {last_err_server}：{last_err}）",
            code=CODE_POOL_EXHAUSTED,
        )

    def report(self, server, ok, latency=None, kind="std"):
        """调用方请求回执：失败计数 + 延迟记录 + 健康分写回。

        ``req`` 由 :meth:`next_client` 交付时计数（一次交付 = 一次请求发出，
        与限流粒度一致）；report 只负责结果侧：失败 ``+fail``、成功记延迟，
        并写健康分（池视角的降级/恢复）。恢复路径 = ``ok=True``；连续失败
        ≥3 次该服务器进冷却期，后续轮转自动让位健康节点（不主动剔除连接）。

        健康分写回（``health.get`` → ``health_update`` → ``health[server]=``）
        三步非原子，并发 report 同一 server 可能 lost update——benign（健康
        分自纠正、dict 结构不损坏，同 ``TdxClient._pick`` 既有语义），不加锁。

        :param server: ``next_client`` 返回的 ``(host, port)``
        :param ok: 本次请求是否成功
        :param latency: 本次请求耗时秒；None 不更新 EWMA 与延迟记录
        :param kind: 所属分片
        """
        if kind not in self.KINDS:
            raise ValueError(f"kind 必须是 {self.KINDS} 之一，收到 {kind!r}")
        shard = self._shards[kind]
        now = time.monotonic()
        health = self._health()
        with shard.lock:
            entry = shard.stats[server]
            if ok:
                if latency is not None:
                    entry["last_latency_ms"] = round(latency * 1000, 1)
            else:
                entry["fail"] += 1
        state = health_update(health.get(server), ok, latency, now)
        health[server] = state

    def stats(self, kind=None):
        """池内统计快照。

        :param kind: ``"std"``/``"ex"``；None 返回两分片合并字典
            ``{(host, port): {...}}``（ex 键与 std 不重叠，host:port 唯一）
        :return: ``{(host, port): {"req": int, "fail": int, "last_latency_ms": float|None}}``
        """
        if kind is not None and kind not in self.KINDS:
            raise ValueError(f"kind 必须是 {self.KINDS} 或 None，收到 {kind!r}")
        shards = [self._shards[k] for k in self.KINDS if kind is None or k == kind]
        merged = {}
        for shard in shards:
            with shard.lock:
                for server, entry in shard.stats.items():
                    merged[server] = dict(entry)
        return merged

    def phase(self):
        """当前生效时段（取 std 分片任一 limiter 的判定，全池一致）。"""
        limiter = next(iter(self._shards["std"].limiters.values()))
        return limiter.phase()
