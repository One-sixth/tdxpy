# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.logger import logger
from tdxpy.parser.base import BaseParser


class GetCompanyInfoCategory(BaseParser):
    def setParams(self, market, code):
        """
        设置参数
        :param market: 市场
        :param code: 代码
        """
        if type(code) is str:
            code = code.encode("gbk")  # 服务器期望 GBK 字节（UTF-8 往返有生僻字/截断风险）

        pkg = bytearray.fromhex("0c 0f 10 9b 00 01 0e 00 0e 00 cf 02")
        pkg.extend(struct.pack("<H6sI", market, code, 0))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """
        解析结果
        :param body_buf:
        :return:
        """
        pos = 0

        (num,) = struct.unpack("<H", body_buf[:2])

        pos += 2

        category = []

        def get_str(b):
            """
            :param b:
            :return:
            """
            p = b.find(b"\x00")

            if p != -1:
                b = b[0:p]

            try:
                # replace：F10 是给人读的文本，生僻字可见（\ufffd）好过静默丢字
                n = b.decode("gbk", "replace")
            except Exception as e:
                logger.exception(e)
                n = "unknown_str"

            return n

        for _ in range(num):
            name, filename, start, length = struct.unpack("<64s80sII", body_buf[pos : pos + 152])
            pos += 152

            # filename_raw：GBK 原始字节（trim 填充 null）。0x9C07 内容请求必须
            # 原样回传本字段，禁止经 str 编码往返（GBK→UTF-8→GBK 有损耗）。
            filename_raw = filename.rstrip(b"\x00")

            entry = OrderedDict(
                [
                    ("name", get_str(name)),
                    ("filename", get_str(filename)),
                    ("filename_raw", filename_raw),
                    ("start", start),
                    ("length", length),
                ]
            )
            category.append(entry)

        return category
