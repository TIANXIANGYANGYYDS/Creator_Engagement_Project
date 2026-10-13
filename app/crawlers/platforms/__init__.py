"""Independent protocol collectors for each supported media platform."""

from functools import partial

from app.crawlers.platforms import (
    sina, baidu_publishers, sohu, dongchedi, bytedance_publishers,
    netease, xueqiu, autohome, qq_stock,
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
    "sina": sina.fetch,
    "sina_finance": partial(sina.fetch, platform="sina_finance"),
    "baijiahao": baidu_publishers.fetch,
    "yoojia": partial(baidu_publishers.fetch, platform="yoojia"),
    "sohu": sohu.fetch,
    "dongchedi": dongchedi.fetch,
    "weitoutiao": bytedance_publishers.fetch,
    "ixigua": partial(bytedance_publishers.fetch, platform="ixigua"),
    "netease": netease.fetch,
    "xueqiu": xueqiu.fetch,
    "autohome": autohome.fetch,
    "qq_stock": qq_stock.fetch,
}

__all__ = ["PLATFORM_HANDLERS"]
