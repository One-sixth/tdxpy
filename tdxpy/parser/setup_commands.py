# cython: language_level=3
import struct

from tdxpy.parser.base import BaseParser


class BaseSetup(BaseParser):
    def parseResponse(self, body_buf):
        """
        解析返回结果
        :param body_buf:
        :return:
        """
        return body_buf


class SetupCmd1(BaseSetup):
    def setup(self):
        self.send_pkg = bytearray.fromhex("0c 02 18 93 00 01 03 00 03 00 0d 00 01")


class SetupCmd2(BaseSetup):
    def setup(self):
        self.send_pkg = bytearray.fromhex("0c 02 18 94 00 01 03 00 03 00 0d 00 02")


class SetupCmd3(BaseSetup):
    def setup(self):
        self.send_pkg = bytearray.fromhex(
            "0c 03 18 99 00 01 20 00 20 00 db 0f d5 d0"
            "c9 cc d6 a4 a8 af 00 00 00 8f c2 25 40 13"
            "00 00 d5 00 c9 cc bd f0 d7 ea 00 00 00 02"
        )


class ExSetupCmd1(BaseParser):  # noqa
    def setup(self):
        self.send_pkg = bytearray.fromhex(
            "01 01 48 65 00 01 52 00 52 00 54 24 1f 32 c6 e5"
            "d5 3d fb 41 1f 32 c6 e5 d5 3d fb 41 1f 32 c6 e5"
            "d5 3d fb 41 1f 32 c6 e5 d5 3d fb 41 1f 32 c6 e5"
            "d5 3d fb 41 1f 32 c6 e5 d5 3d fb 41 1f 32 c6 e5"
            "d5 3d fb 41 1f 32 c6 e5 d5 3d fb 41 cc e1 6d ff"
            "d5 ba 3f b8 cb c5 7a 05 4f 77 48 ea"
        )


class NewHandshakeCmd(BaseSetup):
    """2026+ 新式握手（标准市场 7709）。

    单条 cmd=0x000D、payload=[0x01]，等价于 eltdx HandshakeRequest 冻结字节
    （0c 01 00 00 00 01 03 00 03 00 0d 00 01）。实测：现代服务器在老三条 SETUP
    会话下 K 线命令(0x052D)返回空，必须用本握手才能取到 K 线数据；
    分时/历史分时/分笔等老构包命令在新握手会话下不受影响。
    """

    def setup(self):
        # 0c | msg_id=1 | control=01 | len=3 | len=3 | cmd=0x000D | payload=[0x01]
        self.send_pkg = bytearray.fromhex("0c 01 00 00 00 01 03 00 03 00 0d 00 01")


class ExLoginCmd(BaseSetup):
    """扩展市场（7727）2026+ 新版登录。

    EX 独立帧头 struct.pack("<BIBHH", head_flag=0x01, customize=0, version=1, zipsize, unzipsize)
    + inner（cmd u16=0x2454 + 80 字节登录 body）。字节来源：easy_tdx MacExLoginCmd
    （参考 opentdx 实现，2026-09 真实服务器实测 login 后 EX 全部命令恢复正常）。
    老 ExSetupCmd1 在现代服务器上已失效（无响应/命令返回空）。
    """

    LOGIN_BODY = bytes(bytearray.fromhex(
        "e5bb1c2fafe52594"
        "1f32c6e5d53dfb41"
        "5b734cc9cdbf0ac9"
        "2021bfdd1eb06d22"
        "d008884c1611cb13"
        "78f6abd824d899d2"
        "1f32c6e5d53dfb41"
        "1f32c6e5d53dfb41"
        "a9325ac935dc0837"
        "335a16e4ce17c1bb"
    ))

    def setup(self):
        inner = struct.pack("<H", 0x2454) + self.LOGIN_BODY
        header = struct.pack("<BIBHH", 0x01, 0, 1, len(inner), len(inner))
        self.send_pkg = header + inner
