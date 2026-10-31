"""SMZDM article-bound public counters and root-comment cursor API."""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable, checked_list
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    soup = BeautifulSoup(response.text, "html.parser")
    def value(selector):
        node = soup.select_one(selector)
        return node.get("value") if node else None
    article_id = value("#articleID") or value("#data-aid")
    identity = value("#articleHashid") or article_id
    channel_id = value("#channelID") or value("#data-cid")
    if identity != wid or not article_id or not channel_id:
        raise PlatformCrawlerError("什么值得买页面缺少目标文章与评论频道映射")
    if value("#article_comment_open") == "closed":
        raise PublicDataUnavailable("什么值得买目标文章关闭公开评论")
    stats = EngagementStats()
    # Article labels are scoped to the target header, never recommendation cards.
    header = soup.select_one("#articleId .item-preferential .xilie")
    if header:
        for key, label in (("likes", "点赞"), ("favorites", "收藏")):
            match = re.search(r"(\d+)" + label, header.get_text(" ", strip=True))
            if match:
                setattr(stats, key, int(match[1]))
    endpoint = "https://zhiyou.smzdm.com/user/comment/ajax_comment_list"
    current = json.loads(cursor) if cursor else {"comment_id": 0}
    if not isinstance(current, dict) or "comment_id" not in current or set(current) - {"comment_id", "sort_v2"}:
        raise PlatformCrawlerError("什么值得买评论游标无效，不能覆盖目标文章或评论层级")
    rows, following = [], None
    for step in range(1 if cursor or not comments else page):
        payload = await crawler._get_json(endpoint, params={
            "article_id": article_id, "channel_id": channel_id, "order": "new",
            "root_id": 0, "with_link": 1, "with_a": 0, "atta": 1, **current,
        }, headers={"Referer": url})
        if payload.get("error_code") != 0:
            raise PlatformCrawlerError("什么值得买评论接口返回失败")
        data = payload.get("data") or {}
        if data.get("article_close_comment_switch"):
            raise PublicDataUnavailable("什么值得买目标文章关闭公开评论")
        stats.comments = to_int(data.get("total"))
        rows = checked_list(data.get("rows"), to_int(data.get("total_root")),
                            first=not cursor and step == 0)
        if any(str(x.get("article_id")) != article_id or x.get("root_id") not in (0, "0") for x in rows):
            raise PlatformCrawlerError("什么值得买评论响应不是目标文章的一级评论")
        more = bool(data.get("next_page"))
        next_id = data.get("next_comment_id")
        if more and (not next_id or str(next_id) == str(current.get("comment_id"))):
            raise PlatformCrawlerError("什么值得买评论游标未前进")
        next_params = {"comment_id": next_id, "sort_v2": data.get("next_sort_v2", 0)}
        following = json.dumps(next_params, separators=(",", ":")) if more else None
        require_page(rows, more, cursor if step == 0 else json.dumps(current, separators=(",", ":")), following)
        if not more:
            if comments and step + 1 < (1 if cursor else page):
                rows = []
            break
        current = next_params
    result = [comment(x.get("comment_id"), x.get("display_name"), x.get("content"),
                      likes=x.get("up_num"), replies=x.get("child_total"),
                      created=x.get("creation_time")) for x in rows] if comments else []
    return stats, result, following if comments else None, endpoint
