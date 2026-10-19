"""全量 TDX 服务器画像 v3：协议支持 / 品种覆盖 / 历史深度。

本地研究工具，结果不追踪。

用法：
    python tools/server_survey.py [--limit N] [--workers 16] [--ports 7709,7727] [--hosts ip1,ip2] [--fresh]

探测矩阵：
  Phase0 TCP 预筛
  Phase1 协议握手：std(new / legacy / skip3 / skip12)、ex(login / setup92)、mac、mac_ex
  Phase2 品种：std 市场计数 + 代表品种 + 全代码段采样统计；ex 市场清单 + 24 样例 bars
  Phase3 历史深度：日/5分/1分 页二分；tick/分时 年度密度扫描（6 样本/年）
输出：untrack/survey_result.jsonl（断点续传），进度 untrack/survey_progress.log（untrack/ 目录整体不追踪）
"""
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import logging
import pathlib
import socket
import sys
import threading
import time
import traceback
from collections import Counter

TDXPY_ROOT = pathlib.Path(r"D:\Data\github_repo\tdxpy")
sys.path.insert(0, str(TDXPY_ROOT))

import tdxpy.logger  # noqa: E402

tdxpy.logger.logger.setLevel(logging.CRITICAL)

from tdxpy.ex_mac import TdxMacEx_API  # noqa: E402
from tdxpy.exhq import TdxExHq_API  # noqa: E402
from tdxpy.hq import TdxHq_API  # noqa: E402
from tdxpy.mac import TdxMac_API  # noqa: E402
from tdxpy.parser.setup_commands import (  # noqa: E402
    ExLoginCmd,
    ExSetupCmd1,
    NewHandshakeCmd,
    SetupCmd1,
    SetupCmd2,
    SetupCmd3,
)

TOOLS = TDXPY_ROOT / "untrack"
CENSUS = TOOLS / "server_census.json"
RESULT = TOOLS / "survey_result.jsonl"
PROGRESS = TOOLS / "survey_progress.log"

LOG_LOCK = threading.Lock()
_log_fh = None


def log(msg: str) -> None:
    global _log_fh
    with LOG_LOCK:
        if _log_fh is None:
            _log_fh = open(PROGRESS, "a", encoding="utf-8")
        _log_fh.write(f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}\n")
        _log_fh.flush()


# ---------------- 握手变体 ----------------

class _HqBase(TdxHq_API):
    _handshake = "new"

    def setup(self):
        h = self._handshake
        if h == "new":
            self._cmd(NewHandshakeCmd).call_api()
        elif h == "legacy":
            self._cmd(SetupCmd1).call_api()
            self._cmd(SetupCmd2).call_api()
            self._cmd(SetupCmd3).call_api()
        elif h == "skip3":
            self._cmd(SetupCmd1).call_api()
            self._cmd(SetupCmd2).call_api()
        elif h == "skip12":
            self._cmd(SetupCmd3).call_api()


class HqNew(_HqBase):
    _handshake = "new"


class HqLegacy(_HqBase):
    _handshake = "legacy"


class HqSkip3(_HqBase):
    _handshake = "skip3"


class HqSkip12(_HqBase):
    _handshake = "skip12"


class _ExBase(TdxExHq_API):
    _handshake = "login"

    def setup(self):
        if self._handshake == "login":
            self._cmd(ExLoginCmd).call_api()
        else:
            self._cmd(ExSetupCmd1).call_api()


class ExLogin(_ExBase):
    _handshake = "login"


class ExSetup(_ExBase):
    _handshake = "setup92"


# ---------------- 通用工具 ----------------

def tcp_probe(host: str, port: int, timeout: float = 2.0) -> dict:
    t0 = time.time()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return {"ok": True, "ms": round((time.time() - t0) * 1000, 1)}
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}
    finally:
        try:
            s.close()
        except Exception:
            pass


def weekday_near(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def year_density(probe_fn, years_back: int = 27, samples_per_year: int = 6) -> dict:
    """按年密度扫描：每年取 samples_per_year 个均匀样本（月中工作日），返回 {年: 命中/样本}。"""
    today = dt.date.today()
    out = {}
    for yb in range(years_back + 1):
        year = today.year - yb
        hits = 0
        total = 0
        for m in range(2, 13, max(1, 12 // samples_per_year)):
            if total >= samples_per_year:
                break
            try:
                d = weekday_near(dt.date(year, m, 15))
            except ValueError:
                continue
            if d > today:
                continue
            total += 1
            try:
                if probe_fn(d):
                    hits += 1
            except Exception:
                pass
        if total:
            out[str(year)] = f"{hits}/{total}"
    return out


def first_year_with_data(density: dict) -> dict:
    """从密度表推出：最早有数据年 / 首个全覆盖年（命中=全样本）/ 最近年覆盖。"""
    years = sorted(density, reverse=False)  # 升序
    earliest = None
    full = None
    for y in years:
        h, t = density[y].split("/")
        if int(h) > 0 and earliest is None:
            earliest = y
        if int(h) == int(t) and int(t) >= 3 and full is None:
            full = y
    latest_key = str(dt.date.today().year)
    return {"earliest_year": earliest, "full_cover_year": full, "latest": density.get(latest_key)}


def max_valid_page(fetch_page, page_size: int = 800, cap: int = 200) -> dict:
    def has(p: int) -> bool:
        try:
            return bool(fetch_page(p))
        except Exception:
            return False

    if not has(0):
        return {"bars": 0, "pages": 0}
    lo, hi = 0, cap
    if has(hi - 1):
        return {"bars": cap * page_size, "pages": cap, "cap_hit": True}
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if has(mid):
            lo = mid
        else:
            hi = mid
    last = fetch_page(lo)
    return {
        "bars": lo * page_size + (len(last) if last else 0),
        "pages": lo + 1,
        "oldest": str((last[0].get("datetime") or last[0].get("date") or ""))[:19] if last else None,
    }


STD_KINDS = [
    ("stock_sh", 1, "600000", "bars"),
    ("stock_sz", 0, "000001", "quotes"),
    ("stock_bj", 2, "920001", "bars"),
    ("index_sh", 1, "000001", "quotes"),
    ("index_sz", 0, "399001", "quotes"),
    ("etf_sh", 1, "510300", "quotes"),
    ("cb_sh", 1, "113050", "quotes"),
    ("bond_sh", 1, "019547", "quotes"),
    ("fund_sz", 0, "160105", "quotes"),
]


def probe_std(host: str, port: int, timeout: float = 4.0) -> dict:
    out: dict = {"handshake": {}, "kinds": {}, "depth": {}, "errors": []}
    sessions = {}
    for name, cls in [("new", HqNew), ("legacy", HqLegacy), ("skip3", HqSkip3), ("skip12", HqSkip12)]:
        api = cls(raise_exception=False)
        try:
            t0 = time.time()
            if api.connect(host, port, time_out=timeout) is False:
                out["handshake"][name] = {"ok": False, "err": "connect"}
                continue
            n = api.get_security_count(1)
            ms = round((time.time() - t0) * 1000, 1)
            bars_ok = None
            adjust_ok = None
            if n is not None:
                try:
                    b0 = api.get_security_bars(4, 1, "600000", 0, 1, adjust=0)
                    bars_ok = bool(b0)
                    if bars_ok:
                        try:
                            # 复权须用老 bar 比较（最新一根前复权价与不复权价相同）
                            c0 = api.get_security_bars(4, 1, "600000", 5000, 1, adjust=0)
                            c1 = api.get_security_bars(4, 1, "600000", 5000, 1, adjust=1)
                            adjust_ok = bool(c0 and c1) and (c1[0].get("close") != c0[0].get("close"))
                        except Exception:
                            adjust_ok = False
                except Exception:
                    bars_ok = False
            out["handshake"][name] = {"ok": n is not None, "ms": ms, "count_sh": n, "bars_ok": bars_ok, "adjust_ok": adjust_ok}
            if n is not None:
                sessions[name] = api
            else:
                try:
                    api.disconnect()
                except Exception:
                    pass
        except Exception as e:
            out["handshake"][name] = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:40]}"}
            try:
                api.disconnect()
            except Exception:
                pass

    api = None
    for pref in ("new", "skip3", "legacy", "skip12"):
        if pref in sessions:
            api = sessions[pref]
            out["std_session"] = pref
            break
    if api is None:
        for a in sessions.values():
            try:
                a.disconnect()
            except Exception:
                pass
        return out

    # Phase2 品种
    try:
        mc = {}
        for m, label in [(0, "SZ"), (1, "SH"), (2, "BJ")]:
            try:
                mc[label] = api.get_security_count(m)
            except Exception:
                mc[label] = None
        out["market_counts"] = mc
        # 全代码段均匀采样 8 页/市场，统计前缀
        prefix = {}
        for m, label in [(0, "SZ"), (1, "SH")]:
            total_n = mc.get(label) or 0
            sample = {}
            if total_n > 0:
                step = max(1, total_n // 8)
                agg = Counter()
                pages = 0
                for i in range(8):
                    try:
                        rows = api.get_security_list(m, min(i * step, max(0, total_n - 800)))
                    except Exception:
                        continue
                    if rows:
                        pages += 1
                        for r in rows:
                            agg[str(r.get("code") or "")[:3]] += 1
                if pages:
                    sample = {"pages": pages, "prefix": dict(agg.most_common(14))}
            prefix[label] = sample
        out["code_prefix"] = prefix
        # 代表品种
        ok_n = 0
        for label, m, c, how in STD_KINDS:
            try:
                if how == "bars":
                    v = bool(api.get_security_bars(4, m, c, 0, 1))
                else:
                    v = bool(api.get_security_quotes([(m, c)]))
                out["kinds"][label] = v
                ok_n += v
            except Exception:
                out["kinds"][label] = False
        out["kinds"]["_ok"] = ok_n
    except Exception as e:
        out["errors"].append(f"kinds: {type(e).__name__}: {str(e)[:40]}")

    # Phase3 深度
    for cat, label, cap, size in [(4, "day", 60, 800), (0, "min5", 120, 800), (7, "min1", 200, 800)]:
        try:
            out["depth"][label] = max_valid_page(lambda p, c=cat: api.get_security_bars(c, 1, "600000", p * size, size), size, cap)
        except Exception as e:
            out["errors"].append(f"{label}: {type(e).__name__}")

    try:
        d1 = year_density(lambda d: bool(api.get_history_transaction_data(1, "600000", 0, 1, d.strftime("%Y%m%d"))))
        out["depth"]["tick_years"] = d1
        out["depth"]["tick_bound"] = first_year_with_data(d1)
    except Exception as e:
        out["errors"].append(f"tick: {type(e).__name__}")

    try:
        d2 = year_density(lambda d: bool(api.get_history_minute_time_data(1, "600000", d.strftime("%Y%m%d"))))
        out["depth"]["minute_years"] = d2
        out["depth"]["minute_bound"] = first_year_with_data(d2)
    except Exception as e:
        out["errors"].append(f"minute: {type(e).__name__}")

    try:
        x = api.get_xdxr_info(1, "600000")
        if x:
            out["depth"]["xdxr"] = {"n": len(x), "earliest": f"{x[0].get('year')}-{x[0].get('month')}"}
    except Exception as e:
        out["errors"].append(f"xdxr: {type(e).__name__}")

    try:
        f = api.get_finance_info(1, "600000")
        if f:
            out["depth"]["finance"] = {"ok": True, "updated": str(f.get("updated_date")), "ipo": str(f.get("ipo_date"))}
    except Exception as e:
        out["errors"].append(f"finance: {type(e).__name__}")

    try:
        cats = api.get_company_info_category(1, "600000")
        out["depth"]["f10_categories"] = len(cats) if cats else 0
    except Exception as e:
        out["errors"].append(f"f10: {type(e).__name__}")

    for a in sessions.values():
        try:
            a.disconnect()
        except Exception:
            pass
    return out


def probe_mac(host: str, port: int = 7709, timeout: float = 4.0) -> dict:
    out: dict = {"ok": False, "errors": []}
    api = TdxMac_API(raise_exception=False)
    try:
        if api.connect(host, port, time_out=timeout) is False:
            out["err"] = "connect"
            return out
        bars = api.get_mac_bars(1, "600000", 4, 0, 2)
        out["bars_n"] = len(bars) if bars else 0
        out["ok"] = bool(bars)
        if bars:
            b0 = bars[0]
            out["sample"] = {"dt": b0.get("datetime"), "close": b0.get("close"), "amount_ok": b0.get("amount") is not None}
            out["adjust_ok"] = bool(api.get_mac_bars(1, "600000", 4, 0, 1, adjust=1))
        out["ticks_n"] = len(api.get_mac_ticks(1, "600000", 0, 0, 2) or [])
        out["minute_ok"] = bool(api.get_mac_minute(1, "600000", 0))
        out["boards_n"] = len(api.get_mac_boards(0, 0, 3) or [])
    except Exception as e:
        out["err"] = f"{type(e).__name__}: {str(e)[:50]}"
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return out


EX_SAMPLE_CODES = {
    "HK_MAIN": (31, "00700"),
    "HK_INDEX": (27, "HSI"),
    "HK_GROWTH": (48, "08003"),
    "HK_FUND": (49, "02800"),
    "US_STOCK": (74, "AAPL"),
    "DE_STOCK": (73, "SAP"),
    "SG_STOCK": (78, "D05"),
    "ZCE": (28, "RSL8"),
    "DCE": (29, "ML8"),
    "SHFE": (30, "RBL8"),
    "CFFEX": (47, "IFL8"),
    "GFEX": (66, "SIL8"),
    "COMEX": (16, "GC00Y"),
    "NYMEX": (17, "CL00Y"),
    "CBOT": (18, "ZS00Y"),
    "FX_BASIC": (10, "EURUSD"),
    "FX_CROSS": (11, "EURJPY"),
    "CSI_INDEX": (62, "000300"),
    "MORE_INDEX": (105, "000001"),
    "GLOBAL_INDEX": (12, "A_CELS"),
    "UK_STOCK": (75, "AZN"),
    "HK_BOND": (22, "04286"),
    "MACRO": (38, "1_GDP"),
    "COMMODITY_INDEX": (42, "IMCI"),
}


def probe_ex(host: str, port: int = 7727, timeout: float = 4.0) -> dict:
    out: dict = {"handshake": {}, "markets": [], "errors": []}
    sessions = {}
    for name, cls in [("login", ExLogin), ("setup92", ExSetup)]:
        api = cls(raise_exception=False)
        try:
            t0 = time.time()
            if api.connect(host, port, time_out=timeout) is False:
                out["handshake"][name] = {"ok": False, "err": "connect"}
                continue
            markets = api.get_markets()
            ms = round((time.time() - t0) * 1000, 1)
            out["handshake"][name] = {"ok": bool(markets), "ms": ms, "markets_n": len(markets) if markets else 0}
            if markets:
                sessions[name] = api
            else:
                try:
                    api.disconnect()
                except Exception:
                    pass
        except Exception as e:
            out["handshake"][name] = {"ok": False, "err": f"{type(e).__name__}: {str(e)[:40]}"}
            try:
                api.disconnect()
            except Exception:
                pass

    api = None
    for pref in ("login", "setup92"):
        if pref in sessions:
            api = sessions[pref]
            out["ex_session"] = pref
            break
    if api is None:
        for a in sessions.values():
            try:
                a.disconnect()
            except Exception:
                pass
        return out

    try:
        mk = api.get_markets() or []
        out["markets"] = [{"id": m.get("market"), "name": m.get("name"), "cat": m.get("category"), "short": m.get("short_name")} for m in mk]
        out["markets_n"] = len(mk)
    except Exception as e:
        out["errors"].append(f"markets2: {type(e).__name__}")

    try:
        out["instrument_count"] = api.get_instrument_count()
    except Exception as e:
        out["errors"].append(f"icount: {type(e).__name__}")

    out["kinds_bars"] = {}
    ok_n = 0
    for label, (mkt, code) in EX_SAMPLE_CODES.items():
        try:
            b = api.get_instrument_bars(4, mkt, code, 0, 2)
            ok = bool(b)
            out["kinds_bars"][label] = {"ok": ok, "n": len(b) if b else 0}
            ok_n += ok
        except Exception as e:
            out["kinds_bars"][label] = {"ok": False, "err": type(e).__name__}
    out["kinds_ok"] = ok_n

    out["depth"] = {}
    for label, (mkt, code) in [("HK_MAIN", (31, "00700")), ("US_STOCK", (74, "AAPL")), ("SHFE", (30, "RBL8"))]:
        try:
            out["depth"][label] = max_valid_page(
                lambda p, m=mkt, c=code: api.get_instrument_bars(4, m, c, p * 700, 700), 700, 60
            )
        except Exception as e:
            out["errors"].append(f"depth_{label}: {type(e).__name__}")

    try:
        d1 = year_density(lambda d: bool(api.get_history_transaction_data(31, "00700", 0, 1, d.strftime("%Y%m%d"))), years_back=8)
        out["depth"]["hk_tick_years"] = d1
        out["depth"]["hk_tick_bound"] = first_year_with_data(d1)
    except Exception as e:
        out["errors"].append(f"hk_tick: {type(e).__name__}")

    for a in sessions.values():
        try:
            a.disconnect()
        except Exception:
            pass
    return out


def probe_mac_ex(host: str, port: int = 7727, timeout: float = 4.0) -> dict:
    out: dict = {"ok": False, "errors": []}
    api = TdxMacEx_API(raise_exception=False)
    try:
        if api.connect(host, port, time_out=timeout) is False:
            out["err"] = "connect"
            return out
        for label, mkt, code in [("hk", 31, "01398"), ("us", 74, "AAPL"), ("shfe", 30, "RBL8"), ("zce", 28, "RSL8")]:
            try:
                b = api.get_ex_bars(mkt, code, 4, 0, 2)
                out[label] = {"ok": bool(b), "n": len(b) if b else 0}
                if b:
                    out["ok"] = True
            except Exception as e:
                out[label] = {"ok": False, "err": type(e).__name__}
        try:
            out["ticks_ok"] = bool(api.get_ex_ticks(31, "01398", 0, 0, 2))
        except Exception:
            out["ticks_ok"] = False
        try:
            out["minute_ok"] = bool(api.get_ex_minute(31, "01398", 0))
        except Exception:
            out["minute_ok"] = False
    except Exception as e:
        out["err"] = f"{type(e).__name__}: {str(e)[:50]}"
    finally:
        try:
            api.disconnect()
        except Exception:
            pass
    return out


def probe_endpoint(host: str, port: int) -> dict:
    rec: dict = {"host": host, "port": port, "ts": dt.datetime.now().isoformat(timespec="seconds")}
    tcp = tcp_probe(host, port)
    rec["tcp"] = tcp
    if not tcp["ok"]:
        return rec

    is_7709 = port == 7709
    is_7727 = port == 7727
    if is_7709:
        rec["std"] = probe_std(host, port)
        rec["mac"] = probe_mac(host, port)
    elif is_7727:
        rec["ex"] = probe_ex(host, port)
        rec["mac_ex"] = probe_mac_ex(host, port)
    else:
        # 非标准端口（census 里有 7719/7720）：std 与 EX 都试一次，只试一遍
        rec["std"] = probe_std(host, port)
        rec["ex"] = probe_ex(host, port)

    std = rec.get("std") or {}
    ex = rec.get("ex") or {}
    d = std.get("depth") or {}
    de = ex.get("depth") or {}
    rec["summary"] = {
        "std_ok": bool(std.get("std_session")),
        "std_handshakes": [k for k, v in (std.get("handshake") or {}).items() if v.get("ok")],
        "std_bars_handshakes": [k for k, v in (std.get("handshake") or {}).items() if v.get("bars_ok")],
        "std_adjust_handshakes": [k for k, v in (std.get("handshake") or {}).items() if v.get("adjust_ok")],
        "ex_ok": bool(ex.get("ex_session")),
        "ex_handshakes": [k for k, v in (ex.get("handshake") or {}).items() if v.get("ok")],
        "ex_markets_n": ex.get("markets_n"),
        "ex_kinds_ok": ex.get("kinds_ok"),
        "mac_ok": bool((rec.get("mac") or {}).get("ok")),
        "mac_ex_ok": bool((rec.get("mac_ex") or {}).get("ok")),
        "tick_earliest_year": (d.get("tick_bound") or {}).get("earliest_year"),
        "tick_full_year": (d.get("tick_bound") or {}).get("full_cover_year"),
        "minute_earliest_year": (d.get("minute_bound") or {}).get("earliest_year"),
        "minute_full_year": (d.get("minute_bound") or {}).get("full_cover_year"),
        "day_bars": (d.get("day") or {}).get("bars"),
        "day_oldest": (d.get("day") or {}).get("oldest"),
        "min5_bars": (d.get("min5") or {}).get("bars"),
        "min1_bars": (d.get("min1") or {}).get("bars"),
        "hk_day_bars": (de.get("HK_MAIN") or {}).get("bars"),
        "us_day_bars": (de.get("US_STOCK") or {}).get("bars"),
        "hk_tick_year": (de.get("hk_tick_bound") or {}).get("earliest_year"),
    }
    return rec


def load_done() -> set:
    done = set()
    if RESULT.exists():
        for line in RESULT.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                done.add((r["host"], int(r["port"])))
            except Exception:
                continue
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--ports", default="")
    ap.add_argument("--hosts", default="")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    census = json.loads(CENSUS.read_text(encoding="utf-8"))
    endpoints = []
    bad = 0
    for r in census:
        try:
            ip = ipaddress.ip_address(r["host"])
        except ValueError:
            bad += 1
            continue
        if ip.is_private or ip.is_loopback or ip.is_multicast or str(ip).startswith("0."):
            bad += 1
            continue
        endpoints.append((r["host"], int(r["port"])))

    if args.ports:
        keep = {int(p) for p in args.ports.split(",")}
        endpoints = [e for e in endpoints if e[1] in keep]
    if args.hosts:
        keep_h = set(args.hosts.split(","))
        endpoints = [e for e in endpoints if e[0] in keep_h]
    seen = set()
    endpoints = [e for e in endpoints if not (e in seen or seen.add(e))]
    if args.limit:
        endpoints = endpoints[: args.limit]

    if args.fresh and RESULT.exists():
        RESULT.unlink()
    done = load_done()
    todo = [e for e in endpoints if e not in done]
    log(f"=== v3 start: total={len(endpoints)} bad_ip={bad} done={len(done)} todo={len(todo)} workers={args.workers} ===")

    import concurrent.futures as cf

    t_start = time.time()
    counter = {"alive": 0, "dead": 0, "n": 0}
    lock = threading.Lock()

    def work(ep):
        host, port = ep
        try:
            rec = probe_endpoint(host, port)
        except Exception:
            rec = {"host": host, "port": port, "ts": dt.datetime.now().isoformat(timespec="seconds"),
                   "fatal": traceback.format_exc()[-600:]}
        with LOG_LOCK:
            with open(RESULT, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with lock:
            s = rec.get("summary") or {}
            if s.get("std_ok") or s.get("ex_ok") or s.get("mac_ok") or s.get("mac_ex_ok"):
                counter["alive"] += 1
            else:
                counter["dead"] += 1
            counter["n"] += 1
            if counter["n"] % 10 == 0 or counter["n"] == len(todo):
                log(f"progress {counter['n']}/{len(todo)} alive={counter['alive']} dead={counter['dead']} "
                    f"elapsed={time.time()-t_start:.0f}s")
        return rec

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, todo))

    log(f"=== v3 done: {len(todo)} in {time.time()-t_start:.0f}s (alive={counter['alive']} dead={counter['dead']}) ===")


if __name__ == "__main__":
    main()
