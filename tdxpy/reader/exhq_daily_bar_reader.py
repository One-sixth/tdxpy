# cython: language_level=3
import struct
from pathlib import Path

import pandas as pd

from tdxpy.reader.base_reader import BaseReader
from tdxpy.reader.base_reader import TdxFileNotFoundException


class TdxExHqDailyBarReader(BaseReader):
    """
    读取通达信扩展市场日线（vipdoc/ds/lday/*.day，期货/中证指数/基金等）

    每条 32 字节，低字节在前（2026-09 与 tqcenter API 逐值对照实测）：
      00~03 日期 (u32, YYYYMMDD)
      04~07 开盘 (f32)   08~11 最高 (f32)
      12~15 最低 (f32)   16~19 收盘 (f32)
      20~23 vol_in_stock 持仓量 (u32, 期货单位手；非期货品种该列可能无意义)
      24~27 volume 成交量 (u32)
      28~31 jiesuan 结算价 (f32)

    注意：扩展市场日线没有成交额字段（tqcenter API 对期货 Amount 恒为 0），
    历史版本此处误标为 amount 并附带 hk_stock_amount 垃圾列，已修正。
    """

    def parse_data_by_file(self, filename):
        """
        按文件名解析内容
        :param filename:
        :return:
        """
        if not Path(filename).is_file():
            raise TdxFileNotFoundException(f"no tdx kline data, please check path {filename}")

        content = Path(filename).read_bytes()
        content = self.unpack_records("<IffffIIf", content)  # noqa

        return content

    def get_df(self, code_or_file, **kwargs):
        """
        转换 pd.DataFrame 格式
        :param code_or_file:
        :param kwargs:
        :return:
        """
        columns = ["date", "open", "high", "low", "close", "vol_in_stock", "volume", "jiesuan"]  # noqa
        data = [self._df_convert(row) for row in self.parse_data_by_file(code_or_file)]

        df = pd.DataFrame(data=data, columns=columns)
        df.index = pd.to_datetime(df.date)
        df = df[["open", "high", "low", "close", "vol_in_stock", "volume", "jiesuan"]]  # noqa

        return df

    @staticmethod
    def _df_convert(row):
        """
        原始行 → (datestr, open, high, low, close, vol_in_stock, volume, jiesuan)
        :param row:
        :return:
        """
        t_date = str(row[0])
        datestr = t_date[:4] + "-" + t_date[4:6] + "-" + t_date[6:]

        return (datestr, row[1], row[2], row[3], row[4], row[5], row[6], row[7])
