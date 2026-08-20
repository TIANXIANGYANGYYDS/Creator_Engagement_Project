"""Kuaishou authenticated detail and cursor-based root-comment flow."""

from __future__ import annotations

import json
from typing import Any

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
from app.models.engagement import (
    EngagementComment,
    EngagementCoverage,
    EngagementResult,
    EngagementStats,
)


GRAPHQL_URL = "https://www.kuaishou.com/graphql"
COMMENT_URL = "https://www.kuaishou.com/rest/v/photo/comment/list"
DETAIL_QUERY = """
query visionVideoDetail($photoId: String) {
  visionVideoDetail(photoId: $photoId) {
    status
    photo {
      id
      viewCount
      likeCount
      realLikeCount
    }
  }
}
"""
COMMENT_QUERY = """
query commentListQuery($photoId: String, $pcursor: String) {
  visionCommentList(photoId: $photoId, pcursor: $pcursor) {
    commentCountV2
    pcursorV2
    rootCommentsV2 {
      commentId
      authorId
      authorName
      content
      timestamp
      likedCount
      subCommentCount
    }
  }
}
"""


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
    cookie = crawler._platform_cookie("kuaishou")
    if not cookie:
        return result_error(
            "kuaishou",
            url,
            work_id,
            "unsupported",
            "快手纯 HTTP 请求缺少短期游客验证状态（kww/kwssectoken，常返回 Need captcha）；将回退浏览器在目标页自动建立游客会话，无需账号登录",
        )
    headers = authenticated_headers(cookie, url)
    headers.update({
        "Content-Type": "application/json",
        "Origin": "https://www.kuaishou.com",
    })
    try:
        stats = EngagementStats()
        comments: list[EngagementComment] = []
        next_cursor: str | None = None
        sources: list[str] = []
        if include_stats:
            detail_payload = {
                "operationName": "visionVideoDetail",
                "variables": {"photoId": work_id},
                "query": DETAIL_QUERY,
            }
            detail_response = await crawler._post_json(
                GRAPHQL_URL,
                headers=headers,
                data=json.dumps(detail_payload, separators=(",", ":")),
            )
            raise_response_errors(detail_response)
            detail = (detail_response.get("data") or {}).get("visionVideoDetail") or {}
            photo = detail.get("photo") or {}
            if str(photo.get("id") or "") != work_id:
                raise PlatformCrawlerError("快手详情响应未匹配目标 photoId")
            stats = EngagementStats(
                views=to_int(photo.get("viewCount")),
                likes=to_int(photo.get("realLikeCount") or photo.get("likeCount")),
                comments=to_int(photo.get("commentCount")),
            )
            sources.append("graphql visionVideoDetail")

        if include_comments:
            cursor = ""
            total: int | None = None
            for current_page in range(1, page + 1):
                comment_payload = await fetch_comment_page(crawler, work_id, cursor, headers)
                comments = parse_comments(comment_payload.get("rootCommentsV2") or [])
                total = to_int(
                    comment_payload.get("commentCountV2")
                    or comment_payload.get("commentCount")
                )
                raw_cursor = comment_payload.get("pcursorV2") or comment_payload.get("pcursor")
                next_cursor = (
                    str(raw_cursor)
                    if raw_cursor not in {None, "", "0", "no_more"}
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
            elif include_stats and stats.comments is None and total is not None:
                stats.comments = total
            sources.append("rest/v/photo/comment/list")
        elif include_stats and stats.comments is None:
            comment_payload = await fetch_comment_page(crawler, work_id, "", headers)
            stats.comments = to_int(
                comment_payload.get("commentCountV2")
                or comment_payload.get("commentCount")
            )
            sources.append("rest/v/photo/comment/list total")

        return EngagementResult(
            platform="kuaishou",
            canonical_url=f"https://www.kuaishou.com/short-video/{work_id}",
            work_id=work_id,
            coverage="partial",
            reason=(
                "快手登录会话可读取指定页一级评论；子回复未包含在本接口"
                if include_comments else "快手登录会话可读取目标作品当前互动量"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments[:limit],
            next_cursor=next_cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("kuaishou", url, work_id, "blocked", str(exc))
    except Exception as exc:
        coverage: EngagementCoverage = "blocked" if is_challenge_text(str(exc)) else "failed"
        return result_error("kuaishou", url, work_id, coverage, str(exc))


async def fetch_comment_page(
    crawler: PlatformCrawlerContext,
    work_id: str,
    cursor: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    rest_payload = {"photoId": work_id, "pcursor": cursor}
    rest = await crawler._post_json(
        COMMENT_URL,
        headers=headers,
        data=json.dumps(rest_payload, separators=(",", ":")),
    )
    if rest.get("result") == 1:
        return rest
    if is_challenge_text(json.dumps(rest, ensure_ascii=False)):
        raise PlatformBlockedError("快手评论接口要求验证码或会话已失效")
    graphql_payload = {
        "operationName": "commentListQuery",
        "variables": {"photoId": work_id, "pcursor": cursor},
        "query": COMMENT_QUERY,
    }
    graphql = await crawler._post_json(
        GRAPHQL_URL,
        headers=headers,
        data=json.dumps(graphql_payload, separators=(",", ":")),
    )
    raise_response_errors(graphql)
    comment_list = (graphql.get("data") or {}).get("visionCommentList")
    if not isinstance(comment_list, dict):
        raise PlatformCrawlerError("快手评论响应没有目标数据")
    return comment_list


def authenticated_headers(cookie: str, referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
        "Referer": referer,
        "User-Agent": DESKTOP_USER_AGENT,
    }


def parse_comments(items: list[Any]) -> list[EngagementComment]:
    result: list[EngagementComment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        comment_id = str(item.get("commentId") or item.get("comment_id") or "")
        text = str(item.get("content") or "")
        if not comment_id or not text:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(item.get("authorName") or item.get("author_name") or ""),
            text=BeautifulSoup(text, "html.parser").get_text(" ", strip=True),
            created_at=timestamp(item.get("timestamp")),
            likes=to_int(first_present(item, "likedCount", "liked_count")),
            replies=to_int(first_present(item, "subCommentCount", "sub_comment_count")),
        ))
    return result


def raise_response_errors(payload: dict[str, Any]) -> None:
    errors = payload.get("errors")
    if errors:
        message = json.dumps(errors, ensure_ascii=False)
        if is_challenge_text(message):
            raise PlatformBlockedError("快手接口要求验证码或会话已失效")
        raise PlatformCrawlerError(message)


def is_challenge_text(value: str) -> bool:
    lowered = value.casefold()
    return any(
        token in lowered
        for token in ("need captcha", "captcha", "验证码", "security check", "登录")
    )
