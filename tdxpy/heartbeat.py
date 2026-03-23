# cython: language_level=3
import time
from threading import Thread

from tdxpy.logger import logger

# 10秒一个heartbeat
DEFAULT_HEARTBEAT_INTERVAL = 10.0


class HeartBeatThread(Thread):
    # 参考 :https://stackoverflow.com/questions/6524459/stopping-a-thread-after-a-certain-amount-of-time

    def __init__(self, client, stop_event, interval=DEFAULT_HEARTBEAT_INTERVAL):
        """
        初始化
        :param client: 客户端主进程
        :param stop_event: 停止事件进程
        :param interval: 心跳周期
        """

        self.heartbeat_interval = interval
        self.stop_event = stop_event

        # 注意：client 参数是 TdxHq_API/TdxExHq_API 实例；sock 才是底层 socket。
        self.sock = client.client
        self.api = client

        super().__init__()

        # 守护线程：解释器退出时不 join 心跳线程（否则主流程忘记 disconnect 时
        # threading._shutdown 会永久等待本线程，进程无法退出——2026-10-15 实锤修复）。
        # 心跳线程只发保活包，被硬杀无副作用；优雅关闭仍走 disconnect 的 stop_event。
        self.daemon = True

    def run(self):
        # stop_event 未设置就执行
        while not self.stop_event.is_set():
            # auto_retry 重连后 socket 会更换，每次循环刷新引用（旧快照会指向已 close 的 socket）
            self.sock = self.api.client
            # 客户端主进程 and 满足 heartbeat_interval
            if self.sock and (time.time() - self.api.last_ack_time > self.heartbeat_interval):
                try:
                    # 发送一个获取股票数量的包作为心跳包
                    logger.debug("发送一个获取股票数量的包作为心跳包...")
                    self.api.do_heartbeat()
                except Exception as e:
                    logger.exception(str(e))

            # 等待心跳周期
            self.stop_event.wait(self.heartbeat_interval)
