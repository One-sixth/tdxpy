# cython: language_level=3
from pathlib import Path

import pandas as pd

from tdxpy.logger import logger
from tdxpy.reader.base_reader import BaseReader
from tdxpy.reader.base_reader import TdxFileNotFoundException
from tdxpy.reader.base_reader import TdxNotAssignVipdocPathException


class TdxDailyBarReader(BaseReader):
    """
    读取通达信日线数据（vipdoc/{sh,sz,bj}/lday/*.day，A股/指数/基金/债券，北交所同格式）

    每条 32 字节 `<IIIIIfII`（低字节在前）：
      date(u32 YYYYMMDD) | open,high,low,close(u32, ×100) | amount(f32, 单位元) | volume(u32, 股) | rsv(u32)

    28~31 字节 rsv 考古结论（2026-09-15 全历史扫描 + API 除权对照，详见知识库《数据获取经验.md》）：
      - 不是除权/股本标志（跨市场翻转日同步、与除权日零重合）
      - 老数据段（~2010）：lo u16 = 前收盘×100（上市首日 = 发行价×100），hi=0
      - 新数据段：仅 65536(hi=1)/0 两值，为写入批次标志，无解析价值
    """

    # 交易所（bj=北交所，2026-09-27 补：vipdoc bj\lday 同 32 字节格式，系数对齐管道实测）
    SECURITY_EXCHANGE = ["sz", "sh", "bj"]

    # 股票类型
    SECURITY_TYPE = [
        "SH_A_STOCK",
        "SH_B_STOCK",
        "SH_INDEX",
        "SH_FUND",
        "SH_BOND",
        "SZ_A_STOCK",
        "SZ_B_STOCK",
        "SZ_INDEX",
        "SZ_FUND",
        "SZ_BOND",
        "BJ_STOCK",
        "BJ_BOND",
        "BJ_PREFERRED",
        "BJ_INDEX",
    ]

    # todo 股票系数
    SECURITY_COEFFICIENT = {
        "SH_A_STOCK": [0.01, 0.01],
        "SH_B_STOCK": [0.001, 0.01],
        "SH_INDEX": [0.01, 1.0],
        "SH_FUND": [0.001, 1.0],
        "SH_BOND": [0.001, 1.0],
        "SZ_A_STOCK": [0.01, 0.01],
        "SZ_B_STOCK": [0.01, 0.01],
        "SZ_INDEX": [0.01, 1.0],
        "SZ_FUND": [0.001, 0.01],
        "SZ_BOND": [0.001, 0.01],
        "BJ_STOCK": [0.01, 0.01],
        # 2026-10-19 登记：810 定向可转债 / 82 优先股，vipdoc raw×10000（贴面值 100），vol/amount 口径与在线一致
        "BJ_BOND": [0.0001, 1.0],
        "BJ_PREFERRED": [0.0001, 1.0],
        "BJ_INDEX": [0.01, 1.0],
    }

    def generate_filename(self, code, exchange):
        """

        :param code:
        :param exchange:
        :return:
        """
        if not self.vipdoc_path:
            raise TdxNotAssignVipdocPathException(r"Please provide a vipdoc path , such as c:\\newtdx\\vipdoc")  # noqa

        filename = Path(self.vipdoc_path, exchange, "lday", f"{exchange}{code}.day")  # noqa

        return str(filename)

    def get_kline_by_code(self, code, exchange):
        """

        :param code:
        :param exchange:
        :return:
        """
        filename = self.generate_filename(code, exchange)

        return self.parse_data_by_file(filename)

    def parse_data_by_file(self, filename):
        """

        :param filename:
        :return:
        """
        if not Path(filename).is_file():
            raise TdxFileNotFoundException("no tdx kline data, please check path %s", filename)

        content = Path(filename).read_bytes()
        content = self.unpack_records("<IIIIIfII", content)

        return content

    def get_df(self, code_or_file, exchange=None):
        """

        :param code_or_file:
        :param exchange:
        :return:
        """
        if not exchange:
            return self.get_df_by_file(code_or_file)

        return self.get_df_by_code(code_or_file, exchange)

    def get_df_by_file(self, filename):
        """
        通过文件名读数据
        :param filename:
        :return:
        """
        if not Path(filename).is_file():
            raise TdxFileNotFoundException("no tdx kline data, please check path %s", filename)

        security_type = self.get_security_type(filename)

        if security_type not in self.SECURITY_TYPE:
            logger.exception("Unknown security type !\n")
            raise NotImplementedError

        coefficient = self.SECURITY_COEFFICIENT[security_type]
        data = [self._df_convert(row_, coefficient) for row_ in self.parse_data_by_file(filename)]

        df = pd.DataFrame(data=data, columns=["date", "open", "high", "low", "close", "amount", "volume"])
        df.index = pd.to_datetime(df.date, errors="coerce")
        # df.index = pd.to_datetime(df.date)

        return df[["open", "high", "low", "close", "amount", "volume"]]

    def get_df_by_code(self, code, exchange):
        """
        通过股票代码生成文件名
        :param code: 股票代码
        :param exchange: 交易所
        :return:
        """
        name = self.generate_filename(code, exchange)

        return self.get_df_by_file(name)

    @staticmethod
    def _df_convert(row_, coefficient):
        """
        源数据转换

        :param row_:
        :param coefficient:
        :return:
        """
        t_date = str(row_[0])
        datestr = t_date[:4] + "-" + t_date[4:6] + "-" + t_date[6:]

        new_row = (
            datestr,
            row_[1] * coefficient[0],  # * 0.01 * 1000 , zipline need 1000 times to original price
            row_[2] * coefficient[0],
            row_[3] * coefficient[0],
            row_[4] * coefficient[0],
            row_[5],
            row_[6] * coefficient[1],
        )

        return new_row

    def get_security_type(self, filename):
        """
        获取股票类型, A股, B股, 指数等

        :param filename:
        :return:
        """
        logger.debug(f"get_security_type name: {filename}")
        basename = Path(filename).stem

        # exchange = str(name[-12:-10]).lower()
        # code_head = name[-10:-8]

        exchange = str(basename[:2]).lower()
        code_head = basename[2:4]

        if exchange == self.SECURITY_EXCHANGE[0]:
            if code_head in ["00", "30"]:
                return "SZ_A_STOCK"

            if code_head in ["20"]:
                return "SZ_B_STOCK"

            if code_head in ["39"]:
                return "SZ_INDEX"

            if code_head in ["15", "16", "18"]:  # 18=REITs（1801xx，与 helper 09-17 实测版对齐）
                return "SZ_FUND"

            if code_head in ["10", "11", "12", "13", "14"]:
                return "SZ_BOND"

        if exchange == self.SECURITY_EXCHANGE[1]:
            if code_head in ["60", "68"]:  # 688XXX科创板
                return "SH_A_STOCK"

            if code_head in ["90"]:
                return "SH_B_STOCK"

            if code_head in ["00", "88", "99"]:
                return "SH_INDEX"

            if code_head in ["50", "51", "52", "53", "55", "56", "58"]:  # 沪市基金全段（与 helper 09-17 实测版对齐；58=科创ETF）
                return "SH_FUND"

            if code_head in ["01", "10", "11", "12", "13", "14", "20"]:
                return "SH_BOND"

        if exchange == self.SECURITY_EXCHANGE[2]:
            if code_head in ["43", "83", "87", "88", "92"]:  # 北交所股票段（92 现役；与 helper 2026-10-19 规测版对齐）
                return "BJ_STOCK"

            # 2026-10-19 官方规测定性（北证公告〔2024〕20号）：810=定向可转债、820/82=优先股，
            # vipdoc raw×10000（贴面值 100）；80/811+ 政府债券不登记 raise 防御
            if basename[:5] == "bj810":
                return "BJ_BOND"

            if code_head == "82":
                return "BJ_PREFERRED"

            if code_head in ["89"]:  # 89 段指数（899050 北证50 / 899601，系数同 SH_INDEX）
                return "BJ_INDEX"

        logger.debug("Unknown security exchange !")
        raise NotImplementedError(
            f"未登记的 vipdoc 交易所/代码段：exchange={exchange!r} code_head={code_head!r}（文件 {filename}）。"
            "请在 TdxDailyBarReader.get_security_type 登记该段"
        )
