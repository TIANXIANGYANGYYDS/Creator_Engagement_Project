"""Yiche public PC API signing, reproduced from its official axios component."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import checked_list
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementStats


def signed_headers(data, timestamp, guid):
    param = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # This is a public web-client constant, not an account credential.
    raw = "cid=508&param=" + param + "19DDD1FBDFF065D3A4DA777D2D7A81EC" + str(timestamp)
    return param, {"x-platform": "pc", "cid": "508", "x-timestamp": str(timestamp),
                   "x-sign": hashlib.md5(raw.encode()).hexdigest(), "x-user-guid": guid}


async def get(crawler, path, data, url, guid):
    param, headers = signed_headers(data, int(time.time() * 1000), guid)
    headers["Referer"] = url
    payload = await crawler._get_json("https://mapi.yiche.com" + path,
        params={"cid": 508, "param": param}, headers=headers)
    if str(payload.get("status")) != "1" or not isinstance(payload.get("data"), dict):
        raise PlatformCrawlerError("易车接口返回失败：" + str(payload.get("message") or "响应缺少数据"))
    return payload["data"]


async def collect(crawler, url, wid, limit, page, cursor, comments):
    guid = uuid.uuid4().hex
    item = await get(crawler, "/information_api/api/v1/news_article/article_info", {"newsId": wid}, url, guid)
    if str(item.get("newsId")) != wid or not item.get("title") or item.get("contentType") is None:
        raise PlatformCrawlerError("易车详情未返回目标文章")
    stats = EngagementStats(views=to_int(item.get("visitCount")), comments=to_int(item.get("comments")))
    support = await get(crawler, "/information_api/api/v1/support/support_info",
                        {"contentId": wid, "contentType": str(item["contentType"])}, url, guid)
    stats.likes = to_int(support.get("supportCount"))
    result, following = [], None
    if comments:
        paging = json.loads(cursor) if cursor else {"page": 1, "findStartTime": None, "diffCommnetId": None, "userCommentIds": None}
        for step in range(1 if cursor else page):
            current = int(paging["page"])
            data = await get(crawler, "/web_api/information_api/api/v1/comment/top_comment_list", {
                "contentType": int(item["contentType"]), "contentId": int(wid),
                "currentPage": current, "pageSize": min(limit, 20), "hotFlag": True,
                "findStartTime": paging.get("findStartTime"), "diffCommnetId": paging.get("diffCommnetId"),
                "userCommentIds": paging.get("userCommentIds"), "timeClickFlag": False}, url, guid)
            stats.comments = to_int(data.get("total"))
            rows = checked_list(data.get("list"), stats.comments, first=current == 1)
            more = bool(data.get("haveNextPage"))
            require_page(rows, more, current, current + 1)
            paging = {"page": current + 1, **{k: data.get(k) for k in ("findStartTime", "diffCommnetId", "userCommentIds")}}
            following = json.dumps(paging, separators=(",", ":")) if more else None
            if not more:
                if step + 1 < (1 if cursor else page):
                    rows = []
                break
        result = [comment(x.get("id") or x.get("commentId"), x.get("showName"), x.get("content"),
            likes=x.get("supportCount"), replies=x.get("replyCount"), created=x.get("createTime")) for x in rows]
    return stats, result, following, "mapi.yiche.com public signed PC API"
