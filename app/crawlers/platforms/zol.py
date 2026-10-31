"""ZOL comments: article-selected thread, latest roots, explicit HTML pagination."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import checked_list
from app.crawlers.platforms.source_catalog import source_id
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    if urlsplit(url).hostname == "m.zol.com.cn":
        canonical = BeautifulSoup(response.text, "html.parser").select_one('link[rel="canonical"][href]')
        destination = canonical["href"] if canonical else ""
        if not destination or source_id("zol", destination) != wid or urlsplit(destination).hostname == "m.zol.com.cn":
            raise PlatformCrawlerError("中关村在线移动页缺少目标文章的桌面地址")
        response = await crawler._get_response(destination)
    target = re.search(r'var\s+temp_doc_root_id\s*=\s*["\'](\d+)', response.text)
    thread = re.search(r'var\s+commentSrc\s*=\s*["\']//comment\.zol\.com\.cn/(\d+)/a(\d+)\.html', response.text)
    if not target or target[1] != wid or not thread or thread[2] != wid:
        raise PlatformCrawlerError("中关村在线页面缺少目标文章与评论线程映射")
    current = int(cursor) if cursor else (page if comments else 1)
    endpoint = f"https://comment.zol.com.cn/{thread[1]}/{wid}_0_0_{current}.html"
    response = await crawler._get_response(endpoint, headers={"Referer": url})
    binding = re.search(r'var\s+tempDocId\s*=\s*["\'](\d+)', response.text)
    total = re.search(r'var\s+totalNum\s*=\s*["\'](\d+)', response.text)
    if not binding or binding[1] != wid or not total:
        raise PlatformCrawlerError("中关村在线评论页未返回目标文章与计数")
    soup = BeautifulSoup(response.text, "html.parser")
    container = soup.select_one("#list_box_new")
    if container is None:
        raise PlatformCrawlerError("中关村在线最新评论列表缺失")
    roots = container.select(":scope > .comment-item > .comment-box > .comment-list > .commli")
    checked_list(roots, int(total[1]), first=current == 1)
    following = None
    for link in soup.select(".comment-pages a[href]"):
        path = urlsplit(link["href"]).path
        if path == f"/{thread[1]}/{wid}_0_0_{current + 1}.html":
            following = str(current + 1)
    require_page(roots, bool(following), current, following)
    result = []
    if comments:
        for root in roots:
            body = root.select_one(":scope > .comment-content")
            vote = root.select_one(":scope > .comment-vote[data-id]")
            if body is None or vote is None:
                raise PlatformCrawlerError("中关村在线一级评论缺少正文或 ID")
            author = root.select_one(".comment-user-bar .user-name")
            stamp = root.select_one(".comment-user-bar .published-time")
            likes = vote.select_one(".ok-btn span")
            result.append(comment(vote["data-id"], author.get_text(strip=True) if author else "",
                body.get_text(" ", strip=True), created=stamp.get_text(strip=True) if stamp else None,
                likes=to_int(likes.get_text(strip=True)) if likes else None))
    return EngagementStats(comments=int(total[1])), result, following if comments else None, endpoint
