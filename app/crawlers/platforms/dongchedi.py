"""Dongchedi public PC detail API and its article comment proxy."""
from __future__ import annotations
from urllib.parse import parse_qs, urlparse
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.toutiao import parse_comments
from app.crawlers.platforms.web_sources_common import require_page
from app.models.engagement import EngagementResult, EngagementStats


def validate_comment_target(payload, target, work_id):
    repost = payload.get("repost_params") or {}
    binding = repost.get("fw_id")
    if binding in (None, 0, "0") or str(binding) == work_id:
        return
    # The official PC proxy serializes this numeric field through JavaScript.
    # Only tolerate binary64 rounding when independently bound title/author agree.
    author = str((target.get("media_user") or {}).get("id") or "")
    if (type(binding) is int and work_id.isdigit() and len(work_id) <= 20
            and 2**53 < binding < 2**64 and int(work_id) > 2**53
            and float(binding) == float(work_id)
            and target.get("title") and repost.get("title") == target["title"]
            and author and str(repost.get("fw_user_id")) == author
            and str((payload.get("group") or {}).get("user_id")) == author):
        return
    raise PlatformCrawlerError("懂车帝评论响应作品 ID 不匹配")


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor, include_stats, include_comments):
    canonical = f"https://www.dongchedi.com/article/{work_id}"
    try:
        detail = await crawler._get_json("https://www.dongchedi.com/motor/pc/common/article/detail",
            params={"group_id":work_id}, headers={"Referer":canonical})
        target = detail.get("data") or {}
        target_url = urlparse(str(target.get("url") or ""))
        exact_target = (target_url.hostname in {"toutiao.com", "www.toutiao.com", "www.dongchedi.com", "dongchedi.com"}
                        and target_url.path.rstrip("/") in {f"/group/{work_id}", f"/article/{work_id}", f"/item/{work_id}", f"/i{work_id}"})
        if target_url.scheme == "sslocal" and target_url.hostname == "thread_detail":
            exact_target = parse_qs(target_url.query).get("tid") == [work_id]
        if detail.get("status") != 0 or not target.get("title") or not exact_target:
            raise PlatformCrawlerError("懂车帝未返回目标作品详情")
        if target.get("is_visible") is False:
            raise PlatformBlockedError("懂车帝目标作品不可见")
        stats = EngagementStats(views=to_int(target.get("watch_count")), likes=to_int(target.get("digg_count")), comments=to_int(target.get("comment_count")))
        comments, following = [], None
        if include_comments:
            size = min(limit, COMMENT_PAGE_SIZE)
            offset = int(comment_cursor) if comment_cursor is not None else (page - 1) * size
            payload = await crawler._get_json("https://www.dongchedi.com/motor/proxy/comment_list/article/v3/tab_comments/", params={
                "aid":1839,"app_name":"auto_web_pc","group_id":work_id,"item_id":work_id,"count":size,"offset":offset,
            }, headers={"Referer":canonical})
            if payload.get("err_no") != 0 or not isinstance(payload.get("data"), list) or "has_more" not in payload:
                raise PlatformCrawlerError("懂车帝未返回有效评论页")
            validate_comment_target(payload, target, work_id)
            stats.comments = to_int(payload.get("total_number"))
            comments = parse_comments(payload["data"])
            next_offset = to_int(payload.get("offset"))
            require_page(comments, payload["has_more"], offset, next_offset)
            following = str(next_offset) if payload["has_more"] else None
        return EngagementResult(platform="dongchedi", canonical_url=canonical, work_id=work_id,
            coverage="partial", stats=stats, comments=comments, next_cursor=following,
            source="motor/pc/common/article/detail + motor/proxy/comment_list", reason="公开阅读、点赞、评论数和一级评论")
    except PlatformBlockedError as exc:
        return result_error("dongchedi", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("dongchedi", url, work_id, "failed", exc)
