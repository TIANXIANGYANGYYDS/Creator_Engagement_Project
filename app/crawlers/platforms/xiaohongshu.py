"""Xiaohongshu SSR statistics and signed authenticated comment flow."""

from __future__ import annotations

import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import (
    DESKTOP_USER_AGENT,
    first_present,
    result_error,
    timestamp,
    to_int,
)
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
    del limit  # The signed endpoint controls its own public page size.
    parsed = urlparse(url)
    token = parse_qs(parsed.query).get("xsec_token", [""])[0]
    source = parse_qs(parsed.query).get("xsec_source", ["pc_search"])[0] or "pc_search"
    cookie = crawler._platform_cookie("xiaohongshu")
    canonical_url = f"https://www.xiaohongshu.com/explore/{work_id}"
    try:
        stats = EngagementStats()
        comments: list[EngagementComment] = []
        next_cursor: str | None = None
        sources: list[str] = []

        if include_stats:
            if cookie and token:
                feed_payload = {
                    "source_note_id": work_id,
                    "image_formats": ["jpg", "webp", "avif"],
                    "extra": {"need_body_topic": 1},
                    "xsec_source": source,
                    "xsec_token": token,
                }
                try:
                    feed = await _post(crawler, "/api/sns/web/v1/feed", feed_payload, cookie, url)
                    stats = stats_from_note(note_card(feed))
                    if _has_stats(stats):
                        sources.append("api/sns/web/v1/feed")
                except PlatformCrawlerError:
                    pass
            if not _has_stats(stats):
                response = await crawler._get_response(
                    url,
                    headers={"Referer": "https://www.xiaohongshu.com/explore"},
                )
                stats = parse_stats(str(getattr(response, "text", "")), work_id)
                sources.append("note SSR noteDetailMap")

        if include_comments:
            if not cookie:
                if include_stats:
                    return EngagementResult(
                        platform="xiaohongshu",
                        canonical_url=canonical_url,
                        work_id=work_id,
                        coverage="partial",
                        reason="小红书 SSR 已返回互动统计；未登录游客浏览器仅能尝试当前公开首屏评论",
                        source="note SSR noteDetailMap",
                        stats=stats,
                    )
                return result_error(
                    "xiaohongshu",
                    url,
                    work_id,
                    "unsupported",
                    "协议请求没有账号会话；将回退本地游客浏览器读取公开首屏。游客态不支持深分页",
                )
            if not token:
                if include_stats:
                    return EngagementResult(
                        platform="xiaohongshu",
                        canonical_url=canonical_url,
                        work_id=work_id,
                        coverage="partial",
                        reason="小红书 SSR 已返回互动统计；评论 URL 缺少 xsec_token",
                        source="note SSR noteDetailMap",
                        stats=stats,
                    )
                return result_error(
                    "xiaohongshu",
                    url,
                    work_id,
                    "unsupported",
                    "小红书评论需要 URL 中的 xsec_token；请使用搜索或推荐流生成的完整笔记链接",
                )

            cursor = ""
            total: int | None = None
            for current_page in range(1, page + 1):
                params = {
                    "note_id": work_id,
                    "cursor": cursor,
                    "top_comment_id": "",
                    "image_formats": "jpg,webp,avif",
                    "xsec_token": token,
                }
                comment_payload = await _get(
                    crawler,
                    "/api/sns/web/v2/comment/page",
                    params,
                    cookie,
                    url,
                )
                comments = parse_comments(comment_payload)
                total = to_int(
                    comment_payload.get("total_count")
                    or comment_payload.get("comment_count")
                    or comment_payload.get("comments_count")
                )
                next_cursor = (
                    str(comment_payload.get("cursor"))
                    if comment_payload.get("has_more") and comment_payload.get("cursor")
                    else None
                )
                if current_page == page:
                    break
                if next_cursor is None:
                    comments = []
                    break
                cursor = next_cursor
            if not include_stats and total is not None:
                stats = EngagementStats(comments=total)
            sources.append("api/sns/web/v2/comment/page")

        if not comments and not _has_stats(stats):
            return result_error(
                "xiaohongshu",
                url,
                work_id,
                "unsupported",
                "小红书笔记页面未返回可验证互动或评论数据，URL 可能已失效或需要登录验证",
            )

        return EngagementResult(
            platform="xiaohongshu",
            canonical_url=canonical_url,
            work_id=work_id,
            coverage="partial",
            reason=(
                "小红书会话和动态签名已生效；评论仅返回指定公开页"
                if include_comments
                else "小红书互动量来自签名详情接口或笔记 SSR"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments,
            next_cursor=next_cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("xiaohongshu", url, work_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("xiaohongshu", url, work_id, "failed", str(exc))


async def _get(
    crawler: PlatformCrawlerContext,
    uri: str,
    params: dict[str, Any],
    cookie: str,
    referer: str,
) -> dict[str, Any]:
    query = "&".join(
        f"{quote(str(key), safe='')}={quote(str(value), safe=',')}"
        for key, value in params.items()
    )
    for sign_format in ("xys", "xyw"):
        headers = signed_headers(
            uri,
            cookie,
            params=params,
            sign_format=sign_format,
        )
        headers.update(authenticated_headers(cookie, referer))
        try:
            payload = await crawler._get_json(
                f"https://edith.xiaohongshu.com{uri}?{query}",
                headers=headers,
            )
            return unwrap_data(payload)
        except PlatformCrawlerError:
            if sign_format == "xyw":
                raise
    raise AssertionError("unreachable")


async def _post(
    crawler: PlatformCrawlerContext,
    uri: str,
    payload: dict[str, Any],
    cookie: str,
    referer: str,
) -> dict[str, Any]:
    for sign_format in ("xys", "xyw"):
        headers = signed_headers(
            uri,
            cookie,
            payload=payload,
            sign_format=sign_format,
            x_rap=uri == "/api/sns/web/v1/feed",
        )
        headers.update(authenticated_headers(cookie, referer))
        headers["Content-Type"] = "application/json;charset=UTF-8"
        try:
            response = await crawler._post_json(
                f"https://edith.xiaohongshu.com{uri}",
                headers=headers,
                data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            )
            return unwrap_data(response)
        except PlatformCrawlerError:
            if sign_format == "xyw":
                raise
    raise AssertionError("unreachable")


def authenticated_headers(cookie: str, referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
        "Referer": referer,
        "User-Agent": DESKTOP_USER_AGENT,
    }


def signed_headers(
    uri: str,
    cookie: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    sign_format: str = "xys",
    x_rap: bool = False,
) -> dict[str, str]:
    try:
        from xhshow import Xhshow
    except ImportError as exc:
        raise PlatformCrawlerError("小红书签名依赖 xhshow 未安装") from exc
    signer = Xhshow()
    if params is not None:
        signed = signer.sign_headers_get(
            uri=uri,
            cookies=cookie,
            params=params,
            sign_format=sign_format,
            x_rap=x_rap,
        )
    elif payload is not None:
        signed = signer.sign_headers_post(
            uri=uri,
            cookies=cookie,
            payload=payload,
            sign_format=sign_format,
            x_rap=x_rap,
        )
    else:
        raise ValueError("params or payload is required")
    canonical_names = {
        "x-s": "X-S",
        "x-t": "X-T",
        "x-s-common": "X-S-Common",
        "x-b3-traceid": "X-B3-Traceid",
        "x-xray-traceid": "X-Xray-Traceid",
        "x-mns": "X-Mns",
        "xy-direction": "XY-Direction",
        "x-rap-param": "X-Rap-Param",
    }
    return {
        canonical_names.get(key.lower(), key): str(value)
        for key, value in signed.items()
        if value is not None and value != ""
    }


def unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("success") is False:
        raise PlatformCrawlerError(str(payload.get("msg") or "小红书 response failed"))
    if payload.get("success") is True:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    return payload


def note_card(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items") or []
    if isinstance(items, list) and items and isinstance(items[0], dict):
        card = items[0].get("note_card") or items[0].get("noteCard") or items[0]
        return card if isinstance(card, dict) else {}
    return payload.get("note_card") or payload.get("noteCard") or payload


def stats_from_note(note: dict[str, Any]) -> EngagementStats:
    interact = note.get("interact_info") or note.get("interactInfo") or note
    return EngagementStats(
        likes=to_int(first_present(interact, "liked_count", "likedCount")),
        favorites=to_int(first_present(interact, "collected_count", "collectedCount")),
        shares=to_int(first_present(interact, "share_count", "shareCount")),
        comments=to_int(first_present(interact, "comment_count", "commentCount")),
    )


def parse_comments(payload: dict[str, Any]) -> list[EngagementComment]:
    result: list[EngagementComment] = []
    for item in payload.get("comments") or payload.get("comment_list") or []:
        if not isinstance(item, dict):
            continue
        user = (
            item.get("user_info")
            or item.get("userInfo")
            or item.get("user")
            or item.get("author")
            or {}
        )
        if not isinstance(user, dict):
            user = {}
        comment_id = str(item.get("id") or item.get("comment_id") or item.get("commentId") or "")
        text = str(item.get("content") or item.get("text") or "")
        if not comment_id or not text:
            continue
        item_stats = item.get("stats") or {}
        if not isinstance(item_stats, dict):
            item_stats = {}
        like_count = first_present(item, "like_count", "likeCount", "liked_count")
        reply_count = first_present(item, "sub_comment_count", "subCommentCount")
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(user.get("nickname") or user.get("nick_name") or user.get("name") or ""),
            text=BeautifulSoup(text, "html.parser").get_text(" ", strip=True),
            created_at=timestamp(
                item.get("create_time") or item.get("createTime") or item.get("timestamp")
            ),
            likes=to_int(
                like_count if like_count is not None else first_present(item_stats, "like_count")
            ),
            replies=to_int(
                reply_count
                if reply_count is not None
                else first_present(item_stats, "sub_comment_count")
            ),
        ))
    return result


def parse_stats(text: str, work_id: str) -> EngagementStats:
    decoded = html.unescape(text).replace("\\u002F", "/")
    detail_anchor = decoded.find(f'"noteDetailMap":{{"{work_id}"')
    if detail_anchor >= 0:
        detail_segment = decoded[detail_anchor:detail_anchor + 20000]
        detail_match = re.search(r'"interactInfo"\s*:\s*\{(.*?)\}', detail_segment, re.S)
        if detail_match:
            values = interaction_values(detail_match.group(1))
            if values:
                return _stats(values)
    note_pattern = re.compile(r'"noteId"\s*:\s*"' + re.escape(work_id) + r'"')
    for note_match in reversed(list(note_pattern.finditer(decoded))):
        prefix = decoded[max(0, note_match.start() - 3000):note_match.start()]
        matches = list(re.finditer(r'"interactInfo"\s*:\s*\{(.*?)\}', prefix, re.S))
        if matches:
            values = interaction_values(matches[-1].group(1))
            if values:
                return _stats(values)
    return EngagementStats()


def interaction_values(body: str) -> dict[str, int | None]:
    return {
        key: to_int(value)
        for key, value in re.findall(
            r'"(likedCount|collectedCount|shareCount|commentCount)"\s*:\s*"?([^",}]+)',
            body,
        )
    }


def _stats(values: dict[str, int | None]) -> EngagementStats:
    return EngagementStats(
        likes=values.get("likedCount"),
        favorites=values.get("collectedCount"),
        shares=values.get("shareCount"),
        comments=values.get("commentCount"),
    )


def _has_stats(stats: EngagementStats) -> bool:
    return any(value is not None for value in stats.model_dump().values())
