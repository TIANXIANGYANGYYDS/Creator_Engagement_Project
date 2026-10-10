"""Xiaohongshu web SSR statistics and signed web comment flow."""

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
    comment_cursor: str | None,
    include_stats: bool,
    include_comments: bool,
) -> EngagementResult:
    del limit  # The signed endpoint controls its own public page size.
    parsed = urlparse(url)
    token = parse_qs(parsed.query).get("xsec_token", [""])[0]
    source = parse_qs(parsed.query).get("xsec_source", [""])[0]
    cookie = crawler._platform_cookie("xiaohongshu")
    canonical_url = f"https://www.xiaohongshu.com/explore/{work_id}"
    try:
        stats = EngagementStats()
        comments: list[EngagementComment] = []
        comment_page_returned = False
        next_cursor: str | None = None
        sources: list[str] = []

        if include_stats:
            # A direct note-page visit embeds the target note and its public
            # counters in ``window.__INITIAL_STATE__``.  Prefer that single
            # unsigned request: the signed feed endpoint is account-gated and
            # currently returns an empty HTTP 461 response to guest sessions.
            # Accept app-style discovery links without making the caller
            # rewrite them. Preserve the original token and source parameters.
            request_url = parsed._replace(path=f"/explore/{work_id}").geturl()
            if token and not source:
                query = f"{parsed.query}&xsec_source=pc_feed"
                request_url = urlparse(request_url)._replace(query=query).geturl()
            page_headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": DESKTOP_USER_AGENT,
            }
            response = await crawler._get_response(
                request_url,
                headers=page_headers,
                force_direct=True,
            )
            stats = parse_stats(str(getattr(response, "text", "")), work_id)
            if _has_stats(stats):
                sources.append("note SSR noteDetailMap")
            elif not token:
                return result_error(
                    "xiaohongshu",
                    url,
                    work_id,
                    "unsupported",
                    "小红书网页未返回目标笔记，原始 URL 不含 xsec_token；当前网页采集路径未取得互动数据",
                )
            else:
                return result_error(
                    "xiaohongshu",
                    url,
                    work_id,
                    "blocked",
                    "小红书 SSR 未返回目标笔记；当前出口受限或 URL 中的 xsec_token 已失效",
                )

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
                    (
                        "已保存的小红书账号会话不存在或 Cookie 已过期，请在本机重新登录并保存会话"
                        if getattr(crawler, "xiaohongshu_session_store", None) is not None
                        else "当前网页评论请求缺少可用 Cookie；本地游客浏览器可尝试公开首屏，尚未验证游客深分页"
                    ),
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
                    "当前网页评论路径需要 xsec_token，尚未从原始 URL 自动取得该参数",
                )

            cursor = comment_cursor or ""
            total: int | None = None
            start_page = page if comment_cursor is not None else 1
            for current_page in range(start_page, page + 1):
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
                raw_comments = comment_payload.get("comments")
                if not isinstance(raw_comments, list):
                    raise PlatformCrawlerError("小红书评论响应缺少有效的 comments 列表")
                comments = parse_comments(comment_payload)
                if raw_comments and not comments:
                    raise PlatformCrawlerError("小红书评论列表未包含可识别的评论数据")
                comment_page_returned = True
                total = to_int(
                    first_present(
                        comment_payload,
                        "total_count",
                        "comment_count",
                        "comments_count",
                    )
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

        if not comments and not _has_stats(stats) and not comment_page_returned:
            has_detail_session = bool(cookie and token)
            return result_error(
                "xiaohongshu",
                url,
                work_id,
                "blocked" if has_detail_session else "unsupported",
                (
                    "小红书详情会话未返回目标数据"
                    if has_detail_session
                    else "小红书笔记页面未返回可验证互动或评论数据，URL 可能已失效或需要登录验证"
                ),
            )

        return EngagementResult(
            platform="xiaohongshu",
            canonical_url=canonical_url,
            work_id=work_id,
            coverage="partial",
            reason=(
                "小红书会话和动态签名已生效；评论仅返回指定公开页"
                if include_comments
                else "小红书互动量来自公开笔记页 SSR"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments,
            next_cursor=next_cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("xiaohongshu", url, work_id, "blocked", str(exc))
    except Exception as exc:
        if "登录已过期" in str(exc):
            result = result_error(
                "xiaohongshu", url, work_id, "blocked",
                "小红书登录态已失效，请重新扫码授权",
            )
            result.retryable = False
            return result
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
    # Proximity in a script is not an identity check: neighbouring recommended
    # notes have their own counters. Decode the map and bind both key and ID.
    for match in re.finditer(r'"noteDetailMap"\s*:\s*', text):
        raw = text[match.end():]
        raw = re.sub(
            r'("(?:\\.|[^"\\])*"|\bundefined\b)',
            lambda item: "null" if item[0] == "undefined" else item[0],
            raw,
        )
        try:
            details, _ = json.JSONDecoder().raw_decode(raw)
        except (ValueError, RecursionError):
            continue
        entry = details.get(work_id) if isinstance(details, dict) else None
        note = entry.get("note") if isinstance(entry, dict) else None
        if not isinstance(note, dict) or note.get("noteId") != work_id:
            continue
        values = note.get("interactInfo")
        if isinstance(values, dict):
            return _stats({key: to_int(value) for key, value in values.items()})
    return EngagementStats()


def _stats(values: dict[str, int | None]) -> EngagementStats:
    return EngagementStats(
        likes=values.get("likedCount"),
        favorites=values.get("collectedCount"),
        shares=values.get("shareCount"),
        comments=values.get("commentCount"),
    )


def _has_stats(stats: EngagementStats) -> bool:
    return any(value is not None for value in stats.model_dump().values())
