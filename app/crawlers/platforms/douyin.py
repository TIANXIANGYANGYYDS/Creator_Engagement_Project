"""Douyin detail and paged public-comment protocol flow."""

from __future__ import annotations

from datetime import datetime, timezone
import secrets
from typing import Any
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import (
    COMMENT_PAGE_SIZE,
    result_error,
    to_int,
)
from app.crawlers.platforms.douyin_abogus import DouyinABogusSigner
from app.models.engagement import EngagementComment, EngagementResult, EngagementStats


DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
COMMENT_URL = "https://www.douyin.com/aweme/v1/web/comment/list/"
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
MSTOKEN_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789="
DOUYIN_PROTOCOL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0.0.0 Safari/537.36"
)
_SIGNER = DouyinABogusSigner(DOUYIN_PROTOCOL_USER_AGENT)


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
    referer = f"https://www.douyin.com/video/{work_id}?previous_page=web_code_link"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
        "User-Agent": DOUYIN_PROTOCOL_USER_AGENT,
    }
    try:
        cookie = crawler._platform_cookie("douyin")
        if cookie:
            headers["Cookie"] = cookie
        else:
            headers["Cookie"] = await prepare_anonymous_session(crawler, referer)

        stats = EngagementStats()
        comments: list[EngagementComment] = []
        next_cursor: str | None = None
        reason = ""
        sources: list[str] = []

        if include_stats:
            detail_response = await crawler._get_response(
                DETAIL_URL,
                params=signed_params({
                    "device_platform": "webapp",
                    "aid": "6383",
                    "channel": "channel_pc_web",
                    "aweme_id": work_id,
                    "msToken": random_ms_token(),
                }),
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
            filter_detail = detail_payload.get("filter_detail") or {}
            if not detail and filter_detail:
                filter_reason = str(filter_detail.get("filter_reason") or "unavailable")
                detail_message = str(
                    filter_detail.get("detail_msg")
                    or filter_detail.get("notice")
                    or "作品不可用"
                )
                result = result_error(
                    "douyin",
                    url,
                    work_id,
                    "unsupported",
                    f"抖音作品不可用（{filter_reason}）：{detail_message}",
                )
                result.retryable = False
                return result
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
            cursor = comment_cursor or "0"
            start_page = page if comment_cursor is not None else 1
            for current_page in range(start_page, page + 1):
                comment_params = {
                    "device_platform": "webapp",
                    "aid": "6383",
                    "channel": "channel_pc_web",
                    "aweme_id": work_id,
                    "cursor": cursor,
                    "count": str(min(limit, COMMENT_PAGE_SIZE)),
                    "item_type": "0",
                    "whale_cut_token": "",
                    "cut_version": "1",
                    "rcFT": "",
                    "msToken": random_ms_token(),
                }
                comment_response = await crawler._get_response(
                    COMMENT_URL,
                    params=signed_params(comment_params),
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


async def prepare_anonymous_session(
    crawler: PlatformCrawlerContext,
    referer: str,
) -> str:
    """Create a fresh first-party visitor cookie without a user login."""

    response = await crawler._post_response(
        TTWID_REGISTER_URL,
        headers={
            # The HTTP client is shared by concurrent platform requests.  Do
            # not let cookies created on another proxy contaminate this new
            # proxy-bound visitor identity.
            "Cookie": "",
            "Content-Type": "application/json",
            "Referer": referer,
            "User-Agent": DOUYIN_PROTOCOL_USER_AGENT,
        },
        json_body={
            "region": "cn",
            "aid": 6383,
            "need_t": 1,
            "service": "www.douyin.com",
            "migrate_priority": 0,
            "cb_url_protocol": "https",
            "domain": ".douyin.com",
        },
    )
    try:
        payload = response.json()
    except Exception as exc:
        raise PlatformCrawlerError("抖音访客标识初始化返回非 JSON") from exc
    if payload.get("status_code") not in {0, None}:
        raise PlatformCrawlerError("抖音访客标识初始化失败")
    cookie_jar = getattr(response, "cookies", None)
    ttwid = str(cookie_jar.get("ttwid") if cookie_jar is not None else "").strip()
    if not ttwid:
        raise PlatformCrawlerError("抖音访客标识初始化未返回 ttwid")
    return f"ttwid={ttwid}"


def random_ms_token(length: int = 107) -> str:
    return "".join(secrets.choice(MSTOKEN_ALPHABET) for _ in range(length))


def signed_params(params: dict[str, str]) -> dict[str, str]:
    query = urlencode(params)
    return {**params, "a_bogus": _SIGNER.sign(query)}


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
