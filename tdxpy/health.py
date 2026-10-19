# ----------------------------------------------------------------------
# tdxpy/health.py —— 轻量健康分引擎（纯内存状态机，零依赖、无网络 IO）
# ----------------------------------------------------------------------
"""连接选路的健康分状态机（F1.4，吸收 pytdxdata ``pool/health.py`` 经验参数）。

规则（按 pytdxdata 实测参数 + tdxpy 场景简化）：

- 成功 ``+0.2``（上限 1.0），失败 ``×0.5``（下限 0.05）——坏节点快速降权、
  好节点稳步回升；
- 连续失败 ≥3 次进入冷却期（默认 120s），冷却期内选路让位健康节点、
  到期自动恢复参与；
- 延迟用 EWMA（α=0.3）平滑——单次抖动不会让排名骤变；
- 选路排序键 ``effective_latency = ewma_latency / score``（又快又稳者优先）。

状态不可变（NamedTuple），``health_update`` 返回新状态——纯函数无副作用，
可完全离线单测（tests/test_health.py）。健康字典由 client.TdxClient 持有，
跨 auto() 调用与同进程所有实例共享。
"""

from __future__ import annotations

import time
from typing import NamedTuple

__all__ = [
    "HealthState",
    "health_update",
    "health_is_available",
    "health_effective_latency",
    "health_rank",
    "HEALTH_INIT",
    "SUCCESS_BONUS",
    "HEALTH_CEILING",
    "FAIL_FACTOR",
    "HEALTH_FLOOR",
    "COOLDOWN_SECONDS",
    "COOLDOWN_AFTER_FAILS",
    "EWMA_ALPHA",
]

HEALTH_INIT = 0.5
"""初始分数：中性偏谨慎，冷启动节点不占优也不吃亏。"""
SUCCESS_BONUS = 0.2
"""单次成功的加分数。"""
HEALTH_CEILING = 1.0
"""分数上限。"""
FAIL_FACTOR = 0.5
"""单次失败的乘数（×0.5）。"""
HEALTH_FLOOR = 0.05
"""分数下限：永不归零，冷却后仍保留微弱翻身机会。"""
COOLDOWN_SECONDS = 120.0
"""连续失败触发的冷却时长（秒）。"""
COOLDOWN_AFTER_FAILS = 3
"""触发冷却所需的连续失败次数。"""
EWMA_ALPHA = 0.3
"""EWMA 平滑系数：新延迟权重 0.3、历史权重 0.7。"""


class HealthState(NamedTuple):
    """单节点健康状态。

    :ivar score: 健康分 ∈ [0.05, 1.0]
    :ivar ewma_latency: EWMA 平滑延迟（秒）；None=尚无延迟数据
    :ivar consecutive_fails: 连续失败次数（成功即清零）
    :ivar cooldown_until: 冷却截止时刻（time.monotonic() 秒）；0=不在冷却
    """

    score: float = HEALTH_INIT
    ewma_latency: float | None = None
    consecutive_fails: int = 0
    cooldown_until: float = 0.0


def health_update(state, ok, latency=None, now=None):
    """一次探测/调用结果 → 新状态（原状态不变）。

    :param state: 当前 :class:`HealthState`；None=首次记录，取初始分
    :param ok: 本次是否成功
    :param latency: 本次耗时（秒）；None 表示本次无延迟数据（如断线标记），
        EWMA 保持原值
    :param now: ``time.monotonic()`` 秒；None 取当前时刻（测试可注入固定值）
    :return: 新 :class:`HealthState`
    """
    now = time.monotonic() if now is None else now
    state = state or HealthState()
    if ok:
        score = min(state.score + SUCCESS_BONUS, HEALTH_CEILING)
        fails = 0
        cooldown_until = 0.0
    else:
        score = max(state.score * FAIL_FACTOR, HEALTH_FLOOR)
        fails = state.consecutive_fails + 1
        if fails >= COOLDOWN_AFTER_FAILS:
            cooldown_until = now + COOLDOWN_SECONDS
        else:
            cooldown_until = state.cooldown_until  # 未达连续阈值，维持原冷却
    ewma = state.ewma_latency
    if latency is not None:
        ewma = latency if ewma is None else (ewma * (1 - EWMA_ALPHA) + latency * EWMA_ALPHA)
    return HealthState(score, ewma, fails, cooldown_until)


def health_is_available(state, now=None):
    """节点是否可参与选路：冷却期内 False，其余（含无状态 None）True。"""
    now = time.monotonic() if now is None else now
    if state is None:
        return True
    return now >= state.cooldown_until


def health_effective_latency(state):
    """排序键：``ewma_latency / score``——延迟越低、分数越高越靠前。

    无延迟数据时退化为 ``1 / score``（纯分数排序）。
    """
    state = state or HealthState()
    if state.ewma_latency is not None:
        return state.ewma_latency / max(state.score, HEALTH_FLOOR)
    return 1.0 / max(state.score, HEALTH_FLOOR)


def health_rank(candidates, now=None):
    """按健康度排序候选节点。

    :param candidates: ``[(key, state_or_None), ...]``——key 通常为 (host, port)
    :param now: ``time.monotonic()`` 秒；None 取当前
    :return: ``[(key, state), ...]`` 升序（最优在前）。规则：
        冷却期节点整体排到非冷却节点之后（全池冷却时仍可兜底选优，
        不直接剔除以免无解）；同层按 effective_latency 升序。
    """
    now = time.monotonic() if now is None else now
    rows = []
    for key, state in candidates:
        state = state or HealthState()
        rows.append((not health_is_available(state, now), health_effective_latency(state), key, state))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(r[2], r[3]) for r in rows]
