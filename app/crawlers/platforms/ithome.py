"""IT Home's public root-comment stream; expired archives are unavailable."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.public_news import PublicDataUnavailable
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    node = BeautifulSoup(response.text, "html.parser").select_one("#post_comm[data-id][data-nid]")
    if node is None or node["data-nid"] != wid:
        raise PlatformCrawlerError("IT之家页面缺少目标新闻与评论参数映射")
    endpoint = "https://cmt.ithome.com/api/webcomment/getnewscomment"
    current = cursor or "0"
    rows, following = [], None
    for step in range(1 if cursor or not comments else page):
        payload = await crawler._get_json(endpoint, params={
            "sn": node["data-id"], "cid": current,
            "isInit": "true" if current == "0" else "false", "appver": 900,
        }, headers={"Referer": url})
        data = payload.get("content") or {}
        if payload.get("success") is not True or str(data.get("newsId")) != wid:
            raise PlatformCrawlerError("IT之家评论响应失败或内容 ID 不匹配")
        hint = str(data.get("endHint") or "")
        if "不再显示" in hint or "登录" in hint:
            raise PublicDataUnavailable("IT之家评论访问受限：" + hint)
        rows = data.get("comments")
        if not isinstance(rows, list):
            raise PlatformCrawlerError("IT之家评论列表缺失")
        if any(not isinstance(x, dict) or x.get("parentCommentId") not in (0, "0") for x in rows):
            raise PlatformCrawlerError("IT之家评论响应混入非一级评论或缺少层级标识")
        following = str(rows[-1].get("id")) if rows else None
        require_page(rows, bool(rows), current, following)
        if not rows:
            break
        current = following
    response = await crawler._get_response("https://cmt.ithome.com/api/comment/count",
        params={"newsid": wid}, headers={"Referer": url})
    count = re.search(r"ccd\.innerHTML\s*=\s*['\"](\d+)['\"]", response.text)
    if not count:
        raise PlatformCrawlerError("IT之家未返回公开评论计数")
    total = int(count[1])
    if not cursor and page == 1 and total > 0 and not rows:
        raise PlatformCrawlerError("IT之家声明存在评论但首屏为空")
    result = []
    if comments:
        for row in rows:
            elements = row.get("elements")
            if not isinstance(elements, list):
                raise PlatformCrawlerError("IT之家一级评论正文结构缺失")
            text = "".join(str(x.get("content") or "") for x in elements if x.get("type") == 0)
            # children are loaded replies; expandCount counts the remaining ones.
            replies = len(row["children"]) + row["expandCount"] if (
                isinstance(row.get("children"), list) and isinstance(row.get("expandCount"), int)) else None
            result.append(comment(row.get("id"), (row.get("userInfo") or {}).get("userNick"), text,
                likes=row.get("support"), replies=replies, created=row.get("postTime")))
    return EngagementStats(comments=total), result, following if comments else None, endpoint
