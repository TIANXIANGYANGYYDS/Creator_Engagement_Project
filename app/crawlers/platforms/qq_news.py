"""Tencent News article counters and root comments, bound to window.DATA."""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable, checked_list
from app.crawlers.platforms.web_sources_common import assigned_json, comment, require_page
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    data = assigned_json(response.text, "window.DATA")
    article_id = str(data.get("article_id") or "")
    mapped_url = urlsplit(str(data.get("url") or ""))
    video_mapping = (len(article_id) > 8 and article_id[8] == "V" and wid == article_id + "00"
                     and mapped_url.hostname == "view.inews.qq.com" and mapped_url.path == "/a/" + wid)
    if (article_id != wid and not video_mapping) or not data.get("title") or not data.get("comment_id"):
        raise PlatformCrawlerError("腾讯新闻页面未返回目标文章及评论标识")
    counts = data.get("interationCount") or {}
    stats = EngagementStats(likes=to_int(counts.get("like")),
                            favorites=to_int(counts.get("collect")), shares=to_int(counts.get("share")))
    endpoint = "https://i.news.qq.com/getQQNewsComment"
    current = cursor or "{}"
    rows, following = [], None
    for step in range(1 if cursor or not comments else page):
        payload = await crawler._get_json(endpoint, params={
            "apptype": "web", "article_id": article_id, "reqNum": min(limit, 20), "transparam": current,
        }, headers={"Referer": url})
        if payload.get("ret") != 0:
            raise PlatformCrawlerError("腾讯新闻评论接口返回失败")
        block = payload.get("comments") or {}
        if block.get("isClose"):
            if comments:
                raise PublicDataUnavailable("腾讯新闻目标文章已关闭公开评论")
            break
        stats.comments = to_int(block.get("count"))
        groups = checked_list(block.get("new"), stats.comments, first=not cursor and step == 0)
        rows = []
        for group in groups:
            # Each group begins with a root; reply_list contains replies, which
            # are deliberately not returned as root comments.
            root = group[0] if isinstance(group, list) and group else group
            if not isinstance(root, dict) or str(root.get("article_id")) not in {wid, article_id}:
                raise PlatformCrawlerError("腾讯新闻评论内容与目标文章不匹配")
            if str(root.get("commentid")) != str(data["comment_id"]):
                raise PlatformCrawlerError("腾讯新闻评论线程与目标页面不匹配")
            rows.append(root)
        more = payload.get("bnext") in (1, "1")
        following = payload.get("transparam") if more else None
        if more:
            try:
                if not isinstance(json.loads(following), dict):
                    raise ValueError
            except (ValueError, TypeError):
                raise PlatformCrawlerError("腾讯新闻评论游标缺失或无效") from None
        require_page(rows, more, current, following)
        if not more:
            if comments and step + 1 < (1 if cursor else page):
                rows = []
            break
        current = following
    result = [comment(x.get("reply_id"), x.get("nick"), x.get("reply_content"),
                      likes=x.get("agree_count"), replies=x.get("reply_num"),
                      created=x.get("pub_time")) for x in rows] if comments else []
    return stats, result, following if comments else None, endpoint
