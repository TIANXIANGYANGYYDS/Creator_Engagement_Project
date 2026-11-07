"""Xueqiu public status API; challenge responses remain blocked, never zero."""
from __future__ import annotations
import re

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementResult, EngagementStats


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor, include_stats, include_comments):
    try:
        await crawler._get_response("https://www.xueqiu.com/", headers={"Referer":url})
        data = await crawler._get_json("https://www.xueqiu.com/statuses/show.json", params={"id":work_id}, headers={"Referer":url})
        if str(data.get("id")) != work_id:
            raise PlatformBlockedError("雪球未返回目标帖子，游客会话可能受限或需要验证")
        # The public status API returns a zero placeholder even for read posts;
        # the website does not expose a corresponding public reading counter.
        views = to_int(data.get("view_count"))
        stats = EngagementStats(views=views if views is not None and views > 0 else None, likes=to_int(data.get("like_count")), comments=to_int(data.get("reply_count")),
            reposts=to_int(data.get("retweet_count")), favorites=to_int(data.get("fav_count")))
        comments, following = [], None
        if include_comments:
            size = min(limit, COMMENT_PAGE_SIZE)
            max_id, kind = -1, 4
            if comment_cursor:
                match = re.fullmatch(r"xq:(\d+):(\d+):(\d+)", comment_cursor)
                if not match or match[1] != work_id or int(match[2]) not in {2, 4}:
                    raise PlatformCrawlerError("雪球评论游标与目标内容不匹配")
                kind, max_id = int(match[2]), int(match[3])
            for step in range(1 if comment_cursor else page):
                payload = await crawler._get_json("https://www.xueqiu.com/statuses/v3/comments.json", params={
                    "id": work_id, "size": size, "type": kind, "max_id": max_id,
                }, headers={"Referer": url})
                items = payload.get("comments")
                total = to_int(payload.get("status_reply_count"))
                roots = to_int(payload.get("comment_tl_count"))
                next_id, next_kind = payload.get("next_max_id"), payload.get("type")
                if (payload.get("error_code") or not isinstance(items, list) or total is None
                        or roots is None or type(next_id) is not int or next_id < -1
                        or next_kind not in {2, 4}):
                    raise PlatformBlockedError("雪球 v3 评论未返回有效列表、总数或分页状态")
                if max_id == -1 and roots > 0 and not items:
                    raise PlatformCrawlerError("雪球评论总数非零但首屏为空")
                stats.comments = total
                more = next_id != -1
                require_page(items, more, max_id, next_id)
                comments = []
                for item in items:
                    # Nested child_comments are previews; only retain their count.
                    if item.get("in_reply_to_comment_id") or to_int(item.get("root_reply_to_cid")) not in {None, -1, 0}:
                        continue
                    if str(item.get("root_in_reply_to_status_id")) != work_id:
                        raise PlatformCrawlerError("雪球一级评论不属于目标帖子")
                    comments.append(comment(item.get("id"), (item.get("user") or {}).get("screen_name"),
                        item.get("text"), likes=item.get("like_count"), replies=item.get("reply_count"),
                        created=item.get("created_at")))
                following = f"xq:{work_id}:{next_kind}:{next_id}" if more else None
                if not more:
                    if step + 1 < (1 if comment_cursor else page):
                        comments = []
                    break
                max_id, kind = next_id, next_kind
        return EngagementResult(platform="xueqiu", canonical_url=url, work_id=work_id,
            coverage="partial", stats=stats, comments=comments, next_cursor=following,
            source="xueqiu statuses/show.json + statuses/v3/comments.json", reason="雪球会话可见数据；阅读量的零值无法确认，保留 null；一级评论按 v3 游标分页，游客仍可能被筛选或验证限制")
    except PlatformBlockedError as exc:
        return result_error("xueqiu", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("xueqiu", url, work_id, "failed", exc)
