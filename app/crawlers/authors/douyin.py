from __future__ import annotations

import re

from app.crawlers.authors.common import date, encode_cursor, require_list, urls
from app.crawlers.authors.common import AuthorCursorError
from app.crawlers.authors.identity import resolve_douyin
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.douyin import (
    DOUYIN_PROTOCOL_USER_AGENT,
    prepare_anonymous_session,
    random_ms_token,
    signed_params,
)
from app.models.author import AuthorPage, AuthorWork


async def fetch_page(crawler, request, state, *, include_details=True):
    sec_uid = state.get("sec_uid") or (
        request.uid if request.uid.startswith("MS4wLj") else ""
    )
    if sec_uid and not re.fullmatch(r"MS4wLj[A-Za-z0-9_-]{10,250}", sec_uid):
        raise AuthorCursorError("cursor 中 sec_uid 无效")
    referer = f"https://www.douyin.com/user/{sec_uid or request.uid}"
    if not getattr(crawler, "_author_douyin_cookie", None):
        crawler._author_douyin_cookie = (
            await crawler.cookie_header("https://www.douyin.com")
            if hasattr(crawler, "cookie_header")
            else await prepare_anonymous_session(crawler, referer)
        )
    headers = {
        "Referer": referer,
        "User-Agent": DOUYIN_PROTOCOL_USER_AGENT,
        "Cookie": crawler._author_douyin_cookie,
    }
    base = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "msToken": random_ms_token(),
    }
    # A cursor is supplied by the caller, so its sec_uid is not evidence of
    # ownership. Resolve handles once per discovery, then reuse that verified
    # mapping across pages and proxy rotations.
    resolved = await resolve_douyin(crawler, request.uid, base, headers)
    if sec_uid and sec_uid != resolved:
        raise AuthorCursorError("cursor 中作者身份与 author_id 不匹配")
    sec_uid = resolved
    headers["Referer"] = f"https://www.douyin.com/user/{sec_uid}"
    cursor = state.get("max_cursor", "0")
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise AuthorCursorError("cursor 中 max_cursor 无效")
    params = {
        **base,
        "sec_user_id": sec_uid,
        "max_cursor": cursor,
        "min_cursor": "0",
        "count": "20",
        "locate_query": "false",
        "need_time_list": "1",
        "time_list_query": "0",
        "publish_video_strategy_type": "2",
        "show_live_replay_strategy": "1",
        "from_user_page": "1",
        "whale_cut_token": "",
        "cut_version": "1",
    }
    payload = None
    if hasattr(crawler, "captured"):
        payload = await crawler.captured(
            ["/aweme/v1/web/aweme/post/"],
            cursor_key="max_cursor",
            cursor=cursor,
            uid_key="sec_user_id",
            uid=sec_uid,
        )
        if payload is not None and (
            payload.get("status_code") != 0
            or not isinstance(payload.get("aweme_list"), list)
        ):
            payload = None
    if payload is None:
        payload = await crawler._get_json(
            "https://www.douyin.com/aweme/v1/web/aweme/post/",
            params=params
            if hasattr(crawler, "captured") or getattr(crawler, "handles_douyin_signing", False)
            else signed_params(params),
            headers=headers,
        )
    if payload.get("status_code") != 0:
        raise PlatformCrawlerError("抖音作品列表返回失败，可能需要有效会话")
    posts = require_list(payload.get("aweme_list"), "抖音")
    if payload.get("has_more") not in (0, 1, True, False):
        raise PlatformCrawlerError("抖音作品列表缺少分页状态")
    works = []
    for post in posts:
        author = post.get("author") or {}
        if author.get("sec_uid") != sec_uid:
            raise PlatformCrawlerError("抖音作品作者与请求 UID 不匹配")
        work_id = str(post.get("aweme_id") or "")
        if not work_id.isdigit():
            raise PlatformCrawlerError("抖音作品 ID 无效")
        video = post.get("video") or {}
        images = post.get("images") or []
        media = []
        for image in images:
            media.extend(image.get("url_list") or [])
        if not images:
            media.extend((video.get("play_addr") or {}).get("url_list") or [])
        cover = (video.get("cover") or {}).get("url_list") or []
        works.append(
            AuthorWork(
                work_id=work_id,
                author_uid=request.uid,
                resolved_author_uid=str(author.get("uid") or sec_uid),
                media_name="douyin",
                author_name=author.get("nickname") or "",
                content_type="image_post" if images else "video",
                url=f"https://www.douyin.com/{'note' if images else 'video'}/{work_id}",
                title=str(post.get("desc") or ""),
                description=str(post.get("desc") or ""),
                text=str(post.get("desc") or ""),
                published_at=date(post.get("create_time")),
                media_urls=urls(media),
                cover_url=cover[0] if cover else None,
                duration_ms=video.get("duration"),
            )
        )
    next_cursor = None
    if payload.get("has_more"):
        next_value = str(payload.get("max_cursor") or "")
        if not next_value.isdigit() or next_value == cursor:
            raise PlatformCrawlerError("抖音列表游标没有推进")
        next_cursor = encode_cursor(
            request, {"sec_uid": sec_uid, "max_cursor": next_value}
        )
    return AuthorPage(
        uid=request.uid,
        media_name="douyin",
        data=works,
        next_cursor=next_cursor,
        reason="仅覆盖当前会话可访问的作者列表；不保证包含隐藏、删除或权限不可见的作品",
    )
