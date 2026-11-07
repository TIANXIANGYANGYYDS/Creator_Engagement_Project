"""Target-bound Tonghuashun counters; read requests never increment them."""
from __future__ import annotations

import hashlib

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable
from app.crawlers.platforms.web_sources_common import get_jsonp
from app.models.engagement import EngagementStats


def encode_news_id(article_id: str) -> str:
    """Current web encodedSeq: exact integer arithmetic and MD5 checksum."""
    if not article_id.isdigit() or not 0 <= int(article_id) < 100_000_000_000:
        raise ValueError("无效的同花顺新闻编号")
    article_id = str(int(article_id))
    digit = sum(map(int, article_id)) % 10
    digest = hashlib.md5(f"{article_id}{digit}".encode()).hexdigest()
    mixed = int(article_id) * 2_147_483_647 % 100_000_000_000
    return f"{digest[:4]}{mixed:011d}{digit}{digest[-3:]}"


async def read_counter(crawler, url, key, app, field, token=None):
    params = {"app": app, "field": field, "key": key, "callback": "cb"}
    if token:
        params["token"] = token
    payload = await get_jsonp(crawler, "https://bbsclick.10jqka.com.cn/get",
        params=params, headers={"Referer": url})
    value = to_int((payload.get("result") or {}).get(key))
    if payload.get("errorcode") != 0 or value is None:
        raise PlatformCrawlerError("同花顺计数接口未返回目标计数")
    return value


async def comment_total(crawler, url, article_id, biz_type, requested):
    base = "https://t.10jqka.com.cn/lgt/content/open/api/comment/v3/"
    params = {"resource_id": article_id, "biz_type": biz_type}
    payload = await crawler._get_json(base + "init", params=params, headers={"Referer": url})
    disabled = (payload.get("data") or {}).get("comment_disabled")
    if payload.get("status_code") != 0 or not isinstance(disabled, bool):
        raise PlatformCrawlerError("同花顺未返回有效评论开放状态")
    if disabled:
        if requested:
            raise PublicDataUnavailable("同花顺目标文章未开放评论")
        return None
    payload = await crawler._get_json(base + "list", params={**params,
        "page": 1, "limit": 10, "query_type": 1, "cid": 0}, headers={"Referer": url})
    data = payload.get("data") or {}
    total, rows = to_int(data.get("comment_count")), data.get("comments")
    if payload.get("status_code") != 0 or total is None or not isinstance(rows, list):
        raise PlatformCrawlerError("同花顺评论接口未返回有效列表与总数")
    if total == 0 and rows:
        raise PlatformCrawlerError("同花顺评论列表与总数矛盾")
    if requested and (total != 0 or data.get("comment_filter") is not False):
        raise PublicDataUnavailable("同花顺已取得评论总数；非空或筛选评论的正文分页尚未实网验证")
    return total


async def news(crawler, url, article_id, comments):
    encoded = encode_news_id(article_id)
    endpoint = "https://news.10jqka.com.cn/mobile_api/news/article/v1/encoded/"
    payload = await crawler._get_json(endpoint + encoded, headers={"Referer": url})
    data = payload.get("data") or {}
    item = data.get("newsInfo") or {}
    key = "seq_" + article_id
    if (payload.get("status_code") != 0 or str(item.get("seq")) != article_id
            or item.get("encodedSeq") != encoded or not item.get("title")
            or data.get("hotKey") != key):
        raise PlatformCrawlerError("同花顺新闻接口未返回目标文章")
    supported = item.get("supportComment")
    if comments and supported is not True:
        raise PublicDataUnavailable("同花顺目标新闻未开放评论" if supported is False
                                    else "同花顺目标新闻评论状态未知")
    stats = EngagementStats()
    if not comments:
        stats.views = await read_counter(crawler, url, key, "news", "clicks", data.get("token"))
        stats.likes = await read_counter(crawler, url, key, "news", "likes", data.get("token"))
    if supported is True:
        if str((data.get("comment") or {}).get("pid")) != article_id:
            raise PlatformCrawlerError("同花顺新闻评论标识与目标文章不匹配")
        stats.comments = await comment_total(crawler, url, article_id, 8, comments)
    return stats, [], None, "10jqka news encoded detail + bbsclick/get (no inc) + comment/v3"


async def collect(crawler, url, wid, limit, page, cursor, comments):
    kind, article_id = wid.split(":", 1)
    if kind == "news":
        return await news(crawler, url, article_id, comments)
    if kind != "post":
        raise PublicDataUnavailable("同花顺内容类型未支持")
    payload = await crawler._get_json("https://t.10jqka.com.cn/m/post/getPostShareData/",
        params={"pid": article_id}, headers={"Referer": url})
    data = (payload.get("result") or {}).get("data") or {}
    item = data.get("post") or {}
    key = item.get("hotKey")
    if payload.get("errorCode") != 0 or key != "sns_post_" + article_id or not item.get("title"):
        raise PlatformCrawlerError("同顺号分享接口未返回目标文章")
    views = None if comments else await read_counter(crawler, url, key, "sns", "clicks")
    total = await comment_total(crawler, url, article_id, 1, comments)
    return EngagementStats(views=views, comments=total), [], None, "10jqka getPostShareData + bbsclick/get (no inc) + comment/v3"
