# cython: language_level=3
import struct
from pathlib import Path

from tdxpy.writer.base_writer import BaseWriter


class TdxExHqDailyBarWriter(BaseWriter):
    """
    写入通达信扩展市场日线（vipdoc/ds/lday/*.day，期货/指数/基金等）

    每条 32 字节 `<IffffIIf`，与 TdxExHqDailyBarReader 对应：
      date(u32 YYYYMMDD) | open,high,low,close(f32)
      | vol_in_stock 持仓量(u32) | volume(u32) | jiesuan 结算价(f32)

    注意：扩展市场日线无成交额字段（通达信不提供，API Amount 恒 0）。
    """

    FMT = "<IffffIIf"

    def _encode_row(self, dt, o, h, l, c, vol_in_stock, volume, jiesuan):
        date_num = dt.year * 10000 + dt.month * 100 + dt.day
        return (
            date_num,
            float(o),
            float(h),
            float(l),
            float(c),
            int(vol_in_stock),
            int(volume),
            float(jiesuan),
        )

    def _last_date_num(self, filename):
        size = Path(filename).stat().st_size
        with open(filename, "rb") as f:
            f.seek(max(0, size - 32))
            rec = f.read(32)

        return struct.unpack_from("<I", rec, 0)[0]

    def write_df(self, df, filename, append=False):
        """
        写入 DataFrame
        :param df: index 为 DatetimeIndex，列 open/high/low/close/vol_in_stock/volume/jiesuan
        :param filename: 目标 .day 文件路径
        :param append: True=追加（要求日期递增）；False=覆盖写
        :return: 写入条数
        """
        if append and Path(filename).is_file():
            last = self._last_date_num(filename)
            first_dt = df.index[0]
            first_num = first_dt.year * 10000 + first_dt.month * 100 + first_dt.day
            if first_num <= last:
                raise ValueError(f"append 数据首日 {first_num} 不晚于文件最后日期 {last}，拒绝乱序写入")

        rows = [
            self._encode_row(idx, row.open, row.high, row.low, row.close, row.vol_in_stock, row.volume, row.jiesuan)
            for idx, row in df.iterrows()
        ]
        payload = self.pack_records(self.FMT, rows)
        self._ensure_parent(filename)
        with open(filename, "ab" if append else "wb") as f:
            f.write(payload)

        return len(rows)
