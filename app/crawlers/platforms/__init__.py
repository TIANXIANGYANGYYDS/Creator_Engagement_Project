"""Independent protocol collectors for each supported media platform."""

from app.crawlers.platforms import (
    bilibili,
    douyin,
    haokan,
    kuaishou,
    toutiao,
    wechat,
    wechat_channels,
    weibo,
    xiaohongshu,
)
from app.crawlers.platforms.base import PlatformFetchHandler
from app.models.engagement import EngagementPlatform


PLATFORM_HANDLERS: dict[EngagementPlatform, PlatformFetchHandler] = {
    "douyin": douyin.fetch,
    "toutiao": toutiao.fetch,
    "wechat": wechat.fetch,
    "wechat_channels": wechat_channels.fetch,
    "xiaohongshu": xiaohongshu.fetch,
    "haokan": haokan.fetch,
    "kuaishou": kuaishou.fetch,
    "bilibili": bilibili.fetch,
    "weibo": weibo.fetch,
}

__all__ = ["PLATFORM_HANDLERS"]
