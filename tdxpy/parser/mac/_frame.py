# cython: language_level=3
"""MAC 通道请求帧构造（10B 头 + cmd u16 + body；自 pytdxdata mac_frame.py 移植）。"""
import struct

MAC_HEAD_FLAG = 0x1C


def build_mac_frame(cmd, body):
    """MAC 请求帧：[flag 0x1C][customize 0][version 1][zipsize u16][unzipsize u16][cmd u16][body]。

    MAC 请求不压缩，zipsize == unzipsize == len(cmd) + len(body)。
    """
    inner = struct.pack("<H", cmd) + body
    return struct.pack("<BIBHH", MAC_HEAD_FLAG, 0, 1, len(inner), len(inner)) + inner


# ---------------------------------------------------------------------------
# MAC EX 通道（0x2454 login + 帧首字节 0x1C→0x01；港股/美股/期货/指数扩展行情）
# ---------------------------------------------------------------------------

EX_HEAD_FLAG = 0x01

#: 80 字节 login body（0x2454 登录载荷，冻结字节；opentdx/easy-tdx/pytdxdata 三源一致）
EX_LOGIN_BODY = bytes(
    bytearray.fromhex(
        "e5bb1c2fafe52594" "1f32c6e5d53dfb41" "5b734cc9cdbf0ac9"
        "2021bfdd1eb06d22" "d008884c1611cb13" "78f6abd824d899d2"
        "1f32c6e5d53dfb41" "1f32c6e5d53dfb41" "a9325ac935dc0837"
        "335a16e4ce17c1bb"
    )
)

EX_LOGIN_CMD = 0x2454

#: MAC/EX 帧 10B 头长度（<BIBHH；EX 帧与 MAC 帧仅首字节 flag 不同，其余全同）
MAC_FRAME_HEADER_SIZE = struct.calcsize("<BIBHH")


def repack_ex_frame(mac_frame):
    """把已组好的 MAC 帧改写为 EX 模式：仅首字节 0x1C→0x01，载荷逐字节不动。

    EX 帧与 MAC 帧的唯一差异是 flag 字节（customize/version/zipsize/unzipsize/
    cmd/body 全同）——比拆解重组更简单也更不易错。parser 子类在 super().setParams()
    组好 MAC 帧后调用本函数一步转换。
    """
    return bytes([EX_HEAD_FLAG]) + mac_frame[1:]


def build_ex_frame(cmd, body):
    """MAC EX 请求帧：与 build_mac_frame 同构，仅 flag 0x1C→0x01。

    扩展行情服务器（7727）要求先 login（0x2454），此后所有请求帧首字节为 0x01
    （2026-10-19 在线实测：0x1C 帧无响应，改写后 0x122D/E/F 全通）。
    """
    inner = struct.pack("<H", cmd) + body
    return struct.pack("<BIBHH", EX_HEAD_FLAG, 0, 1, len(inner), len(inner)) + inner


def build_ex_login_frame():
    """EX 通道登录帧（0x2454 + 80B 载荷；响应非空即成功）。"""
    return build_ex_frame(EX_LOGIN_CMD, EX_LOGIN_BODY)
