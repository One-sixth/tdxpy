# cython: language_level=3
import struct
from pathlib import Path

from tdxpy.writer.base_writer import BaseWriter


class TdxLCMinBarWriter(BaseWriter):
    """
    写入通达信分钟线（vipdoc/{sh,sz,bj}/minline/*.lc1、fzline/*.lc5 及 ds/ 期货分钟线）

    每条 32 字节，与 TdxLCMinBarReader 对应（market 双模式）：
      date(u16: (y-2004)*2048+m*100+d) | time(u16: h*60+m) | open,high,low,close(f32)
      | 20~23 字节 | volume(u32) | reserved(u32=0)

      market="stock"（默认）: 20~23 = amount 成交额 (f32, 单位元)
      market="futures":       20~23 = vol_in_stock 持仓量 (u32, 单位手)

    注意：日期编码年份下限 2004（编码空间至约 2035 年）。
    """

    FMT_STOCK = "<HHfffffII"
    FMT_FUTURES = "<HHffffIII"

    def __init__(self, market="stock"):
        """
        :param market: "stock"(默认) 或 "futures"
        """
        if market not in ("stock", "futures"):
            raise ValueError(f"market 仅支持 'stock'/'futures'，收到 {market!r}")
        self.market = market

    def _encode_row(self, dt, o, h, l, c, extra, volume):
        """
        DataFrame 行 → 结构体元组
        :param extra: stock=amount(元, float)；futures=vol_in_stock(手, int)
        """
        date_u16 = (dt.year - 2004) * 2048 + dt.month * 100 + dt.day
        time_u16 = dt.hour * 60 + dt.minute
        if self.market == "stock":
            return (date_u16, time_u16, float(o), float(h), float(l), float(c), float(extra), int(volume), 0)

        return (date_u16, time_u16, float(o), float(h), float(l), float(c), int(extra), int(volume), 0)

    def _last_datetime_key(self, filename):
        """读已有文件最后一条的 (date_u16, time_u16)"""
        size = Path(filename).stat().st_size
        with open(filename, "rb") as f:
            f.seek(max(0, size - 32))
            rec = f.read(32)

        return struct.unpack_from("<HH", rec, 0)

    def write_df(self, df, filename, append=False):
        """
        写入 DataFrame
        :param df: index 为 DatetimeIndex，列 open/high/low/close/amount/volume（stock）
                   或 open/high/low/close/vol_in_stock/volume（futures）
        :param filename: 目标 .lc1/.lc5 文件路径
        :param append: True=追加（要求 df 首条时间晚于文件最后时间）；False=覆盖写
        :return: 写入条数
        """
        if append and Path(filename).is_file():
            last_date, last_time = self._last_datetime_key(filename)
            first_dt = df.index[0]
            first_key = ((first_dt.year - 2004) * 2048 + first_dt.month * 100 + first_dt.day, first_dt.hour * 60 + first_dt.minute)
            if first_key <= (last_date, last_time):
                raise ValueError(f"append 数据首条 {first_key} 不晚于文件最后时间 {(last_date, last_time)}，拒绝乱序写入")

        fmt = self.FMT_STOCK if self.market == "stock" else self.FMT_FUTURES
        extra_col = "amount" if self.market == "stock" else "vol_in_stock"
        rows = [
            self._encode_row(idx, row.open, row.high, row.low, row.close, row[extra_col], row.volume)
            for idx, row in df.iterrows()
        ]
        payload = self.pack_records(fmt, rows)
        self._ensure_parent(filename)
        with open(filename, "ab" if append else "wb") as f:
            f.write(payload)

        return len(rows)
