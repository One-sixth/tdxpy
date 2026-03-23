# cython: language_level=3
import struct


class BaseWriter:
    """通达信二进制数据文件写入基类，与 BaseReader 对称"""

    @staticmethod
    def pack_records(fmt, rows):
        """
        按结构体格式批量打包
        :param fmt: struct 格式串（单条记录）
        :param rows: 可迭代的元组序列
        :return: bytes
        """
        record = struct.Struct(fmt)
        return b"".join(record.pack(*row) for row in rows)

    @staticmethod
    def _ensure_parent(filename):
        """确保父目录存在"""
        from pathlib import Path

        Path(filename).parent.mkdir(parents=True, exist_ok=True)
