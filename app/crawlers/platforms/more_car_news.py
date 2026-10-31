"""Article-bound public counters and root comments for automotive publishers."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable, checked_list
from app.crawlers.platforms.web_sources_common import assigned_json, comment, require_page
from app.models.engagement import EngagementStats


async def chooseauto(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://www.chooseauto.com.cn/news/{wid}.shtml")
    def config(name):
        match = re.search(r"var\s+" + name + r"\s*=\s*(\d+)", response.text)
        return match[1] if match else None
    thread = config("conTID")
    if config("currID") != wid or not thread:
        raise PlatformCrawlerError("选车网页面未提供目标文章及评论线程")
    base = "https://api.chooseauto.com.cn/api/"
    payload = await crawler._get_json(base + "content/getContentOtherInfo", params={
        "uid": 0, "id": wid, "platform": "PC", "product": 0}, headers={"Referer": url})
    item = payload.get("data") or {}
    if payload.get("code") != 200 or str(item.get("id")) != wid:
        raise PlatformCrawlerError("选车网计数不属于目标文章")
    stats = EngagementStats(**{k: to_int(item.get(v)) for k, v in {
        "views": "browseCount", "likes": "likeCount", "comments": "commentCount",
        "shares": "shareCount", "favorites": "collectCount"}.items()})
    result, following = [], None
    if comments:
        current = int(cursor) if cursor else page
        size = min(limit, 10)
        payload = await crawler._get_json(base + "comment/getCommentList", params={
            "uid": 0, "tid": thread, "orderType": 0, "pageIndex": current, "pageSize": size},
            headers={"Referer": url})
        data = payload.get("data") or {}
        if payload.get("code") != 200 or data.get("pageIndex") != current:
            raise PlatformCrawlerError("选车网评论接口未返回请求页码")
        rows = checked_list(data.get("list"), to_int(data.get("totalCount")), first=current == 1)
        for row in rows:
            if str(row.get("tid")) != thread or row.get("layer") != 1:
                raise PlatformCrawlerError("选车网评论不是目标文章的一级评论")
            result.append(comment(row.get("pid"), row.get("authorName"), row.get("message"),
                likes=row.get("digg"), replies=(row.get("replyList") or {}).get("totalCount"),
                created=str(row.get("postdatetime") or "").replace("/", "-")))
        more = current < int(data.get("totalPage") or 0)
        require_page(rows, more, current, current + 1)
        following = str(current + 1) if more else None
    return stats, result, following, base + "content/getContentOtherInfo + comment/getCommentList"


async def qcwp(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://www.qcwp.com/news/{wid}")
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.select_one("#__NUXT_DATA__")
    try:
        table = json.loads(node.string)
        root = table[table[0][1]]
        data = table[root["data"]]
        payload = table[data["ARTICLE_" + wid]]
        article = table[payload["data"]]
        def field(name):
            return table[article[name]] if name in article else None
        if table[payload["code"]] != 200 or str(field("id")) != wid or not field("title"):
            raise ValueError
        stats = EngagementStats(views=to_int(field("look")), comments=to_int(field("commentCount")))
    except (TypeError, KeyError, ValueError, IndexError, AttributeError):
        raise PlatformCrawlerError("汽车网评页面未提供目标文章数据") from None
    result, following = [], None
    endpoint = "https://www.qcwp.com/api/news/portal/articleComment/list"
    if comments:
        current = int(cursor) if cursor else page
        payload = await crawler._get_json(endpoint, params={"articleId": wid, "level": 1,
            "pageNum": current, "pageSize": min(limit, 10), "version": "2.0"}, headers={"Referer": url})
        data = payload.get("data") or {}
        if payload.get("code") != 200 or data.get("pageNum") != current:
            raise PlatformCrawlerError("汽车网评评论接口未返回请求页码")
        rows = checked_list(data.get("list"), to_int(data.get("total")), first=current == 1)
        for row in rows:
            if str(row.get("articleId")) != wid or str(row.get("level")) != "1":
                raise PlatformCrawlerError("汽车网评评论不是目标文章的一级评论")
            result.append(comment(row.get("id"), row.get("mediaName"), row.get("content"),
                replies=row.get("replyCount"), created=row.get("createTime")))
        more = current < int(data.get("pages") or 0)
        require_page(rows, more, current, current + 1)
        following = str(current + 1) if more else None
    return stats, result, following, "qcwp.com target Nuxt article + articleComment/list"


async def buycar(crawler, url, wid, limit, page, cursor, comments):
    if urlsplit(url).hostname == "www.maiche.com":
        return await maiche(crawler, url, wid, comments)
    response = await crawler._get_response(url)
    soup = BeautifulSoup(response.text, "html.parser")
    article = soup.select_one(".news_info")
    thread = soup.select_one("#linkwjpr[rel]")
    # The article's share QR URL binds its public GUID to the original ID.
    qr = soup.select_one('#wecharimg[style*="MakeQRCode.aspx"]') if article else None
    match = re.search(r"data=([^);]+)", qr.get("style", "")) if qr else None
    path = urlsplit(unquote(match[1])).path if match else ""
    if not re.fullmatch(r"/news/\d+/" + re.escape(wid) + r"_\d+\.html", path) or thread is None:
        raise PlatformCrawlerError("买车网页面未提供目标文章及评论标识")
    guid = " ".join(thread["rel"]) if isinstance(thread["rel"], list) else thread["rel"]
    if not re.fullmatch(r"[0-9a-f-]{36}", guid):
        raise PlatformCrawlerError("买车网评论标识无效")
    view = article.select_one(".news_date .icon-yuedu + i")
    stats = EngagementStats(views=to_int(view.get_text(strip=True)) if view else None)
    current = (int(cursor) if cursor else page) if comments else 1
    size = min(limit, 20)
    endpoint = "https://www.buycar.cn/Service/CommentService.ashx"
    payload = await crawler._get_json(endpoint, params={"Method": "GetCommentListWithPager",
        "pageSize": size, "currentPage": current, "PageId": guid}, headers={"Referer": url})
    if payload.get("ErrMsg") != "":
        raise PlatformCrawlerError("买车网评论接口返回错误")
    stats.comments = to_int(payload.get("TotCount"))
    rows = checked_list(payload.get("Items"), stats.comments, first=current == 1)
    more = current * size < stats.comments
    require_page(rows, more, current, current + 1)
    result = [comment(x.get("CommentId"), x.get("UserName") or x.get("ShowPhone"), x.get("CommentText"),
        likes=x.get("Support"), replies=len(x["Replays"]) if isinstance(x.get("Replays"), list) else None,
        created=x.get("strCreateTime")) for x in rows] if comments else []
    return stats, result, str(current + 1) if comments and more else None, endpoint


async def maiche(crawler, url, wid, comments):
    if comments:
        raise PublicDataUnavailable("买车网 maiche.com 评论接口返回业务错误，正文尚未通过实网验收")
    response = await crawler._get_response(url)
    state = assigned_json(response.text, "Simple.Tools.ajax._nodeData")
    for key, raw in state.items():
        target = urlsplit(key)
        query = parse_qs(target.query)
        if target.path != "/open/content/view.htm" or (query.get("articleId") or query.get("id")) != [wid]:
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        data = payload.get("data") or {}
        if payload.get("success") is not True or str(data.get("id")) != wid or not data.get("title"):
            continue
        # The site inserts a default zero when this counter is absent upstream.
        likes = to_int(data.get("upCount"))
        if likes is not None and likes > 0:
            return EngagementStats(likes=likes), [], None, "maiche.com target content/view SSR upCount"
    raise PublicDataUnavailable("买车网 maiche.com 未提供可确认的目标互动计数；页面占位零值不作为真实零值")


COLLECTORS = {"chooseauto": chooseauto, "qcwp": qcwp, "buycar": buycar}
