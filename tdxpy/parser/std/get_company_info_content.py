# cython: language_level=3
import struct

from tdxpy.parser.base import BaseParser


class GetCompanyInfoContent(BaseParser):
    def setParams(self, market, code, filename, start, length):
        """

        :param market:
        :param code:
        :param filename: 分类响应里的数据文件名——**bytes 原样回传**（GBK 原始
            字节，推荐走 `filename_raw`）；str 兼容路径按 GBK 编码
        :param start:
        :param length:
        """
        if type(code) is str:
            code = code.encode("gbk")  # 服务器期望 GBK 字节（UTF-8 往返有损耗风险）

        if type(filename) is str:
            filename = filename.encode("gbk")  # bytes（filename_raw）直通不编码

        if len(filename) > 80:
            filename = filename[:80]  # 截断守卫（对齐 tdxrs f10_client.rs truncate；超长会让 struct.error 畸形包）

        if len(filename) != 80:
            filename = filename.ljust(80, b"\x00")

        pkg = bytearray.fromhex("0c 07 10 9c 00 01 68 00 68 00 d0 02")
        pkg.extend(struct.pack("<H6sH80sIII", market, code, 0, filename, start, length, 0))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        pos = 0

        _, length = struct.unpack("<10sH", body_buf[:12])

        pos += 12

        content = body_buf[pos : pos + length]
        # replace：F10 是给人读的文本，生僻字可见（\ufffd）好过静默丢字
        content = content.decode("gbk", "replace")

        return content
