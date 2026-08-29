"""Weibo mobile detail and cursor-based comment flow."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from typing import Any
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import result_error, to_int
from app.models.engagement import (
    EngagementComment,
    EngagementResult,
    EngagementStats,
)


DESKTOP_STATUS_URL = "https://weibo.com/ajax/statuses/show"
VIDEO_COMPONENT_URL = "https://weibo.com/tv/api/component"
VIDEO_FID_RE = re.compile(r"(?:^|[?&])fid=((?:\d+:)?\d{8,})(?:&|$)", re.I)
DESKTOP_BID_RE = re.compile(r"^/\d+/([0-9A-Za-z]+)(?:/|$)")

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
    cookie = crawler._platform_cookie("weibo")
    public_target = public_protocol_target(url, work_id)
    resolved_work_id = work_id
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
        confirmed_empty_public_comments = False
        if include_stats:
            data: dict[str, Any]
            if public_target is not None:
                data, resolved_work_id, source = await fetch_public_stats(
                    crawler,
                    url,
                    work_id,
                    public_target,
                )
                headers.update(crawler._weibo_visitor_headers())
                sources.append(source)
            else:
                payload = await crawler._get_json(
                    "https://m.weibo.cn/statuses/show",
                    params={"id": work_id},
                    headers=headers,
                )
                data = payload.get("data") or {}
                sources.append("m.weibo.cn/statuses/show")
            stats = EngagementStats(
                likes=to_int(data.get("attitudes_count")),
                comments=to_int(data.get("comments_count")),
                reposts=to_int(data.get("reposts_count")),
            )
        elif include_comments and public_target is not None:
            _, resolved_work_id, source = await fetch_public_stats(
                crawler,
                url,
                work_id,
                public_target,
            )
            headers.update(crawler._weibo_visitor_headers())
            sources.append(source)
        if include_comments:
            # Weibo's cursor also depends on max_id_type. Keep replaying from
            # page 1 until both values are carried by the internal contract.
            request_cursor: str | None = None
            request_cursor_type = 0
            used_numbered_fallback = False
            for current_page in range(1, page + 1):
                params: dict[str, Any] = {
                    "id": resolved_work_id,
                    "mid": resolved_work_id,
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
                    hotflow_confirms_empty = response_confirms_no_visible_comments(
                        comments_payload
                    )
                    comments_payload = await crawler._get_json(
                        "https://m.weibo.cn/api/comments/show",
                        params={"id": resolved_work_id, "page": page},
                        headers=headers,
                    )
                    if not response_ok(comments_payload):
                        if (
                            hotflow_confirms_empty
                            and response_confirms_no_visible_comments(comments_payload)
                        ):
                            comments = []
                            cursor = None
                            used_numbered_fallback = True
                            confirmed_empty_public_comments = True
                            break
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
        result = EngagementResult(
            platform="weibo",
            canonical_url=f"https://m.weibo.cn/detail/{resolved_work_id}",
            work_id=resolved_work_id,
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
        if confirmed_empty_public_comments:
            result.retryable = False
        return result
    except WeiboUnavailableError as exc:
        result = result_error("weibo", url, work_id, "unsupported", str(exc))
        result.retryable = False
        return result
    except PlatformBlockedError as exc:
        if public_target is not None:
            crawler._invalidate_weibo_visitor_session()
        return result_error("weibo", url, work_id, "blocked", str(exc))
    except Exception as exc:
        if public_target is not None:
            crawler._invalidate_weibo_visitor_session()
        return result_error("weibo", url, work_id, "failed", str(exc))


def public_protocol_target(url: str, work_id: str) -> tuple[str, str] | None:
    """Return the desktop URL kind and its original public identifier."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    fid_match = VIDEO_FID_RE.search(f"?{parsed.query}")
    if host == "video.weibo.com" and fid_match:
        return "video", fid_match.group(1)
    tv_match = re.search(r"/tv/show/((?:\d+:)?\d{8,})(?:/|$)", parsed.path)
    if host.endswith("weibo.com") and tv_match:
        return "video", tv_match.group(1)
    bid_match = DESKTOP_BID_RE.match(parsed.path)
    if host.endswith("weibo.com") and bid_match:
        return "desktop", bid_match.group(1)
    return None


async def fetch_public_stats(
    crawler: PlatformCrawlerContext,
    original_url: str,
    work_id: str,
    target: tuple[str, str],
) -> tuple[dict[str, Any], str, str]:
    kind, public_id = target
    if kind == "video":
        return await fetch_video_stats(crawler, public_id)
    return await fetch_desktop_stats(crawler, original_url, public_id, work_id)


async def fetch_desktop_stats(
    crawler: PlatformCrawlerContext,
    original_url: str,
    bid: str,
    work_id: str,
) -> tuple[dict[str, Any], str, str]:
    target_url = original_url.replace("http://", "https://", 1)
    await crawler._ensure_weibo_visitor_session(target_url, entry="miniblog")
    payload = await crawler._get_json(
        DESKTOP_STATUS_URL,
        params={"id": bid, "locale": "zh-CN", "isGetLongText": "true"},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": target_url,
            **crawler._weibo_visitor_headers(),
        },
    )
    if payload.get("ok") == 0:
        error_code = to_int(payload.get("error_code"))
        message = str(payload.get("message") or "微博不可用")
        if error_code in {20101, 20112}:
            raise WeiboUnavailableError(message)
        crawler._invalidate_weibo_visitor_session()
        raise PlatformBlockedError(message)
    resolved_work_id = str(payload.get("idstr") or payload.get("mid") or work_id)
    return payload, resolved_work_id, "weibo.com/ajax/statuses/show"


async def fetch_video_stats(
    crawler: PlatformCrawlerContext,
    fid: str,
) -> tuple[dict[str, Any], str, str]:
    oid = fid if ":" in fid else f"1034:{fid}"
    target_url = f"https://weibo.com/tv/show/{oid}"
    await crawler._ensure_weibo_visitor_session(target_url, entry="krvideo")
    page = f"/tv/show/{oid}"
    payload = await crawler._post_json(
        VIDEO_COMPONENT_URL,
        params={"page": page},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://weibo.com",
            "Page-Referer": page,
            "Referer": target_url,
            **crawler._weibo_visitor_headers(),
        },
        data={
            "data": json.dumps(
                {"Component_Play_Playinfo": {"oid": oid}},
                separators=(",", ":"),
            )
        },
    )
    data = (payload.get("data") or {}).get("Component_Play_Playinfo") or {}
    code = str(payload.get("code") or "")
    if code == "100000" and not data:
        raise WeiboUnavailableError("微博视频不存在或无查看权限")
    if code != "100000":
        message = str(payload.get("msg") or "微博视频不可用")
        if code in {"100006", "100009"}:
            raise WeiboUnavailableError(message)
        crawler._invalidate_weibo_visitor_session()
        raise PlatformBlockedError(message)
    resolved_work_id = str(data.get("mid") or "")
    if not resolved_work_id:
        raise PlatformBlockedError("微博视频详情未返回 MID")
    return data, resolved_work_id, "weibo.com/tv/api/component"


class WeiboUnavailableError(RuntimeError):
    """The public status is deleted, private, or otherwise unavailable."""


def parse_comments(payload: dict[str, Any]) -> tuple[list[EngagementComment], str | None]:
    data = payload.get("data") or {}
    result = parse_comment_items(data.get("data") or [])
    return result, str(data.get("max_id")) if data.get("max_id") else None


def response_confirms_no_visible_comments(payload: dict[str, Any]) -> bool:
    """Return whether Weibo explicitly reports no comments visible to this session."""

    message = str(payload.get("msg") or payload.get("message") or "").strip()
    return message in {"已过滤部分评论", "暂无数据"}


def parse_comment_items(items: list[Any]) -> list[EngagementComment]:
    result: list[EngagementComment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
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
    return result


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
