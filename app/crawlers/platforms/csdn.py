"""Counters from the target blog article; no inferred recommendation counts."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    if comments:
        raise PublicDataUnavailable("CSDN 已接入文章公开互动计数；评论接口当前返回访问挑战，正文尚未通过实网验收")
    response = await crawler._get_response(url)
    soup = BeautifulSoup(response.text, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    target = re.search(r"\bvar\s+articleId\s*=\s*(\d+)\s*;", response.text)
    canonical_url = urlsplit(canonical.get("href", "")) if canonical else None
    if (not target or target[1] != wid or canonical_url is None
            or canonical_url.hostname != "blog.csdn.net"
            or not canonical_url.path.endswith("/article/details/" + wid)):
        raise PlatformCrawlerError("CSDN 页面未提供目标文章标识")
    views = re.search(r"\bvar\s+viewCountFormat\s*=\s*(\d+)\s*;", response.text)
    def count(selector):
        node = soup.select_one(selector)
        return to_int(node.get_text(strip=True)) if node else None
    stats = EngagementStats(views=int(views[1]) if views else None,
        likes=count("#sidebar-likeCount, #is-like .count"),
        favorites=count("#sidebar-collect > span, #is-collection .count"))
    if not any(value is not None for value in stats.model_dump().values()):
        raise PlatformCrawlerError("CSDN 目标文章未提供公开互动计数")
    return stats, [], None, "blog.csdn.net target article counters"
