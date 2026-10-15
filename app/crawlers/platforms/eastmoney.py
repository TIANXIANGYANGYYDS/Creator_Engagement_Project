"""Eastmoney's public article metadata and the same-origin comment proxy."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.web_sources_common import assigned_json, comment, require_page
from app.models.engagement import EngagementStats


async def proxy(crawler, path, params):
    # The proxy rejects requests without the page Referer or form content type.
    return await crawler._post_json(
        "https://guba.eastmoney.com/interface/GetData.aspx", params={"path": path},
        data=urlencode({"param": urlencode(params), "path": path, "env": 2}),
        headers={"Referer": "https://guba.eastmoney.com/",
                 "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "X-Requested-With": "XMLHttpRequest"})


async def collect(crawler, url, work_id, limit, page, cursor, include_comments, platform):
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    query.update(parse_qs(parsed.fragment.partition("?")[2]))
    post_id = (query.get("postid") or query.get("postId") or [None])[0]
    artcode = (query.get("artCode") or [None])[0]
    if platform == "caifuhao":
        artcode = work_id
    if platform in {"guba", "fund1234567"} or "gubaf10" in parsed.netloc or "hfmgb" in parsed.netloc:
        post_id = work_id
    if artcode and not post_id:
        response = await crawler._get_response(f"https://caifuhao.eastmoney.com/news/{artcode}")
        config = re.search(r"\baccountInfo\s*=\s*\{(.*?)\}", response.text, re.S)
        article = re.search(r'articleid\s*:\s*[\"\'](\d+)', config[1]) if config else None
        post = re.search(r'postId\s*:\s*[\"\'](\d+)', config[1]) if config else None
        if not article or article[1] != artcode or not post:
            raise PlatformCrawlerError("财富号页面未返回目标文章的股吧映射")
        post_id = post[1]
    if post_id:
        response = await crawler._get_response(f"https://guba.eastmoney.com/news,0,{post_id}.html")
        post = assigned_json(response.text, "post_article")
        if isinstance(post.get("post"), dict):
            post = post["post"]
        if str(post.get("post_id")) != str(post_id):
            raise PlatformCrawlerError("股吧页面未返回目标文章")
    else:
        # Official news clients pass their news ID with type=1, then receive a
        # mapped post ID. Check the source ID before accepting that mapping.
        news_id = re.sub(r"^NW", "", work_id)
        payload = await proxy(crawler, "abstract/api/PostShort/ArticleBriefInfo",
                              {"postid": news_id, "type": 1})
        rows = payload.get("re")
        if payload.get("rc") != 1 or not isinstance(rows, list) or not rows:
            raise PlatformCrawlerError("东方财富未返回文章映射")
        post = rows[0]
        if str(post.get("post_sourceid_id")) != news_id:
            raise PlatformCrawlerError("东方财富响应的原文标识不匹配")
        post_id = post.get("post_id")
        if not post_id:
            raise PlatformCrawlerError("东方财富响应缺少评论文章标识")
    stats = EngagementStats(views=to_int(post.get("post_click_count")),
        likes=to_int(post.get("post_like_count")), shares=to_int(post.get("post_forward_count")),
        comments=to_int(post.get("post_comment_count")))
    comments, following = [], None
    if include_comments:
        size = min(limit, 30)
        current = int(cursor) if cursor else page
        payload = await proxy(crawler, "reply/api/Reply/ArticleNewReplyList",
            {"postid": post_id, "sort": 1, "sorttype": 1, "p": current, "ps": size})
        rows = payload.get("re")
        if payload.get("rc") != 1 or not isinstance(rows, list):
            raise PlatformCrawlerError("东方财富评论接口未成功返回列表")
        total = to_int(payload.get("count"))
        if total is None:
            raise PlatformCrawlerError("东方财富评论接口缺少计数")
        if current == 1 and total > 0 and not rows:
            raise PlatformCrawlerError("东方财富声明存在评论但首屏为空")
        if any(str(x.get("source_post_id")) != str(post_id) for x in rows):
            raise PlatformCrawlerError("东方财富评论不属于目标文章")
        stats.comments = to_int(payload.get("reply_total_count"))
        if stats.comments is None:
            stats.comments = total
        comments = [comment(x.get("reply_id"), (x.get("reply_user") or {}).get("user_nickname"),
            x.get("reply_text"), likes=x.get("reply_like_count"), replies=x.get("reply_count"),
            created=x.get("reply_time")) for x in rows]
        more = current * size < total
        require_page(rows, more, current, current + 1)
        following = str(current + 1) if more else None
    return stats, comments, following, "guba.eastmoney.com: target article + comment proxy"
