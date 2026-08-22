"""Bilibili video/article detail and paged WBI reply flows."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import md5
import json
import re
from typing import Any
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.models.engagement import EngagementComment, EngagementResult, EngagementStats


NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
REPLY_WBI_URL = "https://api.bilibili.com/x/v2/reply/wbi/main"
REPLY_LEGACY_URL = "https://api.bilibili.com/x/v2/reply"
VIDEO_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
ARTICLE_VIEWINFO_URL = "https://api.bilibili.com/x/article/viewinfo"
WBI_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


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
    if work_id.startswith("article:"):
        return await fetch_article(
            crawler,
            url,
            work_id.removeprefix("article:"),
            limit,
            page=page,
            include_stats=include_stats,
            include_comments=include_comments,
        )
    if work_id.startswith("opus:"):
        return await fetch_opus(
            crawler,
            url,
            work_id.removeprefix("opus:"),
            limit,
            page=page,
            include_stats=include_stats,
            include_comments=include_comments,
        )
    return await fetch_video(
        crawler,
        url,
        work_id,
        limit,
        page=page,
        include_stats=include_stats,
        include_comments=include_comments,
    )


async def fetch_video(
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
        view = await crawler._get_json(
            VIDEO_VIEW_URL,
            params={"bvid": work_id} if work_id.startswith("BV") else {"aid": work_id},
        )
        data = view.get("data") or {}
        if view.get("code") != 0 or not data:
            raise PlatformCrawlerError("bilibili view payload is unavailable")
        actual_id = str(data.get("bvid") or work_id)
        stat = data.get("stat") or {}
        stats = (
            EngagementStats(
                views=to_int(stat.get("view")),
                likes=to_int(stat.get("like")),
                comments=to_int(stat.get("reply")),
                shares=to_int(stat.get("share")),
                favorites=to_int(stat.get("favorite")),
                coins=to_int(stat.get("coin")),
                danmaku=to_int(stat.get("danmaku")),
            )
            if include_stats else EngagementStats()
        )
        aid = to_int(data.get("aid"))
        comments: list[EngagementComment] = []
        cursor: str | None = None
        comment_source = ""
        if include_comments:
            comments_payload, comment_source = await fetch_comments(
                crawler,
                aid,
                page,
                limit,
                reply_type=1,
                referer=f"https://www.bilibili.com/video/{actual_id}",
            )
            comments, cursor = parse_comments(comments_payload)
            if not include_stats:
                stats = EngagementStats(comments=comment_count(comments_payload))
        return EngagementResult(
            platform="bilibili",
            canonical_url=f"https://www.bilibili.com/video/{actual_id}",
            work_id=actual_id,
            coverage="partial",
            reason=(
                "B 站评论仅获取当前公开页（由 page 指定），不能证明评论全集"
                if include_comments else "B 站详情接口提供当前公开互动量"
            ),
            source=(
                f"x/web-interface/view + {comment_source}"
                if include_comments else "x/web-interface/view"
            ),
            stats=stats,
            comments=comments[:limit],
            next_cursor=cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("bilibili", url, work_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("bilibili", url, work_id, "failed", str(exc))


async def fetch_article(
    crawler: PlatformCrawlerContext,
    url: str,
    article_id: str,
    limit: int,
    *,
    page: int,
    include_stats: bool,
    include_comments: bool,
) -> EngagementResult:
    try:
        stats = EngagementStats()
        sources: list[str] = []
        if include_stats:
            payload = await crawler._get_json(
                ARTICLE_VIEWINFO_URL,
                params={"id": article_id},
                headers={"Referer": f"https://www.bilibili.com/read/cv{article_id}/"},
            )
            data = payload.get("data") or {}
            if payload.get("code") != 0 or not isinstance(data.get("stats"), dict):
                raise PlatformCrawlerError("bilibili article payload is unavailable")
            stats = parse_article_stats(data)
            sources.append("x/article/viewinfo")

        comments: list[EngagementComment] = []
        cursor: str | None = None
        if include_comments:
            payload, comment_source = await fetch_comments(
                crawler,
                article_id,
                page,
                limit,
                reply_type=12,
                referer=f"https://www.bilibili.com/read/cv{article_id}/",
            )
            comments, cursor = parse_comments(payload)
            if not include_stats:
                stats = EngagementStats(comments=comment_count(payload))
            sources.append(comment_source)

        return EngagementResult(
            platform="bilibili",
            canonical_url=f"https://www.bilibili.com/read/cv{article_id}/",
            work_id=f"cv{article_id}",
            coverage="partial",
            reason=(
                "B 站专栏评论仅获取 page 指定的当前公开页"
                if include_comments
                else "B 站专栏详情接口提供当前公开互动量"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments[:limit],
            next_cursor=cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("bilibili", url, article_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("bilibili", url, article_id, "failed", str(exc))


async def fetch_opus(
    crawler: PlatformCrawlerContext,
    url: str,
    opus_id: str,
    limit: int,
    *,
    page: int,
    include_stats: bool,
    include_comments: bool,
) -> EngagementResult:
    try:
        canonical_url = f"https://www.bilibili.com/opus/{opus_id}"
        response = await crawler._get_response(
            canonical_url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://www.bilibili.com/",
            },
            discard_cookies=True,
        )
        state = parse_opus_initial_state(str(getattr(response, "text", "") or ""))
        detail = state.get("detail") or {}
        if str(detail.get("id_str") or "") != opus_id:
            raise PlatformCrawlerError("bilibili opus page contains no matching detail")

        stats = parse_opus_stats(detail) if include_stats else EngagementStats()
        comments: list[EngagementComment] = []
        cursor: str | None = None
        sources = ["opus __INITIAL_STATE__"]
        if include_comments:
            basic = detail.get("basic") or {}
            comment_oid = str(
                basic.get("comment_id_str") or basic.get("rid_str") or ""
            )
            reply_type = to_int(basic.get("comment_type"))
            if not comment_oid or reply_type is None:
                raise PlatformCrawlerError("bilibili opus contains no public comment target")
            payload, comment_source = await fetch_comments(
                crawler,
                comment_oid,
                page,
                limit,
                reply_type=reply_type,
                referer=canonical_url,
            )
            comments, cursor = parse_comments(payload)
            if not include_stats:
                stats = EngagementStats(comments=comment_count(payload))
            sources.append(comment_source)

        return EngagementResult(
            platform="bilibili",
            canonical_url=canonical_url,
            work_id=opus_id,
            coverage="partial",
            reason=(
                "B 站 Opus/图文评论仅获取 page 指定的当前公开页"
                if include_comments
                else "B 站 Opus 页面预载当前公开互动量"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments[:limit],
            next_cursor=cursor,
        )
    except PlatformBlockedError as exc:
        return result_error("bilibili", url, opus_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("bilibili", url, opus_id, "failed", str(exc))


def parse_article_stats(data: dict[str, Any]) -> EngagementStats:
    stat = data.get("stats") or {}
    return EngagementStats(
        views=to_int(stat.get("view")),
        likes=to_int(stat.get("like")),
        comments=to_int(stat.get("reply")),
        shares=to_int(stat.get("share")),
        favorites=to_int(stat.get("favorite")),
        coins=to_int(stat.get("coin")),
        reposts=to_int(stat.get("dynamic")),
    )


def parse_opus_initial_state(text: str) -> dict[str, Any]:
    assignment = re.search(r"window\.__INITIAL_STATE__\s*=\s*", text)
    if assignment is None:
        raise PlatformCrawlerError("bilibili opus page contains no initial state")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[assignment.end():].lstrip())
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PlatformCrawlerError("bilibili opus initial state is invalid") from exc
    if not isinstance(value, dict):
        raise PlatformCrawlerError("bilibili opus initial state is invalid")
    return value


def parse_opus_stats(detail: dict[str, Any]) -> EngagementStats:
    module_stat: dict[str, Any] = {}
    for module in detail.get("modules") or []:
        if isinstance(module, dict) and isinstance(module.get("module_stat"), dict):
            module_stat = module["module_stat"]
            break

    def count(key: str) -> int | None:
        value = module_stat.get(key) or {}
        return to_int(value.get("count")) if isinstance(value, dict) else None

    return EngagementStats(
        likes=count("like"),
        comments=count("comment"),
        shares=count("forward"),
        favorites=count("favorite"),
        coins=count("coin"),
    )


async def fetch_comments(
    crawler: PlatformCrawlerContext,
    oid: int | str | None,
    page: int,
    limit: int,
    *,
    reply_type: int,
    referer: str,
) -> tuple[dict[str, Any], str]:
    """Fetch one requested page, preferring the current cursor-based WBI API."""

    if oid is None or str(oid) == "":
        raise PlatformCrawlerError("bilibili content contains no comment oid")
    try:
        nav = await crawler._get_json(NAV_URL)
        mixin_key = extract_wbi_mixin_key(nav)
        offset = ""
        payload: dict[str, Any] = {}
        for current_page in range(1, page + 1):
            base_params = {
                "oid": str(oid),
                "type": str(reply_type),
                "mode": "2",
                "pagination_str": json.dumps(
                    {"offset": offset},
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                "plat": "1",
                "seek_rpid": "",
                "web_location": "1315875",
            }
            payload = await crawler._get_json(
                REPLY_WBI_URL,
                params=sign_wbi_params(base_params, mixin_key=mixin_key),
                headers={"Referer": referer},
            )
            if payload.get("code") != 0:
                raise PlatformCrawlerError(
                    f"bilibili WBI reply returned code {payload.get('code')}"
                )
            if current_page < page:
                cursor = (payload.get("data") or {}).get("cursor") or {}
                pagination_reply = cursor.get("pagination_reply") or {}
                next_offset = pagination_reply.get("next_offset")
                if not next_offset:
                    return {
                        "code": 0,
                        "data": {
                            "cursor": {
                                "all_count": cursor.get("all_count"),
                                "next": 0,
                            },
                            "replies": [],
                        },
                    }, "x/v2/reply/wbi/main"
                offset = str(next_offset)
        return payload, "x/v2/reply/wbi/main"
    except PlatformCrawlerError:
        payload = await crawler._get_json(
            REPLY_LEGACY_URL,
            params={
                "type": reply_type,
                "oid": oid,
                "pn": page,
                "ps": min(limit, COMMENT_PAGE_SIZE),
                "sort": 2,
            },
            headers={"Referer": referer},
        )
        if payload.get("code") != 0:
            raise PlatformCrawlerError(
                f"bilibili legacy reply returned code {payload.get('code')}"
            )
        return payload, "x/v2/reply"


def parse_comments(payload: dict[str, Any]) -> tuple[list[EngagementComment], str | None]:
    data = payload.get("data") or {}
    result: list[EngagementComment] = []
    for item in data.get("replies") or []:
        member = item.get("member") or {}
        comment_id = str(item.get("rpid") or item.get("rpid_str") or "")
        if not comment_id:
            continue
        raw_text = str(item.get("content", {}).get("message") or "")
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(member.get("uname") or ""),
            text=(
                BeautifulSoup(raw_text, "html.parser").get_text(" ", strip=True)
                if "<" in raw_text and ">" in raw_text
                else raw_text.strip()
            ),
            created_at=(
                datetime.fromtimestamp(int(item.get("ctime") or 0), tz=timezone.utc)
                if item.get("ctime") else None
            ),
            likes=to_int(item.get("like")),
            replies=to_int(item.get("rcount")),
        ))
    page = data.get("page") or {}
    cursor = data.get("cursor") or {}
    next_cursor = cursor.get("next")
    pagination_reply = cursor.get("pagination_reply") or {}
    next_offset = (
        pagination_reply.get("next_offset")
        if isinstance(pagination_reply, dict)
        else None
    )
    if (
        cursor.get("is_end") is not True
        and next_cursor not in (None, "", 0)
        and next_offset
    ):
        return result, str(next_cursor)
    if page.get("num") and page.get("count", 0) > page.get("num", 1) * page.get("size", 1):
        return result, str(int(page.get("num", 1)) + 1)
    return result, None


def comment_count(payload: dict[str, Any]) -> int | None:
    data = payload.get("data") or {}
    cursor = data.get("cursor") or {}
    if cursor.get("all_count") is not None:
        return to_int(cursor.get("all_count"))
    return to_int((data.get("page") or {}).get("count"))


def _image_key(url: Any) -> str:
    filename = urlparse(str(url or "")).path.rsplit("/", 1)[-1]
    key = filename.rsplit(".", 1)[0]
    if not key:
        raise PlatformCrawlerError("bilibili nav contains an invalid WBI image URL")
    return key


def extract_wbi_mixin_key(payload: dict[str, Any]) -> str:
    if payload.get("code") not in {0, -101} or not isinstance(payload.get("data"), dict):
        raise PlatformCrawlerError(f"bilibili nav returned code {payload.get('code')}")
    wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
    source = _image_key(wbi_img.get("img_url")) + _image_key(wbi_img.get("sub_url"))
    if len(source) <= max(WBI_MIXIN_KEY_ENC_TAB):
        raise PlatformCrawlerError("bilibili nav WBI image keys are invalid")
    return "".join(source[index] for index in WBI_MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi_params(
    params: dict[str, Any],
    *,
    mixin_key: str,
    wts: int | None = None,
) -> dict[str, str]:
    timestamp = int(datetime.now(timezone.utc).timestamp()) if wts is None else wts
    signed = {
        key: re.sub(r"[!'()*]", "", str(value))
        for key, value in params.items()
    }
    signed["wts"] = str(timestamp)
    query = urlencode(sorted(signed.items()))
    signed["w_rid"] = md5(f"{query}{mixin_key}".encode()).hexdigest()
    return signed
