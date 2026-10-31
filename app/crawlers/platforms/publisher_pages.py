"""Article-scoped counters from public HTML; unrelated page totals are excluded."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.article_communities import article_stats, zero_or_unavailable
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable, checked_list
from app.crawlers.platforms.web_sources_common import comment, get_jsonp
from app.models.engagement import EngagementStats


def count_text(soup, selector):
    node = soup.select_one(selector)
    return to_int(node.get_text(strip=True).strip("()")) if node else None


async def donews(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    if not re.search(r"var\s+id\s*=\s*['\"]" + re.escape(wid) + r"['\"]", response.text):
        raise PlatformCrawlerError("DoNews 未返回目标文章")
    soup = BeautifulSoup(response.text, "html.parser")
    stats = EngagementStats(views=count_text(soup, "#read_count"))
    if stats.views is None:
        raise PublicDataUnavailable("DoNews 文章未公开阅读计数")
    zero_or_unavailable(stats, comments, "DoNews")
    return stats, [], None, "donews.com target article #read_count (no read-counter update request)"


async def growthhk(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    soup = BeautifulSoup(response.text, "html.parser")
    button = soup.select_one(".entry-action .btn-zan")
    canonical = soup.select_one('link[rel="canonical"]')
    if (button is None or button.get("data-id") != wid or canonical is None
            or urlsplit(canonical.get("href", "")).path != urlsplit(url).path):
        raise PlatformCrawlerError("增长黑客页面未返回目标文章")
    info = soup.select_one(".entry-info")
    views = re.search(r"阅读\s*([\d,.]+(?:万|亿)?)", info.get_text(" ", strip=True) if info else "")
    stats = EngagementStats(views=to_int(views[1]) if views else None,
        likes=count_text(button, ".entry-action-num"),
        comments=count_text(soup, 'a.meta-item[href="#comments"] .data'))
    zero_or_unavailable(stats, comments, "增长黑客")
    return stats, [], None, "growthhk.cn target entry-info + entry-action + article comment badge"


async def ofweek(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    if not re.search(r"(?:var\s+)?DetailID\s*=\s*['\"]?" + re.escape(wid) + r"(?:['\"]|\s*;)", response.text):
        raise PlatformCrawlerError("OFweek 页面未返回目标文章")
    base = "https://" + urlsplit(url).hostname
    payload = await crawler._post_json(base + "/Ow_newscommentListInfo.html", data={"DetailID": wid}, headers={"Referer": url})
    total = to_int(payload.get("ccount"))
    if total is None:
        raise PlatformCrawlerError("OFweek 未返回有效评论计数")
    stats = EngagementStats(comments=total)
    zero_or_unavailable(stats, comments, "OFweek")
    if comments:
        payload = await crawler._post_json(base + "/Ow_newscommentListWithStep.html",
            data={"DetailID": wid, "flag": "NEW", "limit": 5, "page": 1}, headers={"Referer": url})
        rows = checked_list(payload.get("json"), total, first=True)
        if rows:
            raise PlatformCrawlerError("OFweek 评论数量与公开列表不一致")
    return stats, [], None, "ofweek target DetailID + Ow_newscommentListInfo.ccount"


async def icswb(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url.replace("_m.html", ".html"))
    if not re.search(r"ICMS_DOCUMENT_NEWS_ID\s*=\s*['\"]?" + re.escape(wid) + r"(?:['\"]|\s*;)", response.text):
        raise PlatformCrawlerError("长沙晚报页面未返回目标文章")
    payload = await get_jsonp(crawler, "https://www.icswb.com/default.php", params={
        "mod": "document_news", "a": "async_get_agree_count", "document_news_id": wid, "jsonpcallback": "cb"},
        headers={"Referer": url})
    value = to_int(payload.get("result"))
    if payload.get("error_message") or value is None:
        raise PlatformCrawlerError("长沙晚报未返回有效点赞计数")
    stats = EngagementStats(likes=value)
    zero_or_unavailable(stats, comments, "长沙晚报")
    return stats, [], None, "icswb.com target document ID + async_get_agree_count"


async def d1ev(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.body is None or soup.body.get("data-newid") != wid:
        raise PlatformCrawlerError("第一电动页面未返回目标文章")
    stats = EngagementStats(likes=count_text(soup, ".likeCount span"),
        comments=count_text(soup, ".sprite-comment span"))
    result, following = [], None
    if comments:
        current = int(cursor) if cursor else page
        response = await crawler._get_response("https://www.d1ev.com/contents/comment/get_comment",
            params={"per_page": current, "id": wid, "type": 1}, headers={"Referer": url})
        try:
            payload = json.loads(response.text.lstrip("\ufeff\r\n "))
        except ValueError:
            raise PlatformCrawlerError("第一电动评论接口未返回 JSON") from None
        if str((payload.get("article") or {}).get("targetId")) != wid:
            raise PlatformCrawlerError("第一电动评论不属于目标文章")
        stats.comments = to_int(payload.get("total_number"))
        rows = checked_list(payload.get("comment"), stats.comments, first=current == 1)
        if not isinstance(payload.get("pagination"), str):
            raise PlatformCrawlerError("第一电动评论缺少分页标记")
        links = BeautifulSoup(payload["pagination"], "html.parser").select("[data-ci-pagination-page]")
        pages = [int(node["data-ci-pagination-page"]) for node in links
            if str(node["data-ci-pagination-page"]).isdigit() and int(node["data-ci-pagination-page"]) > current]
        if pages:
            if not rows or min(pages) != current + 1:
                raise PlatformCrawlerError("第一电动评论分页未连续前进")
            following = str(current + 1)
        for row in rows:
            if str(row.get("level")) != "1":
                continue
            result.append(comment(row.get("id"), row.get("nickName"), row.get("content"),
                likes=row.get("likeNum"), created=row.get("addtime")))
    return stats, result, following, "d1ev.com target data-newid + likeCount + contents/comment/get_comment"


async def mydigit(crawler, url, wid, limit, page, cursor, comments):
    current = (int(cursor) if cursor else page) if comments else 1
    response = await crawler._get_response(f"https://www.mydigit.cn/thread-{wid}-{current}-1.html")
    soup = BeautifulSoup(response.text, "html.parser")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical is None or not re.fullmatch(r"/thread-" + re.escape(wid) + r"-\d+-1\.html",
            urlsplit(canonical.get("href", "")).path) or soup.select_one("#thread_subject") is None:
        raise PlatformCrawlerError("数码之家未返回目标帖子")
    header = soup.select_one("#postlist > table .hm")
    values = header.select(".xi1") if header else []
    if len(values) != 2:
        raise PlatformCrawlerError("数码之家缺少帖子查看/回复计数")
    stats = EngagementStats(views=to_int(values[0].get_text()), comments=to_int(values[1].get_text()),
        likes=count_text(soup, "#recommendv_add"), favorites=count_text(soup, "#favoritenumber"))
    result, following = [], None
    if comments:
        for node in soup.find_all("div", id=re.compile(r"^post_\d+$")):
            number = node.select_one('[id^="postnum"] em')
            if number is None:
                raise PlatformCrawlerError("数码之家帖子缺少楼层信息")
            if number.get_text(strip=True) == "1":
                continue
            body = node.select_one('[id^="postmessage_"]')
            if body is None or body.select_one(".locked"):
                raise PlatformBlockedError("数码之家回帖正文要求登录，不能将登录提示作为评论正文")
            for child in body.select(".quote, .pstatus, script, style"):
                child.decompose()
            author = node.select_one(".pls .authi a")
            stamp = node.select_one('[id^="authorposton"] span[title]')
            result.append(comment(node["id"].split("_")[1], author.get_text(strip=True) if author else None,
                body.get_text(" ", strip=True), created=stamp.get("title") if stamp else None))
        link = soup.select_one(".pg a.nxt")
        if link:
            match = re.fullmatch(r"(?:https://www\.mydigit\.cn/)?thread-" + re.escape(wid) + r"-(\d+)-1\.html", link.get("href", ""))
            if not match or int(match[1]) != current + 1 or not result:
                raise PlatformCrawlerError("数码之家分页链接未前进或不属于目标帖子")
            following = match[1]
        if current == 1 and not result and stats.comments != 0:
            raise PlatformCrawlerError("数码之家有回帖计数但未返回正文")
    return stats, result, following, "mydigit.cn target thread header + public root posts (excluding original post)"


async def ichaoqi(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    soup = BeautifulSoup(response.text, "html.parser")
    canonical = soup.select_one('meta[property="og:url"]')
    if canonical is None or urlsplit(canonical.get("content", "")).path != urlsplit(url).path:
        raise PlatformCrawlerError("潮起网未返回目标文章")
    metadata = soup.select_one(".entry-meta.contpost-meta")
    text = metadata.get_text(" ", strip=True) if metadata else ""
    views = re.search(r"([\d,.]+(?:w|万|亿)?)阅读", text)
    total = re.search(r"(\d+)\s*评论", text)
    if not views and not total:
        raise PublicDataUnavailable("潮起网文章未公开互动计数")
    stats = EngagementStats(views=to_int(views[1].replace("w", "万")) if views else None,
        comments=to_int(total[1]) if total else None)
    zero_or_unavailable(stats, comments, "潮起网")
    return stats, [], None, "ichaoqi.com target entry-meta (rounded display; no hits increment request)"


async def guanhai(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://m.guanhai.com.cn/p/{wid}.html")
    if not re.search(r'article_id\s*:\s*["\']' + re.escape(wid) + r'["\']', response.text):
        raise PlatformCrawlerError("观海新闻页面未返回目标文章")
    payload = await crawler._get_json("https://m.guanhai.com.cn/index/ajax/analytics",
        params={"contentid": wid}, headers={"Referer": url})
    if payload.get("state") is not True:
        raise PlatformCrawlerError("观海新闻互动接口返回业务错误")
    # The official page uses only digg from this endpoint; pv/share are not
    # rendered and may be disabled counters. Do not call them article views.
    stats = article_stats(payload.get("data"), wid, "contentid", {"likes": "digg"})
    zero_or_unavailable(stats, comments, "观海新闻")
    return stats, [], None, "guanhai.com.cn target analytics.contentid + displayed digg"


COLLECTORS = {"donews": donews, "growthhk": growthhk, "ofweek": ofweek, "icswb": icswb, "d1ev": d1ev,
    "mydigit": mydigit, "ichaoqi": ichaoqi, "guanhai": guanhai}
