# tdxpy 快查文档（QUICK_REFERENCE）

> 面向日常使用的 API 速查。点分式 symbol 是唯一语法；详细设计决策与实测记录见仓库 `MEMORY.md`。
>
> 章节不编号——按标题搜即可，新增小节不触发全篇重排。

## 一分钟上手

```python
from tdxpy import TdxClient          # PEP 562 惰性导出，import 不拉起 pandas

with TdxClient.auto() as c:          # 并发测速选优 + 心跳 + 断线重连
    df = c.bars("sh.601919", period="day", count=100)   # A股日线
    q  = c.quotes(["sh.600519", "us.AAPL"])             # 混合市场实时快照
```

- editable 安装（normal 环境 `import tdxpy` 即最新）
- 连接管理：`TdxClient.auto(timeout=2.0, heartbeat=True, ...)` 自动选优；`TdxClient(host="111.229.247.189")` 指定服务器（`ex_host` 缺省同测速池）；支持 `with` 上下文
- 多通道懒加载：用到 EX 品种才连 7727，用到 MAC/MAC EX 才连对应池（自动握手/登录）
- 环境变量：`TDXPY_HANDSHAKE_MODE=new|legacy`（协议握手回退开关，默认 new）
- 异常带 `.code`（分段错误码）与 `.server`（出错服务器）→ 见「错误码与异常」
- 需要多连接并发批量下载时用 `ServerPool`（不是开 N 个 `TdxClient`）→ 见「服务器池与限流」
- 示例均为可直接粘贴运行的片段（联网部分需能访问通达信服务器）；设计决策与实测记录见 `MEMORY.md`

## 服务器池与限流

`TdxClient.auto()` 是「单实例 + 内部选优」，适合日常单线程取数；要**多连接并发批量下载**（全市场扫数据）时用独立的 `ServerPool`——它只管「选谁」，连接对象是轻量 `TdxHq_API`/`TdxExHq_API`，不带门面的多通道与心跳线程。

```python
from tdxpy import ServerPool

pool = ServerPool(size=10, rate_limit=15)       # 默认取 STD_SERVERS 前 10 台
for sym in sh_codes:            # 例如 600000, 600519, ...
    server, api = pool.next_client(kind="std")  # 轮转 + 健康分跳过 + 限流等待
    try:
        rows = api.get_security_bars(9, 1, sym, 0, 800)   # 9=日线, 1=上海
        pool.report(server, ok=True, latency=0.12)   # 回执写入健康分
    except Exception:
        pool.report(server, ok=False)                # 失败降分，不主动剔除

pool.stats()    # {(host,port): {req, fail, last_latency_ms}}
pool.phase()    # 当前时段：trading / pre_post / closed
```

- **时段自适应限流**：per-server 基准 15 req/s（分时档 10），按东八区时段乘数放大——交易时段 ×1、盘前盘后 ×2/×1.5、周末 ×4/×3，有效速率封顶 200 req/s。`phase="trading"` 可强制时段（测试/离线可用）。
- **健康分**：成功 +0.2（上限 1.0）、失败 ×0.5（下限 0.05）、连续失败 ≥3 冷却 120 s；与 `TdxClient.auto()` 共用类级 `TdxClient._health`，两处互通。
- **全池不可用**时抛 `TdxConnectionError`，`code=CODE_POOL_EXHAUSTED`（2050）。
- 其他参数：`servers=` / `ex_servers=` 自定义服务器表、`timeout=10.0` 建连超时、`client_factory=` 注入假工厂、`health_dict=` 注入独立健康分字典。
- 锁粒度：池锁只包「选服务器 + 轮转指针 + 取引用」三步；限流 `wait` 在每台自己的锁内执行——同一台被两线程同时选中时由限流锁串行，不会绕过限速。

## symbol 语法（唯一格式：`{市场别名}.{代码}`）

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
| `qs.CUL7` / `qd.AL7` | 期货次连（L7；连续合约 L7=次连/L8=主连/L9=加权，2026-11-05 补记） | EX 7727 |
| `qs.CUL9` / `qd.AL9` | 期货加权（品种+L9 后缀；实测当日数据可用：CUL9=107500 / AL9=5090） | EX 7727 |
| `上期所.cu2510` / `zce.AP814` | 期货具体月合约 | EX 7727 |
| `38.1_GDP` / `38.2_CPI` | **纯数字市场段 = ExMarket id 直用**（仅已登记 id；宏观指标 38，2026-11-05） | EX 7727 |
| ~~`601919`~~ / ~~`sh601919`~~ | **已移除**，raise 并提示正确写法 | — |

> ⚠️ `main.*`（60 号"主力期货合约"市场，别名 main/主力/主力连续）虽然市场代号存在，但当前服务器数据面为空（实测 5 品种 0 行），主力连续请用上面的"交易所+L8"写法。

**市场段别名**：三源合一，大小写不敏感——
1. short_name 全集（`us`/`kh`/`qs`/`ma`/`zz`… 共 54 个，见 `ex_market_table()`）
2. 中英文昵称（`美股`/`港股`/`香港`/`恒生`/`郑商所`/`大商所`/`上期所`/`中金所`/`广期所`/`主力`/`comex`/`nymex`/`cbot`/`英国`/`香港债券`/`新三板`/`黄金`…）
3. `ExMarket` 英文成员名小写（`us_stock`/`hk_main`/`hk_index`…）

**一眼看全**：`ex_market_table()`（离线 DataFrame：market/category/name/short_name/alias/enum_member）、`list(ExMarket)`（54 成员）、`client.ex_markets()`（服务器实时）。

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
| sg, 沪金, 黄金 | 46 | 上海黄金 | SH_GOLD |
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
| kb, hk_bond, 香港债券 | 22 | 香港债券 | HK_BOND |
| uk, 英国 | 75 | 英国股票 | UK_STOCK |
| mz | 105 | 更多其它指数 | MORE_INDEX |
| se, sg, 新加坡 | 78 | 新加坡股票 | SG_STOCK |
| fd | 91 | 资金市场 | MONEY_MARKET |
| ap | 98 | 港股暗盘 | HK_GREY |
| cm | 100 | 代码镜像 | CODE_MIRROR |
| gz | 102 | 国证指数 | CNI_INDEX |

## K 线与行情方法

### bars —— 万能 K 线

```python
c.bars(symbol, period="day", start=0, count=100, adjust=None,
       all_pages=False, page_size=800, max_pages=None)
```

| 参数 | 说明 |
|---|---|
| `period` | `"1min"/"5min"/"15min"/"30min"/"1h"/"day"/"week"/"month"`（别名 `1m`/`5m`/`d`/`w`…） |
| `start` | 0=最新，向前翻页递增 |
| `count` | 单次条数。`all_pages=False` 原样透传（std 超 800 **整请求失败**、EX 超 700 截断）；`all_pages=True` 自动钳制（std 800 / EX 700） |
| `adjust` | `None`/`"qfq"`/`"hfq"` 服务端复权，**仅标准市场**；EX 传非 None 报错 |
| `all_pages` | True 走自适应翻页拉全量：静态钳制 + 欠填即降档 + `max_pages` 熔断（底层另有 `max_requests=20000` 总请求数护栏） |

- 路由：std→`get_security_bars`；指数段（sh 000/880/999、sz 399、bj 899）→`get_index_bars`；ex→`get_instrument_bars`
- 返回列（实测顺序）：std = `open/close/high/low/vol/amount/year/month/day/hour/minute/datetime`；EX = `open/high/low/close/position/trade/price/year/month/day/hour/minute/datetime/amount`（`datetime` 在尾部）。按 `datetime` 升序。⚠️ EX 路径（`get_instrument_bars`）用的是 parser 原始字段：**权益类市场（港股主板 31/创业板 48/港基 49/新三板 44/美股 74）第 5 字段=真实成交额**（恒等式 `amount/(close×trade×100)≈1` 主板/基金 vol=手，`≈1` 其余 vol=股，2026-11-04 实测）；期货/期权类第 5 字段=持仓量 position、amount 恒 0（协议帧无成交额）、price 实为结算价
- 例：`c.bars("sh.000001")`、`c.bars("us.AAPL", period="1min", count=50)`、`c.bars("qs.CUL8", all_pages=True)`

### bars_range —— EX 专属按日期区间 1 分钟线

```python
c.bars_range("zce.APL8", start_date="2020-01-22", end_date="2020-01-22")   # 单次 ~1500 根上限
```

仅 EX 市场；遇 `sh.`/`sz.`/`bj.` 前缀报错（标准市场用 bars 的 start/count 翻页）。已摘牌合约查不到。32B 记录的 24B 数据段与 `bars()` EX 路径同构（**列名差异**：本路径第 9 字段叫 `settlementprice`，`bars()` 里叫 `price`；期货两者均为结算价）——权益类市场（31/48/49/44/74）第 5 字段=真实成交额（`amount` 列，恒等式同 bars）、position 置 0.0；期货类 position=持仓量、amount 恒 0（2026-11-05 交叉复审补齐，实测 AAPL 恒等式≈1.0/hk01398≈100）。另含 `day_offset` 列：d2 时间跨日不回绕（美股 1 分钟线凌晨段 hour 24~27 → 归一化 + day_offset=1，此前会产出非法时钟）。⚠️ 美股当日返回的首行 `00:00` 为服务器开盘标记行（close=当日参考价），按 datetime 排序后位于行首，做时序计算时注意。

### minute / minute_history —— 分时

```python
c.minute(symbol)                              # 当日 240 点（非交易时段返回空属正常）
c.minute_history(symbol, date="2020-01-02")   # 指定交易日，date 缺省=今天
```

返回列：**std = `price/vol`**（无 datetime，序号即分钟序，A 股 240 点）；**EX = `hour/minute/day_offset/price/avg_price/volume/open_interest`**（无 datetime、无 `vol` 列名，成交量叫 `volume`；港股 330 点 / 期货含夜盘 345 点）。EX 路径（0x0B24 当日 + 0x0C24 历史日，2026-11-05 同步归一化）的 `day_offset` 是跨日偏移：raw_time 跨日不回绕，美股/期货夜盘 hour 取模归一化（21:30~次日 03:59 → hour 21~3 + day_offset=1，修复前会产出 24~27 非法时钟）；A 股/港股正常时段恒 0。⚠️ **期货历史分时 summary 全 0**（2026-11-05 补记，eltdx 30d3dac 同结论）：0x0C24 响应的汇总字段（开/高/低/量/额）服务器不填（charts 明细完整、仅 pre_close 有值）——汇总请自行从 charts 聚合，勿信 summary 段。

> 当日分时走 0x051D（2026-10-19 新格式破解落地：响应=变长头部+差分记录流，parser 扫描+close 锚点自检自适应定位，价格与 minute_history 基准一致）。BJ 810 定向可转债 / 82 优先股价格系数 0.0001（vipdoc 与在线分时同口径）。

### ticks —— 分笔

```python
c.ticks(symbol, date=None, all=True)   # date 缺省=今天（走历史分笔接口，盘后可用）
```

返回列分市场：
- **std**: `time(HH:MM) / price / vol / buyorsell`（buyorsell 实测值域 1/2/3/5，官方语义待考证——文档旧说"0=买 1=卖 2=中性"与实测不符）
- **ex**: `date(datetime) / hour / minute / price / volume / zengcang / natrue_name / nature_name / direction / nature`

`all=True` 时 std **2000**/页、EX 1800/页自适应翻页后按时间重排；`all=False` 是单页取数（不翻页），std 固定 800 笔、EX 1800 笔——要全天数据请用 `all=True`。EX 分笔服务器仅保留数月级深度。

⚠️ 无数据日期（服务器未存该日分笔）返回**同列结构的空 DataFrame**（`len(df)==0`）。科创系列指数（000688/000698/000699）早期日期及个别特殊日（如 2011-11-21）普遍无数据。同分钟内笔序依赖服务器返回顺序（分笔为 3 秒快照聚合，HH:MM 内无更细时间戳），跨页同分钟笔序不保证稳定。

⚠️ **当日分笔要次日才可拉**（2026-10-19 实测：10-19 盘后当天 `ticks()` 返回 0 笔，10-16 正常 4745 笔）——当日盘中分笔走实时接口 `get_transaction_data`（列 `time/price/vol/num/buyorsell`，多一个 `num`）。

📌 **与分钟 K 线的口径差（2026-10-19 交叉审计实测）**：服务器分钟 bar 按服务器时钟切片、标签=结束时刻（bar[09:31]=tick[09:30]+09:25 竞价笔，bar[11:30]=tick[11:29]+tick[11:30] 合并）；tick 的 HH:MM 是撮合回报时间——分钟边界附近笔归属有半拍差，逐分钟对比约 2~4% 分钟不匹配（固定出现在 10:08/10:48/11:28/14:18/14:59 等），属两数据源口径差而非数据错误。1day/5min 粒度合成与服务器 bar 完全一致（ETF diff ≤0.02=f4 精度）。

### delisted_bars —— 退市股不复权日线（0x052B）

```python
c.delisted_bars("sz.000038")                      # 单只（count=800）
c.delisted_bars("T000038")                        # 兼容 eltdx 四种写法：sz.000038 / sz000038 / T000038 / 000038
c.delisted_bars("sz.000038", all_pages=True)      # 自动翻页拉全量（1994 上市 → 退市日）；max_pages 默认 200（800×200=16 万根熔断）
c.delisted_bars(["T000038", "sh.600001"])         # 批量 → dict{标准代码: DataFrame}（先全校验后查询）
```

返回 `datetime/open/close/high/low/vol/amount`，时间升序。裸 6 位代码按首位推断市场（92/8→BJ、6/9→SH、0/1/2/3→SZ）。start 语义同 bars()（0=最新）。批量列表含非法代码时**一次性全部拒绝**（不产生半成品查询）。

### mac_bars / mac_minute / mac_ticks —— MAC 通道（独立连接）

```python
c.mac_bars("sh.600000", period="day", count=700)   # MAC K 线（支持复权、含 amount）
c.mac_bars("sh.600000", period="1min", times=5)    # 参数化 N 分钟（times=5 → 5 分钟 bar）
c.mac_bars("sh.600000", period="10min")            # "Nmin" 别名（N=2~240，PERIOD_MAP 外的新周期）
c.mac_minute("sh.600000", date="2026-10-16")       # MAC 单日分时 240 点（含均价 avg_price，可查历史）
c.mac_ticks("sh.600000", date="2026-10-19", count=1000)  # MAC 逐笔（秒级、含成交笔数 trade_count；默认剔除竞价委托 bs_flag=8）
```

MAC 通道 = 老三条 SETUP 会话 + MAC 帧（0x1C 头）+ MAC 专用服务器池（`TdxClient.MAC_SERVERS`，懒加载独立连接）。⚠️ 与 std 通道的差异：仅 sh/sz/bj 标准市场；vol 口径——std `bars()` 与 `mac_bars` 均为**股**，`delisted_bars` 为**手**（/100）；日线 hour 恒 15:00；分钟 bar 标签=开始时刻（与 std 一致，逐 bar vol 对照实测一致）。MAC 周期编码 0-6 与 std 同值，**7=固定 1min（times 被服务器忽略）、8=参数化 1min（times=N → N 分钟 bar，实测 2/3/4/5/7/10/15/20/30/45/60/120 全通）**、9=day、10=季、11=年（全周期在线探测实锤；⚠️ 7/8 曾被记为"都是 1min 系"——2026-10-21 三台服务器实测勘误：times 钩子仅 8 生效，std "1min"(7) 直传会**静默丢失 times**，门面经 `resolve_mac_period` 自动映射 7→8）；未知周期 raise 不静默回退；times 与 period 冲突 / times 用于非 1 分钟系 raise 不静默丢弃。数据面与 std 互相独立，MAC 侧部分品种更新更快。

**mac_ticks 深度（2026-10-19 实测）**：sh.600000 可查至 **2000 年**（26 年，std 历史分笔仅到 2006 年末）；单页 ≤1000（请求 2000 服务器仍回 1000），start=0 最新一笔向前翻页；600000 全天约 4381 笔（09:25:01 含集合竞价 → 15:00 盘后散笔）；时间戳 3 秒节奏为主（3s 占 94%）；约 2015 前的老数据 trade_count 恒 0、time 秒粒度退化为分钟（14:59:00 群聚形态）。**竞价委托过滤（2026-11-04 吸收自 pytdxdata 0.6.5）**：`drop_auction_orders=True`（默认）剔除 bs_flag=8 的集合竞价**委托**记录（非成交、vol 为累计值，计入虚高 Σvol 20%~160%，且跨主机收录 0~135 条不稳定）；trade_count 已改有符号解析（竞价段为负值，uint32 会读出 42 亿脏数据）；EX 通道（mac_ex_ticks）语义未定性，不过滤、保持 uint32。

### index_trade_stats —— 指数每分钟成交量/笔数（2026-11-05）

```python
c.index_trade_stats("sh.000300", workers=4)   # 成分股逐笔聚合，~10s/300 只
c.index_trade_stats("sz.399001", date="2026-11-04")
```

**为什么绕成分聚合**：协议里指数逐笔 trade_count = 当时均价×1000（非笔数，pytdxdata b84a6c8 定性）——指数自身无真实笔数。返回 `minute/vol/trade_count/cum_vol/cum_trades`（分钟增量+累计，09:25 集合竞价起；**15:05–15:30 盘后定价只有科创板成分才有**，多数指数实际覆盖到 15:00）。**对账实锤**：上证50 全成分 Σvol vs 指数日线 vol 比值 1.00065（差 0.065%=成分覆盖边界；综指 000001/399106 等有 ~4.6% 系统性低估——服务器成分缺近期新股，已知综指清单自动告警）。vol 单位 = **手**，与日线 vol（**股**）不同口径：docstring 记录的全天 Σ/日线 vol=0.010001（即 1/100）正是这个单位差；跨源对账须先 ×100 换算（上证50 折算后比值 1.00065），勿直接把两者相加。北交所成分 trade_count 恒 0 → cum_trades 全 0。板块码映射：000xxx→31000+int、399xxx→-399000+30000、899xxx→-899000+32000、880xxx→-880000+20000。

### mac_ex_bars / mac_ex_minute / mac_ex_ticks —— MAC EX 通道（独立连接，港美股指/期货）

```python
c.mac_ex_bars("us.AAPL", count=700)                 # 美股日线（深度 ~6945 根，1999-03 起）
c.mac_ex_bars("hk.01398", period="day")             # 港股（深度 ~3500 根，2012-07 起）
c.mac_ex_bars("上期所.RBL8", count=700)             # 期货主力连续（~3171 根，2013-09 起）
c.mac_ex_bars("hk.HSI", period="1min", times=5)     # 全球指数 + 参数化 N 分钟
c.mac_ex_minute("hk.01398")                         # 港股分时 330 点（实测值，含均价；期货 345 点含夜盘）
c.mac_ex_ticks("hk.HSI", count=1000)                # 逐笔（美股/指数=分钟级聚合笔）
```

MAC EX 通道 = EX 服务器（7727）+ 0x2454 login + 帧首字节 0x1C→0x01（`TdxClient.MAC_EX_SERVERS`，懒加载独立连接）。0x122D/E/F 载荷与 A 股 MAC 完全同构，复权/周期/times 钩子全复用。**symbol 仅扩展市场**（hk/us/期货/指数…）。⚠️ std 市场（sh/sz/bj）id 0–2 与 ExMarket id 空间不通用：**门面层直接 `raise ValueError`**（`_ex_symbol` 拦下，A 股请走 mac_bars/mac_minute/mac_ticks）；只有绕过门面在 API 层直调时，std symbol 才会被 EX 服务器按 ExMarket 语义错解释返回空（sh.600000/sz.000001/bj.899050 实测全空）。⚠️ 本通道 0x122D/E/F **MAC 帧族**在 login 服务器上外盘期货（COMEX/NYMEX/CBOT）与期权市场空板（2026-10 实测）——但**经典 EX 0x23xx 命令在全部 5 台服务器上都有外盘/期权数据**（交叉复测逐值一致），要外盘数据直接走 bars()（见下「EX 池 setup 双握手」）；期货逐笔深度仅 ~1 个月、bs_flag/trade_count 字段语义未定性（勿套用 A 股方向语义）；港股逐笔无数据（分时可用）。
### mac_boards / mac_ex_boards —— 板块列表（0x1231）

```python
c.mac_boards(1, 0, 100)        # A 股行业板块（881xxx + 领涨股；board_type 0=全部 128 / 1=行业 345）
c.mac_ex_boards(0, 0, 100)     # 港股板块（HK0xxx，market=70；board_type=0 全部 160 个）
```

0x1231 双通道实测（2026-10-20）：每行 = 板块（market/code/name/price/pre_close）+ `leader_*` 领涨股六字段；头 4B 含 total（该类板块总数）。MAC 通道走 A 股 MAC 连接（A 股板块），MAC EX 通道走 EX 侧 MAC 连接（港股板块）；`board_type` 除 0/1 外语义待探。rise_speed 字段实测恒 0（保留待定性）。
### mac_members / mac_auction / mac_unusual / mac_snapshot —— MAC 扩展命令族（2026-10-20）

```python
c.mac_members("881248")                  # 板块成分报价（跨深沪北；pre/open/high/low/close/vol/量比/额）
c.mac_members("881001", start=20)        # 翻页；sort_type 其余值语义待探
c.mac_auction("sz.000001")               # 集合竞价逐笔（09:15-09:25 3 秒一条，末条价格=开盘价）
c.mac_unusual()                          # 市场异动 600 条/页（type_name 竞价试买/竞价涨跌…15 类）
c.mac_unusual(all_pages=True)            # 全天异动聚合（实测 ~7572 条，09:15→15:00 全覆盖）
c.mac_snapshot("sz.000001")              # 个股快照（OHLC/量额 + 内外盘 in_vol/out_vol + activity 笔数）
c.mac_quotes(["sz.000001", "sh.600519"]) # 批量自定义字段位报价（≤80 只/批自动分批）
c.mac_quotes(["sz.000001"], bits=[0x00, 0x04])  # 自定义字段位（昨收+收）
```

- **0x122C 成分**：board_code '881248' 自动换算 → 21248（881xxx→-880000+20000；399xxx→+30000 段；899xxx→+32000 段）；响应=20B 位图+`<IH`(total,rows)+68B/行+按位图字段流；请求字段位可定制（`bits` 参数，已验证 0x00 昨收~0x07 额）；成分跨市场（0/1/2）；**sort_type 实测定论（2026-10-21）：0-7 全部无效果**——双板块（9 只银行 + 80 只深板块）×8 值，顺序/成员集/字段逐值相同（服务器忽略该字段），成分序≈固定序（近代码升序）；门面保留参数仅表协议字段存在，排序请客户端自行做
- **0x123D 竞价**：count@24 + 16B/条 `<IfIi`（time_sec/price/matched 累计/unmatched 可负）；实测一天 59 条；unmatched 负值=未匹配卖压。
- **0x1237 异动**：32B/条 = market/code/type@9/idx@11 + 13B 数据（f32 触发时点价 + f32 触发时点额(万)）+ BCD 时间；尾部 GBK 名字表。⚠️ **price/amount 是触发时点快照**（000530 触发价 7.02 vs 当日开盘 7.66；竞价时段为虚拟撮合值），非全天口径；type 0x13 竞价试买 price=0（试盘无成交）。类型码表：0x03 主力买卖 0x04 加速拉升 0x05 加速下跌 0x06 低位反弹 0x07 高位回落 0x08 撑杆跳高 0x09 平台跳水 0x0A 单笔冲涨跌 0x0B 区间放量 0x0C 区间缩量 0x10 大单托盘 0x11 大单压盘 0x12 大单锁盘 0x13 竞价试买 0x14 竞价涨跌；请求尾部 5 参数（1,200,30,40,50,200）语义待探。
- **0x122A 快照**：@96 `<III6fIfII`（date/time/activity/6f/pre_close..momentum/vol u32/amount f/in_vol/out_vol u32）；000001 全字段与 std quotes 交叉验证逐值一致，**独有增量 = 内外盘 + activity**；momentum 实测恒 0（期货分时同名字段为 3e-40 垃圾值——float 下溢残留，parser 已丢弃该字段不输出，2026-11-05 补记）；@148 起 68B 尾部未定不出。

### mac_belong_boards / mac_capital_flow / mac_unusual(all_pages) —— 所属板块·资金流·全天异动（2026-10-20 / 11-05）

```python
c.mac_belong_boards("sz.000001")         # 所属板块（000001→13 个：深圳板块/跨境支付CIPS/…/股份制银行）
c.mac_capital_flow("sz.000001")          # 资金流（当日主力/散户流入流出净额 + 5 日六值，2026-11-05 实测复活）
c.mac_unusual(all_pages=True)            # 全天异动聚合（实测 ~7572 条，09:15→15:00 全覆盖）
c.mac_unusual(start=600)                 # 单页模式仍可用
```

- **0x1218 所属板块**：请求 47B（`<H8s16x21s` + "Stock_GLHQ" 报表名）；响应 = 27B 头 + GBK JSON（每行 9 列：类别/market/板块码/板块名/板块指数 close/pre_close + 3 个小整数 flag 语义未定）；**板块码 880/881 段混合**（880218 深圳板块、881388 股份制银行），可直接喂 `mac_members()`（convert_board_code 已覆盖 88 前缀全段）；⚠️ 帧头 flag 0x1C/0x01 实测等价（参考实现用 0x01），落地统一 0x1C；头 @23 u16 = JSON 字节数（000001=815 / 600519=1161 双样本自洽；曾误读 @22 得幻影漂移，复审修正），JSON 边界按 body 实际长度解析
- **0x1237 全天聚合**：异动按时间序（09:15 开盘竞价类在前），`all_pages=True` 以 600/页循环到短页即停（max_pages=50 防失控）；全天实测 7572 条、0 跨页重复；**全天拉取暴露码表外类型** 0x15/0x16/0x1D/0x1E（盘中异动专属，price 常为 0，语义待探）——未知码兜底输出十进制字符串不丢记录
- **0x122B 批量报价（2026-10-21 复活，`mac_quotes()`）**：⚠️ **请求位图 20B——与 0x122C 的 16B 不同**！曾误用 16B 导致请求错位 4B，服务器把请求解成乱码列表并 fallback 默认池，呈现「列表语义不生效」假象（78/80 行垃圾跟随、行尺寸 104/108 漂移全是错位次生症状），一度误判搁置。20B 修正后请求 N 只精确返回 N 行（000001/600000/300750/600519 字段与 std quotes 逐值一致）。响应与 0x122C 完全同构（20B 位图+`<IH`+68B/行+字段流），共享解析器 `_parse_members_response`；`total` 字段回显请求数。bits 支持自定义字段位（0x38 主力净流入语义未验证）。深层教训：**「服务器忽略请求」的表象，先怀疑自己的请求构造错位**——同类命令的位图尺寸也未必相同（0x122B=20B / 0x122C=16B，参考实现各自硬编码）
- **0x122B/0x122C 缺失校验（2026-11-05，`on_missing` 参数）**：`mac_quotes`/`mac_ex_quotes` 每页经 `_validate_batch_quotes` 校验——行数 > 请求数无条件 raise（防外来记录混入，含 ignore）；缺失（请求-返回）与全零占位行（close/pre_close/vol/amount 全 0；pre_close 也 0 才算——盘前/停牌 close=0 但 pre_close 有值不误报）默认 warn 告警，`on_missing="raise"` 显式收紧 / `"ignore"` 关闭告警（硬校验保留）。⚠️ EX 语义"允许少于 N"（服务器可丢无报价成员）——缺失默认只告警不炸

### mac_capital_flow —— 个股资金流（0x1218 + 报表名 Stock_ZJLX，2026-11-05）

```python
c.mac_capital_flow("sz.000001")   # 当日主力/散户四值 + 5 日六值（单行 DataFrame）
```

- **同命令异报表名**：0x1218 请求体 21s 报表名段换 `Stock_ZJLX`（所属板块是 `Stock_GLHQ`）即返回资金流；head_flag 0x1C/0x02 等价（又一例 flag 非硬校验）。响应 = 27B 头（报表名回显）+ GBK JSON **两行数字**：
- **行1（4 值）= 当日** `[主力流入, 主力流出, 散户流入, 散户流出]`（元）→ 列 main_in/main_out/main_net/retail_in/retail_out/retail_net（net=in-out 派生）。量级 sanity 四股票全过（|net| < 当日成交额）；当日主力净额与 std 0x0FFC 同量级旁证（eltdx 实测 sh600900 两通道同为 -1.49 亿）——**std 资金流 0x0FFC 已下线，本命令是 MAC 替代**
- **行2（6 值）= 5 日累计口径**（[0]/[1] ≈ 行1 对应值 ×4.4~7 实锤）→ 列 flow5d_0..flow5d_5 保守命名；[2..5] 精确语义未定性（eltdx 猜 [3]=large_5d/[4]=mid_5d 不可靠，[2]≠[0]-[1]），待对照第三方数据源校准后命名

### mac_ex_quotes / 参数化周期 / 健康分（2026-10-21）

```python
c.mac_ex_quotes(["hk.01398", "hk.00700", "us.AAPL", "hk.HSI"])  # EX 批量报价（≤80/批自动分批）
c.mac_ex_quotes("hk.01398")                          # 单 symbol 也可
c.mac_bars("sh.600000", period="10min")              # 参数化 N 分钟别名（2~240）
c = TdxClient.auto()                                 # 三段业务探测 + 健康分选优
TdxClient._health                                    # 类级健康分字典（跨 auto() 保持）
```

- **0x122B EX 帧（`mac_ex_quotes()`）**：同一 cmd 加 `channel="ex"`（EX 帧首字节 0x01 vs MAC 0x1C，其余逐字节相同）即打通港美股批量报价——实测港股主板（01398 工商银行/00005 汇丰/00700 腾讯）+ 恒指（27, "HSI"）+ 美股（74, "AAPL"）全通，字段（pre_close/open/high/low/close/vol/volume_ratio/amount + GBK 中文名）与 0x122A/0x122B std 侧同构。⚠️ **EX 侧无行对应承诺**：请求 6 只返回 5 行（08001 创业板被服务器丢弃），按实际返回行数为准——与 std 侧「N 请求 N 回」不同。
- **参数化周期勘误（`resolve_mac_period`）**：MAC 侧 7=固定 1min（times 被忽略）、8=参数化 1min——门面此前把 std "1min"(7) 直传 MAC 7，**times 静默丢失**（门面 mac_bars times=1/5/30 返回逐字节相同 1 分钟 bar；API 层直传 8 正常聚合——同参数不同编码位导致）。修复：门面统一经 `resolve_mac_period` 映射 7→8 + "Nmin"（N=2~240，仅接管 PERIOD_MAP 外的新周期，"5min" 等标准名保持原映射零行为变更）+ 冲突/非法值 raise。在线验证：固定 5min（MAC 0）与 times=5（MAC 8）数据逐值一致；EX 侧同构（mac_ex_bars "10min" 港股 15:40-16:00 对齐）。
- **三段业务探测（`_business_ping`）**：测速从纯 TCP 升级为三段判定——TCP 连接 → 协议握手（connect 内置 setup）→ 轻量业务命令（std=证券数量 / ex=品种数量 / mac=老三条 SETUP）；「能建 TCP 但返回空/垃圾」的假活服务器不再被误选。detail 模式返回 (latency, stage) 供分类断言（tcp/connect/probe/ok；TCP 与握手同在 connect 调用内，外部不可分故合并）。
- **健康分（`tdxpy/health.py`）**：纯函数状态机（零依赖可离线单测）——成功 +0.2 上限 1.0、失败 ×0.5 下限 0.05、连续失败 ≥3 进入 120s 冷却、EWMA α=0.3 平滑延迟；选路键 effective_latency=EWMA/score；`_pick` 接入（冷却节点让位健康节点，全池冷却兜底选延迟最优者）、`_ensure_mac/mac_ex` connect 结果入账；类级 `_health` 字典跨 auto() 调用保持。pytdxdata 经验参数吸收，测试 6+4 例全离线（在线 1 例真实池自动 skip）。

### EX 池 setup 双握手（2026-10-20 扩容，交叉复测修正归因）

EX 服务器池（`TdxClient.EX_SERVERS`）5 台：3 台 login 模式 + 2 台 setup 模式（92B 帧，连接时按 `exhq.EX_SETUP_SERVERS` 清单自动选握手，无需配置）：
- `116.205.143.214`（通达信扩展市场，52 市场）/ `103.221.142.82`（国泰君安，60 市场）——市场清单更广（116 机独有「更多其它指数 105」，国泰君安有英国股票 75 / 香港债券 22 等挂牌）；
- ⚠️ **交叉复测修正**：经典 EX 0x23xx 命令（含外盘期货 COMEX/NYMEX/CBOT、德美新英股票、期权 7/8/9 行情）在 **5 台服务器上数据面一致**（逐值实测）——「外盘空板」只是 MAC 帧族（0x122D/E/F）在 login 机上的表现，并非 setup 服务器独有数据；握手也非硬互斥（setup 机接受 login 帧、login 机忽略 setup 帧但会话可用），清单用于固化设计语义。

```python
c.bars("comex.EHR00W", count=3)     # COMEX 日线（经典 EX 0x23xx，5 台服务器全可用）
c.bars("de.ADS", count=3)           # 德国股票（Adidas，日线 ~3500 根）
c.bars("uk.020Y", count=3)          # 英国股票（1.1 万只）
```

⚠️ setup 会话不吃 MAC 帧族（0x122E 无响应实测）——故这两台归 EX 池而非 MAC EX 池。外盘期货逐笔带 nature（空换/多换）与 zengcang 字段；日线深度多在千根级（翻页伪影待细查，个别品种首尾日期错乱）。

### quotes —— 实时快照（支持混合列表）

```python
c.quotes(["sh.600519", "sz.000001", "us.AAPL"])   # std 批量(≤80/次) + ex 逐只，列并集合并
```

- std 列：`market/code/price/last_close/open/high/low/vol/cur_vol/amount/s_vol/b_vol/bid1-5/ask1-5/bid_vol1-5/servertime/...`
- ex 列：公共列对齐（`pre_close→last_close`、`zongliang→vol`、`xianliang→cur_vol`）+ EX 特有（`kaicang/chicang/neipan/waipan/position`）。⚠️ EX **五档列齐全**（`bid1-5/ask1-5/bid_vol1-5/ask_vol1-5`），但实测多数扩展市场只填 `bid1/ask1`，其余档位为 0
- 缺失列填 NaN；全部无记录返回空 DataFrame

### xdxr / finance —— 仅标准市场

```python
c.xdxr("sh.601919")     # 除权除息：year/month/day/category/name/fenhong/peigujia/songzhuangu/peigu/...
c.finance("sh.601919")  # 财务摘要：liutongguben/zongguben/jinglirun/meigujingzichan/... （拼音列，~38 列）
```

⚠️ **单位（2026-10-19 修正）**：20 个金额字段（zongzichan/jingzichan/zhuyingshouru/jinglirun 等）= **千元**（×1000 → 元；旧版 ×10000 放大 10 倍，恒等式+披露量级双路裁决）；股本字段（liutongguben/zongguben 等）raw 单位=**万股**，parser 已 ×10000 换算为**股**（输出值直接是股）；meigujingzichan = 元/股。银行股（金融类）布局特殊：负债字段返回 0、meigujingzichan 错位。

**空壳判定（2026-11-05）**：退市/无效票服务器返回全零空壳行（code 全 `\x00`、ipo_date=0）——响应行附 `is_shell`（bool）；`c.finance(sym, drop_shell=True)` 过滤空壳并告警（默认 False 保持原始行）。

```python
c.finance_all(["sz.000001", "sh.600519", ...], workers=6)   # 并发批量财务（多连接分片）
c.finance_all(symbols, workers=6, drop_shell=True)          # 空壳过滤透传
c.audit_price_scale(markets=(0, 1))                         # 系数表×协议 decimal_point 交叉审计
```

- **finance_all**：每线程独立 TdxClient（自动选优建连，类级 `_health` 分选缓存共享），分片内串行复用连接；单只失败不炸线程（失败行保留 symbol + error 列）；workers 建议 5~10（服务器 ~4s/请求限速是服务端策略）。
- **audit_price_scale**：security_list 全量逐 code 对照 `10**-decimal_point` 与 SECURITY_COEFFICIENT 价格系数，不一致 → warning + 返回分歧行报告（market/code/name/decimal_point/coef_price/expect；空表 = 零分歧）；未登记代码段（系数表盲区）跳过不计分歧。独立按需调用，不污染解析热路径。

遇 EX 品种 raise（除权/财务数据不在 7727 行情服务器上，协议级无此数据）。

### security_count

```python
c.security_count(market=1)   # int 或 "sh"/"sz"/"bj"；返回该市场品种总数（StdMarket：sz=0 / sh=1 / bj=2）
```

### f10_* —— F10 公司资料（仅 sh/sz）

```python
c.f10_categories("sh.600519")             # 分类列表：name/filename/start/length/size_kb
c.f10_content("sh.600519", "公司概况")     # 单个分类正文（GBK 解码后的 str）
c.f10_all("sh.600519")                    # 全部分类 dict{name: 正文}，单类失败告警跳过
```

走标准连接（0x9B0F 取分类 + 0x9C07 取正文）。仅 `sh`/`sz`，传 EX symbol 报错。分类列表按实例缓存，TTL 由构造参数 `f10_category_ttl` 控制（默认 3600 秒）。正文是服务器原文，不做结构化解析。

## 市场枚举与查询

```python
from tdxpy import ExMarket, StdMarket, ex_market_table

ExMarket.US_STOCK        # 74（IntEnum，兼容 int）
ExMarket("us")           # 74——枚举直接吃字符串别名
list(ExMarket)           # 54 个市场成员，一眼看全
ex_market_table()        # 离线 DataFrame：market/category/name/short_name/alias/enum_member
```

| 方法 | 说明 |
|---|---|
| `ex_markets()` | 服务器实时市场清单（51~60 市场随服务器） |
| `ex_instruments(market, limit=600)` | 指定市场合约列表（market 收 int/ExMarket/字符串别名；内部二分定位自动翻页） |

### 宏观指标市场 38（ExMarket.MACRO，2026-11-05 实测）

```python
c.ex_instruments(38, limit=250)             # 217 个指标（9 类），code 格式 "{类别}_{指标}"
c.mac_ex_bars("38.1_GDP", period="day")     # symbol 数字市场段；门面链路实测全通
c._ensure_mac_ex().get_ex_bars(38, "2_CPI", category=6, start=0, count=5)  # API 层直调（可指定 category）
```

- **217 指标 9 类**（类别前缀 = 代码 `_` 前段；分项为 2026-11-05 在线实测快照）：1 国民经济×3 / 2 物价×5 / 3 景气×12 / 4 外贸×8 / 5 大宗与债券×53 / 6×12 / 7×18 / 8×30 / 9 利率与回购×76（R001/R007/MLF/SLF/中债收益率曲线）。
- **数值型 K 线**（OHLC 同值、vol/amount 恒 0）：指标值与国家统计局公开值核对一致（GDP 2024=134.8 万亿/2025=140.2 万亿✓、CPI 同比 100.5~101.2✓、PMI 49.2~50.3✓、R001 1.32~1.39%✓）。
- **⭐ category 只影响切片粒度，不改变指标自身频率**（2026-11-05 实测）：GDP（年度指标）category=11(year)→逐年度序列、=4(day)→**同样返回全年度序列**（datetime 逐年 12-31）、=6(month)→只回最新 1 根；`period="year"` 不在门面 PERIOD_MAP（合法值 day/month/week/…）会 raise——宏观取数用 `period="day"` 起步即可拿全序列。
- **服务面**：7727 login 机（MAC EX 池）服务市场 38；7709 MAC 通道不服务（eltdx 文档一致）。

## Affair 财务包（官方 HTTP 站下载 + 解析）

```python
from tdxpy import Affair

Affair.files()                                   # 远端清单（filename/hash/filesize）
Affair.fetch("gpcw20231231.zip", downdir="tmp")  # 下载（md5 校验，已存在跳过）
df = Affair.parse("gpcw20231231.zip", "tmp")     # DataFrame（index=code，581 中文列）
```

⚠️ 最新季包（如 `gpcw20260630.zip`）是服务器端 164B 空壳占位，取数用上一季。

## vipdoc 离线读写（reader/writer，零联网零客户端依赖）

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

vipdoc 路径速记：`sh|sz|bj\lday` A股日线、`sh|sz|bj\minline|fzline` 股票分钟、`ds\lday|fzline|minline` 扩展市场（期货文件名 `市场编号#代码`：28=郑商 29=大商 30=上期+INE 47=中金 66=广期；连续合约 `L7` 次连/`L8` 主连/`L9` 加权）。

## 错误码与异常

三个异常类都带 `.code`（分段错误码）与 `.server`（出错服务器 `(host, port)`，参数类通常 None）：

| 异常 | 默认 code | 语义 |
|---|---|---|
| `ValidationException` | `1000` CODE_GENERIC | 参数/写法错误（裸代码、非法周期、EX 传 adjust、日期区间给 std…） |
| `TdxConnectionError` | `2000` CODE_CONNECTION | 连接失败/超时/断线；池或选优全灭时为 `2050` CODE_POOL_EXHAUSTED |
| `TdxFunctionCallError` | `2100` CODE_PROTOCOL | 命令执行失败/被拒（如 K 线页大小超硬上限）；`.original_exception` 保留底层异常 |

```python
from tdxpy.exceptions import TdxFunctionCallError

try:
    df = c.bars("sh.601919", count=5000)
except TdxFunctionCallError as e:
    print(e.code, e.server, e.original_exception)   # 2100 / ("…", 7709) / 底层异常
```

> 分段码对齐 tdxrs 四段式（1000 通用 / 2000 连接 / 3000 解析 / 4000 文件）。tdxpy 目前只用到前两段：限流是「等待」而非抛错、解析失败返回空表，故 3000/4000 段暂无抛出点。

## 错误消息对照（报错即教学）

| 触发写法 | 报错要点 → 正确写法 |
|---|---|
| `c.bars("601919")` | 裸代码已不支持 → `"sh.601919"` |
| `c.bars("sh601919")` | 旧式无点前缀已移除 → `"sh.601919"` |
| `c.bars("xx.123")` | 无法识别的市场 → `ex_market_table()` 查别名 |
| `c.bars("us.AAPL", adjust="qfq")` | EX 不支持服务端复权 → `adjust=None` |
| `c.xdxr("us.AAPL")` / `c.finance("us.AAPL")` | 仅标准市场存在（EX 协议无此数据） |
| `c.bars_range("sh.601919", ...)` | 日期区间查询仅 EX 支持 → bars 的 start/count |
| `c.bars("hk.1234")` | 港股代码须 5 位数字 |

## 已知边界与服务器现状

| 项 | 现状 |
|---|---|
| `main`（60 主力连续）市场 | 服务器数据面当前为空（2026-09-17 三台实测，路由正常），用 `qs.CUL8` 等具体市场替代 |
| EX 分笔深度 | 仅数月级；更早用 `bars_range` 1 分钟线 |
| 当日分时（minute） | 非交易时段返回空 |
| 历史深度 | 日线 ~6400 根（1999-11 起）/ 5min ~23800 根（2024-10 起）/ 1min ~22700 根（2026-06 起）/ tick 2000-06 起；更早的分钟级用 `minute_history` 按日分时/分笔合成 |
| SH 深分页 | 转债等深 offset 段严重限速，定位代码段勿扫 SH 深列表 |
| 复权 | 服务端复权（adjust）仅标准市场；EX 品种无复权 |
| std quotes 死代码占位 | 查询含已退市/不存在代码时，服务器对无效行返回**默认池第一只**（实测 600839 四川长虹，价格全 0）——识别方式：请求 code 与返回 code 不符（2026-11-05 实测；MAC 通道 _validate_batch_quotes 会告警，std quotes 需自行核对 code 列） |
| 分页硬上限 | std K 线 **800**（超限**整请求失败**，不是截断）/ std 分笔 2000 / std 列表 1000 / 五档 80 代码 / EX K 线 700 / EX 分笔 1800 / MAC K 线 700 / MAC 逐笔 1000。`all_pages=True` 自动钳制 + 欠填降档；单发模式原样透传，超限后果按上表分「报错」与「静默截断」两类 |
| 当日分笔 0x0FC5 | 现代服务器已废弃（恒返回 0 笔）——当日 tick 用 `ticks(symbol, date=今天)`（走历史分笔接口）或 `mac_ticks(date=None)` |
| tick 双通道关系 | std `ticks` 与 `mac_ticks` **同源**：同一交易日 4094 笔、245 个分钟桶的笔数/量/额/买卖方向零差异；差异仅**时间精度**（std `HH:MM` vs MAC `HH:MM:SS`）与 MAC 独有 `trade_count`（3 秒快照内成交笔数）。MAC 价格是浮点原值，std 是两位小数 |

## 代码段登记现状（get_security_type / SECURITY_COEFFICIENT，2026-11-05 刷新）

| 段 | 类型 | 价格系数 | 依据 |
|---|---|---|---|
| SH 110/113/118（"11"前缀） | SH_BOND | 0.0001 | **转债系数仲裁定案**：std/MAC 逐值相等 + amount 恒等式 VWAP 落当日区间（4 股票全过）——eltdx ×1000 口径与 tdxpy raw 定义不同，tdxpy 现行正确 |
| SH 02 贴现国债 / 15-19 / 23 / 24 / 27 | SH_BOND | 0.0001 | 2026-11-05 登记：全量 security_list dp=4 + MAC K 线恒等式 amount/(vol×close)=10（手）三段实测 |
| SH 36 | SH_PREFERRED | 0.01 | 优先股（农行优1）；dp=2，段内近 20 日无成交，vol 沿沪股同构外推 |
| SH 78 | SH_A_STOCK | 0.01 | 科创板新股申购段（787827 钶锐申购） |
| SH 79"资金控制" | **不登记** | — | 交易所内部占用段，保持 raise 防御 |
| SZ 19 / 50 / 52 / 56 | SZ_BOND | 0.0001 | 2026-11-05 登记：地方债/ABS/公司债，dp=4 + 恒等式 K=10 两段实测 |
| SZ 14 | SZ_PREFERRED | 0.01 | **×100 偏差修正**（audit_price_scale 首秀实锤）：曾误吞 SZ_BOND，宁行优01 last_close 显示 1.0296 实为 102.96 元；K 线恒等式 amount=vol×close（vol 单位股） |

## 底层模块与测试

| 模块 | 用途 |
|---|---|
| `tdxpy.hq.TdxHq_API` | 标准市场协议层（pytdx 风格 API），门面底层；一般用门面即可 |
| `tdxpy.exhq.TdxExHq_API` | EX 协议层 |
| `tdxpy.pool.ServerPool` | 服务器池调度器（轮转 / 健康分跳过 / 时段自适应限流） |
| `tdxpy.health` | 健康分状态机（纯函数，零依赖可离线单测） |
| `tdxpy.exceptions` | 三个异常类 + 分段错误码常量 |

测试套件（联网用例自动 skip；直接 `python tests/xxx.py` 也可）：

```bash
python -m pytest tests -q                     # 全量：24 个文件 / 423 个用例
python tests/test_routing_and_coefficient.py  # 离线：解析/系数/枚举
python tests/test_facade_smoke.py             # 门面在线冒烟
python tests/test_pagination.py               # 离线：自适应翻页（528 组合矩阵）
python tests/test_anchor_matrix.py            # 在线：95 格历史锚点矩阵
```

- 测试服务器可用环境变量覆盖：`TDXPY_TEST_HQ_HOST` / `TDXPY_TEST_EX_HOST`
- **历史锚点基线** `tests/fixtures/anchor_matrix.json`：95 格「市场×品种×日期×值」，覆盖 1999–2026（含 2016 熔断、2020 疫情首日）；任一格变红即服务器历史数据变更 → `tools/gen_anchor_matrix.py` 重新采集核对
- **服务器探测工具**：`tools/server_census.py`（存活普查）/ `server_survey.py`（协议·品种·深度） / `survey_report.py`（生成报告）

