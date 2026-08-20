"""Haokan anonymous SSR statistics and paged-comment flow."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
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
    try:
        canonical_url = f"https://haokan.baidu.com/v?vid={work_id}"
        stats = EngagementStats()
        sources: list[str] = []
        if include_stats:
            try:
                await crawler._get_response(
                    "https://haokan.baidu.com/",
                    headers={"Referer": "https://haokan.baidu.com/"},
                )
                detail_response = await crawler._get_response(
                    canonical_url,
                    headers={"Referer": "https://haokan.baidu.com/"},
                )
                stats = parse_ssr_stats(
                    str(getattr(detail_response, "text", "") or ""),
                    work_id,
                )
                if _has_stats(stats):
                    sources.append("haokan target-page SSR")
            except PlatformCrawlerError:
                pass

        comments: list[EngagementComment] = []
        next_cursor: str | None = None
        if include_comments or stats.comments is None:
            payload = await crawler._get_json(
                "https://haokan.baidu.com/haokan/ui-web/v2/comment/get",
                params={
                    "rn": min(limit, COMMENT_PAGE_SIZE),
                    "url_key": work_id,
                    "pn": page if include_comments else 1,
                    "child_rn": 2,
                },
                headers={"Referer": canonical_url},
            )
            data = payload.get("data") or {}
            comments = parse_comments(data.get("list") or []) if include_comments else []
            count = to_int(data.get("comment_count"))
            if count is not None:
                stats.comments = count
            if include_comments and not data.get("is_over"):
                next_cursor = str(page + 1)
            sources.append("haokan/ui-web/v2/comment/get")

        return EngagementResult(
            platform="haokan",
            canonical_url=canonical_url,
            work_id=work_id,
            coverage="partial",
            reason=(
                "好看评论接口可匿名读取指定页，不能证明评论全集"
                if include_comments
                else "好看目标页 SSR 提供播放、点赞和评论数；未公开收藏/分享数字"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments[:limit],
            next_cursor=next_cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("haokan", url, work_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("haokan", url, work_id, "failed", str(exc))


def parse_ssr_stats(text: str, work_id: str) -> EngagementStats:
    soup = BeautifulSoup(text, "html.parser")
    canonical = soup.find("meta", attrs={"property": "og:url"})
    canonical_url = str(canonical.get("content") or "") if canonical else ""
    if work_id not in canonical_url:
        return EngagementStats()

    description = ""
    for attrs in ({"name": "description"}, {"property": "og:description"}):
        node = soup.find("meta", attrs=attrs)
        if node and node.get("content"):
            description = str(node.get("content"))
            break
    view_match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?(?:万|亿)?)次播放", description)
    comment_node = soup.select_one(".ssr-icon-comment")
    like_node = soup.select_one(".ssr-icon-like")
    return EngagementStats(
        views=to_int(view_match.group(1)) if view_match else None,
        likes=to_int(like_node.get_text(strip=True)) if like_node else None,
        comments=to_int(comment_node.get_text(strip=True)) if comment_node else None,
    )


def parse_comments(items: list[Any]) -> list[EngagementComment]:
    result: list[EngagementComment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        comment_id = str(item.get("reply_id") or item.get("id") or "")
        if not comment_id:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(item.get("uname") or item.get("user_name") or ""),
            text=BeautifulSoup(str(item.get("content") or ""), "html.parser").get_text(" ", strip=True),
            created_at=(
                datetime.fromtimestamp(int(item.get("create_time") or 0), tz=timezone.utc)
                if item.get("create_time") else None
            ),
            likes=to_int(item.get("like_count")),
            replies=to_int(item.get("reply_count")),
        ))
    return result


def _has_stats(stats: EngagementStats) -> bool:
    return any(value is not None for value in stats.model_dump().values())
