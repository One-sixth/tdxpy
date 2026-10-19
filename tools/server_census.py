"""汇总所有已知 TDX 服务器清单（仅本地研究用，不追踪）。

扫描来源：
1. tdxpy 内置四组：STD_SERVERS / EX_SERVERS / MAC_SERVERS / MAC_EX_SERVERS
2. TDX研究 各库的服务器清单文件（a-share-tdxdata / eltdx / pytdxdata / mootdx / tdxrs）
输出：untrack/server_census.json（untrack/ 目录整体不追踪）
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

TDXPY_ROOT = pathlib.Path(r"D:\Data\github_repo\tdxpy")
RESEARCH = pathlib.Path(r"D:\Data\github_repo\TDX研究")
OUT = TDXPY_ROOT / "untrack" / "server_census.json"

IPRE = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?:[:\s,\"']{1,3}(\d{2,5})\b)?")

census: dict[str, dict] = {}


def add(host: str, port: int, source: str, kind: str = "") -> None:
    key = f"{host}:{port}"
    rec = census.setdefault(key, {"host": host, "port": port, "sources": [], "kinds": []})
    if source not in rec["sources"]:
        rec["sources"].append(source)
    if kind and kind not in rec["kinds"]:
        rec["kinds"].append(kind)


# ---------- 1. tdxpy 内置四组 ----------
sys.path.insert(0, str(TDXPY_ROOT))
from tdxpy.client import STD_SERVERS, EX_SERVERS, MAC_SERVERS, MAC_EX_SERVERS  # noqa: E402

for h, p in STD_SERVERS:
    add(h, p, "tdxpy.STD", "std")
for h, p in EX_SERVERS:
    add(h, p, "tdxpy.EX", "ex")
for h, p in MAC_SERVERS:
    add(h, p, "tdxpy.MAC", "mac")
for h, p in MAC_EX_SERVERS:
    add(h, p, "tdxpy.MAC_EX", "mac_ex")

# ---------- 2. 各库配置文件 ----------
text_files = {
    "a-share-tdxdata/servers.txt": ("a-share-tdxdata", 7709),
    "eltdx/src/eltdx/tdx_server.json": ("eltdx", 7709),
    "eltdx/src/eltdx/money_flow_servers.json": ("eltdx.moneyflow", 7709),
    "pytdxdata/src/pytdxdata/servers.json": ("pytdxdata", 7709),
    "mootdx/mootdx/consts.py": ("mootdx", 7709),
    "mootdx/mootdx/util/hq.py": ("mootdx", 7709),
}
for rel, (name, default_port) in text_files.items():
    f = RESEARCH / rel
    if not f.exists():
        continue
    txt = f.read_text(encoding="utf-8", errors="ignore")
    for m in IPRE.finditer(txt):
        host = m.group(1)
        port = int(m.group(2)) if m.group(2) and 20 <= int(m.group(2)) <= 65535 else default_port
        add(host, port, name)

# ---------- 3. 扫 eltdx ex hosts 与 tdxrs 池 ----------
ex_hosts_py = RESEARCH / "eltdx/src/eltdx/ex/hosts.py"
if ex_hosts_py.exists():
    txt = ex_hosts_py.read_text(encoding="utf-8", errors="ignore")
    for m in IPRE.finditer(txt):
        host = m.group(1)
        port = int(m.group(2)) if m.group(2) and 20 <= int(m.group(2)) <= 65535 else 7727
        add(host, port, "eltdx.EX", "ex")

# tdxrs server pool
for p in RESEARCH.rglob("*.rs"):
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    hits = IPRE.findall(txt)
    if len(hits) >= 3:
        for host, port in hits:
            add(host, int(port) if port and 20 <= int(port) <= 65535 else 7709, f"tdxrs:{p.name}")

# ---------- 输出 ----------
records = sorted(census.values(), key=lambda r: (r["port"], r["host"]))
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

print(f"total unique endpoints: {len(records)}")
by_port: dict[int, int] = {}
for r in records:
    by_port[r["port"]] = by_port.get(r["port"], 0) + 1
print("by port:", dict(sorted(by_port.items())))
for r in records[:8]:
    print(r["host"], r["port"], r["kinds"], r["sources"])
print("written:", OUT)
