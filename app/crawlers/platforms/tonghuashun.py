"""Tonghuashun share articles: public read counter, without incrementing it."""
from __future__ import annotations

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable
from app.crawlers.platforms.web_sources_common import get_jsonp
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    kind, article_id = wid.split(":", 1)
    if kind != "post":
        raise PublicDataUnavailable("同花顺财经新闻与同顺号是不同协议；当前新闻页面未公开可验证的互动计数")
    if comments:
        raise PublicDataUnavailable("同顺号公开分享页未提供评论正文，不能将未获取的评论当作零条")
    payload = await crawler._get_json("https://t.10jqka.com.cn/m/post/getPostShareData/",
        params={"pid": article_id}, headers={"Referer": url})
    data = (payload.get("result") or {}).get("data") or {}
    item = data.get("post") or {}
    key = item.get("hotKey")
    if payload.get("errorCode") != 0 or key != "sns_post_" + article_id or not item.get("title"):
        raise PlatformCrawlerError("同顺号分享接口未返回目标文章")
    payload = await get_jsonp(crawler, "https://bbsclick.10jqka.com.cn/get", params={
        "app": "sns", "field": "clicks", "key": key, "callback": "cb"}, headers={"Referer": url})
    views = to_int((payload.get("result") or {}).get(key))
    if payload.get("errorcode") != 0 or views is None:
        raise PlatformCrawlerError("同顺号阅读量接口未返回目标计数")
    return EngagementStats(views=views), [], None, "t.10jqka.com.cn getPostShareData + bbsclick/get (no inc)"
