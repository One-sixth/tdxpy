# cython: language_level=3
"""MAC EX 通道命令（0x122D/E/F 帧首字节改写版）：港股/美股/期货/指数扩展行情。

协议探明（2026-10-19 在线实测，116.205.135.205:7727）：
  - 连接后须 login（0x2454 + 80B 载荷），此后请求帧首字节 0x1C→0x01（EX 模式）；
  - 0x122D/0x122E/0x122F 请求与响应**载荷与 A 股 MAC 通道完全同构**（同头/同记录
    布局），仅帧 flag 不同——因此本模块三命令直接继承 A 股 MAC parser，
    只覆写 send_pkg 帧构造器，解析逻辑零重复；
  - 市场段为 ExMarket id（HK_MAIN=31/US_STOCK=74/SHFE=30/CFFEX=47/HK_INDEX=27…），
    代码段 22s gbk（港股 5 位数字/美股字母/期货主力 RBL8 等）。
"""
from tdxpy.parser.mac._frame import repack_ex_frame
from tdxpy.parser.mac.mac_bars import MacBarsCmd
from tdxpy.parser.mac.mac_minute import MacMinuteCmd
from tdxpy.parser.mac.mac_ticks import MacTicksCmd


class ExBarsCmd(MacBarsCmd):
    """MAC EX 通道 K 线（0x122E；市场=ExMarket id，港股 31/美股 74/上期所 30…）。

    继承 A 股 MAC K 线解析（36B 记录/复权/周期表全复用），仅帧构造走 EX 模式
    （flag 0x01，repack_ex_frame 一步改写）。实测深度：美股日线 ~6945 根
    （1999-03 起）、港股 ~3500 根（2012-07 起）、期货主力连续 ~3171 根
    （2013-09 起）。
    """

    def setParams(self, market, code, category=4, start=0, count=700, adjust=0, times=1):
        super().setParams(market, code, category, start, count, adjust, times)
        self.send_pkg = repack_ex_frame(self.send_pkg)


class ExMinuteCmd(MacMinuteCmd):
    """MAC EX 通道单日分时（0x122D；18B 记录/均价线全复用）。

    港股 330 点/美股 390/恒指/期货 345 点（含夜盘）；响应头与 A 股 MAC 分时同构。
    一天点数随市场而变（A 股 240/港股 330/美股 390/期货 345），不设上限 cap，
    按响应实际条数解析。
    """

    _MAX_POINTS = None

    def setParams(self, market, code, date=0):
        super().setParams(market, code, date)
        self.send_pkg = repack_ex_frame(self.send_pkg)


class ExTicksCmd(MacTicksCmd):
    """MAC EX 通道逐笔成交（0x122F；18B 记录全复用）。

    ⚠️ 市场语义差异（2026-10-19 实测）：
      - 美股/指数为分钟级聚合笔（同分钟多笔、trade_count=0）；
      - 期货逐笔 bs_flag 出现 >10000 值、trade_count 有 u32 环绕值——字段含义
        与 A 股不同，未定性前不要直接套用 A 股方向语义；
      - 期货逐笔深度仅 ~1 个月；港股逐笔无数据（330 点分时可用）。
    """

    #: EX 侧 trade_count 语义未定性（期货有 u32 环绕值），保持 uint32——
    #: 不套用 A 股"竞价段负值"结论（2026-11-04，A 股侧已改有符号）
    _TRADE_COUNT_FMT = "I"

    def setParams(self, market, code, date=0, start=0, count=1000):
        super().setParams(market, code, date, start, count)
        self.send_pkg = repack_ex_frame(self.send_pkg)
