"""Bilibili detail, WBI signing, and paged reply flow."""

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
LIVE_ROOM_INFO_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
LIVE_HISTORY_URL = "https://api.live.bilibili.com/xlive/web-room/v1/dM/gethistory"
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
    if work_id.startswith("live:"):
        return await fetch_live(
            crawler,
            url,
            work_id.removeprefix("live:"),
            limit,
            page=page,
            include_stats=include_stats,
            include_comments=include_comments,
        )
    try:
        view = await crawler._get_json(
            "https://api.bilibili.com/x/web-interface/view",
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
            comments_payload, comment_source = await fetch_comments(crawler, aid, page, limit)
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


async def fetch_live(
    crawler: PlatformCrawlerContext,
    url: str,
    room_id: str,
    limit: int,
    *,
    page: int,
    include_stats: bool,
    include_comments: bool,
) -> EngagementResult:
    """Collect current room counters and the public recent-danmaku window."""

    try:
        stats = EngagementStats()
        sources: list[str] = []
        if include_stats:
            payload = await crawler._get_json(
                LIVE_ROOM_INFO_URL,
                params={"room_id": room_id},
                headers={"Referer": f"https://live.bilibili.com/{room_id}"},
            )
            data = payload.get("data") or {}
            if payload.get("code") != 0 or str(data.get("room_id") or "") != room_id:
                raise PlatformCrawlerError("bilibili live room payload is unavailable")
            stats = EngagementStats(
                **{
                    "online": to_int(data.get("online")),
                    "followers": to_int(data.get("attention")),
                    "live_status": to_int(data.get("live_status")),
                }
            )
            sources.append("Room/get_info")

        comments: list[EngagementComment] = []
        if include_comments:
            if page > 1:
                return result_error(
                    "bilibili",
                    url,
                    room_id,
                    "unsupported",
                    "B 站直播公开接口只提供最近弹幕窗口，不支持历史评论页码",
                )
            payload = await crawler._post_json(
                LIVE_HISTORY_URL,
                data={"roomid": room_id},
                headers={"Referer": f"https://live.bilibili.com/{room_id}"},
            )
            if payload.get("code") != 0:
                raise PlatformCrawlerError("bilibili live history payload is unavailable")
            comments = parse_live_comments(payload)[:limit]
            sources.append("dM/gethistory")

        return EngagementResult(
            platform="bilibili",
            canonical_url=f"https://live.bilibili.com/{room_id}",
            work_id=room_id,
            coverage="partial",
            reason=(
                "B 站直播公开接口只提供最近弹幕窗口，不代表历史评论全集"
                if include_comments
                else "B 站直播互动量仅提供当前在线人数、关注数和开播状态"
            ),
            source=" + ".join(sources),
            stats=stats,
            comments=comments,
        )
    except PlatformBlockedError as exc:
        return result_error("bilibili", url, room_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("bilibili", url, room_id, "failed", str(exc))


def parse_live_comments(payload: dict[str, Any]) -> list[EngagementComment]:
    data = payload.get("data") or {}
    result: list[EngagementComment] = []
    for item in [*(data.get("admin") or []), *(data.get("room") or [])]:
        comment_id = str(item.get("id_str") or item.get("rnd") or "")
        text = str(item.get("text") or "")
        if not comment_id or not text:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(item.get("nickname") or ""),
            text=text,
        ))
    return result


async def fetch_comments(
    crawler: PlatformCrawlerContext,
    aid: int | None,
    page: int,
    limit: int,
) -> tuple[dict[str, Any], str]:
    """Fetch one requested page, preferring the current cursor-based WBI API."""

    if aid is None:
        raise PlatformCrawlerError("bilibili view contains no aid")
    try:
        nav = await crawler._get_json(NAV_URL)
        mixin_key = extract_wbi_mixin_key(nav)
        offset = ""
        payload: dict[str, Any] = {}
        for current_page in range(1, page + 1):
            base_params = {
                "oid": str(aid),
                "type": "1",
                "mode": "3",
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
                headers={"Referer": "https://www.bilibili.com/"},
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
        return await crawler._get_json(
            REPLY_LEGACY_URL,
            params={
                "type": 1,
                "oid": aid,
                "pn": page,
                "ps": min(limit, COMMENT_PAGE_SIZE),
                "sort": 2,
            },
        ), "x/v2/reply"


def parse_comments(payload: dict[str, Any]) -> tuple[list[EngagementComment], str | None]:
    data = payload.get("data") or {}
    result: list[EngagementComment] = []
    for item in data.get("replies") or []:
        member = item.get("member") or {}
        comment_id = str(item.get("rpid") or item.get("rpid_str") or "")
        if not comment_id:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(member.get("uname") or ""),
            text=BeautifulSoup(
                str(item.get("content", {}).get("message") or ""),
                "html.parser",
            ).get_text(" ", strip=True),
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
    if next_cursor not in (None, "", 0):
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
