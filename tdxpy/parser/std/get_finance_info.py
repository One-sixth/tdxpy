# cython: language_level=3
import struct
from collections import OrderedDict

from tdxpy.parser.base import BaseParser


class GetFinanceInfo(BaseParser):
    def setParams(self, market, code):
        """

        :param market:
        :param code:
        """
        if type(code) is str:
            code = code.encode("utf-8")

        pkg = bytearray.fromhex("0c 1f 18 76 00 01 0b 00 0b 00 10 00 01 00")
        pkg.extend(struct.pack("<B6s", market, code))

        self.send_pkg = pkg

    def parseResponse(self, body_buf):
        """

        :param body_buf:
        :return:
        """
        pos = 0
        pos += 2  # skip num ,we only query 1 in this case

        market, code = struct.unpack("<B6s", body_buf[pos : pos + 7])

        pos += 7

        (
            liutongguben,
            province,
            industry,
            updated_date,
            ipo_date,
            zongguben,
            guojiagu,
            faqirenfarengu,
            farengu,
            bgu,
            hgu,
            zhigonggu,
            zongzichan,
            liudongzichan,
            gudingzichan,
            wuxingzichan,
            gudongrenshu,
            liudongfuzhai,
            changqifuzhai,
            zibengongjijin,
            jingzichan,
            zhuyingshouru,
            zhuyinglirun,
            yingshouzhangkuan,
            yingyelirun,
            touzishouyu,
            jingyingxianjinliu,
            zongxianjinliu,
            cunhuo,
            lirunzonghe,
            shuihoulirun,
            jinglirun,
            weifenlirun,
            baoliu1,
            baoliu2,
        ) = struct.unpack("<fHHIIffffffffffffffffffffffffffffff", body_buf[pos:])

        def _get_v(v):
            return v

        # 金额字段单位 = 千元（×1000 → 元）。2026-10-19 T0.1 实测门禁裁决：
        # 恒等式「股本raw(万股)×每股净资产 ÷ 净资产raw = 10.0」（600519/000002 双股交叉一致）
        # + 披露量级对照（茅台总资产 3090 亿 / 平安 6.03 万亿 / 万科 9598 亿，×1000 全吻合）。
        # 旧实现 ×10000 放大 10 倍（a-share-tdxdata 结论正确，pytdx 系沿用的误传）。
        # 股本字段单位 = 万股（×10000 → 股），维持不动。
        # ⚠️ 银行股（000001 实测）负债字段返回 0、meigujingzichan 错位（24≠52）——
        #    服务器对金融类股票的 finance 布局不同，银行股本字段的量级结论仍成立。
        result = OrderedDict(
            [
                ("market", market),
                ("code", code.decode("utf-8")),
                ("liutongguben", _get_v(liutongguben) * 10000),
                ("province", province),
                ("industry", industry),
                ("updated_date", updated_date),
                ("ipo_date", ipo_date),
                ("zongguben", _get_v(zongguben) * 10000),
                ("guojiagu", _get_v(guojiagu) * 10000),
                ("faqirenfarengu", _get_v(faqirenfarengu) * 10000),
                ("farengu", _get_v(farengu) * 10000),
                ("bgu", _get_v(bgu) * 10000),
                ("hgu", _get_v(hgu) * 10000),
                ("zhigonggu", _get_v(zhigonggu) * 10000),
                ("zongzichan", _get_v(zongzichan) * 1000),
                ("liudongzichan", _get_v(liudongzichan) * 1000),
                ("gudingzichan", _get_v(gudingzichan) * 1000),
                ("wuxingzichan", _get_v(wuxingzichan) * 1000),
                ("gudongrenshu", _get_v(gudongrenshu)),
                ("liudongfuzhai", _get_v(liudongfuzhai) * 1000),
                ("changqifuzhai", _get_v(changqifuzhai) * 1000),
                ("zibengongjijin", _get_v(zibengongjijin) * 1000),
                ("jingzichan", _get_v(jingzichan) * 1000),
                ("zhuyingshouru", _get_v(zhuyingshouru) * 1000),
                ("zhuyinglirun", _get_v(zhuyinglirun) * 1000),
                ("yingshouzhangkuan", _get_v(yingshouzhangkuan) * 1000),
                ("yingyelirun", _get_v(yingyelirun) * 1000),
                ("touzishouyu", _get_v(touzishouyu) * 1000),
                ("jingyingxianjinliu", _get_v(jingyingxianjinliu) * 1000),
                ("zongxianjinliu", _get_v(zongxianjinliu) * 1000),
                ("cunhuo", _get_v(cunhuo) * 1000),
                ("lirunzonghe", _get_v(lirunzonghe) * 1000),
                ("shuihoulirun", _get_v(shuihoulirun) * 1000),
                ("jinglirun", _get_v(jinglirun) * 1000),
                ("weifenpeilirun", _get_v(weifenlirun) * 1000),
                ("meigujingzichan", _get_v(baoliu1)),
                ("baoliu2", _get_v(baoliu2)),
                # 空壳判定（2026-11-05 MEDIUM③，a-share-tdxdata e064228 判据在线实锤）：
                # 退市/无效票服务器返回全零行——code 全 \x00 且 ipo_date=0
                # （sz.000003 退市 / sz.999999 假票双样本；正常票 000001=19910403 对照）
                ("is_shell", not code.strip(b"\x00") or ipo_date <= 0),
            ]
        )

        return result
