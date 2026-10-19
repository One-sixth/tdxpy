# tdxpy 本地版记忆

## 定位

pytdx 的延续/修复版，editable 安装到 normal 环境，供量化项目读取通达信数据（在线行情 + 离线 vipdoc 读写 + 财务文件解析）。API 用法见 `QUICK_REFERENCE.md`，本文只记架构决策与最新经验。

## 当前形态

- **协议层**：现代服务器新式握手（0x000D）+ 42 字节 K 线 payload + EX 新版 login（0x2454）；老式握手保留作 legacy 回退（环境变量 `TDXPY_HANDSHAKE_MODE`）；K 线支持服务端复权 adjust=0/1/2；EX K 线 28 字节记录无成交额字段（0.0 占位）
- **门面 `TdxClient`**：唯一推荐入口。`auto()` 并发测速选优（仅主线程调用）或显式 host/port；std/ex 双连接懒加载、断线自动重建；symbol 点分式 `{市场别名}.{代码}`（sh.601919 / us.AAPL / hk.01398 / qd.AL8 / 上期所.cu2510），market 参数已全删，旧语法教学式 raise；返回统一 DataFrame
- **方法收敛**：bars（万能路由，指数代码自动转指数命令）/ bars_range / minute（0x051D 直连，2026-10-19 新格式 parser）/ minute_history / ticks / quotes（std+ex 混合）/ xdxr / finance / security_count / ex_instruments；`Affair` 财务文件走官方 HTTP 站 `data.tdx.com.cn/tdxfin/` 下载解析（581 中文列）
- **vipdoc 离线**：reader（日线/分钟线，股票/期货双模式）+ writer（对称写回）；reader 市场段登记制——未登记代码段 raise 而非猜系数
- **并发安全**：`BaseSocketClient._cmd()` 工厂统一注入锁（结构性防漏）、RLock 可重入、retry 下沉 call_api 原子化、心跳线程 daemon 化（进程可正常退出）；管道侧超时包装须绕锁强关底层 socket，不可走 `client.close()`（内部锁被卡死任务持有会死锁）
- **惰性导出**：`import tdxpy` 不加载 pandas（PEP 562）

## 最新经验（实测结论）

### 服务器行为
- 老协议（26 字节 payload + 老式 SETUP）被现代服务器全面拒收；EX 不 login 第一条命令即被踢
- 行情协议下载通道对 tdxfin/.zip 财务包已死（全 0 字节），财务只能走 HTTP 站
- 在线 K 线深度：1min 约数月、5min 约两年、日线到上市日（start u16 上限 65535，实际深度受服务器限制）；历史分笔（0x0FC6）标准市场可达 2006 年末，EX 仅保留数月级
- 历史分时（0x0FB4）可拉任意历史日 240 点；当日分时同接口，交易时段也可能返回空（服务器行为，时间敏感用例须守卫）
- SH 证券列表深分页被限速（每页 10s+ 直至超时），定位转债别扫深列表；BJ 列表命令直接断连（挡板警告是正确防御）
- EX 主力连续（市场 60）数据面为空；加权合约（L9）可用；商品期权 EX 通道无数据（换 tq-local/QMT/vipdoc 源）
- 最新季度财务包可能是服务器端空壳占位（md5 通过但 parse 报错），取数用上一季

### 单位与系数

- 系数表按代码段登记（helper.py / reader 同步维护）：A 股 0.01、ETF 0.001、转债 0.0001、**BJ 固收（810 定转/82 优先股）0.0001**——缺登记 raise，不许静默回退
- tick vol=手、分钟/日线 bar vol=股（合成需 ×100）；指数分钟 bar vol 是成分股口径≠tick（不可跨接口比）；指数 tick vol 是成分股总手数（落盘 dtype 用 int64）
- 服务器 bar 标签=结束时刻制；无成交分钟用前收盘价填充 vol=0；分笔是 3 秒快照口径，同分钟内笔序依赖服务器返回顺序
- bj 81/82 段定性修正（2026-10-19 官方规程，北证公告〔2024〕20号）：**810=定向可转债（BJ_BOND）、820/82=优先股（BJ_PREFERRED）**，价格 raw×10000，helper/reader 已按此登记；80/811+ 为政府债券不登记（raise 防御）；此前"81/82 转债"的叫法有误

### 实用捷径

- 交易日历：`bars(symbol, period="day", all_pages=True)` 的日期列表即该标的全部交易日
- QMT 代码转换：`510050.SH` → `rsplit('.', 1)` 拆后 swap+lower → `sh.510050`
- 指数 tick 无数据日返回无列空 DataFrame，下游按列访问前必判 `len(df)`

## 已知边界 / 未做

- 参数化周期（"10m"/"5s"）未做（协议支持 8=N分钟 / 9=N日 / 13=N秒）
- 当日分时新格式（**0x051D**，旧笔记笔误 0x0523）已破解且 parser 已落地（2026-10-19）：响应=变长头部（58~75B，含 market+code ASCII+varint 摘要流含 close 锚点）+ 与 0x0FB4 同构的差分记录流；parser 扫描记录起点（精确对齐+vol≥0+close 锚点自检）自适应定位，兼容 legacy 旧 6B 头；在线冒烟 8 品种全过（含 BJ 固收）——破解细节见知识库《数据获取经验.md》；头部除 close/vol/amount 外的字段语义未定
- bj 81/82 段精度已修复落地（BJ_BOND/BJ_PREFERRED 0.0001，见「单位与系数」节）

## 约定

- 修改不自动 commit，由作者决定提交时机；API 变更同步 `QUICK_REFERENCE.md`
- README 不修改；文档为不写修改历史与日期
- 测试入口（联网用例自动 skip）：`python tests/test_xxx.py -v`——routing_and_coefficient（离线 33）/ facade_smoke（门面 40）/ minute_time_data（parser 离线 9，fixture=真实响应存档）/ online_smoke（协议 12）/ concurrency_safety（并发 5）/ server_guard（守护 13）
- 0x051D parser 单测的 fixture 是 2026-10-19 真实服务器响应存档（tests/fixtures/minute_*.bin，4 新格式 + 1 legacy）：换锚点须先在线重抓核实，勿凭记忆改数
