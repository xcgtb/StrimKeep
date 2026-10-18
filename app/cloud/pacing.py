# -*- coding: utf-8 -*-
"""频道轮询的节奏策略。

纯计算：不做 IO、不 import 第三方库，因此可以脱离 Telegram 与容器直接单测。

三条规则（顺序即优先级）：

1. **限流优先** —— Telegram 返回 FloodWait 时，必须按服务端给的秒数等待。
   提前重试不会成功，还会把下一次的等待时间罚得更长。
2. **积压立即继续** —— 本轮取满上限说明后面可能还有未读，
   让调度马上再跑一轮，而不是干等一个 poll_seconds。
3. **其余按规则周期** —— 正常情况用频道配置里的 poll_seconds。
"""
from app.cloud.errors import CloudError

# 限流时多等几秒，避免恰好卡在服务端边界上二次触发。
FLOOD_MARGIN_SEC = 5

DEFAULT_POLL_SEC = 300


class ChannelFloodWait(CloudError):
    """Telegram 限流。带着秒数往上抛，调度才知道该等多久。"""

    def __init__(self, seconds):
        self.seconds = max(0, int(seconds or 0))
        super().__init__('Telegram 限流，需等待 %d 秒后重试' % self.seconds, 429)


def flood_wait_seconds(exc):
    """从 Telethon 的 FloodWaitError 里取秒数；不是限流异常则返回 0。"""
    for name in ('seconds', 'value'):
        try:
            seconds = getattr(exc, name, None)
            if seconds is not None:
                return max(0, int(seconds))
        except (TypeError, ValueError):
            continue
    return 0


def next_poll_delay(error_seconds=0, default_seconds=DEFAULT_POLL_SEC, backlog=False):
    """算出下次轮询间隔（秒）。

    error_seconds: 服务端要求的等待秒数；0 表示本轮没有限流
    backlog:       本轮是否取满上限、可能还有未读
    """
    if error_seconds:
        return max(1, int(error_seconds)) + FLOOD_MARGIN_SEC
    if backlog:
        return 0
    try:
        value = int(default_seconds or DEFAULT_POLL_SEC)
    except (TypeError, ValueError):
        return DEFAULT_POLL_SEC
    # 非法（0/负数）配置按默认周期处理：绝不能退化成 1 秒去撞 Telegram。
    return value if value > 0 else DEFAULT_POLL_SEC
