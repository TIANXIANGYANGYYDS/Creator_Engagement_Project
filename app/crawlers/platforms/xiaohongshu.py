"""Xiaohongshu web SSR statistics and signed web comment flow."""

from __future__ import annotations

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


MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 "
    "Mobile/15E148 Safari/604.1"
)


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
        comments_complete = True
        comment_error = ""

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
            if cookie:
                page_headers["Cookie"] = cookie
            response = await crawler._get_response(
                request_url,
                headers=page_headers,
                force_direct=True,
            )
            stats = parse_stats(str(getattr(response, "text", "")), work_id)
            if _has_stats(stats):
                sources.append("note SSR noteDetailMap")
            target_exists = bool(_target_note(str(getattr(response, "text", "")), work_id))
            if not _has_stats(stats) and target_exists and token:
                # The mobile share page has a separate, numeric comment total.
                # Its explicit zero is usable even when interactInfo is blank.
                mobile_url = urlparse(request_url)._replace(
                    path=f"/discovery/item/{work_id}",
                ).geturl()
                mobile = await crawler._get_response(
                    mobile_url,
                    headers={**page_headers, "User-Agent": MOBILE_USER_AGENT},
                    force_direct=True,
                )
                stats = parse_mobile_stats(str(getattr(mobile, "text", "")), work_id)
                if _has_stats(stats):
                    sources.append("mobile SSR noteData + commentData")
            if not _has_stats(stats) and target_exists:
                if not include_comments:
                    result = result_error(
                        "xiaohongshu", url, work_id, "unsupported",
                        "目标笔记可访问，但平台未返回有效互动数值；空字段不视为零，也不代表登录失效",
                    )
                    result.retryable = False
                    return result
                # Missing counters must not prevent collecting valid comments.
            elif not _has_stats(stats) and not token:
                return result_error(
                    "xiaohongshu",
                    url,
                    work_id,
                    "unsupported",
                    "小红书网页未返回目标笔记，原始 URL 不含 xsec_token；当前网页采集路径未取得互动数据",
                )
            elif not _has_stats(stats):
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
                has_more = comment_payload.get("has_more")
                if not isinstance(has_more, bool):
                    raise PlatformCrawlerError("小红书评论响应缺少有效分页标记")
                following = str(comment_payload.get("cursor") or "")
                if has_more and (not raw_comments or not following or following == cursor):
                    raise PlatformCrawlerError("小红书评论分页未前进，不能将空页或重复游标当成末页")
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
                    following
                    if has_more
                    else None
                )
                if current_page == page:
                    comments, comments_complete, comment_error = await _expand_replies(
                        crawler, raw_comments, work_id, token, cookie, url,
                    )
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
                comment_error if not comments_complete else
                "小红书会话和动态签名已生效；评论仅返回指定公开页"
                if include_comments
                else "小红书互动量来自公开笔记页 SSR"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments,
            next_cursor=next_cursor,
            # A validated empty last page is a usable response, even when the
            # platform omits its total. Repeating it cannot fill in that total.
            retryable=False if comment_page_returned else None,
            comments_complete=comments_complete,
        )
    except PlatformBlockedError as exc:
        result = result_error("xiaohongshu", url, work_id, "blocked", exc)
        result.retryable = False
        return result
    except Exception as exc:
        if "登录已过期" in str(exc):
            result = result_error(
                "xiaohongshu", url, work_id, "blocked",
                "小红书登录态已失效，请重新扫码授权",
            )
            result.retryable = False
            return result
        return result_error("xiaohongshu", url, work_id, "failed", exc)


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
        except PlatformCrawlerError as exc:
            # Only the known signature-format rejection merits a second
            # signature. Never overwrite a captcha/session failure with a retry.
            if sign_format == "xyw" or "HTTP 406" not in str(exc):
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
        if payload.get("code") in {-100, -101}:
            raise PlatformBlockedError("小红书登录态已失效，请重新扫码授权")
        if payload.get("code") in {-104, -103, -13002, 461, 471}:
            raise PlatformBlockedError("小红书请求被限制，业务码 " + str(payload.get("code")))
        raise PlatformCrawlerError(str(payload.get("msg") or "小红书 response failed"))
    if payload.get("success") is True:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    return payload


def parse_comments(payload: dict[str, Any], *, parent_comment_id: str = "") -> list[EngagementComment]:
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
            **({"parent_comment_id": parent_comment_id} if parent_comment_id else {}),
        ))
    return result


async def _expand_replies(crawler, roots, note_id, token, cookie, referer):
    """Keep inline replies and follow each remaining reply cursor without writes."""
    comments = parse_comments({"comments": roots})
    seen = {c.comment_id for c in comments}

    def append_replies(rows, parent):
        if not isinstance(rows, list):
            raise PlatformCrawlerError("小红书回复列表缺失")
        if any(not isinstance(row, dict) or row.get("note_id", note_id) != note_id for row in rows):
            raise PlatformCrawlerError("小红书回复笔记归属不匹配")
        parsed = parse_comments({"comments": rows}, parent_comment_id=parent)
        if len(parsed) != len(rows):
            raise PlatformCrawlerError("小红书回复列表包含无法解析的正文")
        added = 0
        for comment in parsed:
            if comment.comment_id not in seen:
                seen.add(comment.comment_id)
                comments.append(comment)
                added += 1
        return added

    try:
        # Preserve all already-received replies before making further requests.
        if len(comments) != len(roots):
            raise PlatformCrawlerError("小红书主评论列表包含无法解析的正文")
        for root in roots:
            append_replies(root.get("sub_comments") or [], str(root.get("id") or ""))
        for root in roots:
            parent = str(root.get("id") or "")
            inline_count = len(root.get("sub_comments") or [])
            expected = to_int(root.get("sub_comment_count"))
            has_more = root.get("sub_comment_has_more") is True or (
                expected is not None and expected > inline_count
            )
            cursor = str(root.get("sub_comment_cursor") or "")
            visited = set()
            while has_more:
                if not parent or cursor in visited or len(visited) >= 100:
                    raise PlatformCrawlerError("小红书回复分页游标异常或达到分页上限")
                visited.add(cursor)
                payload = await _get(crawler, "/api/sns/web/v2/comment/sub/page", {
                    "note_id": note_id, "root_comment_id": parent, "num": "10",
                    "cursor": cursor, "image_formats": "jpg,webp,avif",
                    "top_comment_id": "", "xsec_token": token,
                }, cookie, referer)
                added = append_replies(payload.get("comments"), parent)
                has_more = payload.get("has_more")
                following = str(payload.get("cursor") or "")
                if not isinstance(has_more, bool) or (has_more and (
                    not added or not following or following in visited
                )):
                    raise PlatformCrawlerError("小红书回复分页未推进")
                cursor = following
    except PlatformCrawlerError as exc:
        return comments, False, "已保留主评论和已获取回复；楼中楼未完成：" + str(exc)
    return comments, True, ""


def parse_stats(text: str, work_id: str) -> EngagementStats:
    values = _target_note(text, work_id).get("interactInfo")
    if isinstance(values, dict):
        return _stats({key: to_int(value) for key, value in values.items()})
    return EngagementStats()


def parse_mobile_stats(text: str, work_id: str) -> EngagementStats:
    """Read the exact H5 note; never infer counters from blank display strings."""
    match = re.search(r"window\.__SETUP_SERVER_STATE__\s*=\s*", text)
    if not match:
        return EngagementStats()
    raw = re.sub(
        r'("(?:\\.|[^"\\])*"|\bundefined\b)',
        lambda item: "null" if item[0] == "undefined" else item[0],
        text[match.end():],
    )
    try:
        state, _ = json.JSONDecoder().raw_decode(raw)
    except (ValueError, RecursionError):
        return EngagementStats()
    data = state.get("LAUNCHER_SSR_STORE_PAGE_DATA") if isinstance(state, dict) else None
    note = data.get("noteData") if isinstance(data, dict) else None
    if not isinstance(note, dict) or note.get("noteId") != work_id:
        return EngagementStats()
    info = note.get("interactInfo")
    stats = _stats({key: to_int(value) for key, value in info.items()}) if isinstance(info, dict) else EngagementStats()
    comments = data.get("commentData")
    if stats.comments is not None or not isinstance(comments, dict):
        return stats
    total = comments.get("commentCount")
    roots_total = comments.get("commentCountL1")
    rows = comments.get("comments")
    has_more = comments.get("hasMore")
    fetched_at = comments.get("time")
    # The official H5 loader returns null on failure. Validate the populated
    # endpoint result, not a missing field or a desktop placeholder list.
    if (
        type(total) is int and total >= 0
        and type(roots_total) is int and 0 <= roots_total <= total
        and type(fetched_at) is int and fetched_at > 0
        and isinstance(rows, list) and len(rows) <= roots_total
        and all(isinstance(row, dict) and row.get("noteId") == work_id for row in rows)
        and isinstance(has_more, bool)
        and (not has_more or (rows and comments.get("cursor")))
        and (total != 0 or (not rows and not has_more))
    ):
        stats.comments = total
    return stats


def _target_note(text: str, work_id: str) -> dict:
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
        return note
    return {}


def _stats(values: dict[str, int | None]) -> EngagementStats:
    return EngagementStats(
        likes=values.get("likedCount"),
        favorites=values.get("collectedCount"),
        shares=values.get("shareCount"),
        comments=values.get("commentCount"),
    )


def _has_stats(stats: EngagementStats) -> bool:
    return any(value is not None for value in stats.model_dump().values())
