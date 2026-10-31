"""Toutiao article SSR counters and anonymous paged-comment flow."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.registry import is_toutiao_share_url, toutiao_share_target
from app.crawlers.platforms.web_sources_common import require_page
from app.models.engagement import EngagementComment, EngagementResult, EngagementStats


COMMENT_URL = "https://www.toutiao.com/article/v4/tab_comments/"


async def fetch(
    crawler: PlatformCrawlerContext,
    url: str,
    work_id: str,
    limit: int,
    *,
    page: int,
    comment_cursor: str | None,
    include_stats: bool,
    include_comments: bool,
) -> EngagementResult:
    try:
        share_response = None
        if is_toutiao_share_url(url):
            share_response = await crawler._get_response(url)
            target_url = str(getattr(share_response, "url", ""))
            try:
                target_platform, work_id = toutiao_share_target(target_url)
            except ValueError as exc:
                raise PlatformBlockedError(str(exc)) from exc
            if target_platform == "weitoutiao":
                from app.crawlers.platforms.bytedance_publishers import fetch as fetch_micro

                return await fetch_micro(crawler, target_url, work_id, limit, page=page,
                    comment_cursor=comment_cursor, include_stats=include_stats,
                    include_comments=include_comments)
        stats = EngagementStats()
        sources: list[str] = []
        reason = ""
        if include_stats:
            try:
                article_response = share_response or await crawler._get_response(
                    f"https://www.toutiao.com/article/{work_id}/",
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Referer": f"https://www.toutiao.com/article/{work_id}/",
                    },
                    force_direct=True,
                )
                stats = parse_stats(str(getattr(article_response, "text", "") or ""), work_id)
                if _has_stats(stats):
                    sources.append("article SSR itemCounter")
                else:
                    reason = "头条文章 SSR 未返回 itemCounter，当前只报告可验证字段"
            except PlatformCrawlerError as exc:
                reason = f"头条文章 SSR 请求受阻，互动统计不可用: {exc}"

        comments, following = [], None
        if include_comments or not _has_stats(stats):
            comments, stats.comments, following = await fetch_comment_page(
                crawler, work_id, limit, page=page, comment_cursor=comment_cursor,
            )
            if not sources and include_stats:
                reason = "文章页面统计不可用，仅取得经目标 ID 核验的评论数；阅读/点赞等缺失字段保留 null"
            sources.append("article/v4/tab_comments (verified group_id)")
        if not include_comments:
            return EngagementResult(
                platform="toutiao",
                canonical_url=f"https://www.toutiao.com/article/{work_id}/",
                work_id=work_id,
                coverage="partial",
                reason=reason or "头条互动统计来自文章 SSR，字段可能随页面挑战而缺失",
                source=" + ".join(sources) or "article SSR",
                stats=stats,
            )

        return EngagementResult(
            platform="toutiao",
            canonical_url=f"https://www.toutiao.com/article/{work_id}/",
            work_id=work_id,
            coverage="partial",
            reason=reason or "头条评论按时间读取指定页，不能证明评论全集；未确认阅读量保留 null",
            source=" + ".join(sources),
            stats=stats,
            comments=comments[:limit],
            next_cursor=following,
        )
    except PlatformBlockedError as exc:
        return result_error("toutiao", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("toutiao", url, work_id, "failed", exc)


async def fetch_comment_page(crawler, work_id, limit, *, page, comment_cursor):
    size = min(limit, COMMENT_PAGE_SIZE)
    offset = int(comment_cursor) if comment_cursor is not None else (page - 1) * size
    payload = await crawler._get_json(
        COMMENT_URL,
        params={
            "aid": "24",
            "app_name": "toutiao_web",
            "offset": str(offset),
            "count": size,
            "group_id": work_id,
            "item_id": work_id,
            "tab_index": 1,
        },
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://www.toutiao.com/article/{work_id}/",
        },
    )
    return parse_comment_page(payload, work_id, offset)


def parse_comment_page(payload, work_id, offset):
    group = payload.get("group") or {}
    if (payload.get("err_no") != 0 or not isinstance(payload.get("data"), list)
            or payload.get("has_more") not in (True, False, 0, 1)
            or str(group.get("group_id")) != work_id):
        raise PlatformCrawlerError("头条评论接口失败或不是目标作品")
    comments = parse_comments(payload["data"])
    total = to_int(payload.get("total_number"))
    if total is None or total < 0 or (not comments and not group.get("user_id")):
        raise PlatformCrawlerError("头条未返回可验证的评论计数，不能将不存在的作品判为零评论")
    next_offset = to_int(payload.get("offset"))
    has_more = bool(payload.get("has_more"))
    require_page(comments, has_more, offset, next_offset)
    if has_more and next_offset <= offset:
        raise PlatformCrawlerError("头条评论分页游标未前进")
    return comments, total, str(next_offset) if has_more else None


def parse_comments(items: list[Any]) -> list[EngagementComment]:
    result: list[EngagementComment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        comment = item.get("comment") or {}
        comment_id = str(comment.get("id_str") or comment.get("id") or "")
        if not comment_id:
            continue
        created_at = comment.get("create_time")
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(comment.get("user_name") or ""),
            text=BeautifulSoup(str(comment.get("text") or ""), "html.parser").get_text(" ", strip=True),
            created_at=(
                datetime.fromtimestamp(int(created_at), tz=timezone.utc)
                if created_at else None
            ),
            likes=to_int(comment.get("digg_count")),
            replies=to_int(comment.get("reply_count")),
        ))
    return result


def parse_stats(text: str, work_id: str) -> EngagementStats:
    """Read only the target article's SSR cell, excluding recommendations."""
    node = BeautifulSoup(text, "html.parser").find("script", id="RENDER_DATA")
    if not node or not node.string:
        return EngagementStats()
    try:
        data = json.loads(unquote(node.string)).get("data") or {}
    except (ValueError, AttributeError) as exc:
        raise PlatformCrawlerError("头条 SSR 数据无效") from exc
    cell = data.get("itemCell") or {}
    identity = data.get("groupId") or (cell.get("articleBase") or {}).get("groupID")
    if str(identity) != work_id:
        raise PlatformCrawlerError("头条 SSR 不是目标作品")
    values = cell.get("itemCounter") or {}
    likes = values.get("diggCount")
    if likes is None:
        likes = (data.get("likeData") or {}).get("count")
    return EngagementStats(
        # Zero is not exposed as a reading counter on the public article page.
        # showCount is impressions and must not substitute for reads.
        views=public_read_count(values.get("readCount")),
        likes=to_int(likes),
        comments=to_int(values.get("commentCount")),
        shares=to_int(values.get("shareCount")),
        **{"show_count": to_int(values.get("showCount"))},
    )


def public_read_count(value) -> int | None:
    count = to_int(value)
    return count if count is not None and count > 0 else None


def _has_stats(stats: EngagementStats) -> bool:
    return any(value is not None for value in stats.model_dump().values())
