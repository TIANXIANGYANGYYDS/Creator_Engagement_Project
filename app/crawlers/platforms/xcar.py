"""Aikahao article views and Xcar's paginated root-comment protocol."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import checked_list, PublicDataUnavailable
from app.crawlers.platforms.web_sources_common import comment, require_page, json_or_jsonp
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    canonical = f"https://aikahao.xcar.com.cn/item/{wid}.html"
    response = await crawler._get_response(canonical)
    soup = BeautifulSoup(response.text, "html.parser")
    identity = soup.select_one('.detail_lf input[name="article_id"]')
    if identity is None or identity.get("value") != wid or not soup.select_one(".detail_title h1"):
        raise PlatformCrawlerError("爱咖号页面未返回目标文章")
    views = soup.select_one(".detail_main .browse_number")
    reading = re.fullmatch(r"([\d,.万亿]+)\s*浏览", views.get_text(strip=True)) if views else None
    current = int(cursor) if cursor else (page if comments else 1)
    endpoint = "https://comment.xcar.com.cn/interface/index.php"
    response = await crawler._get_response(endpoint, params={
        "iact": "CommentLevel", "action": "getNewsComment", "cid": wid, "ctype": 14,
        "sort": "time", "page": current, "limit": 25,
    }, headers={"Referer": canonical})
    # This endpoint declares GBK. curl_cffi's response.json() decodes bytes as
    # UTF-8 and rejects the Chinese message even when the JSON is valid.
    payload = json_or_jsonp(response.text)
    news = payload.get("news") or {}
    if ((payload.get("info") or {}).get("status") != "success"
            or str(news.get("cid")) != wid or str(news.get("ctype")) != "14"):
        raise PlatformCrawlerError("爱咖号评论响应失败或目标文章不匹配")
    config = payload.get("config") or {}
    total = to_int(config.get("count"))
    rows = checked_list(payload.get("list"), total, first=current == 1)
    root_total = to_int(config.get("toplevelcount"))
    if total == 0:
        root_total = 0
    if root_total is None:
        raise PlatformCrawlerError("爱咖号评论接口缺少一级评论分页计数")
    more = current * 25 < root_total
    require_page(rows, more, current, current + 1)
    roots = [x for x in rows if str(x.get("status")) != "2"]
    if comments and rows and not roots:
        raise PublicDataUnavailable("爱咖号当前页评论仅作者可见，不能返回公开正文")
    if any(str(x.get("parent_id")) != "0" for x in roots):
        raise PlatformCrawlerError("爱咖号评论响应混入非一级评论")
    result = [comment(x.get("id"), x.get("user_name") or x.get("username"), x.get("conts"),
                      likes=x.get("ups"), replies=x.get("count"),
                      created=x.get("pub_time") or x.get("add_time")) for x in roots] if comments else []
    return (EngagementStats(views=to_int(reading[1]) if reading else None, comments=total),
            result, str(current + 1) if comments and more else None, endpoint)
