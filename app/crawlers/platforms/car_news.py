"""Public automotive publisher detail and comment read endpoints."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import checked_list, PublicDataUnavailable
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementStats


async def pcauto(crawler, url, wid, limit, page, cursor, comments):
    payload = await crawler._get_json(f"https://hj.pcauto.com.cn/api/articles/{wid}")
    item = payload.get("data") or {}
    if payload.get("code") != 200 or str(item.get("articleId")) != wid or not item.get("title"):
        raise PlatformCrawlerError("太平洋号未返回目标文章")
    # cmtNum in article SSR is not kept in sync; query the actual comment thread.
    stats = EngagementStats(views=to_int(item.get("pvCount")), likes=to_int(item.get("showLikeNum")))
    current = int(cursor) if cursor else page
    payload = await crawler._get_json("https://cmt.pcauto.com.cn/action/comment/list_new_json.jsp",
        params={"urlHandle": 1, "url": f"//hj.pcauto.com.cn/article/{wid}.html",
                "pageSize": min(limit, 20), "pageNo": current if comments else 1}, headers={"Referer": url})
    if payload.get("title") != item["title"]:
        raise PlatformCrawlerError("太平洋评论线程标题与文章不匹配")
    stats.comments = to_int(payload.get("total"))
    rows = checked_list(payload.get("data"), stats.comments, first=current == 1)
    more = current < int(payload.get("availablePageCount") or 0)
    require_page(rows, more, current, current + 1)
    result = [comment(x.get("id"), x.get("nickName"), x.get("content"),
        likes=x.get("support"), created=x.get("createTime")) for x in rows] if comments else []
    return stats, result, str(current + 1) if comments and more else None, "hj.pcauto.com.cn + cmt.pcauto.com.cn"


async def news18(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    binding = re.search(r"var\s+story_id\s*=\s*['\"](\d+)", response.text)
    if not binding or binding[1] != wid:
        raise PlatformCrawlerError("网通社页面不是目标文章")
    current = int(cursor) if cursor else page
    parsed = urlsplit(url)
    payload = await crawler._get_json(f"{parsed.scheme}://{parsed.netloc}/news/comments/{wid}/{current}/",
                                      headers={"Referer": url})
    total = to_int(payload.get("num"))
    rows = payload.get("data")
    if rows is False and total == 0:
        rows = []
    checked_list(rows, total, first=current == 1)
    result = [comment(x.get("id"), x.get("name"), x.get("content"),
        replies=x.get("son_num"), created=x.get("add_time")) for x in rows] if comments else []
    # The web client asks for another page after every nonempty response.
    return EngagementStats(comments=total), result, str(current + 1) if comments and rows else None, "news18a.com/news/comments"


async def dripcar(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    binding = re.search(r"ajaxCommentList\.html['\"],\s*\{\s*page:\s*page,\s*id:\s*['\"](\d+)", response.text)
    if not binding or binding[1] != wid:
        raise PlatformCrawlerError("水滴汽车页面不是目标文章")
    soup = BeautifulSoup(response.text, "html.parser")
    read_icon = soup.select_one('img[src="/Public/home/images/pic_7.png"]')
    read_count = read_icon.find_next_sibling("span") if read_icon else None
    count = soup.select_one("#pinglun h3 span")
    stats = EngagementStats(views=to_int(read_count.get_text(strip=True)) if read_count else None,
                            comments=to_int(count.get_text(strip=True)) if count else None)
    result, following = [], None
    if comments:
        current = int(cursor) if cursor else page
        payload = await crawler._post_json("https://www.dripcar.com/Home/Comment/ajaxCommentList.html",
            data={"page": current, "id": wid}, headers={"Referer": url})
        html = payload.get("item")
        if not isinstance(html, str):
            raise PlatformCrawlerError("水滴汽车评论响应缺少列表")
        if not html.strip():
            if current == 1 and stats.comments != 0:
                raise PlatformCrawlerError("水滴汽车评论首屏为空但未确认零评论")
        else:
            # Missing stable IDs are an explicit error, never an empty success.
            dom = BeautifulSoup(html, "html.parser")
            for node in dom.select(".comment_item"):
                cid = node.get("data-id") or node.get("id")
                body = node.select_one(".comment_item_r_bottom")
                author = node.select_one(".comment_item_r_top")
                result.append(comment(cid, author.get_text(" ", strip=True) if author else "",
                                      body.get_text(" ", strip=True) if body else ""))
            if not result:
                raise PlatformCrawlerError("水滴汽车评论 HTML 结构未识别")
            following = str(current + 1)
    return stats, result, following, "dripcar.com target DOM + read-only comment list"


async def qctt(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://m.qctt.cn/news/{wid}")
    binding = re.search(r"var\s+newsId\s*=\s*['\"](\d+)", response.text)
    if not binding or binding[1] != wid:
        raise PlatformCrawlerError("汽车头条页面不是目标文章")
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.select_one(".read_num")
    match = re.search(r"([\d.,]+[万亿]?)", node.get_text()) if node else None
    stats = EngagementStats(views=to_int(match[1]) if match else None)
    result = []
    block = soup.select_one(".pic-list2")
    if block and block.select_one(".no_commont"):
        stats.comments = 0
    if comments:
        if page != 1 or cursor:
            raise PublicDataUnavailable("汽车头条网页仅提供当前公开评论页")
        if stats.comments != 0:
            if not block:
                raise PublicDataUnavailable("汽车头条页面未公开评论区域")
            for node in block.select("li"):
                vote = node.select_one(".zancomment[id]")
                body = node.select_one(".com_content, .comment_content")
                if vote and body:
                    author = node.select_one(".username, .user_name")
                    result.append(comment(vote["id"], author.get_text(strip=True) if author else "",
                                          body.get_text(" ", strip=True)))
            if not result:
                raise PlatformCrawlerError("汽车头条非空评论区域结构未识别")
    return stats, result, None, "m.qctt.cn target article DOM"


async def cheshi(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    binding = re.search(r"var\s+article_id\s*=\s*['\"](\d+)", response.text)
    if not binding or binding[1] != wid:
        raise PlatformCrawlerError("车市号页面不是目标文章")
    base = "https://apis.cheshi.com/c4/self-media/"
    payload = await crawler._post_json(base + "article-info", data={"id": wid, "cheshi_site_id": ""}, headers={"Referer": url})
    if payload.get("status") != 200 or not isinstance(payload.get("data"), dict):
        raise PlatformCrawlerError("车市号互动接口返回失败")
    item = payload["data"]
    stats = EngagementStats(**{k: to_int(item.get(v)) for k, v in {
        "views": "views", "likes": "likes", "comments": "comments", "favorites": "focus"}.items()})
    result, following = [], None
    if comments:
        current = int(cursor) if cursor else page
        size = min(limit, 15)
        payload = await crawler._post_json(base + "get-comment-list",
            data={"aid": wid, "page": current, "limit": size, "session_id": 0}, headers={"Referer": url})
        if payload.get("status") != 200:
            raise PlatformCrawlerError("车市号评论接口返回失败")
        data = payload.get("data") or {}
        total = to_int(data.get("count"))
        rows = checked_list(data.get("data"), total, first=current == 1)
        if any(str(x.get("aid")) != wid for x in rows):
            raise PlatformCrawlerError("车市号评论不属于目标文章")
        more = current * size < total
        require_page(rows, more, current, current + 1)
        following = str(current + 1) if more else None
        result = [comment(x.get("id"), (x.get("user") or {}).get("username"), x.get("content"),
            likes=x.get("likes"), replies=x.get("reply_count"), created=x.get("created_at")) for x in rows]
    return stats, result, following, base + "article-info + get-comment-list"


COLLECTORS = {"pcauto": pcauto, "news18": news18, "dripcar": dripcar, "qctt": qctt, "cheshi": cheshi}
