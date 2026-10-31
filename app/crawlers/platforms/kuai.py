"""360 Kuai's article-bound comment read protocol and page-state decoding."""
import base64
import binascii
import json
import re
from urllib.parse import urlsplit

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable, checked_list
from app.crawlers.platforms.web_sources_common import comment, get_jsonp, require_page
from app.models.engagement import EngagementStats


def article_state(text, wid):
    match = re.search(r"window\.__INITIAL_DATA__\s*=\s*'\\?\s*\n?(.*?)'\s*;", text, re.S)
    try:
        encoded = match[1].replace("\\\n", "").strip()
        # Current detail2025 module 33373: adjust the first 1000 base64 chars.
        decoded = "".join(chr(ord(c) - i % 2) for i, c in enumerate(encoded[:1000])) + encoded[1000:]
        detail = json.loads(base64.b64decode(decoded, validate=True))["detail"]
    except (TypeError, ValueError, KeyError, binascii.Error):
        raise PlatformCrawlerError("快资讯页面数据未能解析") from None
    raw_url = urlsplit(str(detail.get("rawurl") or ""))
    matches_target = (
        raw_url.hostname == "zm.news.so.com" and raw_url.path == "/" + wid[4:]
        if wid.startswith("raw:") else str(detail.get("gnid")) == wid
    )
    if not matches_target or not detail.get("title") or not detail.get("rptid") or not detail.get("rawurl"):
        raise PlatformCrawlerError("快资讯页面未提供目标文章及评论标识")
    if detail.get("del") or str(detail.get("cmnt")) == "0":
        raise PublicDataUnavailable("快资讯目标文章已删除或未开放公开评论")
    return detail


async def collect(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    article = article_state(response.text, wid)
    current = (int(cursor) if cursor else page) if comments else 1
    endpoint = "https://comment.360kuai.com/comment/lists"
    payload = await get_jsonp(crawler, endpoint, params={
        "type": 1, "page_key": article["rptid"], "page": current, "from": 0, "client_id": 15,
        "start_date": "", "url": article["rawurl"], "pid": "", "sub_limit": 5,
        "num": min(limit, 10), "uid": "", "src": "Chrome",
    }, headers={"Referer": url})
    data = payload.get("data") or {}
    if payload.get("errno") != 0 or data.get("current_page") != current:
        raise PlatformCrawlerError("快资讯评论接口失败或未返回请求页码")
    total = to_int(data.get("new_total"))
    rows = checked_list(data.get("comments"), total, first=current == 1)
    more = current < int(data.get("pages") or 0)
    require_page(rows, more, current, current + 1)
    result = []
    if comments:
        for row in rows:
            if row.get("page_key") not in (None, article["rptid"]):
                raise PlatformCrawlerError("快资讯评论不属于目标文章线程")
            if row.get("pid") not in (None, "", 0, "0"):
                raise PlatformCrawlerError("快资讯评论响应混入非一级评论")
            try:
                user = json.loads(row.get("user_info") or "{}")
                extra = json.loads(row.get("ext") or "{}")
            except (ValueError, TypeError):
                raise PlatformCrawlerError("快资讯评论用户或回复计数结构异常") from None
            result.append(comment(row.get("id"), user.get("nick_name") or user.get("user_name"),
                row.get("message"), likes=row.get("likes"), replies=extra.get("sub_num"), created=row.get("pdate")))
    return EngagementStats(comments=total), result, str(current + 1) if comments and more else None, endpoint
