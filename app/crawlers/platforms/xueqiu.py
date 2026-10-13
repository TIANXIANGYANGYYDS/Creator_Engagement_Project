"""Xueqiu public status API; challenge responses remain blocked, never zero."""
from __future__ import annotations
from app.crawlers.http_client import PlatformBlockedError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementResult, EngagementStats


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor, include_stats, include_comments):
    try:
        await crawler._get_response("https://www.xueqiu.com/", headers={"Referer":url})
        data = await crawler._get_json("https://www.xueqiu.com/statuses/show.json", params={"id":work_id}, headers={"Referer":url})
        if str(data.get("id")) != work_id:
            raise PlatformBlockedError("雪球未返回目标帖子，游客会话可能受限或需要验证")
        stats = EngagementStats(views=to_int(data.get("view_count")), likes=to_int(data.get("like_count")), comments=to_int(data.get("reply_count")),
            reposts=to_int(data.get("retweet_count")), favorites=to_int(data.get("fav_count")))
        comments, following = [], None
        if include_comments:
            size = min(limit, COMMENT_PAGE_SIZE)
            payload = await crawler._get_json("https://www.xueqiu.com/statuses/comments.json", params={
                "id":work_id,"count":size,"page":page,"reply":"false","asc":"false",
            }, headers={"Referer":url})
            if not isinstance(payload.get("comments"), list) or "count" not in payload:
                raise PlatformBlockedError("雪球评论未返回有效列表，可能需要登录/验证")
            items = payload["comments"]
            comments = [comment(x.get("id"), (x.get("user") or {}).get("screen_name"), x.get("text"),
                likes=x.get("like_count"), replies=x.get("reply_count"), created=x.get("created_at")) for x in items]
            total = to_int(payload.get("count"))
            more = page * size < total if total is not None else len(items) >= size
            require_page(items, more, page, page + 1)
            following = str(page + 1) if more else None
        return EngagementResult(platform="xueqiu", canonical_url=url, work_id=work_id,
            coverage="partial", stats=stats, comments=comments, next_cursor=following,
            source="xueqiu statuses/show.json + statuses/comments.json", reason="雪球会话可见数据；游客可能被验证或登录限制")
    except PlatformBlockedError as exc:
        return result_error("xueqiu", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("xueqiu", url, work_id, "failed", exc)
