# cython: language_level=3

# -- 分段错误码常量（吸收 tdxrs error_codes.rs 分段表，轻量版：类默认 + 抛出点覆盖）--
# 1200（限流）/3000（解析）/4000（文件）段暂无对应抛出点：tdxpy 限流是 wait 阻塞、
# 解析失败静默返回空——待行为变化时再分配（2026-11-09 抛出点普查结论）。

CODE_GENERIC = 1000
"""通用/参数段（ValidationException 默认）。"""

CODE_CONNECTION = 2000
"""连接段（TdxConnectionError 默认）：连接失败/超时/断线。"""

CODE_POOL_EXHAUSTED = 2050
"""连接段子码（tdxpy 特有）：服务器池/选优全灭，无可用服务器。"""

CODE_PROTOCOL = 2100
"""协议段（TdxFunctionCallError 默认）：命令执行失败/被拒。"""


class TdxConnectionError(Exception):
    """
    当连接服务器出错的时候，会抛出的异常

    :ivar code: 分段错误码（默认 :data:`CODE_CONNECTION`；池/选优全灭抛出点
        传 :data:`CODE_POOL_EXHAUSTED`）
    :ivar server: 出错服务器 ``(host, port)``；池调度场景下由抛出点附带
    """

    def __init__(self, *args, code=CODE_CONNECTION, server=None):
        super().__init__(*args)
        self.code = code
        self.server = server


class TdxFunctionCallError(Exception):
    """
    当行数调用出错的时候

    :ivar original_exception: 底层原始异常（包装路径由抛出点赋值）
    :ivar code: 分段错误码（默认 :data:`CODE_PROTOCOL`）
    :ivar server: 出错服务器 ``(host, port)``；池调度场景下由抛出点附带
    """

    def __init__(self, *args, code=CODE_PROTOCOL, server=None):
        super().__init__(*args)
        self.original_exception = None
        self.code = code
        self.server = server


class ValidationException(Exception):
    """
    参数校验错误

    :ivar code: 分段错误码（默认 :data:`CODE_GENERIC`）
    :ivar server: 出错服务器 ``(host, port)``；参数类异常通常为 None
    """

    def __init__(self, *args, code=CODE_GENERIC, server=None):
        super().__init__(*args)
        self.code = code
        self.server = server
