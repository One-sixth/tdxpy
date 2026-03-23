# cython: language_level=3
"""通达信官方历史财务数据（gpcw）下载与解析。

下载源为通达信官方 HTTP 站 ``data.tdx.com.cn/tdxfin/``（清单文件 gpcw.txt
含每包文件名、md5、大小；urllib 标准库下载，无第三方依赖）。

.. note::
   历史上财务包可经行情协议命令 ``get_report_file_by_size("tdxfin/...")`` 下载，
   2026-09-16 实测：所有实测服务器对 tdxfin/ 与 .zip 类文件一律返回 0 字节
   （tdxhy.cfg / block_gn.dat 等仍可拉），故改用官方 HTTP 站（与
   mootdx tools/DownloadTDXCaiWu.py 同源）。

解析逻辑改写自 mootdx financial 模块：

- 包为 zip，内含单个 .dat；
- .dat 头部 ``<1hI1H3L``（20 字节）：[1]=报告期、[2]=股票数 max_count、[4]=单股票报表字节数 report_size；
- 随后 max_count × ``<6s1c1L``（code + 占位 + 报表块绝对偏移 foa）；
- 每只股票在 foa 处存 ``<Nf`` 浮点报表（N = report_size / 4），
  列名对应 ``_financial_columns.FINANCIAL_COLUMNS``（中文，搬运自 mootdx）。

用法::

    from tdxpy.affair import Affair

    Affair.files()                                   # 远端清单
    Affair.fetch("gpcw20231231.zip", downdir="tmp")  # 下载（md5 校验）
    df = Affair.parse("gpcw20231231.zip", "tmp")     # DataFrame（index=code）
"""

import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path
from struct import calcsize, unpack

import pandas as pd

from tdxpy._financial_columns import columns as FINANCIAL_COLUMNS

#: 官方 HTTP 下载站
HTTP_BASE = "https://data.tdx.com.cn/tdxfin"

#: 清单文件名（相对 HTTP_BASE）
LIST_FILE = "gpcw.txt"

#: .dat 头部与条目结构
_HEADER_FMT = "<1hI1H3L"
_ITEM_FMT = "<6s1c1L"
_HEADER_SIZE = calcsize(_HEADER_FMT)
_ITEM_SIZE = calcsize(_ITEM_FMT)

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) tdxpy-affair"


def _http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _md5sum(path):
    digest = hashlib.md5()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Affair:
    """通达信官方历史财务数据（每季度一包，gpcwYYYYMMDD.zip）。

    三个静态方法即全部接口：files() 看有什么、fetch() 下载、parse() 解析。
    """

    @staticmethod
    def files(timeout=30):
        """远端财务文件清单。

        :return: list[dict]，元素含 filename / hash / filesize
        """
        text = _http_get(f"{HTTP_BASE}/{LIST_FILE}", timeout=timeout).decode("utf-8", errors="replace")

        rows = []
        for line in text.strip().splitlines():
            parts = line.strip().split(",")
            if len(parts) >= 3:
                rows.append({"filename": parts[0], "hash": parts[1], "filesize": int(parts[2])})
        return rows

    @staticmethod
    def fetch(filename, downdir="./"):
        """下载财务数据包（md5 校验，已存在且校验通过则跳过）。

        :param filename: 如 "gpcw20231231.zip"（先 files() 查看可用文件）
        :param downdir: 下载目录
        :return: 下载后的本地路径 Path
        """
        # 2026-09-16 父Agent 审核加固：filename 只允许单段文件名（防路径穿越/URL 注入）
        if Path(filename).name != filename or "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError(f"filename 须为纯文件名（如 'gpcw20231231.zip'），收到 {filename!r}")

        down_dir = Path(downdir)
        down_dir.mkdir(parents=True, exist_ok=True)
        target = down_dir / filename

        expected_md5 = None
        for item in Affair.files():
            if item["filename"] == filename:
                expected_md5 = item.get("hash")
                break

        if target.exists():
            if expected_md5 is None or _md5sum(target) == expected_md5:
                return target
            target.unlink()  # 校验不过，重新下载

        content = _http_get(f"{HTTP_BASE}/{filename}")
        target.write_bytes(content)

        actual = _md5sum(target)
        if expected_md5 is not None and actual != expected_md5:
            raise ValueError(f"财务包 md5 校验失败：{filename} 期望 {expected_md5} 实际 {actual}")

        return target

    @staticmethod
    def parse(filename, downdir="./"):
        """解析财务数据包为 DataFrame（不存在时自动先 fetch）。

        :param filename: 如 "gpcw20231231.zip"（.zip 或已解压的 .dat 均可）
        :param downdir: 文件所在目录
        :return: pd.DataFrame（index=code，columns=中文财务字段，首列 report_date）
        """
        filepath = Path(downdir) / filename
        if not filepath.exists():
            filepath = Affair.fetch(filename, downdir)

        if filepath.suffix.lower() == ".zip":
            with zipfile.ZipFile(filepath) as zf:
                dat_names = [name for name in zf.namelist() if name.lower().endswith(".dat")]
                if not dat_names:
                    raise ValueError(f"压缩包内没有 .dat 文件：{filepath}")
                data = zf.read(dat_names[0])
            dat = io.BytesIO(data)
        else:
            dat = open(filepath, "rb")

        try:
            header = unpack(_HEADER_FMT, dat.read(_HEADER_SIZE))
            max_count = header[2]
            report_size = header[4]
            field_count = report_size // 4

            names = list(FINANCIAL_COLUMNS[:field_count])
            if len(names) < field_count:
                names.extend(f"col_{i}" for i in range(len(names), field_count))

            records = {}
            for idx in range(max_count):
                dat.seek(_HEADER_SIZE + idx * _ITEM_SIZE)
                code_raw, _flag, foa = unpack(_ITEM_FMT, dat.read(_ITEM_SIZE))
                code = code_raw.decode("utf-8", errors="replace").strip("\x00 ")
                if not code:
                    continue
                dat.seek(foa)
                values = unpack(f"<{field_count}f", dat.read(field_count * 4))
                records[code] = values

            df = pd.DataFrame.from_dict(records, orient="index", columns=names)
            df.index.name = "code"
            return df
        finally:
            dat.close()
