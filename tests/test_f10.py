# -*- coding: utf-8 -*-
"""F10 命令族单测（0x9B0F/0x9C07，全离线）。

覆盖 11 提案 2.3 验证的离线部分：filename_raw 原始字节保真（编码往返 bug
回归）、0x9C07 包布局（114B / filename 80B 段）、GBK replace 解码、门面
三方法（缓存 / raw 直通 / BJ+EX 拒绝 / 失败隔离）。
"""
import struct
import unittest

from tdxpy.client import TdxClient
from tdxpy.parser.std.get_company_info_category import GetCompanyInfoCategory
from tdxpy.parser.std.get_company_info_content import GetCompanyInfoContent


class _FakeClient:
    """parser 构造只需占位（单测直接调 setParams/parseResponse，不走 socket）。"""


def _make_category_parser():
    return GetCompanyInfoCategory(_FakeClient())


def _make_content_parser():
    return GetCompanyInfoContent(_FakeClient())


def _synth_category_body(entries):
    """合成 0x9B0F 响应 body。entries = [(name, filename_bytes, start, length)]。"""
    body = struct.pack("<H", len(entries))
    for name, filename, start, length in entries:
        body += struct.pack(
            "<64s80sII",
            name.encode("gbk").ljust(64, b"\x00"),
            filename.ljust(80, b"\x00"),
            start,
            length,
        )
    return body


def _synth_content_body(text_bytes):
    """合成 0x9C07 响应 body：12B 头（offset 10-11 = length u16 LE）+ GBK 文本。"""
    header = bytearray(12)
    struct.pack_into("<H", header, 10, len(text_bytes))
    return bytes(header) + text_bytes


class TestCategoryParser(unittest.TestCase):
    """0x9B0F 分类解析：filename_raw 保真是编码往返 bug 的回归锚。"""

    def test_parse_and_raw_passthrough(self):
        """filename_raw == 分类响应里的 GBK 原始字节（trim 填充 null）。"""
        raw = "公司概况2026".encode("gbk")
        body = _synth_category_body([("公司概况", raw, 0, 5120)])
        rows = _make_category_parser().parseResponse(body)
        self.assertEqual(len(rows), 1)
        e = rows[0]
        self.assertEqual(e["name"], "公司概况")
        self.assertEqual(e["filename"], "公司概况2026")
        self.assertIsInstance(e["filename_raw"], bytes)
        self.assertEqual(e["filename_raw"], raw)  # 原始字节，未经 str 往返
        self.assertEqual(e["start"], 0)
        self.assertEqual(e["length"], 5120)

    def test_raw_survives_non_gbk_roundtrip(self):
        """GBK 扩展区字节（str 解码有损）在 raw 中原样保留。"""
        # 0x81 0x40 是 GBK 扩展区合法编码（无对应 Unicode 单字，往返回不到原字节）
        raw = b"\x81\x40report.dat"
        body = _synth_category_body([("研究报告", raw, 1024, 2048)])
        rows = _make_category_parser().parseResponse(body)
        self.assertEqual(rows[0]["filename_raw"], raw)

    def test_code_gbk_encoded_in_request(self):
        """分类请求 code 段按 GBK 编码拼包。"""
        p = _make_category_parser()
        p.setParams(1, "600519")
        self.assertEqual(p.send_pkg[:12], bytes.fromhex("0c 0f 10 9b 00 01 0e 00 0e 00 cf 02"))
        self.assertEqual(p.send_pkg[14:20], b"600519")


class TestContentParser(unittest.TestCase):
    """0x9C07 内容请求包布局与响应解码。"""

    def test_request_layout_raw_passthrough(self):
        """raw bytes filename 原样进包（114B 总长、filename 段 offset 22-102）。"""
        raw = "公司概况.dat".encode("gbk")
        p = _make_content_parser()
        p.setParams(1, "600519", raw, 0, 5120)
        self.assertEqual(len(p.send_pkg), 114)
        self.assertEqual(p.send_pkg[:12], bytes.fromhex("0c 07 10 9c 00 01 68 00 68 00 d0 02"))
        self.assertEqual(p.send_pkg[14:20], b"600519")
        self.assertEqual(p.send_pkg[22:102], raw.ljust(80, b"\x00"))
        start, length = struct.unpack_from("<II", p.send_pkg, 102)
        self.assertEqual((start, length), (0, 5120))

    def test_request_layout_str_compat(self):
        """str filename 兼容路径：按 GBK 编码（修复前是 UTF-8）。"""
        p = _make_content_parser()
        p.setParams(0, "000001", "gsgk.dat", 10, 20)
        self.assertEqual(p.send_pkg[22:102], "gsgk.dat".encode("gbk").ljust(80, b"\x00"))

    def test_decode_replace_keeps_visible(self):
        """坏字节解码 replace：可见占位符（\\ufffd）而非静默丢字。"""
        body = _synth_content_body("正常文字".encode("gbk") + b"\xff\xfe" + "后续".encode("gbk"))
        text = _make_content_parser().parseResponse(body)
        self.assertIn("正常文字", text)
        self.assertIn("后续", text)
        self.assertIn("\ufffd", text)


class _FakeFacadeClient(TdxClient):
    """门面单测：只 mock _hq_call，不打网络。"""

    def __init__(self, entries, content="正文内容", fail_names=()):
        super().__init__(host="127.0.0.1")
        self._entries = entries
        self._content = content
        self._fail_names = set(fail_names)
        self.hq_calls = []

    def _hq_call(self, method, *args, **kwargs):
        self.hq_calls.append((method, args))
        if method == "get_company_info_category":
            return self._entries
        # get_company_info_content：args = (market_id, code, filename_raw, start, length)
        for e in self._entries:
            if e["filename_raw"] == args[2]:
                if e["name"] in self._fail_names:
                    raise TdxFunctionCallError("mock 网络点")
                return self._content
        raise AssertionError(f"未知的 filename_raw：{args[2]!r}")


from tdxpy.exceptions import TdxFunctionCallError  # noqa: E402 —— _FakeFacadeClient 用


ENTRIES = [
    {
        "name": "公司概况",
        "filename": "公司概况.dat",
        "filename_raw": "公司概况.dat".encode("gbk"),
        "start": 0,
        "length": 5120,
    },
    {
        "name": "财务分析",
        "filename": "财务分析.dat",
        "filename_raw": "财务分析.dat".encode("gbk"),
        "start": 5120,
        "length": 10240,
    },
]


class TestFacade(unittest.TestCase):
    """门面三方法：缓存 / raw 直通 / 边界拒绝 / 失败隔离。"""

    def setUp(self):
        self.client = _FakeFacadeClient(ENTRIES, content="mock 正文")

    def test_categories_df(self):
        """DataFrame 列齐全、filename_raw 不进 DF、size_kb 换算。"""
        df = self.client.f10_categories("sh.600519")
        self.assertEqual(list(df["name"]), ["公司概况", "财务分析"])
        self.assertNotIn("filename_raw", df.columns)
        self.assertEqual(list(df["size_kb"]), [5.0, 10.0])
        self.assertEqual(list(df["start"]), [0, 5120])

    def test_category_cache_single_call(self):
        """分类列表 TTL 缓存：连续两次门面调用只发一次 0x9B0F。"""
        self.client.f10_categories("sh.600519")
        self.client.f10_content("sh.600519", "公司概况")
        cat_calls = [c for c in self.client.hq_calls if c[0] == "get_company_info_category"]
        self.assertEqual(len(cat_calls), 1)

    def test_content_raw_passthrough(self):
        """f10_content 传给底层的 filename 是原始 bytes（编码 bug 回归锚）。"""
        text = self.client.f10_content("sh.600519", "财务分析")
        self.assertEqual(text, "mock 正文")
        method, args = self.client.hq_calls[-1]
        self.assertEqual(method, "get_company_info_content")
        self.assertIsInstance(args[2], bytes)
        self.assertEqual(args[2], "财务分析.dat".encode("gbk"))
        self.assertEqual(args[3:], (5120, 10240))  # start/length 来自分类条目

    def test_content_unknown_name_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.client.f10_content("sh.600519", "不存在的分类")
        self.assertIn("公司概况", str(ctx.exception))  # 报错列出可用分类

    def test_all_failure_isolation(self):
        """f10_all 单分类失败告警跳过，其余正常返回。"""
        self.client._fail_names = {"财务分析"}
        out = self.client.f10_all("sh.600519")
        self.assertEqual(out, {"公司概况": "mock 正文"})

    def test_bj_rejected(self):
        """北证 F10 未验证：直接 raise 不静默。"""
        with self.assertRaises(ValueError):
            self.client.f10_categories("bj.430047")

    def test_ex_rejected(self):
        """EX 市场 F10 协议不存在：直接 raise。"""
        with self.assertRaises(ValueError):
            self.client.f10_content("us.AAPL", "公司概况")

    def test_empty_category_raises(self):
        """空分类列表 raise（无公司资料的票不静默返回空）。"""
        self.client._entries = []
        with self.assertRaises(ValueError):
            self.client.f10_categories("sh.600519")


    def test_all_oserror_isolated(self):
        """B-M1 回归锁：裸 OSError 也被 f10_all 隔离（捕获集对齐 _hq_call）。"""

        class _OSErrorClient(_FakeFacadeClient):
            def _hq_call(self, method, *args, **kwargs):
                if method == "get_company_info_content":
                    for e in self._entries:
                        if e["filename_raw"] == args[2] and e["name"] in self._fail_names:
                            raise OSError("simulated raw oserror")
                return super()._hq_call(method, *args, **kwargs)

        client = _OSErrorClient(ENTRIES, content="mock 正文", fail_names={"财务分析"})
        out = client.f10_all("sh.600519")
        self.assertEqual(out, {"公司概况": "mock 正文"})

    def test_content_filename_truncated(self):
        """B-M2 回归锁：str filename 超长截断为 80B 而非 struct.error 畸形包。"""
        p = _make_content_parser()
        p.setParams(1, "600519", "x" * 100, 0, 1)
        self.assertEqual(len(p.send_pkg), 114)
        self.assertEqual(p.send_pkg[22:102], b"x" * 80)


if __name__ == "__main__":
    unittest.main()
