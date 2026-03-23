# cython: language_level=3
import abc
import datetime
import struct
import time
import zlib

from tdxpy.exceptions import TdxFunctionCallError
from tdxpy.exceptions import ValidationException
from tdxpy.logger import logger

try:
    import cython  # noqa

    if cython.compiled:

        def buffer(x):
            return x

except ImportError:
    pass


class SocketClientNotReady(Exception):
    pass


class SendPkgNotReady(Exception):
    pass


class SendRequestPkgFails(Exception):
    pass


class ResponseHeaderRecvFails(Exception):
    pass


class ResponseRecvFails(Exception):
    pass


RSP_HEADER_LEN = 0x10


class BaseParser:
    def __init__(self, client, lock=None):
        """

        :rtype: object
        """

        self.send_pkg = None
        self.data = None

        self.rsp_header_len = RSP_HEADER_LEN
        self.rsp_header = None
        self.rsp_body = None

        self.client = client  # 底层 socket（TrafficStatSocket）
        self.lock = lock or None
        # API 实例引用（BaseSocketClient 子类），由 _cmd 工厂注入。
        # retry 分支需要访问 auto_retry/retry_strategy/disconnect/connect/raise_exception，
        # 这些都在 API 实例上——直接构造 parser（绕过工厂）时为 None，retry 降级。
        self.api = None

        self.category = None

    def setParams(self, *args, **xargs):  # noqa
        """
        构建请求
        :return:
        """
        pass

    @abc.abstractmethod
    def parseResponse(self, body_buf):  # noqa
        """
        解析结果
        :param body_buf:
        """
        pass

    @staticmethod
    def _parse_date(num):
        """

        :param num:
        :return:
        """
        month = (num % 2048) // 100
        year = num // 2048 + 2004
        day = (num % 2048) % 100

        return year, month, day

    @staticmethod
    def _parse_time(num):
        """

        :param num:
        :return:
        """
        return (num // 60), (num % 60)

    @staticmethod
    def _cal_price1000(base_p, diff):
        return float(base_p + diff) / 1000

    def setup(self):
        pass

    def call_api(self):
        """发送请求并解析响应；失败时按客户端的重试策略重连重发。

        锁保护范围：call_api 持有连接锁时，发送+接收+重连+重发全部原子化——
        多线程共享连接场景下，重连不会与其他线程的在途查询互相踩踏。
        """
        if self.lock:
            with self.lock:
                logger.debug("sending thread lock api call")
                return self._call_with_retry()
        return self._call_with_retry()

    def _call_with_retry(self):
        """单次命令执行 + 自动重试（调用方须已持锁）。

        重试语义（2026-09-20 从 last_ack_time 装饰器下沉到此）：
        - TypeError/ValueError → ValidationException（参数错误，重试无意义）
        - 其他异常：auto_retry 开启且 socket 存在时按 retry_strategy 重连重发；
          全部失败后按 raise_exception 决定抛出 TdxFunctionCallError 或返回 None。
        - api 为 None（直接构造 parser、绕过 _cmd 工厂）时 retry/异常语义降级：
          不重试、不吞异常，原样上抛。

        注意：self.client 是底层 socket；API 实例引用在 self.api（_cmd 工厂注入）。
        """
        try:
            return self._call_api()
        except (TypeError, ValueError) as e:
            raise ValidationException(*e.args)
        except Exception as e:
            current_exception = e
            api = self.api

            if api is None:
                raise

            if api.auto_retry and api.client:
                for time_interval in api.retry_strategy.generate():
                    logger.debug(f"auto retry in {time_interval}s, exception is {e}")
                    time.sleep(time_interval)

                    try:
                        api.disconnect()
                    except Exception as retry_disconnect_e:
                        # 旧 socket 可能已被外部 close（shutdown 抛 OSError）——disconnect 内部
                        # finally 已把 self.client 置 None，清理失败不影响重连，忽略即可。
                        logger.debug(f"disconnect on retry failed (ignored): {retry_disconnect_e}")
                    api.connect(api.ip, api.port)
                    # 重连后 API 换了新 socket，parser 必须刷新引用，否则重发仍走旧 socket
                    self.client = api.client

                    try:
                        return self._call_api()
                    except Exception as retry_e:
                        logger.debug(f"hit exception on *retry* req exception is {retry_e}")
                        current_exception = retry_e

                logger.debug("perform auto retry on req ")

            if api.raise_exception:
                to_raise = TdxFunctionCallError("calling function error")
                to_raise.original_exception = current_exception
                raise to_raise

            return None

    def _call_api(self):
        """

        :return:
        """
        self.setup()

        if not self.client:
            raise SocketClientNotReady("socket client not ready")

        if not self.send_pkg:
            raise SendPkgNotReady("send pkg not ready")

        # 发送数据包
        descended = self.client.send(self.send_pkg)

        logger.debug(f"descended, {descended}")

        self.client.send_pkg_num += 1
        self.client.send_pkg_bytes += descended
        self.client.last_api_send_bytes = descended

        if not self.client.first_pkg_send_time:
            self.client.first_pkg_send_time = datetime.datetime.now()

        if descended != len(self.send_pkg):
            logger.debug("send bytes error")
            raise SendRequestPkgFails("send fails")
        else:
            # 接收数据
            logger.debug("send rsp_header_len:" + str(self.rsp_header_len))
            head_buf = self.client.recv(self.rsp_header_len)

            # 长度符合
            if len(head_buf) == self.rsp_header_len:
                self.client.recv_pkg_num += 1
                self.client.recv_pkg_bytes += self.rsp_header_len

                # 解包
                _, _, _, zip_size, unzip_size = struct.unpack("<IIIHH", head_buf)

                logger.debug(f"zip size is: {zip_size}")
                logger.debug(f"unzip size is: {unzip_size}")

                body_buf = bytearray()
                last_api_recv_bytes = self.rsp_header_len

                while True:
                    # 接收数据包
                    buf = self.client.recv(zip_size)
                    len_buf = len(buf)

                    self.client.recv_pkg_num += 1
                    self.client.recv_pkg_bytes += len_buf

                    last_api_recv_bytes += len_buf
                    body_buf.extend(buf)

                    # 接收数据到结束
                    if not buf or len_buf == 0 or len(body_buf) == zip_size:
                        logger.debug("接收数据到结束")
                        break

                self.client.last_api_recv_bytes = last_api_recv_bytes

                if len(buf) == 0:
                    logger.debug("接收数据体失败服务器断开连接")
                    raise ResponseRecvFails("接收数据体失败服务器断开连接")

                if zip_size != unzip_size:
                    logger.debug("需要解压, 解压数据")
                    body_buf = zlib.decompress(body_buf)

                return self.parseResponse(body_buf)

            else:
                # 长度不符合抛出异常
                logger.debug("head_buf is not 0x10")
                raise ResponseHeaderRecvFails("head_buf is not 0x10 : " + str(head_buf))
