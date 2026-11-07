"""Article-scoped public state for independent publishing communities."""
from __future__ import annotations

import json

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable, checked_list
from app.crawlers.platforms.web_sources_common import assigned_json, comment, nuxt_literal_state, require_page
from app.models.engagement import EngagementStats


def next_data(html):
    node = BeautifulSoup(html, "html.parser").select_one("#__NEXT_DATA__")
    try:
        return json.loads(node.string)
    except (AttributeError, TypeError, ValueError):
        raise PlatformCrawlerError("页面未提供有效的目标文章状态") from None


def article_stats(item, wid, id_field, fields):
    if not isinstance(item, dict) or str(item.get(id_field)) != wid:
        raise PlatformCrawlerError("公开计数不属于请求的文章")
    stats = EngagementStats(**{key: to_int(item.get(field)) for key, field in fields.items()})
    if not any(v is not None for v in stats.model_dump().values()):
        raise PublicDataUnavailable("目标文章未公开互动计数")
    return stats


def zero_or_unavailable(stats, requested, name):
    if requested and stats.comments != 0:
        raise PublicDataUnavailable(f"{name}公开页未提供完整的一级评论接口；不能将未知评论当作空列表")


async def guancha(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response("https://user.guancha.cn/main/content", params={"id": wid})
    soup = BeautifulSoup(response.text, "html.parser")
    target = soup.select_one("#comments-container")
    if target is None or target.get("data-id") != wid or target.get("data-type") != "2":
        raise PlatformCrawlerError("风闻页面未返回目标文章的评论标识")
    praise = soup.select_one(f'.tools_praise[data-id="{wid}"][data-type="2"] > span:last-child')
    stats = EngagementStats(likes=to_int(praise.get_text(strip=True)) if praise else None)
    payload = await crawler._get_json("https://user.guancha.cn/comment/cmt-list.json", params={
        "codeId": wid, "codeType": 2, "pageNo": 1, "order": 1, "ff": "user"}, headers={"Referer": url})
    if payload.get("code") != 0 or payload.get("status") not in {1, 2, 3}:
        raise PlatformCrawlerError("风闻评论接口返回业务错误")
    if payload["status"] == 3:
        if comments:
            raise PublicDataUnavailable("风闻目标文章已关闭评论")
    else:
        stats.comments = to_int(payload.get("count"))
        rows = checked_list(payload.get("items"), stats.comments, first=True)
        if stats.comments is None or (stats.comments == 0 and rows):
            raise PlatformCrawlerError("风闻评论列表与总数缺失或矛盾")
        zero_or_unavailable(stats, comments, "风闻")
    return stats, [], None, "user.guancha.cn target article + comment/cmt-list.json (nonempty bodies unverified)"


async def jike(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    data = next_data(response.text).get("props", {}).get("pageProps", {})
    item = data.get("post") or {}
    stats = article_stats(item, wid, "id", {"likes": "likeCount", "comments": "commentCount",
        "shares": "shareCount", "reposts": "repostCount"})
    # The mobile share page exposes a preview, not a paginated comment archive.
    zero_or_unavailable(stats, comments, "即刻")
    return stats, [], None, "m.okjike.com target pageProps.post"


async def chemm(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://www.chemm.com/newsDetail?article_id={wid}")
    state = assigned_json(response.text, "window.__INITIAL_STATE__")
    item = state.get("newsDetail", {}).get("articleDetailState", {}).get("article") or {}
    stats = article_stats(item, wid, "id", {"views": "post_hits", "likes": "post_like", "comments": "comment_count"})
    # The public page formats views with a lower-case w suffix. Keep that
    # displayed magnitude; post_hits_real is a different internal counter.
    if isinstance(item.get("post_hits"), str) and item["post_hits"].lower().endswith("w"):
        stats.views = to_int(item["post_hits"][:-1] + "万")
    zero_or_unavailable(stats, comments, "玩车教授")
    return stats, [], None, "chemm.com target article public post_hits (rounded display), post_like, comment_count"


async def ijiwei(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://ijiwei.com/n/{wid}")
    item = assigned_json(response.text, "simpledata")
    stats = article_stats(item, wid, "news_id", {"views": "view_num", "likes": "like_num",
        "comments": "comment_num", "favorites": "collect_num"})
    result, following = [], None
    if comments:
        current = cursor or ""
        size = min(limit, 10)
        for step in range(1 if cursor else page):
            params = {"res_type": 1, "res_id": wid, "limit": size, "source": "pc", "token": ""}
            if current:
                params["after_id"] = current
            payload = await crawler._post_json("https://ijiwei.com/api/comment/newlist", data=params, headers={"Referer": url})
            data = payload.get("data") or {}
            if payload.get("errno") != 0:
                raise PlatformCrawlerError("爱集微评论接口返回业务错误")
            stats.comments = to_int(data.get("total_num"))
            rows = checked_list(data.get("list"), stats.comments, first=not current)
            more = len(rows) >= size
            following = str(rows[-1].get("comment_id") or "") if more else None
            require_page(rows, more, current, following)
            if not more:
                if step + 1 < (1 if cursor else page):
                    rows = []
                break
            current = following
        result = [comment(row.get("comment_id"), (row.get("user_info") or {}).get("nickname"),
            row.get("content"), likes=row.get("like_num"), replies=row.get("comment_num"),
            created=row.get("create_time")) for row in rows]
    return stats, result, following, "ijiwei.com target simpledata + api/comment/newlist"


async def jianshu(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    item = next_data(response.text).get("props", {}).get("initialState", {}).get("note", {}).get("data") or {}
    stats = article_stats(item, wid, "slug", {"views": "views_count", "likes": "likes_count", "comments": "comments_count"})
    zero_or_unavailable(stats, comments, "简书")
    return stats, [], None, "jianshu.com target initialState.note.data"


async def geekpark(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    item = assigned_json(response.text, "window.__INITIAL_STATE__").get("news", {}).get("post") or {}
    stats = article_stats(item, wid, "id", {"views": "views", "likes": "like_count", "comments": "comments_count"})
    result = []
    if comments:
        payload = await crawler._get_json(f"https://mainssl.geekpark.net/api/v1/posts/{wid}/comments",
            headers={"Referer": url, "X-Requested-With": "XMLHttpRequest"})
        rows = checked_list(payload.get("comments"), stats.comments, first=True)
        if page == 1 and not cursor:
            result = [comment(row.get("id"), (row.get("commenter_info") or [{}])[0].get("nickname"),
                row.get("content"), likes=row.get("like_count"),
                replies=len(row["childrens"]) if isinstance(row.get("childrens"), list) else None,
                created=row.get("created_at")) for row in rows if not row.get("parent_id")]
    return stats, result, None, "geekpark.net target news.post + api/v1/posts/comments"


async def tidenews(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    item = next_data(response.text).get("props", {}).get("pageProps", {}).get("article") or {}
    stats = article_stats(item, wid, "id", {"views": "read_count", "likes": "like_count",
        "shares": "share_count", "favorites": "collect_count"})
    zero_or_unavailable(stats, comments, "潮新闻")
    return stats, [], None, "tidenews.com.cn target pageProps.article"


async def acfun(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://www.acfun.cn/a/ac{wid}")
    item = assigned_json(response.text, "window.articleInfo")
    stats = article_stats(item, wid, "articleId", {"views": "viewCount", "likes": "likeCount",
        "comments": "commentCount", "shares": "shareCount", "favorites": "stowCount"})
    result, following = [], None
    if comments:
        current = int(cursor) if cursor else page
        payload = await crawler._get_json("https://www.acfun.cn/rest/pc-direct/comment/list", params={
            "sourceId": wid, "sourceType": 3, "page": current, "pivotCommentId": 0,
            "newPivotCommentId": "", "supportZtEmot": "true"}, headers={"Referer": url})
        if payload.get("result") != 0 or payload.get("curPage") != current or payload.get("sourceType") != 3:
            raise PlatformCrawlerError("AcFun 评论接口返回业务错误或页码不匹配")
        stats.comments = to_int(payload.get("commentCount"))
        rows = checked_list(payload.get("rootComments"), stats.comments, first=current == 1)
        more = current < int(payload.get("totalPage") or 0)
        require_page(rows, more, current, current + 1)
        following = str(current + 1) if more else None
        for row in rows:
            if row.get("sourceId") is not None and str(row["sourceId"]) != wid:
                raise PlatformCrawlerError("AcFun 评论不属于目标文章")
            result.append(comment(row.get("commentId"), row.get("userName"), row.get("content"),
                likes=row.get("likeCount"), replies=row.get("subCommentCount"), created=row.get("timestamp")))
    return stats, result, following, "acfun.cn target articleInfo + pc-direct/comment/list"


async def how42(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://www.42how.com/article/{wid}")
    state = nuxt_literal_state(response.text)
    item = state.get("data", [{}])[0].get("articleInfo") or {}
    # /view is the discussion service, not article readership. Neither
    # viewCount nor the undisplayed internal pv is a verified reading count.
    stats = article_stats(item, wid, "id", {"likes": "likeCount"})
    payload = await crawler._get_json("https://api.42how.com/view", params={
        "page": 1, "pageSize": 20, "orderBy": "like", "order": "DESC",
        "id": wid, "type": "article"}, headers={"Referer": url})
    data = payload.get("data") or {}
    if payload.get("code") != "00000":
        raise PlatformCrawlerError("42号车库评论接口返回业务错误")
    stats.comments = to_int(data.get("count"))
    rows = checked_list(data.get("list"), stats.comments, first=True)
    if stats.comments is None or (stats.comments == 0 and rows):
        raise PlatformCrawlerError("42号车库评论总数缺失或与列表矛盾")
    zero_or_unavailable(stats, comments, "42号车库")
    return stats, [], None, "42how.com target data.articleInfo + api.42how.com/view (nonempty bodies unverified)"


async def ql1d(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://www.ql1d.com/general/{wid}.html")
    state = nuxt_literal_state(response.text)
    item = state.get("data", [{}])[0].get("newsData") or {}
    stats = article_stats(item, wid, "newsId", {"views": "viewCount", "likes": "praisecount", "comments": "commentcount"})
    zero_or_unavailable(stats, comments, "齐鲁壹点")
    return stats, [], None, "ql1d.com target data.newsData"


COLLECTORS = {"guancha": guancha, "jike": jike, "chemm": chemm, "ijiwei": ijiwei, "jianshu": jianshu,
    "geekpark": geekpark, "tidenews": tidenews, "acfun": acfun, "how42": how42, "ql1d": ql1d}
