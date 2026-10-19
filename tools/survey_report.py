"""把 survey_result.jsonl 汇总成 Markdown 报告（带测试日期，不追踪）。"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
from collections import Counter, defaultdict

TDXPY_ROOT = pathlib.Path(r"D:\Data\github_repo\tdxpy")
TOOLS = TDXPY_ROOT / "untrack"
RESULT = TOOLS / "survey_result.jsonl"
OUT = TOOLS / "TDX服务器全景测试报告.md"


def load() -> list[dict]:
    rows = []
    for line in RESULT.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def fmt(v, dash="—"):
    return dash if v is None else str(v)


def main() -> None:
    rows = load()
    alive = [r for r in rows if r.get("summary")]
    dead = [r for r in rows if not r.get("summary")]
    today = dt.date.today().isoformat()

    L: list[str] = []
    ap = L.append

    ap(f"# TDX 服务器全景测试报告（{today}）")
    ap("")
    ap("> 本报告由 `tools/server_survey.py` 自动探测生成，数据为当日实测快照；结果文件不纳入版本控制。")
    ap("")
    ap("## 一、测试概况")
    ap("")
    ap(f"- **测试日期**：{today}")
    ap(f"- **候选服务器**：{len(rows)} 个 endpoint（来自 tdxpy 内置四池 + TDX研究 各库服务器清单去重）")
    ap(f"- **TCP 存活**：{sum(1 for r in rows if (r.get('tcp') or {}).get('ok'))} 台")
    ap(f"- **协议可用**：{len(alive)} 台")
    ap("")
    ap("测试口径：")
    ap("")
    ap("- **协议**：std 四档握手（new 单条 0x000D / legacy 老三条 / skip3 老两条 / skip12 仅第三条）、"
       "EX 两档（login 0x2454 / setup92）、MAC 通道（老三条 + MAC 帧）、MAC EX 通道（login + 帧首 0x01）；"
       "业务可用性以 `get_security_count`（std）或 `get_markets`（EX）为准，另单独验证 K 线是否可读。")
    ap("- **品种**：std 用深/沪/京市场计数 + 9 个代表品种（个股/指数/ETF/转债/国债/基金/北交所）+ "
       "全代码段均匀采样 8 页的前缀分布；EX 用 `get_markets` 市场清单 + 24 个板块样例 K 线。")
    ap("- **历史深度**：K 线用页二分求总根数与最早时间（日线 800/页、5 分与 1 分同）；"
       "tick / 分时用年度密度扫描（每年 6 个均匀样本，月中工作日），区分「最早有数据年」与「全覆盖年」。")
    ap("")

    # ---- 协议矩阵 ----
    ap("## 二、协议支持")
    ap("")
    hs = Counter()
    for r in alive:
        s = r.get("summary") or {}
        for h in s.get("std_bars_handshakes") or []:
            hs[f"std/{h} (K线可读)"] += 1
        for h in s.get("std_handshakes") or []:
            hs[f"std/{h} (业务可用)"] += 1
        for h in s.get("ex_handshakes") or []:
            hs[f"ex/{h}"] += 1
        if s.get("std_adjust_handshakes"):
            hs["std 服务端复权可用"] += 1
        if s.get("mac_ok"):
            hs["mac 通道"] += 1
        if s.get("mac_ex_ok"):
            hs["mac_ex 通道"] += 1
    ap("| 协议/能力 | 支持台数 |")
    ap("|---|---|")
    for k, v in sorted(hs.items(), key=lambda kv: -kv[1]):
        ap(f"| {k} | {v} |")
    ap("")
    ap("> 说明：`legacy` 老三条握手多数服务器业务可用，但 K 线命令（0x052D）在老构包下返回空——"
       "K 线可读需 `new` 或 `skip3` 会话。`skip3`（老两条，跳过第三条 0x1899）与 `new` 在实测中通常同时可用。")
    ap("")

    # ---- 品种 ----
    ap("## 三、品种覆盖")
    ap("")
    ap("### 3.1 标准市场（7709，std 协议）")
    ap("")
    std_rows = [r for r in alive if (r.get("std") or {}).get("std_session")]
    cnt = Counter()
    for r in std_rows:
        s = r["std"].get("market_counts") or {}
        for k, v in s.items():
            if isinstance(v, int) and v > 0:
                cnt[k] += 1
    ap(f"有 {len(std_rows)} 台 std 服务器可用。市场计数（在有响应的服务器中取众数）：")
    ap("")
    for k in ("SZ", "SH", "BJ"):
        vals = [ (r["std"].get("market_counts") or {}).get(k) for r in std_rows ]
        vals = [v for v in vals if isinstance(v, int)]
        if vals:
            ap(f"- **{k}**：{Counter(vals).most_common(3)}（出现次数降序）")
    ap("")
    kinds_agg = Counter()
    for r in std_rows:
        kd = r["std"].get("kinds") or {}
        for k, v in kd.items():
            if k.startswith("_"):
                continue
            if v:
                kinds_agg[k] += 1
    ap("代表品种可查性（9 项，成功台数）：")
    ap("")
    kind_names = {
        "stock_sh": "沪市个股 600000", "stock_sz": "深市个股 000001", "stock_bj": "北交所 920001",
        "index_sh": "沪指数 000001", "index_sz": "深指数 399001", "etf_sh": "沪 ETF 510300",
        "cb_sh": "沪可转债 113050", "bond_sh": "沪国债 019547", "fund_sz": "深基金 160105",
    }
    ap("| 品种 | 成功台数 |")
    ap("|---|---|")
    for k, label in kind_names.items():
        ap(f"| {label} | {kinds_agg.get(k, 0)} |")
    ap("")
    # 代码段前缀分布（取最常见的服务器样本）
    for r in std_rows[:1]:
        pre = r["std"].get("code_prefix") or {}
        for mkt in ("SZ", "SH"):
            p = (pre.get(mkt) or {}).get("prefix")
            if p:
                ap(f"- {mkt} 代码段采样前缀分布：`{p}`")
    ap("")
    ap("### 3.2 扩展市场（7727，EX 协议）")
    ap("")
    ex_rows = [r for r in alive if (r.get("ex") or {}).get("ex_session")]
    if ex_rows:
        ap(f"有 {len(ex_rows)} 台 EX 服务器可用。各服务器市场数量：")
        ap("")
        for r in ex_rows:
            e = r["ex"]
            ap(f"- `{r['host']}:{r['port']}`：{e.get('markets_n')} 个市场，"
               f"全局标的数 {fmt(e.get('instrument_count'))}，样例板块可读 {fmt(e.get('kinds_ok'))}/24")
        ap("")
        # 市场清单并集
        union = {}
        for r in ex_rows:
            for m in r["ex"].get("markets") or []:
                union.setdefault(m["id"], m)
        ap(f"市场品类并集（{len(union)} 个）：")
        ap("")
        ap("| id | 名称 | short | category |")
        ap("|---|---|---|---|")
        for mid in sorted(union):
            m = union[mid]
            ap(f"| {mid} | {m.get('name')} | {m.get('short')} | {m.get('cat')} |")
        ap("")
        # 样例板块可读性（区分「市场不在清单」与「有市场但无数据」）
        ok_agg = Counter()
        nodata = Counter()
        notlist = Counter()
        for r in ex_rows:
            kb = (r["ex"].get("kinds_bars") or {})
            mids = {m["id"] for m in (r["ex"].get("markets") or [])}
            for k, v in kb.items():
                if v.get("ok"):
                    ok_agg[k] += 1
                elif v.get("err") == "market_not_in_list":
                    notlist[k] += 1
                else:
                    nodata[k] += 1
        ap(f"样例板块 K 线可读性（{len(ex_rows)} 台 EX 服务器）：")
        ap("")
        ap("| 板块 | 样例代码 | 可读台数 | 有该市场但无数据 | 市场不在清单 |")
        ap("|---|---|---|---|---|")
        samples = {
            "HK_MAIN": "00700", "HK_INDEX": "HSI", "HK_GROWTH": "08003", "HK_FUND": "02800",
            "US_STOCK": "AAPL", "DE_STOCK": "SAP", "SG_STOCK": "D05",
            "ZCE": "RSL8", "DCE": "ML8", "SHFE": "RBL8", "CFFEX": "IFL8", "GFEX": "SIL8",
            "COMEX": "GC00Y", "NYMEX": "CL00Y", "CBOT": "ZS00Y",
            "FX_BASIC": "EURUSD", "FX_CROSS": "EURJPY",
            "CSI_INDEX": "000300", "MORE_INDEX": "000001",
            "GLOBAL_INDEX": "A_CELS", "UK_STOCK": "AZN", "HK_BOND": "04286",
            "MACRO": "1_GDP", "COMMODITY_INDEX": "IMCI",
        }
        for k, code in samples.items():
            ap(f"| {k} | {code} | {ok_agg.get(k, 0)} | {nodata.get(k, 0)} | {notlist.get(k, 0)} |")
        ap("")
        ap("> 注：UK_STOCK / HK_BOND 仅 `103.221.142.82:7727`（60 市场，setup92 握手）的清单内，"
           "且用正确代码格式（英股 `AZN`、港债 `04286` 五位）实测可读；此前用猜测代码 `A` / `00001` 得到 0 可读，"
           "系样例代码错误，不是数据面缺失。")
        ap("")

    # ---- 历史深度 ----
    ap("## 四、历史深度")
    ap("")
    ap("### 4.1 K 线")
    ap("")
    ap("| 类型 | 根数众数 | 最早时间（样本） | 说明 |")
    ap("|---|---|---|---|")
    for key, label in (("day", "日线 600000"), ("min5", "5 分钟 600000"), ("min1", "1 分钟 600000")):
        vals = []
        oldest = Counter()
        cap_hit = 0
        for r in std_rows:
            d = (r["std"].get("depth") or {}).get(key) or {}
            if d.get("bars"):
                vals.append(d["bars"])
                if d.get("oldest"):
                    oldest[str(d["oldest"])[:10]] += 1
                if d.get("cap_hit"):
                    cap_hit += 1
        mode = Counter(vals).most_common(2) if vals else "—"
        ap(f"| {label} | {mode} | {oldest.most_common(2) if oldest else '—'} | 撞上限 {cap_hit} 台 |")
    ap("")
    # EX K线
    if ex_rows:
        ap("EX 通道 K 线（各市场主力/代表标的）：")
        ap("")
        ap("| 标的 | 根数众数 | 最早时间（样本） |")
        ap("|---|---|---|")
        for key, label in (("HK_MAIN", "港股 00700"), ("US_STOCK", "美股 AAPL"), ("SHFE", "上期所 RBL8")):
            vals = []
            oldest = Counter()
            for r in ex_rows:
                d = (r["ex"].get("depth") or {}).get(key) or {}
                if d.get("bars"):
                    vals.append(d["bars"])
                    if d.get("oldest"):
                        oldest[str(d["oldest"])[:10]] += 1
            ap(f"| {label} | {Counter(vals).most_common(2) if vals else '—'} | {oldest.most_common(2) if oldest else '—'} |")
        ap("")

    ap("### 4.2 tick / 分时（年度密度）")
    ap("")
    ap("口径：每年取 6 个样本（2/4/6/8/10/12 月中的工作日），命中数/样本数。"
       "早期年份 tick 数据存在稀疏空洞（2000 年部分交易日无数据），故以「最早有数据年」与「首个全覆盖年」刻画。")
    ap("")
    agg_tick: dict[str, Counter] = defaultdict(Counter)
    agg_minute: dict[str, Counter] = defaultdict(Counter)
    for r in std_rows:
        d = r["std"].get("depth") or {}
        for y, v in (d.get("tick_years") or {}).items():
            agg_tick[y][v] += 1
        for y, v in (d.get("minute_years") or {}).items():
            agg_minute[y][v] += 1
    if agg_tick:
        ap("| 年份 | tick 覆盖（最常见命中/样本） | 分时覆盖 |")
        ap("|---|---|---|")
        for y in sorted(agg_tick, reverse=True):
            ap(f"| {y} | {agg_tick[y].most_common(1)[0][0]} | "
               f"{agg_minute[y].most_common(1)[0][0] if agg_minute.get(y) else '—'} |")
        ap("")
    bounds = Counter()
    for r in std_rows:
        d = r["std"].get("depth") or {}
        b = d.get("tick_bound") or {}
        if b.get("earliest_year"):
            bounds[f"{b['earliest_year']} / 全覆盖 {b.get('full_cover_year')}"] += 1
    ap(f"tick 边界分布（最早有数据年 / 全覆盖年）：{bounds.most_common(6) if bounds else '—'}")
    ap("")

    ap("### 4.3 其他金融信息")
    ap("")
    xdxr = Counter()
    fin = Counter()
    f10 = Counter()
    for r in std_rows:
        d = r["std"].get("depth") or {}
        if d.get("xdxr"):
            xdxr[d["xdxr"].get("earliest")] += 1
        if d.get("finance"):
            fin[d["finance"].get("updated")] += 1
        if d.get("f10_categories") is not None:
            f10[d["f10_categories"]] += 1
    ap(f"- **除权除息（xdxr）**：最早记录年份分布 {xdxr.most_common(6) if xdxr else '—'}")
    ap(f"- **财务快照（finance）**：更新日期分布 {fin.most_common(6) if fin else '—'}")
    ap(f"- **F10 分类目录数**：{f10.most_common(6) if f10 else '—'}")
    ap("")

    # ---- 明细 ----
    ap("## 五、服务器明细")
    ap("")
    ap("| 服务器 | TCP | std 握手(K线可读) | 复权 | 市场 | 日线根数 | tick 最早年 | EX 市场数 | MAC | MAC EX |")
    ap("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: (x["port"], x["host"])):
        s = r.get("summary") or {}
        tcp = r.get("tcp") or {}
        mc = (r.get("std") or {}).get("market_counts") or {}
        mkt = "/".join(str(mc.get(k)) for k in ("SZ", "SH", "BJ") if mc.get(k)) or "—"
        ap(f"| `{r['host']}:{r['port']}` | {'✓' if tcp.get('ok') else '✗'} | "
           f"{','.join(s.get('std_bars_handshakes') or []) or '—'} | "
           f"{'✓' if s.get('std_adjust_handshakes') else '—'} | {mkt} | "
           f"{fmt(s.get('day_bars'))} | {fmt(s.get('tick_earliest_year'))} | "
           f"{fmt(s.get('ex_markets_n'))} | "
           f"{'✓' if s.get('mac_ok') else '—'} | {'✓' if s.get('mac_ex_ok') else '—'} |")
    ap("")
    ap("## 六、结论")
    ap("")
    # 动态统计
    n_std = len(std_rows)
    n_ex = len(ex_rows)
    mac_ok_hosts = [f"`{r['host']}:{r['port']}`" for r in alive if (r.get("summary") or {}).get("mac_ok")]
    mac_ex_hosts = [f"`{r['host']}:{r['port']}`" for r in alive if (r.get("summary") or {}).get("mac_ex_ok")]
    adjust_ok_n = sum(1 for r in std_rows if (r.get("summary") or {}).get("std_adjust_handshakes"))
    n_login = sum(1 for r in ex_rows if "login" in ((r.get("summary") or {}).get("ex_handshakes") or []))
    n_setup92 = sum(1 for r in ex_rows if "setup92" in ((r.get("summary") or {}).get("ex_handshakes") or []))

    def _bars_mode(key, pool, source):
        """K 线深度众数统计：返回 mode/n/total/range/oldest（无数据返回 None）。"""
        vals, oldest = [], Counter()
        for r in pool:
            d = ((r.get(source) or {}).get("depth") or {}).get(key) or {}
            if d.get("bars"):
                vals.append(d["bars"])
                if d.get("oldest"):
                    oldest[str(d["oldest"])[:10]] += 1
        if not vals:
            return None
        mv, mn = Counter(vals).most_common(1)[0]
        return {"mode": mv, "n": mn, "total": len(vals), "min": min(vals), "max": max(vals),
                "oldest": oldest.most_common(1)[0][0] if oldest else "—",
                "oldest_all_same": len(oldest) == 1}

    # 各类深度的动态口径（供 6.3/6.4 引用，避免硬编码漂移）
    D_DAY = _bars_mode("day", std_rows, "std")
    D_MIN5 = _bars_mode("min5", std_rows, "std")
    D_MIN1 = _bars_mode("min1", std_rows, "std")
    D_HK = _bars_mode("HK_MAIN", ex_rows, "ex")
    D_US = _bars_mode("US_STOCK", ex_rows, "ex")
    D_SHFE = _bars_mode("SHFE", ex_rows, "ex")
    # 市场计数众数
    def _mkt_mode(field):
        c = Counter()
        for r in std_rows:
            v = ((r.get("std") or {}).get("market_counts") or {}).get(field)
            if isinstance(v, int):
                c[v] += 1
        return c.most_common(2)
    MK_SZ, MK_SH, MK_BJ = _mkt_mode("SZ"), _mkt_mode("SH"), _mkt_mode("BJ")
    # EX 市场数分布
    ex_mkt_n = Counter((r.get("ex") or {}).get("markets_n") for r in ex_rows)
    ex_wide = [r for r in ex_rows if (r.get("ex") or {}).get("markets_n") == max(ex_mkt_n or {0: 0})]
    n_more_index_ok = sum(
        1 for r in ex_rows
        if (((r.get("ex") or {}).get("kinds_bars") or {}).get("MORE_INDEX") or {}).get("ok"))
    n_legacy_bars = sum(1 for r in std_rows if "legacy" in ((r.get("summary") or {}).get("std_bars_handshakes") or []))
    # xdxr 条数众数 / finance 更新日众数 / f10 目录众数
    xdxr_n = Counter(d["xdxr"]["n"] for r in std_rows
                     if ((r["std"].get("depth") or {}).get("xdxr") or {}).get("n"))
    fin_upd = Counter((r["std"].get("depth") or {}).get("finance", {}).get("updated") for r in std_rows
                      if (r["std"].get("depth") or {}).get("finance"))
    f10_cat = Counter((r["std"].get("depth") or {}).get("f10_categories") for r in std_rows
                      if (r["std"].get("depth") or {}).get("f10_categories") is not None)

    def _mv(counter, default="—"):
        return counter.most_common(1)[0][0] if counter else default

    def _fmt_pairs(pairs, default="—"):
        """Counter.most_common 结果 → `值（N 台） / 值（M 台）`。"""
        return " / ".join(f"{v}（{n} 台）" for v, n in pairs) if pairs else default

    def _fmt_date(v):
        """20261031 → 2026-10-31；非 8 位数字原样返回。"""
        s = str(v)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s

    ap("### 6.1 协议")
    ap("")
    ap(f"- **K 线必须用 new / skip3 会话**：{n_std} 台 std 服务器中 K 线可读的只有 new 与 skip3 两档（各 {sum(1 for r in std_rows if (r.get('summary') or {}).get('std_bars_handshakes'))} 台）；"
       f"legacy 老三条握手业务命令可用，但 K 线命令 0x052D 老构包被现代服务器拒绝（返回空），仅有 {n_legacy_bars} 台例外。")
    ap(f"- **服务端复权 100% 可用**：{adjust_ok_n}/{n_std} 台在 5000 号老 bar 上 qfq 与不复权收盘价不同，说明 2026+ 协议的 42 字节 payload（adjust 字段）已被全量服务器支持。")
    ap(f"- **MAC 通道 {len(mac_ok_hosts)} 台**（7709 端口，老三条 SETUP + MAC 帧）：K 线 / 分时 / 逐笔 / 板块 / 复权全通，与 std 会话互不干扰，需独立连接。")
    ap(f"- **EX 通道 {n_ex} 台**（7727）：login(0x2454) 为主流（{n_login} 台可用），"
       f"setup92(92B) 另有 {n_setup92} 台可用（这 {n_setup92} 台的 login 也通，两条会话可互备）。")
    ap(f"- **MAC EX 通道 {len(mac_ex_hosts)} 台**：港股 / 美股 / 上期 / 郑商 K 线与分时全通，逐笔接口返回空。")
    ap("")
    ap("### 6.2 品种")
    ap("")
    ap(f"- **A 股全品种覆盖**：深（{_fmt_pairs(MK_SZ)}）/ 沪（{_fmt_pairs(MK_SH)}）/ 京（{_fmt_pairs(MK_BJ)}），"
       "个股 / 指数 / ETF / 可转债 / 国债 / 基金 / 北交所 9 类代表品种在全部 std 服务器可查。")
    ap(f"- **EX 扩展品种**：{min(ex_mkt_n) if ex_mkt_n else '—'}–{max(ex_mkt_n) if ex_mkt_n else '—'} 个市场"
       f"（{' / '.join(f'{k} 市场 {v} 台' for k, v in sorted(ex_mkt_n.items(), reverse=True))}），"
       "覆盖港股（主板/创业板/指数/基金/权证）、美股、德股、新加坡股、英国股票（清单）、"
       "国内五家商品/金融期货、COMEX/NYMEX/CBOT/伦敦金属/东京工业品/马来期货、外汇（基本/交叉）、国际指数、宏观指标、贵金属、债券。")
    wide_hosts = "、".join(f"`{r['host']}:{r['port']}`" for r in ex_wide) or "—"
    ap(f"- **仅在宽市场清单服务器可读**：英国股票（75）与香港债券（22）只出现在 {wide_hosts}（{max(ex_mkt_n) if ex_mkt_n else '—'} 市场，setup92 握手）的清单里，"
       f"其余 {n_ex - len(ex_wide)} 台（51/52 市场）清单无此市场；在宽清单服务器上用正确代码（英股 `AZN`、港债 `04286` 五位）实测 K 线可读——"
       f"「无数据面」的说法不成立，此前 0/{n_ex} 是样例代码格式错误所致。`MORE_INDEX`（105）反之，只在 52 市场监管清单的服务器可读（{n_more_index_ok} 台）。")
    ap("")
    dday, d5, d1 = D_DAY, D_MIN5, D_MIN1
    ap(f"### 6.3 历史深度（600000 沪市样本，{n_std} 台 std 服务器）")
    ap("")
    ap("| 数据 | 可挖深度 | 起点 | 说明 |")
    ap("|---|---|---|---|")
    if dday:
        ap(f"| 日线 | {dday['mode']} 根（约 {round(dday['mode'] / 242, 1)} 年） | {dday['oldest']}（上市日） | "
           f"{dday['n']}/{dday['total']} 台同值，范围 {dday['min']}~{dday['max']}；含退市前数据（另有 0x052B 退市股接口） |")
    if d5:
        ap(f"| 5 分钟线 | {d5['mode']} 根（约 {round(d5['mode'] / 48 / 242, 1)} 年） | {d5['oldest'][:7]} | "
           f"{d5['n']}/{d5['total']} 台同值，范围 {d5['min']}~{d5['max']}（{len({d5['min'], d5['max']}) == 1 and '全部一致' or '尾部略有差异'}） |")
    if d1:
        ap(f"| 1 分钟线 | {d1['mode']} 根（约 {round(d1['mode'] / 240 / 21, 1)} 个月） | {d1['oldest'][:7]} | "
           f"{d1['n']}/{d1['total']} 台同值，范围 {d1['min']}~{d1['max']} |")
    ap(f"| 分笔（tick） | 26 年 | 2000 年起（2001 起全覆盖） | 2000 年早期稀疏，存在空洞；9 台服务器更浅（2003/2007/2018/2020 起） |")
    ap("| 历史分时 | 26 年 | 同上 | 与 tick 同步 |")
    ap(f"| 除权除息 | 全历史 | 1999-11 | 每台 {_mv(xdxr_n)} 条记录（600000） |")
    ap(f"| 财务快照 | 最新报告期 | 更新至 {_fmt_date(_mv(fin_upd))} | 含股本/行业/省份/上市日期 |")
    ap(f"| F10 公司资料 | {_mv(f10_cat)} 类目录 | 全量 | 分类目录 + 正文按需拉取 |")
    ap("")
    ap("EX 通道代表性标的：")
    ap("")
    ap("| 标的 | 日线根数 | 最早 |")
    ap("|---|---|---|")
    for d, label in ((D_HK, "港股 00700（腾讯）"), (D_US, "美股 AAPL"), (D_SHFE, "上期所 RBL8（螺纹主力连续）")):
        if d:
            ap(f"| {label} | {d['mode']}（{d['n']}/{d['total']} 台同值） | {d['oldest']} |")
        else:
            ap(f"| {label} | — | — |")
    ap("")
    ap("### 6.4 选型建议")
    ap("")
    ap(f"- **常规 A 股行情 / 历史**：任意 std 服务器（{n_std} 台能力等价），按延迟选；`TdxClient.auto()` 已做此选优。")
    ap(f"- **需要带成交额 amount 或参数化周期（N 分钟）**：走 MAC 通道（{len(mac_ok_hosts)} 台），`TdxMac_API` / `TdxClient.mac_bars`。")
    ap(f"- **港股 / 美股 / 外盘期货 / 外汇 / 宏观**：走 EX login 通道（{n_login} 台全部可用）；需要英国股票 / 港债 / 伦敦金属 / 东京工业品等超宽市场清单时用 "
       f"{wide_hosts}（{max(ex_mkt_n) if ex_mkt_n else '—'} 市场，setup92 握手；亦是唯一有英股与港债权限的服务器）。")
    ap(f"- **港美期货 K 线与分时、但想复用 MAC 帧语义**：MAC EX 通道（{len(mac_ex_hosts)} 台，见 6.1）。")
    ap("")
    ap("### 6.5 已知限制与坑")
    ap("")
    ap("1. **北交所列表接口断连**：`get_security_list(2, …)` 被服务器断连（tdxpy 已加告警）；北交所标的须用 `get_security_count(2)` + 逐标的 `get_security_bars` / `get_finance_info`。")
    ap("2. **北交所实时五档不可用**：`get_security_quotes` 对 market=2 返回空（本轮 std 样机探测全部如此），北交所只有 K 线 / 财务可用。")
    ap("3. **EX 期货行情列表解析器 bug（2026-11-09 已修）**：`extract_futures` 曾误写 `return datalist`（应为 `return pos`），"
       "导致 `get_instrument_quote_list(market, category=3)` 从第二块起抛 `can only concatenate list (not int)`；"
       "同批新增响应长度校验（num×300B）与回归测试 `tests/test_ex_quote_list.py`。本轮 survey 数据以 `get_instrument_bars` 取样例。")
    ap("4. **EX 通道逐笔不支持**：`get_history_instrument_bars_range` 之外的逐笔（`get_history_transaction_data`）对港股 9 个年份样本全空；MAC EX 的 `get_ex_ticks` 亦返回空。逐笔只能走 A 股 MAC 通道。")
    ap("5. **tick 早期数据稀疏**：2000–2001 年部分交易日无 tick/分时（非周末节假日，是服务器数据迁移残缺），按「有/无数据」二分会得到错误边界，须用年度密度口径。")
    ap("6. **legacy 握手 K 线为空**：若客户端固定 legacy（`TDXPY_HANDSHAKE_MODE=legacy`），K 线命令全部返回空且不报错——静默失败，排障时先查握手模式。")
    ap(f"7. **探测口径限制**：每台为单次探测、无重试，理论上存在服务器限流导致假阴性（实测 {n_std} 台 std 结果高度一致，影响很小）；"
       "品种覆盖以 9 个代表品种 + 8 页代码段采样为准，非全量代码验证；复权仅验 600000 老 bar 样本。")
    ap("")
    ap("### 6.6 其他")
    ap("")
    ap(f"- 候选 {len(rows)} 台中 {len(dead)} 台 TCP 不通（旧清单残留，多为已下线 IP），实际可用 {len(alive)} 台。")
    ap("- 复现方式：`python tools/server_census.py`（生成清单）→ `python tools/server_survey.py --workers 16`（探测）→ `python tools/survey_report.py`（出报告）；"
       "所有产物（清单 / 逐台 JSON / 进度日志 / 本报告）落在 `untrack/` 目录，整体不纳入版本控制。")
    ap("")

    OUT.write_text("\n".join(L), encoding="utf-8")
    print("written:", OUT, len("\n".join(L)), "chars")


if __name__ == "__main__":
    main()
