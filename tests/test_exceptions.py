# -*- coding: utf-8 -*-
"""分段错误码单测（轻量版）：类默认 code / 抛出点覆盖 / server 属性 / args 透传。"""
import unittest

from tdxpy.exceptions import (
    CODE_CONNECTION,
    CODE_GENERIC,
    CODE_POOL_EXHAUSTED,
    CODE_PROTOCOL,
    TdxConnectionError,
    TdxFunctionCallError,
    ValidationException,
)


class TestErrorCodes(unittest.TestCase):
    def test_default_codes(self):
        """三类异常默认段：参数 1000 / 连接 2000 / 协议 2100。"""
        self.assertEqual(ValidationException("参数错").code, CODE_GENERIC)
        self.assertEqual(TdxConnectionError("连接错").code, CODE_CONNECTION)
        self.assertEqual(TdxFunctionCallError("调用错").code, CODE_PROTOCOL)

    def test_code_override(self):
        """抛出点可覆盖 code：池全灭 2050。"""
        exc = TdxConnectionError("池全灭", code=CODE_POOL_EXHAUSTED)
        self.assertEqual(exc.code, CODE_POOL_EXHAUSTED)

    def test_server_attr(self):
        """server 属性由抛出点附带（池调度场景定位故障节点）。"""
        exc = TdxConnectionError("该台挂了", server=("1.2.3.4", 7709))
        self.assertEqual(exc.server, ("1.2.3.4", 7709))
        self.assertIsNone(TdxConnectionError("x").server)

    def test_args_and_original_untouched(self):
        """位置参数透传（str(exc) 可读）；original_exception 兼容保持。"""
        exc = TdxFunctionCallError("calling function error")
        self.assertEqual(str(exc), "calling function error")
        self.assertIsNone(exc.original_exception)


if __name__ == "__main__":
    unittest.main()
