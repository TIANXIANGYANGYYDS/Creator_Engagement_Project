"""Douyin detail and paged public-comment protocol flow."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import (
    COMMENT_PAGE_SIZE,
    DESKTOP_USER_AGENT,
    result_error,
    to_int,
)
from app.models.engagement import EngagementComment, EngagementResult, EngagementStats


DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
COMMENT_URL = "https://www.douyin.com/aweme/v1/web/comment/list/"


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
    if not crawler.cookies.strip():
        return result_error(
            "douyin",
            url,
            work_id,
            "unsupported",
            "抖音详情/评论请求需要 a_bogus 之外的动态设备会话；需调用方提供自己的有效会话 Cookie，不能硬编码临时 Cookie",
        )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": f"https://www.douyin.com/video/{work_id}",
        "User-Agent": DESKTOP_USER_AGENT,
    }
    common_params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "aweme_id": work_id,
        "request_source": "600",
        "origin_type": "video_page",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "support_h265": "1",
        "support_dash": "1",
        "cpu_core_num": "8",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "124.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "124.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "device_memory": "8",
        "platform": "PC",
    }
    try:
        stats = EngagementStats()
        comments: list[EngagementComment] = []
        next_cursor: str | None = None
        reason = ""
        sources: list[str] = []

        if include_stats:
            detail_response = await crawler._get_response(
                DETAIL_URL,
                params=common_params,
                headers=headers,
                include_cookies=True,
            )
            detail_text = str(getattr(detail_response, "text", "") or "")
            if not detail_text.strip():
                return result_error(
                    "douyin",
                    url,
                    work_id,
                    "blocked",
                    "抖音详情接口返回 HTTP 200 空包；当前会话缺少可验证的设备风控字段或已失效",
                )
            try:
                detail_payload = detail_response.json()
            except Exception as exc:
                raise PlatformCrawlerError("抖音详情接口返回非 JSON") from exc
            detail = detail_payload.get("aweme_detail") or {}
            if detail_payload.get("status_code") != 0 or not detail:
                raise PlatformCrawlerError("抖音详情接口没有有效作品数据")
            statistics = detail.get("statistics") or {}
            stats = EngagementStats(
                views=public_view_count(statistics),
                likes=to_int(statistics.get("digg_count")),
                comments=to_int(statistics.get("comment_count")),
                shares=to_int(statistics.get("share_count")),
                favorites=to_int(statistics.get("collect_count")),
                **{
                    "admire": to_int(statistics.get("admire_count")),
                    "recommend": to_int(statistics.get("recommend_count")),
                },
            )
            sources.append("aweme/v1/web/aweme/detail")

        if include_comments:
            cursor = "0"
            for current_page in range(1, page + 1):
                comment_params = {
                    **common_params,
                    "cursor": cursor,
                    "count": str(min(limit, COMMENT_PAGE_SIZE)),
                    "item_type": "0",
                    "whale_cut_token": "",
                    "cut_version": "1",
                    "rcFT": "",
                }
                comment_response = await crawler._get_response(
                    COMMENT_URL,
                    params=comment_params,
                    headers=headers,
                    include_cookies=True,
                )
                comment_text = str(getattr(comment_response, "text", "") or "")
                if not comment_text.strip():
                    comments = []
                    next_cursor = None
                    reason = "抖音访客评论接口返回 HTTP 200 空包，评论未伪装为空成功"
                    break
                try:
                    comment_payload = comment_response.json()
                except Exception as exc:
                    raise PlatformCrawlerError("抖音评论接口返回非 JSON") from exc
                comments, next_cursor = parse_comments(comment_payload)
                total = to_int(comment_payload.get("total"))
                if not include_stats and total is not None:
                    stats = EngagementStats(comments=total)
                if current_page == page:
                    break
                if next_cursor is None:
                    comments = []
                    break
                cursor = next_cursor
            sources.append("aweme/v1/web/comment/list")

        return EngagementResult(
            platform="douyin",
            canonical_url=f"https://www.douyin.com/video/{work_id}",
            work_id=work_id,
            coverage="partial",
            reason=reason or (
                "抖音评论仅获取指定公开页，不能证明评论全集"
                if include_comments else "抖音详情接口提供当前公开互动量"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments[:limit],
            next_cursor=next_cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("douyin", url, work_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("douyin", url, work_id, "failed", str(exc))


def parse_comments(
    payload: dict[str, Any],
) -> tuple[list[EngagementComment], str | None]:
    result: list[EngagementComment] = []
    for item in payload.get("comments") or []:
        if not isinstance(item, dict):
            continue
        user = item.get("user") or {}
        comment_id = str(item.get("cid") or item.get("comment_id") or item.get("aweme_id") or "")
        if not comment_id:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(user.get("nickname") or user.get("unique_id") or ""),
            text=BeautifulSoup(str(item.get("text") or ""), "html.parser").get_text(" ", strip=True),
            created_at=(
                datetime.fromtimestamp(int(item.get("create_time") or 0), tz=timezone.utc)
                if item.get("create_time") else None
            ),
            likes=to_int(item.get("digg_count")),
            replies=to_int(item.get("reply_comment_total")),
        ))
    if payload.get("has_more") in {0, False}:
        return result, None
    cursor = payload.get("cursor")
    return result, str(cursor) if cursor not in {None, ""} else None


def public_view_count(statistics: dict[str, Any]) -> int | None:
    """Treat Douyin's hidden zero play count as unavailable, not a real zero."""

    views = to_int(statistics.get("play_count"))
    if views != 0:
        return views
    visible_engagement = (
        "digg_count",
        "comment_count",
        "share_count",
        "collect_count",
    )
    if any((to_int(statistics.get(key)) or 0) > 0 for key in visible_engagement):
        return None
    return views
