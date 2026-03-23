# cython: language_level=3
import struct
from pathlib import Path

from tdxpy.writer.base_writer import BaseWriter


class TdxDailyBarWriter(BaseWriter):
    """
    写入通达信日线数据（vipdoc/{sh,sz,bj}/lday/*.day，A股/指数/基金/债券，北交所同格式）

    每条 32 字节 `<IIIIIfII`，与 TdxDailyBarReader 对应：
      date(u32 YYYYMMDD) | open,high,low,close(u32, ×100) | amount(f32, 单位元) | volume(u32, 股) | rsv(u32=0)

    输入 df 的单位约定（与 TdxDailyBarReader.get_df 输出对称）：
      - open/high/low/close：元（写时 ×100 编码）
      - amount：元（f32 直写）
      - volume：手（写时 ×100 转股编码；reader 读出时 ×0.01 还原为手）
        注意 A 股存在零股交易，原始股数可能非整百，×0.01 后会带伪小数

    28~31 字节 rsv：写 0（该字节在新格式下为写入批次标志，详见 TdxDailyBarReader docstring）
    """

    FMT = "<IIIIIfII"

    def _encode_row(self, dt, o, h, l, c, amount, volume):
        """
        DataFrame 行 → 结构体元组
        :param dt: datetime（来自 index）
        """
        date_num = dt.year * 10000 + dt.month * 100 + dt.day
        return (
            date_num,
            int(round(o * 100)),
            int(round(h * 100)),
            int(round(l * 100)),
            int(round(c * 100)),
            float(amount),
            int(round(volume * 100)),
            0,
        )

    def _last_date_num(self, filename):
        """读已有文件的最后一条日期（YYYYMMDD int）"""
        size = Path(filename).stat().st_size
        with open(filename, "rb") as f:
            f.seek(max(0, size - 32))
            rec = f.read(32)

        return struct.unpack_from("<I", rec, 0)[0]

    def write_df(self, df, filename, append=False):
        """
        写入 DataFrame
        :param df: index 为 DatetimeIndex，列 open/high/low/close/amount/volume
        :param filename: 目标 .day 文件路径
        :param append: True=追加到文件末尾（要求 df 首行日期晚于已有最后日期）；False=覆盖写
        :return: 写入条数
        """
        if append and Path(filename).is_file():
            last = self._last_date_num(filename)
            first_dt = df.index[0]
            first_num = first_dt.year * 10000 + first_dt.month * 100 + first_dt.day
            if first_num <= last:
                raise ValueError(f"append 数据首日 {first_num} 不晚于文件最后日期 {last}，拒绝乱序写入")

        rows = [
            self._encode_row(idx, row.open, row.high, row.low, row.close, row.amount, row.volume)
            for idx, row in df.iterrows()
        ]
        payload = self.pack_records(self.FMT, rows)
        self._ensure_parent(filename)
        with open(filename, "ab" if append else "wb") as f:
            f.write(payload)

        return len(rows)
