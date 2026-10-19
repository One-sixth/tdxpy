# tdxpy 本地版记忆

## 定位

pytdx 的延续/修复版，editable 安装到 normal 环境，供量化项目读取通达信数据（在线行情 + 离线 vipdoc 读写 + 财务文件解析）。API 用法见 `QUICK_REFERENCE.md`，本文只记架构决策与最新经验。

## 当前形态

- **协议层**：现代服务器 new 握手（0x000D）+ 42B K 线 payload；legacy 老三条会话保留作回退（`TDXPY_HANDSHAKE_MODE=legacy`）——业务命令可用，但 **K 线 0x052D 返回空**（静默失败，别据此判断服务器死）。老 26B payload + 老 SETUP 被现代服务器全面拒收。K 线支持服务端复权 adjust=0/1/2。
- **四通道**：std(7709) / EX(7727) / MAC / MAC EX，各自独立连接与服务器池，全部懒加载。
  - **std**：`TdxHq_API`，个股/基金/转债/指数/北交所。
  - **EX**：`TdxExHq_API` + login(0x2454, 80B 冻结载荷)；19 台 login + 2 台 setup92（92B 帧，`EX_SETUP_SERVERS` 清单自动选，其中 103.221.142.82 独有 60 市场清单，含英股 75 / 港债 22）。**不 login 第一条命令即被踢**。
  - **MAC**：老三条 SETUP 会话 + 10B MAC 帧（flag 0x1C）+ 专用 3 台池。**只活在「老 SETUP 会话 × MAC 服务器」组合**——标准池服务器对 MAC 帧无响应，新握手会话下超时。
  - **MAC EX**：7727 + login + 帧首 0x1C→0x01；0x122D/E/F 载荷与 A 股 MAC 完全同构（parser 三子类只重写帧构造）。
- **命令矩阵**：std K 线/分笔/分时/五档/quotes/xdxr/finance/F10；EX 0x23FF K 线（28B 记录，权益类 31/48/49/44/74 第 5 字段=float 成交额，期货=持仓量）+ 0x0B24/0x0C24/0x0D24 分时与日期区间（跨日 day_offset）+ 批量报价 0x122B（EX 帧）+ quote_list；MAC 0x122E K 线（≤700/页，含 amount）/ 0x122D 分时 / 0x122F 逐笔（秒级 + trade_count）/ 0x1231 板块 / 0x122C 成分 / 0x123D 竞价 / 0x1237 异动 / 0x122A 快照 / 0x1218 所属板块与资金流 / 0x122B 批量报价。
- **MAC 周期编码**：0–6 与 std 同值；**7=固定 1min（times 被服务器忽略）、8=参数化 1min（times=2~240 实测全通）、9=day、10=季、11=年**。门面 `resolve_mac_period` 把 std "1min" 映射到 8（直传 7 会静默丢 times），未知周期/冲突 raise。分时点数随市场：A 股 240 / 港股 330 / 期货 345。
- **门面 `TdxClient`**：唯一推荐入口；四连接懒加载 + 断线自动重建；symbol 点分式 `{市场别名}.{代码}`（sh.601919 / us.AAPL / hk.01398 / qd.AL8 / 上期所.cu2510），旧语法教学式 raise；返回统一 DataFrame。公开方法 35 个，用法见 `QUICK_REFERENCE.md`。
- **服务器池 `ServerPool`**（`pool.py`）：多连接并发批量下载用；轮转 + 健康分跳过 + 时段自适应限流（基准 15 / 分时 10 req/s，交易时段 ×1、盘前盘后 ×2·1.5、周末 ×4·3，封顶 200）。连接对象是轻量 `TdxHq_API` 实例而非 N 个门面；锁粒度=池锁只包「选服务器 + 轮转指针 + 取引用」三步，限流 `wait` 在 per-server 锁内。
- **F10**：0x9B0F 取分类 + 0x9C07 取正文（GBK 拼包；`filename_raw` 必须原样回传，禁 str 往返）；门面 3 方法，分类列表按实例 TTL 1h 缓存；仅 sh/sz。
- **错误码**：`exceptions.py` 三段式——1000 通用/参数、2000 连接（池或选优全灭 2050）、2100 协议；异常带 `.code` / `.server`。
- **vipdoc 离线**：reader（日线/分钟线，股票/期货双模式）+ writer 对称写回；未登记代码段 raise 而非猜系数。
- **并发安全**：`BaseSocketClient._cmd()` 工厂统一注入锁、RLock 可重入、retry 下沉 call_api、心跳 daemon 化；管道超时须绕锁强关底层 socket，不可走 `close()`（内部锁被卡死任务持有会死锁）。
- **惰性导出**：`import tdxpy` 不加载 pandas（PEP 562）。
- **测试基线**：24 个 `test_*.py` / 423 用例；联网用例自动 skip；在线锚点全部动态化，历史数据由 95 格固定锚点矩阵守护。

## 最新经验（实测结论）

### 服务器行为
- 老 26B payload 布局的 K 线请求被现代服务器拒收（只能走 42B payload）；老式 SETUP 握手在部分服务器仍可用（MAC 通道依赖它），非"全面失效"
- 行情协议下载通道对 tdxfin/.zip 财务包已死（全 0 字节），财务只能走 HTTP 站
- **在线 K 线深度**：日线到上市日、5min 约 2 年、1min 约 5 个月（精确根数与起始日见下方「服务器全景」）；历史分笔标准市场 2006 年起、MAC 逐笔 2000 年起、EX 仅数月级（更早的分钟级走 `minute_history` 按日合成）
- 历史分时（0x0FB4）可拉任意历史日 240 点；当日分时同接口，交易时段也可能返回空（服务器行为，时间敏感用例须守卫）
- SH 证券列表深分页被限速（每页 10s+ 直至超时），定位转债别扫深列表；BJ 列表命令直接断连（挡板警告是正确防御）
- EX 主力连续（市场 60）数据面为空；加权合约（L9）可用；商品期权 EX 通道无数据（换 tq-local/QMT/vipdoc 源）
- 最新季度财务包可能是服务器端空壳占位（md5 通过但 parse 报错），取数用上一季
- **旧资金流通道已下线（2026-10-19 实测）**：category=22 复用 0x052D 在两种会话下均返回空，xmtdx 扫遍 6 台可达主机同样 0 行。**替代 = `mac_capital_flow`（0x1218 + 报表名 Stock_ZJLX）**：当日四值 + 5 日六值（[2..5] 语义未定）
- **STD_SERVERS 43 台**（业务探针双验证通过，`security_count` 全员一致）；TCP ping 不代表业务可用——pytdxwrapper 有「握手正常 K 线空」反例，扩池依据以业务探针为准
- **42B payload vol/amount 量级验证通过**：000001 日线 vol 1.27 亿股、vol×close/amount=1.007 自洽——a-share 26B f32 的 vol 结论不适用于 42B 布局

### 数据通道与分页（2026-11-09 实测）

- **两条 tick 通道同源、差异只在时间精度**：std `get_history_transaction_data`（0x0FB5，`HH:MM` 分钟精度）与 MAC `get_mac_ticks`（0x122F，`HH:MM:SS` 秒精度）对 600000@2026-11-06 均返回 4094 笔 / 总 vol 642750，245 个分钟桶的笔数·量·金额·买卖方向量**零差异**；深度（2000-06 起）与广度（个股/指数/ETF/基金/北交所/活跃转债）完全一致。**MAC 独有 `trade_count`**（该 3 秒快照内成交笔数，当日合计 56437 ≈ 真逐笔数）与 `bs_flag`（≡`buyorsell`）；MAC 价格是浮点原值（需 round），std 是 ÷100 两位小数。**`get_transaction_data`（0x0FC5 当日分笔）现代服务器已废弃（恒 0 笔）**——当日 tick 走历史分笔接口传今天，或 MAC `date=0`。
- **各命令分页硬上限（服务器契约，超限行为分两类）**：std K 线 **800**（超限 → `_hq_call` 抛 `TdxFunctionCallError`）、std 分笔 **2000**（超限静默截断）、std 列表 1000、五档 80 代码、EX 分笔 1800、EX K 线 700、MAC 逐笔 1000、MAC K 线 700（均静默截断）；count 字段 u16，>65535 客户端打包即错。
- **自适应翻页 `_paginate_adaptive`**：两层防护——①静态钳制（按上面硬上限取 min）②运行时自适应（**每次**「返回数 < 有效页」都把页大小降到实际返回数继续，由空页判定结束，可吸收任意次欠填）+ `max_requests=20000` 熔断。已接入 `bars(all_pages)` / `delisted_bars` / `ticks(std)` / `ticks(ex)`；钳制统一由本函数 `hard_limit` 负责（调用方不再重复 min）。**复审修正两处**：原「一次性 probed 标志」在连续两次短页时丢数（6000 条丢 1500）→ 改为每次降档；原第 3 层「None 降档重试」是死代码（`eff ≤ hard_limit` 恒成立，且 std K 线超限走抛错路径）→ 删除。验证：528 组合矩阵零失败 + 源码级负向验证（回退 probed → 3 测试红）+ 在线 9 品种哈希一致 + facade `page_size=5000` 仍拿全 6423 行/4094 笔；`tests/test_pagination.py` 18 项。**注意**：旧门面本就对 page_size 做了 min 钳制，本次修的是「潜在缺陷」（绕过钳制即触发）+ 分笔页大小 800→2000（请求数减半）。

### 单位与系数

- 系数表按代码段登记（helper.py / reader 同步维护）：A 股 0.01、ETF 0.001、转债 0.0001、**BJ 固收（810 定转/82 优先股）0.0001**——缺登记 raise，不许静默回退
- **finance 金额字段单位 = 千元（2026-10-19 裁决修正 ×10000→×1000）**：恒等式「股本raw(万股)×每股净资产 ÷ 净资产raw = 10.0」（600519/000002 双股一致）+ 披露量级对照双路裁决，pytdx 系沿用万元假设 20 字段全放大 10 倍；股本字段 = 万股（×10000）不动；银行股 finance 布局特殊（负债字段 0、meigujingzichan 错位）——下游已用旧口径的需自行 ×10 修正历史数据
- tick vol=手、分钟/日线 bar vol=股（合成需 ×100）；指数分钟 bar vol 是成分股口径≠tick（不可跨接口比）；指数 tick vol 是成分股总手数（落盘 dtype 用 int64）
- 服务器 bar 标签=结束时刻制；无成交分钟用前收盘价填充 vol=0；分笔是 3 秒快照口径，同分钟内笔序依赖服务器返回顺序
- bj 81/82 段定性修正（2026-10-19 官方规程，北证公告〔2024〕20号）：**810=定向可转债（BJ_BOND）、820/82=优先股（BJ_PREFERRED）**，价格 raw×10000，helper/reader 已按此登记；80/811+ 为政府债券不登记（raise 防御）；此前"81/82 转债"的叫法有误

### 实用捷径

- 交易日历：`bars(symbol, period="day", all_pages=True)` 的日期列表即该标的全部交易日
- QMT 代码转换：`510050.SH` → `rsplit('.', 1)` 拆后 swap+lower → `sh.510050`
- 指数 tick 无数据日返回无列空 DataFrame，下游按列访问前必判 `len(df)`

### 服务器全景（2026-11-09 全量实测）

- **规模**：245 个 endpoint（tdxpy 四池 + TDX研究 五库清单去重），存活 121 台：std 101 / MAC 45 / EX 19 / MAC EX 3；124 台为已下线旧 IP。
- **协议**：std K 线仅在 new(0x000D) 与 skip3(两条) 会话可读，legacy 老三条业务可用但 K 线 0x052D 返回空（静默失败）；服务端复权 101/101 台可用；EX login(0x2454) 19 台全通，setup92 仅 2 台（其中 103.221.142.82 独有 60 市场清单，含英股 75 / 港债 22）。
- **品种**：std 深 24628 / 沪 27845（或 27668）/ 京 393，9 类代表品种全通；北交所仅 K 线 + finance 可用（quotes 空、security_list 断连）。EX 51–60 市场覆盖港美英德新、国内五所期货、外盘商品、外汇、国际指数、宏观。
- **深度（600000 样本，101 台高度一致）**：日线 6423 根至 1999-11-10；5 分钟 ~23800 根（2024-10 起）；1 分钟 ~22770 根（2026-06 起）；tick/分时 2000 年起有数据、2001 起全覆盖（9 台更浅：2003/2007/2018/2020 起）；xdxr 1999-11 起、F10 16 类。EX：港股 00700 起 2004-06、美股 AAPL 起 1980-12-12、上期 RBL8 起 2013-08。
- **方法坑**：早期 tick 数据稀疏有空洞（非周末），按「有/无数据」二分会得错误边界，须用年度密度扫描；样例代码格式错会误判板块无数据（英股用 AZN、港债用五位如 04286）。
- **EX 期货 quote_list bug 已修（2026-11-09）**：`extract_futures` 末尾误写 `return datalist`（应为 `return pos`），第二块起 `body_buf[pos:pos+10]` 抛 `can only concatenate list (not int)`，期货 category=3 列表全失败；修后 28/30/47 市场实测正常（2B 头 + N×300B）。**同批加块长校验**（`len(body_buf) < 2 + num*300` → ValueError）与「解析异常不再静默吞掉」——三路复审实测：末块截断 150B 原本会静默返回 5 条错位数据、num 虚高会读出空 code 垃圾条目。测试 12 项（含 4 项畸形 body），fixture 为真实响应存档。
- 产物与脚本分离：报告 / 逐台 JSON / 清单 / 日志全部落 `untrack/`（整目录 gitignore，不追踪）；流程脚本 `tools/server_census.py` → `tools/server_survey.py --workers 16` → `tools/survey_report.py` 长期留存、后续升级在此处改（脚本内输出路径已锁定 untrack/）。

## 已知边界 / 未做

- **std K 线参数化周期**（"10m"/"5s"）未做（协议支持 8=N 分钟 / 9=N 日 / 13=N 秒）；MAC 侧已支持（`times` 与 "Nmin" 别名）
- **0x051D 当日分时**：parser 已落地，头部除 close/vol/amount 外的字段语义未定（破解细节见知识库《数据获取经验.md》）
- **MAC EX 期货逐笔**：`bs_flag` / `trade_count` 语义未定性（勿套 A 股方向语义）；港股逐笔无数据（分时可用）
- **外盘期货日线**：深度千根级，个别品种首尾日期错乱（翻页伪影待细查）
- **MAC 未定性项**：0x1237 请求尾部 5 参数（1,200,30,40,50,200）语义、码表外类型 0x15/0x16/0x1D/0x1E；0x1231 `board_type` 除 0/1 未探、`rise_speed` 恒 0；0x1218 响应 3 个小 flag；`mac_capital_flow` 行2 [2..5] 语义（保守输出 `flow5d_*`）
- **错误码 3000/4000 段无抛出点**（限流是等待、解析失败返回空表），待行为变化再分配
- **cp315t 研究项已关闭**：基准两环境持平（IO 主导，GIL 非瓶颈）、压测 48917 请求零撕裂——free-threaded 无收益也无劣化，不进推荐

## 约定

- **文档不写序号（2026-11-09 确立）**：快查/说明类文档的章节与条目**一律不编号**——不用 `## 1.`、不用「批次五」「MEDIUM①」「F1.3」「第三连接」，按标题检索；历史批次编号一律换成日期。目的是**新增小节不做全篇重编号**，避免为改一个序号产生大量工具调用。`QUICK_REFERENCE.md` 已按此重构（524 行），并经三路无上下文复审修正 10 处。
- 修改不自动 commit，由作者决定提交时机；API 变更同步 `QUICK_REFERENCE.md`
- README 不修改；文档不写修改历史与日期
- 测试：全量 `python -m pytest tests -q`（24 文件 / 423 用例，联网用例自动 skip）。离线重点 `routing_and_coefficient` / `pagination`（528 组合矩阵）/ `ex_quote_list` / `minute_time_data`；在线重点 `facade_smoke` / `server_guard`（动态锚点）/ `anchor_matrix`（95 格）。
- **历史锚点矩阵（2026-11-09 新增）**：`tests/test_anchor_matrix.py` + `tests/fixtures/anchor_matrix.json`（95 格：std_bar 48 / std_tick 7 / std_minute 6 / std_xdxr 4 / std_fin 6 / mac_bar 6 / ex_bar 16 / ex_range 2）。基线由 `tools/gen_anchor_matrix.py` 采集（**双服务器交叉验证**，只写两台一致的值；tick 笔数容差 ±max(3, 0.2%)——实测 000001@2021-01-04 两台差 1 笔；一律 adjust=0 不复权），覆盖 1999→2026（含 2005-998 点、2007-10-16 顶、2008-10-28 底、2015-06、2016-01-04 熔断、2020-02-03 疫情、2026 近期）。任何一格红 = 服务器历史数据被重灌/缺失/格式变更，或解析回归；处置顺序：换台复测 → 确认后重采基线 → 解析回归则修 parser。扩充只需在 `gen_anchor_matrix.py` 的清单常量里加一行重采（`TDXPY_SKIP_ANCHOR_MATRIX=1` 可整体跳过）。选日期前须确认当日有 bar（600000@2015-06-12 实为停牌，6-05 直跳 6-17；RSL8 主力在 2020-01-06 换月断档）。采集端与测试端翻页窗口必须一致（MAC 已统一 4×700；不一致只会在「白采集」方向出错）。`std_tick` 的 `first_time/last_time` 实为**返回序列首/末笔**，服务器分笔非时间序（实测 6 条 last<first），勿读作当日最早/最晚。
- 0x051D parser 单测的 fixture 是 2026-10-19 真实服务器响应存档（tests/fixtures/minute_*.bin，4 新格式 + 1 legacy）：换锚点须先在线重抓核实，勿凭记忆改数
- delisted/mac/mac_ex 夹具同为 2026-10-19 真实响应存档（delisted_052b_sz000038.bin = eltdx golden 剥 16B 帧；mac_122e/122d/122f_sh600000.bin = A 股 MAC；mac_ex_122e_usAAPL / mac_ex_122d_hk01398 / mac_ex_122f_hkHSI.bin = EX 通道美股/港股/恒指）；0x052B 差分累积与 eltdx 期望值逐字节对齐，换值须在线重抓核实
- 退市股 0x052B 年份校验下限 1990（实测 000038 有 1994 年上市数据、start=800 处出现 1998-12-28——下限设 2000 会误杀在线翻页）
- MAC 通道归一化测试写法：`code.rstrip(b"\x00")` 后再断言（22s 字段零填充）