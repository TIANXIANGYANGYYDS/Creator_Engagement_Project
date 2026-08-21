"""Weibo mobile detail and cursor-based comment flow."""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import re
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import result_error, to_int
from app.models.engagement import EngagementComment, EngagementResult, EngagementStats


async def fetch(
    crawler: PlatformCrawlerContext,
    url: str,
    work_id: str,
    limit: int,
    *,
    page: int,
    include_stats: bool,
    include_comments: bool,
) -> EngagementResult:
    cookie = crawler._platform_cookie("weibo")
    headers = {
        "Referer": f"https://m.weibo.cn/detail/{work_id}",
        "X-Requested-With": "XMLHttpRequest",
        "MWeibo-Pwa": "1",
    }
    if cookie:
        headers["Cookie"] = cookie
        xsrf_match = re.search(r"(?:^|;\s*)XSRF-TOKEN=([^;]+)", cookie, re.I)
        if xsrf_match:
            headers["X-XSRF-TOKEN"] = unquote(xsrf_match.group(1))
    try:
        stats = EngagementStats()
        comments: list[EngagementComment] = []
        cursor: str | None = None
        sources: list[str] = []
        if include_stats:
            payload = await crawler._get_json(
                "https://m.weibo.cn/statuses/show",
                params={"id": work_id},
                headers=headers,
            )
            data = payload.get("data") or {}
            stats = EngagementStats(
                likes=to_int(data.get("attitudes_count")),
                comments=to_int(data.get("comments_count")),
                reposts=to_int(data.get("reposts_count")),
            )
            sources.append("m.weibo.cn/statuses/show")
        if include_comments:
            request_cursor: str | None = None
            request_cursor_type = 0
            used_numbered_fallback = False
            for current_page in range(1, page + 1):
                params: dict[str, Any] = {
                    "id": work_id,
                    "mid": work_id,
                    "max_id_type": request_cursor_type,
                }
                if request_cursor is not None:
                    params["max_id"] = request_cursor
                comments_payload = await crawler._get_json(
                    "https://m.weibo.cn/comments/hotflow",
                    params=params,
                    headers=headers,
                )
                if not response_ok(comments_payload):
                    comments_payload = await crawler._get_json(
                        "https://m.weibo.cn/api/comments/show",
                        params={"id": work_id, "page": page},
                        headers=headers,
                    )
                    if not response_ok(comments_payload):
                        raise PlatformBlockedError(
                            "微博热门流和匿名页码接口均要求登录或触发访问限制"
                        )
                    comments, cursor = parse_numbered_comments(comments_payload, page)
                    if not include_stats:
                        comment_data = comments_payload.get("data") or {}
                        stats = EngagementStats(
                            comments=to_int(comment_data.get("total_number"))
                        )
                    used_numbered_fallback = True
                    break
                comments, cursor = parse_comments(comments_payload)
                comment_data = comments_payload.get("data") or {}
                request_cursor_type = to_int(comment_data.get("max_id_type")) or 0
                if not include_stats:
                    total = to_int(
                        comment_data.get("total_number")
                        or comments_payload.get("total_number")
                    )
                    if total is not None:
                        stats = EngagementStats(comments=total)
                if current_page == page:
                    break
                if cursor is None:
                    comments = []
                    break
                request_cursor = cursor
            sources.append(
                "m.weibo.cn/api/comments/show"
                if used_numbered_fallback
                else "m.weibo.cn/comments/hotflow"
            )
        return EngagementResult(
            platform="weibo",
            canonical_url=f"https://m.weibo.cn/detail/{work_id}",
            work_id=work_id,
            coverage="partial",
            reason=(
                (
                    "微博当前会话可读取热门评论分页，不能证明隐藏或已删除评论全集"
                    if cookie
                    else "微博访客评论接口可能折叠或限流，不能证明评论全集"
                )
                if include_comments else "微博访客详情接口提供当前公开互动量"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments[:limit],
            next_cursor=cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("weibo", url, work_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("weibo", url, work_id, "failed", str(exc))


def parse_comments(payload: dict[str, Any]) -> tuple[list[EngagementComment], str | None]:
    data = payload.get("data") or {}
    result: list[EngagementComment] = []
    for item in data.get("data") or []:
        user = item.get("user") or {}
        comment_id = str(item.get("idstr") or item.get("id") or "")
        if not comment_id:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(user.get("screen_name") or ""),
            text=parse_comment_text(item.get("text")),
            created_at=parse_date(item.get("created_at")),
            likes=to_int(item.get("like_count")),
            replies=to_int(item.get("total_number")),
        ))
    return result, str(data.get("max_id")) if data.get("max_id") else None


def parse_numbered_comments(
    payload: dict[str, Any],
    page: int,
) -> tuple[list[EngagementComment], str | None]:
    comments, _ = parse_comments(payload)
    data = payload.get("data") or {}
    max_page = to_int(data.get("max"))
    next_page = str(page + 1) if max_page is not None and page < max_page else None
    return comments, next_page


def response_ok(payload: dict[str, Any]) -> bool:
    try:
        return int(payload.get("ok", 1)) == 1
    except (TypeError, ValueError):
        return False


def parse_comment_text(value: Any) -> str:
    soup = BeautifulSoup(str(value or ""), "html.parser")
    for image in soup.find_all("img"):
        image.replace_with(str(image.get("alt") or ""))
    return soup.get_text(" ", strip=True)


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None
