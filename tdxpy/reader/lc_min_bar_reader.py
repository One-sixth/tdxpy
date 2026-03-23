# cython: language_level=3
from collections import OrderedDict
from pathlib import Path

import pandas as pd

from tdxpy.reader.base_reader import BaseReader
from tdxpy.reader.base_reader import TdxFileNotFoundException


class TdxLCMinBarReader(BaseReader):
    """
    读取通达信分钟数据（vipdoc/{sh,sz,bj}/minline/*.lc1、fzline/*.lc5，
    及 vipdoc/ds/minline|fzline/*.lc1|lc5 期货分钟线）

    每条 32 字节，低字节在前：
      00~01 日期 (u16, y*2048+m*100+d, 年份+2004)
      02~03 时间 (u16, h*60+m)
      04~07 开盘 (f32)    08~11 最高 (f32)
      12~15 最低 (f32)    16~19 收盘 (f32)
      20~23 字节依品种而异（2026-09 与 tqcenter API 逐值对照实测）：
        - 股票/基金等标准市场：amount 成交额 (f32, 单位元)  → market="stock"（默认）
        - 期货/扩展市场：vol_in_stock 持仓量 (u32, 单位手)   → market="futures"
      24~27 成交量 (u32, 股票单位股/期货单位手)
      28~31 保留 (恒 0)
    """

    def __init__(self, vipdoc_path=None, market="stock"):
        """
        :param vipdoc_path: tdx 的 vipdoc 目录
        :param market: "stock"(默认, 20~23字节为成交额f32) 或 "futures"(为持仓量u32)
        """
        super().__init__(vipdoc_path)
        self.market = market

    def parse_data_by_file(self, filename, market=None):
        """
        :param filename: .lc1/.lc5 文件路径
        :param market: None=用构造时的 self.market
        :return: list[OrderedDict]
        """
        if not Path(filename).is_file():
            raise TdxFileNotFoundException(f"no tdx kline data, please check path {filename}")

        if market is None:
            market = self.market
        # 20~23 字节类型不同：股票=成交额f32；期货=持仓量u32（其余字段一致）
        fmt = "<HHfffffII" if market == "stock" else "<HHffffIII"
        content = Path(filename).read_bytes()
        raw_li = self.unpack_records(fmt, content)

        data = []
        for row in raw_li:
            year, month, day = self._parse_date(row[0])
            hour, minute = self._parse_time(row[1])

            item = OrderedDict(
                [
                    ("date", f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"),
                    ("year", year),
                    ("month", month),
                    ("day", day),
                    ("hour", hour),
                    ("minute", minute),
                    ("open", row[2]),
                    ("high", row[3]),
                    ("low", row[4]),
                    ("close", row[5]),
                ]
            )
            if market == "stock":
                item["amount"] = row[6]
            else:
                item["vol_in_stock"] = row[6]
            item["volume"] = row[7]
            data.append(item)

        return data

    def get_df(self, code_or_file, market=None, **kwargs):
        """
        :param code_or_file: 文件路径（或代码，配合 vipdoc_path）
        :param market: "stock"/"futures"，None=用构造时的 self.market
        :return: pd.DataFrame，index=datetime
        """
        df = pd.DataFrame(data=self.parse_data_by_file(code_or_file, market))
        df.index = pd.to_datetime(df.date)
        cols = ["open", "high", "low", "close", "amount", "volume"]
        if (market or self.market) == "futures":
            cols = ["open", "high", "low", "close", "vol_in_stock", "volume"]

        return df[cols]
