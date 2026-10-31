"""Sina discovery posts use a separate, page-declared discussion thread."""
from __future__ import annotations

import re

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import checked_list
from app.crawlers.platforms.web_sources_common import assigned_json, comment, require_page
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    data = assigned_json(response.text, "window.SM").get("data") or {}
    post = data.get("weibo") or {}
    target = "tiezi:" + wid
    if data.get("base", {}).get("base", {}).get("dataid") != target:
        raise PlatformCrawlerError("新浪新闻社区页面未返回目标帖子")
    interaction = post.get("info", {}).get("interactionInfo") or {}
    likes = interaction.get("likeInfo") or {}
    discussion = interaction.get("comment") or {}
    if likes.get("id") != target:
        raise PlatformCrawlerError("新浪新闻社区点赞计数不属于目标帖子")
    stats = EngagementStats(likes=to_int(likes.get("count")), comments=to_int(discussion.get("commentCount")))
    result, following = [], None
    if comments:
        thread = discussion.get("commentId", "")
        match = re.fullmatch(r"([A-Za-z0-9-]+)_0_(qcmnt|ds)", thread)
        if not match:
            raise PlatformCrawlerError("新浪新闻社区未提供有效评论线程")
        current, size = int(cursor) if cursor else page, min(limit, 20)
        payload = await crawler._get_json("https://comment5.sina.cn/page/info", params={
            "channel": match[2], "newsid": match[1], "commentId": thread, "thread": 2,
            "t_size": 2, "page_size": size, "list": "new", "fake": 1, "page": current},
            headers={"Referer": url})
        data = payload.get("result") or {}
        if data.get("status", {}).get("code") != 0:
            raise PlatformCrawlerError("新浪新闻社区评论返回业务错误")
        total = to_int(data.get("count", {}).get("thread_show"))
        if total is None:
            raise PlatformCrawlerError("新浪新闻社区缺少公开一级评论数量")
        rows = checked_list(data.get("cmntlist"), total, first=current == 1)
        more = current * size < total
        require_page(rows, more, current, current + 1)
        following = str(current + 1) if more else None
        for row in rows:
            if str(row.get("newsid")) != match[1] or row.get("channel") != match[2]:
                raise PlatformCrawlerError("新浪新闻社区评论线程不匹配")
            if str(row.get("layer")) != "1" or str(row.get("parent_mid", "0")) != "0":
                continue
            result.append(comment(row.get("mid"), row.get("nick"), row.get("content"),
                likes=row.get("agree"), replies=(row.get("content_ext") or {}).get("reply"), created=row.get("time")))
    return stats, result, following, "super.sina.cn target SM.data + comment5/page/info (page-declared thread)"
