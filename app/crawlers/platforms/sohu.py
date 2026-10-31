"""Sohu MP topic counters and paged public comments."""
from __future__ import annotations
import re
from urllib.parse import urlparse
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.crawlers.platforms.public_news import PublicDataUnavailable
from app.models.engagement import EngagementResult, EngagementStats


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor, include_stats, include_comments):
    try:
        if urlparse(url).hostname == "3g.k.sohu.com":
            return await fetch_client_article(crawler, url, work_id, limit, page=page,
                comment_cursor=comment_cursor, include_comments=include_comments)
        response = await crawler._get_response(url.replace("http://", "https://", 1))
        text = response.text
        article = re.search(r'(?:news_id|newsId)\s*:\s*["\'](\d+)', text)
        media = re.search(r'(?:media_id|authorId)\s*:\s*["\'](\d+)', text)
        if not article or not media or article[1] != work_id:
            raise PlatformCrawlerError("搜狐页面缺少目标文章配置；客户端时间线/受限分享页不能当成文章")
        if re.search(r"\b__autoArticleConfig\s*=", text):
            return await fetch_auto_article(crawler, url, work_id, limit, page=page,
                include_stats=include_stats, include_comments=include_comments)
        source = "mp_" + article[1]
        size = min(limit, COMMENT_PAGE_SIZE)
        data = await crawler._get_json("https://uis.mp.sohu.com/wap/api/comments", params={
            "pageSize": size, "type": 2, "pageNo": page if include_comments else 1,
            "sourceId": source, "accountId": media[1],
        }, headers={"Referer": url})
        if data.get("code") != 1 or not isinstance((data.get("data") or {}).get("comments"), list):
            raise PlatformCrawlerError("搜狐评论接口失败或缺少列表")
        body = data["data"]
        total = to_int(body.get("totalCount"))
        if total is None:
            raise PlatformCrawlerError("搜狐未返回评论总数")
        stats = EngagementStats(comments=total)
        if include_stats:
            counters = await crawler._get_json("https://uis.mp.sohu.com/wap/api/static/topic", params={"sourceId":source})
            if counters.get("code") == 1:
                target = (counters.get("data") or {}).get(source) or {}
                stats.likes = to_int((target.get("12") or {}).get("totalCount"))
                stats.favorites = to_int((target.get("11") or {}).get("totalCount"))
            pv = await crawler._get_response(f"https://v2.sohu.com/public-api/articles/{article[1]}/pv")
            if re.fullmatch(r"\s*\d+\s*", pv.text):
                stats.views = to_int(pv.text)
        items = body["comments"]
        more = page * size < total
        require_page(items, more, page, page + 1)
        users = body.get("users") or {}
        comments = []
        if include_comments:
            for x in items:
                user = x.get("user") or users.get(str(x.get("userId"))) or {}
                comments.append(comment(x.get("id") or x.get("commentId"), user.get("nickname") or user.get("name"),
                    x.get("content"), likes=x.get("likeCount"), replies=x.get("replyCount"), created=x.get("createTime")))
        return EngagementResult(platform="sohu", canonical_url=url, work_id=work_id,
            coverage="partial", stats=stats, comments=comments,
            next_cursor=str(page + 1) if more and include_comments else None,
            source="uis.mp.sohu.com/wap/api + public-api/articles/pv", reason="搜狐号公开互动和一级评论")
    except PublicDataUnavailable as exc:
        return result_error("sohu", url, work_id, "unsupported", exc).model_copy(update={"retryable": False})
    except PlatformBlockedError as exc:
        return result_error("sohu", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("sohu", url, work_id, "failed", exc)


async def fetch_auto_article(crawler, url, work_id, limit, *, page, include_stats, include_comments):
    # Auto articles have migrated to a separate comment service. The old MP
    # endpoint returns an apparently valid, but empty, thread for these IDs.
    size = min(limit, COMMENT_PAGE_SIZE)
    payload = await crawler._get_json("https://portal.auto.sohu.com/comment/list", params={
        "newsId": work_id, "pageSize": size,
        "pageNo": page if include_comments else 1, "businessType": "article",
    }, headers={"Referer": url})
    body = payload.get("data") or {}
    if (payload.get("code") == 200 and body.get("source_id") is None
            and body.get("topic_id") == 0 and body.get("total_count") == 0
            and body.get("comments") == []):
        # An uncreated thread is not an identified zero-comment discussion.
        if include_comments:
            raise PublicDataUnavailable("搜狐汽车文章的公开评论线程尚未建立")
        pv = await crawler._get_response(f"https://v2.sohu.com/public-api/articles/{work_id}/pv")
        if not re.fullmatch(r"\s*\d+\s*", pv.text):
            raise PlatformCrawlerError("搜狐文章未提供有效阅读量或评论线程")
        return EngagementResult(platform="sohu", canonical_url=url, work_id=work_id,
            coverage="partial", stats=EngagementStats(views=to_int(pv.text)),
            source="v2.sohu.com/public-api/articles/pv",
            reason="目标文章 PV；公开评论线程尚未建立，评论数保留 null")
    if (payload.get("code") != 200 or body.get("source_id") != "mp_" + work_id
            or not isinstance(body.get("comments"), list)):
        raise PlatformCrawlerError("搜狐汽车评论接口失败或不是目标文章")
    total = to_int(body.get("total_count"))
    if total is None:
        raise PlatformCrawlerError("搜狐汽车未返回评论总数")
    participation = to_int(body.get("participation_sum"))
    stats = EngagementStats(comments=participation if participation is not None else total)
    if include_stats:
        pv = await crawler._get_response(f"https://v2.sohu.com/public-api/articles/{work_id}/pv")
        if re.fullmatch(r"\s*\d+\s*", pv.text):
            stats.views = to_int(pv.text)
    items = body["comments"]
    more = page * size < total
    require_page(items, more, page, page + 1)
    comments = []
    if include_comments:
        for item in items:
            if str(item.get("topic_id")) != str(body.get("topic_id")) or body.get("topic_id") is None:
                raise PlatformCrawlerError("搜狐汽车评论不属于目标文章的话题")
            if item.get("reply_id") not in (None, 0, "0"):
                continue
            comments.append(comment(item.get("id"), item.get("user_name"), item.get("content"),
                likes=item.get("like_count"), replies=item.get("reply_count"), created=item.get("date")))
    return EngagementResult(platform="sohu", canonical_url=url, work_id=work_id,
        coverage="partial", stats=stats, comments=comments,
        next_cursor=str(page + 1) if more and include_comments else None,
        source="portal.auto.sohu.com/comment/list + public-api/articles/pv",
        reason="搜狐汽车公开一级评论及文章 PV；未公开的点赞和收藏数保留 null")


async def fetch_client_article(crawler, url, work_id, limit, *, page, comment_cursor, include_comments):
    response = await crawler._get_response(url.replace("http://", "https://", 1))
    target = re.search(r"newsId\s*:\s*['\"]?(\d+)", response.text)
    if not target or target[1] != work_id:
        raise PlatformBlockedError("搜狐客户端分享页未提供目标文章配置")
    size = min(limit, COMMENT_PAGE_SIZE)
    params = {"rollType":1, "type":5, "id":work_id, "busiCode":2,
        "size":size, "h5show":1, "reqFrom":"h5", "page":page}
    if comment_cursor:
        params["cursorId"] = comment_cursor
    payload = await crawler._get_json("https://3g.k.sohu.com/api/comment/getCommentListByCursor.go", params=params, headers={"Referer":url})
    data = payload.get("response") or {}
    if payload.get("isSuccess") != "S" or str(data.get("newsId")) != work_id or not isinstance(data.get("commentList"), list):
        raise PlatformCrawlerError("搜狐客户端分享页评论接口失败或作品不匹配")
    total = to_int(data.get("allCount"))
    if total is None:
        raise PlatformCrawlerError("搜狐客户端未返回评论计数")
    items = data["commentList"]
    more = page * size < (to_int(data.get("totalCount")) or 0)
    require_page(items, more, comment_cursor, data.get("cursorId"))
    comments = [comment(x.get("commentId") or x.get("id"), x.get("author"),
        x.get("content"), likes=x.get("digNum"), replies=x.get("replyCount"), created=x.get("ctime")) for x in items] if include_comments else []
    return EngagementResult(platform="sohu", canonical_url=url, work_id=work_id,
        coverage="partial", stats=EngagementStats(comments=total, favorites=to_int(data.get("favoriteCount"))),
        comments=comments, next_cursor=str(data["cursorId"]) if more and include_comments else None,
        source="3g.k.sohu.com/api/comment/getCommentListByCursor.go", reason="搜狐客户端分享页的公开评论和收藏数")
