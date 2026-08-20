"""Toutiao article SSR counters and anonymous paged-comment flow."""

from __future__ import annotations

from datetime import datetime, timezone
import html
import re
from typing import Any
from urllib.parse import unquote

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.models.engagement import EngagementComment, EngagementResult, EngagementStats


COMMENT_URL = "https://www.toutiao.com/article/v4/tab_comments/"


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
    try:
        stats = EngagementStats()
        sources: list[str] = []
        reason = ""
        if include_stats and not include_comments:
            try:
                article_response = await crawler._get_response(
                    f"https://www.toutiao.com/article/{work_id}/",
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "Referer": f"https://www.toutiao.com/article/{work_id}/",
                    },
                )
                stats = parse_stats(str(getattr(article_response, "text", "") or ""))
                if _has_stats(stats):
                    sources.append("article SSR itemCounter")
                else:
                    reason = "头条文章 SSR 未返回 itemCounter，当前只报告可验证字段"
            except PlatformCrawlerError as exc:
                reason = f"头条文章 SSR 请求受阻，互动统计不可用: {exc}"

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

        payload = await crawler._get_json(
            COMMENT_URL,
            params={
                "aid": "24",
                "app_name": "toutiao_web",
                "offset": str((page - 1) * min(limit, COMMENT_PAGE_SIZE)),
                "count": min(limit, COMMENT_PAGE_SIZE),
                "group_id": work_id,
                "item_id": work_id,
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"https://www.toutiao.com/article/{work_id}/",
            },
        )
        if payload.get("err_no") != 0:
            raise PlatformCrawlerError("toutiao comments payload is unavailable")
        comments = parse_comments(payload.get("data") or [])
        total = to_int(payload.get("total_number"))
        offset = to_int(payload.get("offset"))
        has_more = bool(payload.get("has_more"))
        return EngagementResult(
            platform="toutiao",
            canonical_url=f"https://www.toutiao.com/article/{work_id}/",
            work_id=work_id,
            coverage="partial",
            reason="头条评论接口可匿名读取指定页，不能证明评论全集",
            source="article/v4/tab_comments",
            stats=EngagementStats(comments=total),
            comments=comments[:limit],
            next_cursor=str(offset) if has_more and offset is not None else None,
        )
    except PlatformBlockedError as exc:
        return result_error("toutiao", url, work_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("toutiao", url, work_id, "failed", str(exc))


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


def parse_stats(text: str) -> EngagementStats:
    """Parse public article counters from plain or URL-encoded SSR JSON."""

    decoded = html.unescape(text)
    for _ in range(2):
        decoded = unquote(decoded)
    decoded = decoded.replace(r"\u0022", '"').replace(r'\"', '"')
    match = re.search(r'"itemCounter"\s*:\s*\{([^{}]+)\}', decoded, re.S)
    body = match.group(1) if match else decoded
    values = {
        key: to_int(value)
        for key, value in re.findall(
            r'"(commentCount|diggCount|readCount|shareCount|showCount)"\s*:\s*"?([^",}\s]+)',
            body,
        )
    }
    like_match = re.search(
        r'"likeData"\s*:\s*\{[^{}]*"count"\s*:\s*"?([^",}\s]+)',
        decoded,
        re.S,
    )
    if values.get("diggCount") is None and like_match:
        values["diggCount"] = to_int(like_match.group(1))
    return EngagementStats(
        views=values.get("readCount"),
        likes=values.get("diggCount"),
        comments=values.get("commentCount"),
        shares=values.get("shareCount"),
        **{"show_count": values.get("showCount")},
    )


def _has_stats(stats: EngagementStats) -> bool:
    return any(value is not None for value in stats.model_dump().values())
