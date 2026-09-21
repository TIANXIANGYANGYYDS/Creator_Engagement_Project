from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.crawlers.authors.common import (
    encode_cursor,
    page_number,
    urls,
    pending,
    fill_details,
)
from app.crawlers.http_client import PlatformCrawlerError
from app.models.author import AuthorPage, AuthorWork


async def fetch_page(crawler, request, state, *, include_details=True):
    page = page_number(state)
    root = "https://blog.sina.com.cn"
    listing = f"{root}/s/articlelist_{request.uid}_0_{page}.html"
    response = await crawler._get_response(listing)
    soup = BeautifulSoup(response.text, "html.parser")
    pattern = re.compile(rf"/s/(blog_{int(request.uid):08x}[0-9A-Za-z]+)\.html$")
    items = {}
    for anchor in soup.select(".articleCell a[href]"):
        target = urljoin(root, str(anchor.get("href", "")))
        match = pattern.fullmatch(urlsplit(target).path)
        if not match or match[1] in items:
            continue
        # Construct the canonical fixed-host URL; never follow arbitrary list links.
        work = AuthorWork(
            work_id=match[1],
            author_uid=request.uid,
            media_name="sina_blog",
            content_type="article",
            url=f"{root}/s/{match[1]}.html",
            title=anchor.get_text(" ", strip=True),
        )
        container = anchor.find_parent(class_="articleCell")
        date_match = re.search(
            r"\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?",
            container.get_text(" ", strip=True),
        )
        if date_match:
            work.published_at = datetime.fromisoformat(date_match[0]).replace(
                tzinfo=timezone(timedelta(hours=8))
            )
        pending(work)
        items[work.work_id] = work
    if not items:
        raise PlatformCrawlerError(
            "新浪博客列表没有可核验的文章，不能确认账号为空或已翻到末页"
        )
    if include_details:
        await fill_details(crawler, request, list(items.values()), fetch_detail)
    next_pages = []
    for anchor in soup.select("a[href]"):
        match = re.search(
            rf"articlelist_{re.escape(request.uid)}_0_(\d+)\.html", anchor["href"]
        )
        if match and int(match[1]) > page:
            next_pages.append(int(match[1]))
    cursor = encode_cursor(request, {"page": min(next_pages)}) if next_pages else None
    return AuthorPage(
        uid=request.uid,
        media_name="sina_blog",
        data=list(items.values()),
        next_cursor=cursor,
        coverage="complete",
        reason="已读取公开文章列表及正文；不包含已删除或私密文章",
    )


async def fetch_detail(crawler, request, work):
    root = "https://blog.sina.com.cn"
    detail = await crawler._get_response(work.url)
    body = BeautifulSoup(detail.text, "html.parser")
    # sina_keyword_ad_area is the tags block, not the article body.
    node = body.select_one("#sina_keyword_ad_area2, .articalContent")
    if node is None or not node.get_text(" ", strip=True):
        raise PlatformCrawlerError("新浪博客文章正文缺失或页面受限")
    work.text = node.get_text("\n", strip=True)
    work.description = work.text[:300]
    title = body.select_one(".articalTitle .titName, .articalTitle h2")
    if title:
        work.title = title.get_text(" ", strip=True)
    author = body.select_one("#ownernick, .SG_txtc")
    if author:
        work.author_name = author.get_text(" ", strip=True)
    work.media_urls = urls(
        [
            urljoin(root, img.get("real_src") or img.get("src") or "")
            for img in node.select("img")
        ]
    )
    work.cover_url = work.media_urls[0] if work.media_urls else None
    return work
