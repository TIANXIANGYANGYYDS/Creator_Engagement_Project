"""Zhihu public article state with an anonymous, proxy-bound browser fallback."""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import checked_list
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementStats


def parse_article(html, kind, wid):
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#js-initialData")
    try:
        state = json.loads(node.string)["initialState"]
        item = state["entities"][kind + "s"][wid]
        if str(item.get("id")) != wid or item.get("type") != kind:
            raise ValueError
    except (AttributeError, KeyError, TypeError, ValueError):
        raise PlatformCrawlerError("知乎未返回目标内容的公开状态") from None
    stats = EngagementStats(likes=to_int(item.get("voteupCount")),
        comments=to_int(item.get("commentCount")), favorites=to_int(item.get("favlistsCount")))
    if not any(value is not None for value in stats.model_dump().values()):
        raise PlatformCrawlerError("知乎目标内容未公开互动计数")
    return stats


def require_public_page(url):
    if urlsplit(url).path in {"/account/unhuman", "/signin", "/signup"}:
        raise PlatformBlockedError("知乎要求账号登录或安全验证，匿名会话未获得目标内容")


def parse_comments(payload, expected_path, current):
    if "error" in payload or not isinstance(payload.get("paging"), dict):
        raise PlatformBlockedError("知乎评论接口未返回公开分页数据")
    paging = payload["paging"]
    total = to_int((payload.get("counts") or {}).get("total_counts"))
    rows = checked_list(payload.get("data"), total, first=not current)
    if not isinstance(paging.get("is_end"), bool):
        raise PlatformCrawlerError("知乎评论缺少结束标记")
    more = not paging["is_end"]
    following = None
    if more:
        target = urlsplit(paging.get("next") or "")
        if target.hostname != "www.zhihu.com" or target.path != expected_path:
            raise PlatformCrawlerError("知乎评论游标不属于目标内容")
        following = (parse_qs(target.query).get("offset") or [""])[0]
    require_page(rows, more, current, following)
    result = []
    for row in rows:
        if row.get("reply_comment_id") not in (None, "0", 0, ""):
            continue
        author = row.get("author") or {}
        if isinstance(author.get("member"), dict):
            author = author["member"]
        result.append(comment(row.get("id"), author.get("name"), row.get("content"),
            likes=row.get("like_count"), replies=row.get("child_comment_count"), created=row.get("created_time")))
    return total, result, following


async def collect(crawler, url, work_id, limit, page, cursor, comments):
    kind, wid = work_id.split(":", 1)
    browser = None
    try:
        try:
            response = await crawler._get_response(url)
            stats = parse_article(response.text, kind, wid)
        except PlatformCrawlerError:
            fallback = getattr(crawler, "browser_fallback", None)
            if fallback is None:
                raise
            from app.crawlers.authors.browser import BrowserTransport
            # A fresh anonymous context shares this collection's proxy lease and
            # the service's existing browser semaphore. Never seed an account.
            browser = BrowserTransport(SimpleNamespace(client=crawler.client, platform_cookies={}),
                fallback._semaphore, headless=fallback.settings.headless)
            await browser.open(url)
            require_public_page(browser.page.url)
            await browser.page.wait_for_function(
                "([kind,id]) => { if (['/account/unhuman','/signin','/signup'].includes(location.pathname)) return true;"
                "try { const node=document.querySelector('#js-initialData');"
                "const state=JSON.parse(node.textContent).initialState;"
                "return !!state.entities[kind+'s'][id]; } catch { return false; } }",
                arg=[kind, wid], timeout=20000)
            require_public_page(browser.page.url)
            stats = parse_article(await browser.page.content(), kind, wid)
        result, following = [], None
        if comments:
            path = f"/api/v4/comment_v5/{kind}s/{wid}/root_comment"
            current = cursor or ""
            for step in range(1 if cursor else page):
                transport = browser or crawler
                payload = await transport._get_json("https://www.zhihu.com" + path,
                    params={"order_by": "ts", "limit": min(limit, 20), "offset": current},
                    headers={"Referer": url})
                stats.comments, result, following = parse_comments(payload, path, current)
                if following is None:
                    if step + 1 < (1 if cursor else page):
                        result = []
                    break
                current = following
        return stats, result, following, "zhihu target js-initialData + comment_v5/root_comment"
    finally:
        if browser is not None:
            await browser.close()
