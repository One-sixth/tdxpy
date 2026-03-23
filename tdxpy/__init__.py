__version__ = "0.2.7"
__author__ = "bopo.wang <ibopo@126.com>"

__all__ = ["TdxClient", "Affair", "ExMarket", "StdMarket", "ex_market_table"]


def __getattr__(name):
    # 惰性导出（PEP 562）：import tdxpy 不再强制拉起 pandas/socket，
    # 只用 reader/writer 离线功能的场景不受影响。
    if name == "TdxClient":
        from tdxpy.client import TdxClient

        return TdxClient
    if name == "Affair":
        from tdxpy.affair import Affair

        return Affair
    if name in ("ExMarket", "StdMarket", "ex_market_table"):
        import tdxpy.markets as _markets

        return getattr(_markets, name)
    raise AttributeError(f"module 'tdxpy' has no attribute {name!r}")
