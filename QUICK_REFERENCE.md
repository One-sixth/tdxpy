# tdxpy 快查文档（QUICK_REFERENCE）

> 面向日常使用的 API 速查。点分式 symbol 是唯一语法；详细设计决策与实测记录见仓库 `MEMORY.md`。

## 1. 一分钟上手

```python
from tdxpy import TdxClient          # PEP 562 惰性导出，import 不拉起 pandas

with TdxClient.auto() as c:          # 并发测速选优 + 心跳 + 断线重连
    df = c.bars("sh.601919", period="day", count=100)   # A股日线
    q  = c.quotes(["sh.600519", "us.AAPL"])             # 混合市场实时快照
```

- editable 安装（normal 环境 `import tdxpy` 即最新）
- 连接管理：`TdxClient.auto(timeout=2.0, heartbeat=True, ...)` 自动选优；`TdxClient(host="111.229.247.189")` 指定服务器（`ex_host` 缺省同测速池）；支持 `with` 上下文
- 双通道懒加载：用到 EX 品种才连 7727（自动 login）
- 环境变量：`TDXPY_HANDSHAKE_MODE=new|legacy`（协议握手回退开关，默认 new）

## 2. symbol 语法（唯一格式：`{市场别名}.{代码}`）

只按第一个 `.` 拆分；市场段大小写不敏感；代码段 strip 后原样传递。

| 写法 | 解析 | 通道 |
|---|---|---|
| `sh.601919` / `sz.000001` / `bj.920992` | 标准市场（7709） | A股/基金/转债/指数 |
| `sh.000001` / `sz.399001` / `bj.899050` | 标准指数（自动路由指数命令） | 7709 |
| `hk.01398`（5位数字） | 港股主板（31） | EX 7727 |
| `hk.08001`（"08"开头） | 港股创业板（48） | EX 7727 |
| `hk.HSI` / `港股.恒生`（非数字） | 香港指数（27，自动 upper） | EX 7727 |
| `us.AAPL` / `美股.AAPL` | 美国股票（74） | EX 7727 |
| `qs.CUL8` / `qd.AL8` | 期货主连（品种+L8 后缀，**推荐写法**） | EX 7727 |
| `qs.CUL9` / `qd.AL9` | 期货加权（品种+L9 后缀；实测当日数据可用：CUL9=107500 / AL9=5090） | EX 7727 |
| `上期所.cu2510` / `zce.AP814` | 期货具体月合约 | EX 7727 |
| ~~`601919`~~ / ~~`sh601919`~~ | **已移除**，raise 并提示正确写法 | — |

> ⚠️ `main.*`（60 号"主力期货合约"市场，别名 main/主力/主力连续）虽然市场代号存在，但当前服务器数据面为空（实测 5 品种 0 行），主力连续请用上面的"交易所+L8"写法。

**市场段别名**：三源合一，大小写不敏感——
1. short_name 全集（`us`/`kh`/`qs`/`ma`/`zz`… 共 51 个，见 `ex_market_table()`）
2. 中英文昵称（`美股`/`港股`/`香港`/`恒生`/`郑商所`/`大商所`/`上期所`/`中金所`/`广期所`/`主力`/`comex`/`nymex`/`cbot`/`新三板`/`黄金`…）
3. `ExMarket` 英文成员名小写（`us_stock`/`hk_main`/`hk_index`…）

**一眼看全**：`ex_market_table()`（离线 DataFrame：market/category/name/short_name/alias/enum_member）、`list(ExMarket)`（51 成员）、`client.ex_markets()`（服务器实时）。

**代码段特例**：hk 系市场段（hk/港股/香港/hk_growth/香港创业板）遇**非数字**代码 → 自动落香港指数 27（如 `港股.HSI`）；香港指数代码自动 upper。

**全市场代号速查表**（点分式市场段写法 · id · 中文名 · 别名一览；同一行的任意写法均可用）：

| 市场段写法 | id | 中文名 | ExMarket 成员 |
|---|---|---|---|
| tp | 1 | 临时股 | TEMP |
| oz | 4 | 郑州商品期权 | ZCE_OPTION |
| od | 5 | 大连商品期权 | DCE_OPTION |
| os | 6 | 上海商品期权 | SHFE_OPTION |
| oj | 7 | 中金所期权 | CFFEX_OPTION |
| 50etf期权, qq, 期权 | 8 | 上海股票期权 | SSE_OPTION |
| sq | 9 | 深圳股票期权 | SZSE_OPTION |
| fe | 10 | 基本汇率 | FX_BASIC |
| fx | 11 | 交叉汇率 | FX_CROSS |
| wi | 12 | 国际指数 | GLOBAL_INDEX |
| co, comex | 16 | 纽约COMEX | COMEX |
| ny, nymex | 17 | 纽约NYMEX | NYMEX |
| cb, cbot | 18 | 芝加哥CBOT | CBOT |
| pr | 23 | 香港金融期货 | HK_FIN_FUTURE |
| pj | 24 | 香港金融期权 | HK_FIN_OPTION |
| ph | 25 | 香港股票期货 | HK_STOCK_FUTURE |
| pq | 26 | 香港股票期权 | HK_STOCK_OPTION |
| fh, hk_index, 恒生, 香港指数 | 27 | 香港指数 | HK_INDEX |
| qz, zce, 郑商所 | 28 | 郑州商品 | ZCE |
| dce, qd, 大商所 | 29 | 大连商品 | DCE |
| qs, shfe, 上期所 | 30 | 上海期货 | SHFE |
| hk, kh, 港股, 港股主板, 香港, 香港主板 | 31 | 香港主板 | HK_MAIN |
| fu | 33 | 开放式基金 | OPEN_FUND |
| fb | 34 | 货币型基金 | MONEY_FUND |
| hg | 38 | 宏观指标 | MACRO |
| ti | 42 | 商品指数 | COMMODITY_INDEX |
| hb | 43 | B股转H股 | B_TO_H |
| sb, 新三板, 股转 | 44 | 股转系统 | NEEQ |
| 沪金, 黄金 | 46 | 上海黄金 | SH_GOLD |
| cffex, cz, 中金所 | 47 | 中金所期货 | CFFEX |
| hk_growth, kg, 港股创业板, 香港创业板 | 48 | 香港创业板 | HK_GROWTH |
| kt | 49 | 香港基金 | HK_FUND |
| gy | 54 | 国债预发行 | BOND_PRE_ISSUE |
| ta | 56 | 阳光私募基金 | PRIVATE_FUND |
| tb | 57 | 券商集合理财 | BROKER_PLAN |
| tc | 58 | 券商货币理财 | BROKER_CASH_PLAN |
| ma, main, 主力, 主力连续 | 60 | 主力期货合约 | MAIN_CONTRACT |
| zz | 62 | 中证指数 | CSI_INDEX |
| eg | 65 | 广州套利期货 | GFEX_ARB |
| gfex, qg, 广期所 | 66 | 广州期货 | GFEX |
| og | 67 | 广州期权 | GFEX_OPTION |
| tz | 68 | 风控指数 | RISK_INDEX |
| bz | 69 | 华证指数 | HUAZHENG_INDEX |
| uz | 70 | 扩展板块指数 | BLOCK_INDEX_EXT |
| de, 德国 | 73 | 德国股票 | DE_STOCK |
| us, usa, 美股 | 74 | 美国股票 | US_STOCK |
| se, sg, 新加坡 | 78 | 新加坡股票 | SG_STOCK |
| fd | 91 | 资金市场 | MONEY_MARKET |
| ap | 98 | 港股暗盘 | HK_GREY |
| cm | 100 | 代码镜像 | CODE_MIRROR |
| gz | 102 | 国证指数 | CNI_INDEX |

## 3. K 线与行情方法

### bars —— 万能 K 线

```python
c.bars(symbol, period="day", start=0, count=100, adjust=None,
       all_pages=False, page_size=800, max_pages=None)
```

| 参数 | 说明 |
|---|---|
| `period` | `"1min"/"5min"/"15min"/"30min"/"1h"/"day"/"week"/"month"`（别名 `1m`/`5m`/`d`/`w`…） |
| `start` | 0=最新，向前翻页递增 |
| `count` | 单次条数（std ≤800，EX ≤700 自动钳制） |
| `adjust` | `None`/`"qfq"`/`"hfq"` 服务端复权，**仅标准市场**；EX 传非 None 报错 |
| `all_pages` | True 自动翻页拉全量（`max_pages` 防失控） |

- 路由：std→`get_security_bars`；指数段（sh 000/880/999、sz 399、bj 899）→`get_index_bars`；ex→`get_instrument_bars`
- 返回：DataFrame `datetime/open/close/high/low/vol/amount`（按时间升序）
- 例：`c.bars("sh.000001")`、`c.bars("us.AAPL", period="1min", count=50)`、`c.bars("qs.CUL8", all_pages=True)`

### bars_range —— EX 专属按日期区间 1 分钟线

```python
c.bars_range("zce.APL8", start_date="2020-01-22", end_date="2020-01-22")   # 单次 ~1500 根上限
```

仅 EX 市场；遇 `sh.`/`sz.`/`bj.` 前缀报错（标准市场用 bars 的 start/count 翻页）。已摘牌合约查不到。

### minute / minute_history —— 分时

```python
c.minute(symbol)                              # 当日 240 点（非交易时段返回空属正常）
c.minute_history(symbol, date="2020-01-02")   # 指定交易日，date 缺省=今天
```

返回：`datetime/price/vol`（EX 品种另含持仓量，期货含夜盘点）。

### ticks —— 分笔

```python
c.ticks(symbol, date=None, all=True)   # date 缺省=今天（走历史分笔接口，盘后可用）
```

返回列分市场：
- **std**: `time(HH:MM) / price / vol / buyorsell`（buyorsell 实测值域 1/2/3/5，官方语义待考证——文档旧说"0=买 1=卖 2=中性"与实测不符）
- **ex**: `date(datetime) / hour / minute / price / volume / zengcang / natrue_name / nature_name / direction / nature`

std 800/页、ex 1800/页自动翻页去重排序。EX 分笔服务器仅保留数月级深度。

⚠️ 无数据日期（服务器未存该日分笔）返回**同列结构的空 DataFrame**（`len(df)==0`）。科创系列指数（000688/000698/000699）早期日期及个别特殊日（如 2011-11-21）普遍无数据。同分钟内笔序依赖服务器返回顺序（分笔为 3 秒快照聚合，HH:MM 内无更细时间戳），跨页同分钟笔序不保证稳定。

⚠️ **当日分笔要次日才可拉**（2026-10-19 实测：10-19 盘后当天 `ticks()` 返回 0 笔，10-16 正常 4745 笔）——当日盘中分笔走实时接口 `get_transaction_data`（列 `time/price/vol/num/buyorsell`，多一个 `num`）。

📌 **与分钟 K 线的口径差（2026-10-19 交叉审计实测）**：服务器分钟 bar 按服务器时钟切片、标签=结束时刻（bar[09:31]=tick[09:30]+09:25 竞价笔，bar[11:30]=tick[11:29]+tick[11:30] 合并）；tick 的 HH:MM 是撮合回报时间——分钟边界附近笔归属有半拍差，逐分钟对比约 2~4% 分钟不匹配（固定出现在 10:08/10:48/11:28/14:18/14:59 等），属两数据源口径差而非数据错误。1day/5min 粒度合成与服务器 bar 完全一致（ETF diff ≤0.02=f4 精度）。

### quotes —— 实时快照（支持混合列表）

```python
c.quotes(["sh.600519", "sz.000001", "us.AAPL"])   # std 批量(≤80/次) + ex 逐只，列并集合并
```

- std 列：`market/code/price/last_close/open/high/low/vol/cur_vol/amount/s_vol/b_vol/bid1-5/ask1-5/bid_vol1-5/servertime/...`
- ex 列：公共列对齐（`pre_close→last_close`、`zongliang→vol`、`xianliang→cur_vol`）+ EX 特有（`kaicang/chicang/neipan/waipan/position`）；无五档（仅 bid1/ask1）
- 缺失列填 NaN；全部无记录返回空 DataFrame

### xdxr / finance —— 仅标准市场

```python
c.xdxr("sh.601919")     # 除权除息：year/month/day/category/name/fenhong/peigujia/songzhuangu/peigu/...
c.finance("sh.601919")  # 财务摘要：liutongguben/zongguben/jinglirun/meigujingzichan/... （拼音列，~38 列）
```

遇 EX 品种 raise（除权/财务数据不在 7727 行情服务器上，协议级无此数据）。

### security_count

```python
c.security_count(market=1)   # int 或 "sh"/"sz"/"bj"；返回该市场品种总数
```

## 4. 市场枚举与查询

```python
from tdxpy import ExMarket, StdMarket, ex_market_table

ExMarket.US_STOCK        # 74（IntEnum，兼容 int）
ExMarket("us")           # 74——枚举直接吃字符串别名
list(ExMarket)           # 51 个市场成员，一眼看全
ex_market_table()        # 离线 DataFrame：market/category/name/short_name/alias/enum_member
```

| 方法 | 说明 |
|---|---|
| `ex_markets()` | 服务器实时市场清单（51 市场） |
| `ex_instruments(market, limit=600)` | 指定市场合约列表（market 收 int/ExMarket/字符串别名；内部二分定位自动翻页） |

## 5. Affair 财务包（官方 HTTP 站下载 + 解析）

```python
from tdxpy import Affair

Affair.files()                                   # 远端清单（文件名/md5/大小）
Affair.fetch("gpcw20231231.zip", downdir="tmp")  # 下载（md5 校验，已存在跳过）
df = Affair.parse("gpcw20231231.zip", "tmp")     # DataFrame（index=code，581 中文列）
```

⚠️ 最新季包（如 `gpcw20260630.zip`）是服务器端 164B 空壳占位，取数用上一季。

## 6. vipdoc 离线读写（reader/writer，零联网零客户端依赖）

```python
from tdxpy.reader import TdxDailyBarReader, TdxExHqDailyBarReader, TdxLCMinBarReader
from tdxpy.writer import TdxDailyBarWriter, TdxExHqDailyBarWriter, TdxLCMinBarWriter

df = TdxDailyBarReader().get_df(r"...\vipdoc\sh\lday\sh601919.day")       # A股/指数日线
df = TdxExHqDailyBarReader().get_df(r"...\vipdoc\ds\lday\29#AL8.day")     # 期货日线
df = TdxLCMinBarReader().get_df(r"...\vipdoc\sh\minline\sh600000.lc1")    # 股票分钟线（默认 stock）
df = TdxLCMinBarReader(market="futures").get_df(r"...\vipdoc\ds\minline\30#CUL8.lc1")  # 期货分钟线

TdxDailyBarWriter().write_df(df, r"...\sh\lday\sh601919.day")             # append=False 覆盖
TdxLCMinBarWriter().write_df(df, path, append=True)                       # 追加（乱序拒绝，抛 ValueError）
```

**单位陷阱（高频踩坑点）**：

| 陷阱 | 规则 |
|---|---|
| reader 输出结构 | DataFrame 以 **date（或 datetime）为索引**，列 `open/high/low/close/amount/volume`（期货另含 `vol_in_stock` 持仓量/`jiesuan` 结算价）；reader 输出可直接喂 writer |
| 日线 volume | reader 输出=**手**（内置 ×0.01）；writer 输入=**手**（×100 编码回股）；零股交易会产生伪小数 |
| 分钟线/期货日线 volume | 原始单位直出，无系数 |
| 期货成交额 | 全链路无成交额字段（API Amount 恒 0），需 `volume×结算价×合约乘数` 自算 |
| 日线 28~31 字节 | 非除权标志，**解析时直接忽略**；除权数据走 `xdxr()` |
| 沪市 14:59 分钟根 | OHLC 相同且量额=0 属正常（14:57 收盘集合竞价） |
| 股票 1m 保留期 | 仅约 70 个交易日滚动窗口，长周期研究要定期备份 |

vipdoc 路径速记：`sh|sz|bj\lday` A股日线、`sh|sz|bj\minline|fzline` 股票分钟、`ds\lday|fzline|minline` 扩展市场（期货文件名 `市场编号#代码`：28=郑商 29=大商 30=上期+INE 47=中金 66=广期；连续合约 `L8` 主连/`L9` 加权）。

## 7. 错误消息对照（报错即教学）

| 触发写法 | 报错要点 → 正确写法 |
|---|---|
| `c.bars("601919")` | 裸代码已不支持 → `"sh.601919"` |
| `c.bars("sh601919")` | 旧式无点前缀已移除 → `"sh.601919"` |
| `c.bars("xx.123")` | 无法识别的市场 → `ex_market_table()` 查别名 |
| `c.bars("us.AAPL", adjust="qfq")` | EX 不支持服务端复权 → `adjust=None` |
| `c.xdxr("us.AAPL")` / `c.finance("us.AAPL")` | 仅标准市场存在（EX 协议无此数据） |
| `c.bars_range("sh.601919", ...)` | 日期区间查询仅 EX 支持 → bars 的 start/count |
| `c.bars("hk.1234")` | 港股代码须 5 位数字 |

## 8. 已知边界与服务器现状

| 项 | 现状 |
|---|---|
| `main`（60 主力连续）市场 | 服务器数据面当前为空（2026-09-17 三台实测，路由正常），用 `qs.CUL8` 等具体市场替代 |
| EX 分笔深度 | 仅数月级；更早用 `bars_range` 1 分钟线 |
| 当日分时（minute） | 非交易时段返回空 |
| 在线分钟线深度 | 5min ≈2 年 / 1min ≈4 个月（服务器保留上限）；2020 年的分钟级走 `minute_history` 按日分时/分笔合成 |
| SH 深分页 | 转债等深 offset 段严重限速，定位代码段勿扫 SH 深列表 |
| 复权 | 服务端复权（adjust）仅标准市场；EX 品种无复权 |

## 9. 底层与测试

- 协议层直调：`from tdxpy.hq import TdxHq_API` / `from tdxpy.exhq import TdxExHq_API`（pytdx 风格 API，门面底层；一般用门面即可）
- 测试套件（联网失败自动 skip）：

```
python tests/test_routing_and_coefficient.py   # 离线：解析/系数/枚举
python tests/test_facade_smoke.py              # 门面在线冒烟
python tests/test_online_smoke.py              # 协议层在线冒烟
```

- 测试服务器可用环境变量覆盖：`TDXPY_TEST_HQ_HOST` / `TDXPY_TEST_EX_HOST`
