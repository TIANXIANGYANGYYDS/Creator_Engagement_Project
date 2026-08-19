"""Protocol-first engagement and comment collection by public content URL.

This module deliberately keeps platform-specific parsing small and explicit.  A
Platforms that need browser-generated state can be handed to the optional
runtime fallback after the protocol attempt; no captured browser state is
embedded in this module.
"""

from __future__ import annotations

import html
from hashlib import md5
import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from bs4 import BeautifulSoup
from app.crawlers.http_client import (
    AsyncHttpClient,
    CurlAsyncHttpClient,
    PlatformBlockedError,
    PlatformCrawlerError,
)
from app.models.engagement import (
    CommentPageResult,
    EngagementComment,
    EngagementCoverage,
    EngagementPlatform,
    EngagementResult,
    EngagementStats,
    InteractionResult,
)

if TYPE_CHECKING:
    from app.crawlers.browser_fallback import BrowserFallback
    from app.crawlers.platform_session import PlatformSessionStore


DOUYIN_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
DOUYIN_COMMENT_URL = "https://www.douyin.com/aweme/v1/web/comment/list/"
COMMENT_PAGE_SIZE = 20
DOUYIN_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BILIBILI_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
BILIBILI_REPLY_WBI_URL = "https://api.bilibili.com/x/v2/reply/wbi/main"
BILIBILI_REPLY_LEGACY_URL = "https://api.bilibili.com/x/v2/reply"
BILIBILI_WBI_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
KUAISHOU_GRAPHQL_URL = "https://www.kuaishou.com/graphql"
KUAISHOU_COMMENT_URL = "https://www.kuaishou.com/rest/v/photo/comment/list"
KUAISHOU_DETAIL_QUERY = """
query visionVideoDetail($photoId: String) {
  visionVideoDetail(photoId: $photoId) {
    status
    photo {
      id
      viewCount
      likeCount
      realLikeCount
      commentCount
    }
  }
}
"""
KUAISHOU_COMMENT_QUERY = """
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


class EngagementCrawler:
    """Fetch public interactions/comments without browser automation."""

    def __init__(
        self,
        *,
        client: AsyncHttpClient | None = None,
        timeout_seconds: float = 20,
        cookies: str = "",
        proxy_provider: Any | None = None,
        proxy_mode: str = "direct",
        browser_fallback: "BrowserFallback | None" = None,
        session_store: "PlatformSessionStore | None" = None,
        platform_cookies: dict[EngagementPlatform, str] | None = None,
    ) -> None:
        self._owns_client = client is None
        self.client = client or CurlAsyncHttpClient(
            timeout_seconds=timeout_seconds,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
            proxy_provider=proxy_provider,
            proxy_mode=proxy_mode,
        )
        self.cookies = cookies
        self.browser_fallback = browser_fallback
        self.session_store = session_store
        self.platform_cookies = platform_cookies or {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()  # type: ignore[attr-defined]

    async def fetch(self, url: str, *, comment_limit: int = 20) -> EngagementResult:
        if comment_limit <= 0:
            raise ValueError("comment_limit must be greater than zero")
        return await self._fetch(
            url,
            limit=comment_limit,
            page=1,
            include_stats=True,
            include_comments=True,
        )

    async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
        result = await self._fetch(
            url,
            media_name=media_name,
            limit=COMMENT_PAGE_SIZE,
            page=1,
            include_stats=True,
            include_comments=False,
        )
        return InteractionResult.model_validate(
            result.model_dump(exclude={"comments", "next_cursor"})
        )

    async def fetch_comments(self, url: str, media_name: str, page: int) -> CommentPageResult:
        if page <= 0:
            raise ValueError("page must be greater than zero")
        result = await self._fetch(
            url,
            media_name=media_name,
            limit=COMMENT_PAGE_SIZE,
            page=page,
            include_stats=False,
            include_comments=True,
        )
        return CommentPageResult.model_validate({
            **result.model_dump(exclude={"stats", "next_cursor"}),
            "page": page,
            "next_page": page + 1 if result.next_cursor is not None else None,
            "total_comments": result.stats.comments,
        })

    async def _fetch(
        self,
        url: str,
        *,
        media_name: str | None = None,
        limit: int,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        platform, work_id = validate_media_url(url, media_name) if media_name else identify_url(url)
        if not work_id:
            raise ValueError(f"cannot extract {platform} content id from URL")
        if platform == "bilibili":
            result = await self._bilibili(
                url, work_id, limit, page=page,
                include_stats=include_stats, include_comments=include_comments,
            )
        elif platform == "douyin":
            result = await self._douyin(
                url, work_id, limit, page=page,
                include_stats=include_stats, include_comments=include_comments,
            )
        elif platform == "weibo":
            result = await self._weibo(
                url, work_id, limit, page=page,
                include_stats=include_stats, include_comments=include_comments,
            )
        elif platform == "haokan":
            result = await self._haokan(
                url, work_id, limit, page=page,
                include_stats=include_stats, include_comments=include_comments,
            )
        elif platform == "xiaohongshu":
            result = await self._xiaohongshu(
                url, work_id, page=page,
                include_stats=include_stats, include_comments=include_comments,
            )
        elif platform == "toutiao":
            result = await self._toutiao(
                url, work_id, limit, page=page,
                include_stats=include_stats, include_comments=include_comments,
            )
        elif platform == "kuaishou":
            result = await self._kuaishou(
                url, work_id, limit, page=page,
                include_stats=include_stats, include_comments=include_comments,
            )
        elif platform == "wechat":
            result = await self._wechat(
                url, work_id, limit, page=page,
                include_stats=include_stats, include_comments=include_comments,
            )
        else:
            result = EngagementResult(
                platform=platform,
                canonical_url=url,
                work_id=work_id,
                coverage="unsupported",
                reason=_UNSUPPORTED_REASONS[platform],
                source="protocol_probe",
            )
        if self._should_use_browser_fallback(
            result,
            include_stats=include_stats,
            include_comments=include_comments,
        ):
            browser_result = await self._run_browser_fallback(
                url,
                platform,
                work_id,
                page=page,
                limit=limit,
                include_stats=include_stats,
                include_comments=include_comments,
            )
            if browser_result is not None:
                return browser_result
        return result

    @staticmethod
    def _should_use_browser_fallback(
        result: EngagementResult,
        *,
        include_stats: bool,
        include_comments: bool,
    ) -> bool:
        if result.coverage in {"unsupported", "blocked", "failed"}:
            return True
        if include_stats and not any(value is not None for value in result.stats.model_dump().values()):
            return True
        if include_comments and not result.comments and result.coverage == "partial":
            return True
        return False

    async def _run_browser_fallback(
        self,
        url: str,
        platform: EngagementPlatform,
        work_id: str,
        *,
        page: int,
        limit: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult | None:
        if self.browser_fallback is None:
            return None
        try:
            return await self.browser_fallback.fetch(
                url,
                platform,
                work_id,
                page=page,
                limit=limit,
                include_stats=include_stats,
                include_comments=include_comments,
            )
        except Exception as exc:
            return _result_error(
                platform,
                url,
                work_id,
                "failed",
                f"浏览器兜底调用失败: {type(exc).__name__}: {exc}",
            )

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = await self._get_response(url, params=params, headers=headers)
        try:
            payload = response.json()
        except Exception as exc:
            raise PlatformCrawlerError("engagement endpoint returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformCrawlerError("engagement endpoint returned invalid JSON")
        return payload

    async def _post_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._post_response(
            url,
            params=params,
            headers=headers,
            data=data,
            json_body=json_body,
        )
        try:
            payload = response.json()
        except Exception as exc:
            raise PlatformCrawlerError("engagement endpoint returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformCrawlerError("engagement endpoint returned invalid JSON")
        return payload

    async def _get_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        include_cookies: bool = False,
    ) -> Any:
        request_headers = dict(headers or {})
        if include_cookies and self.cookies:
            request_headers["Cookie"] = self.cookies
        try:
            response = await self.client.get(url, params=params, headers=request_headers)
        except Exception as exc:
            raise PlatformCrawlerError("engagement request failed") from exc
        status = int(getattr(response, "status_code", 0))
        if status in {401, 403, 406, 412, 418, 429, 432, 461, 471}:
            raise PlatformBlockedError(f"engagement endpoint blocked with HTTP {status}")
        if status < 200 or status >= 300:
            raise PlatformCrawlerError(f"engagement endpoint returned HTTP {status}")
        return response

    async def _post_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        post = getattr(self.client, "post", None)
        if post is None:
            raise PlatformCrawlerError("HTTP client does not support POST")
        try:
            response = await post(
                url,
                params=params,
                headers=dict(headers or {}),
                data=data,
                json=json_body,
            )
        except Exception as exc:
            raise PlatformCrawlerError("engagement request failed") from exc
        status = int(getattr(response, "status_code", 0))
        if status in {401, 403, 406, 412, 418, 429, 432, 461, 471}:
            raise PlatformBlockedError(f"engagement endpoint blocked with HTTP {status}")
        if status < 200 or status >= 300:
            raise PlatformCrawlerError(f"engagement endpoint returned HTTP {status}")
        return response

    def _platform_cookie(self, platform: EngagementPlatform) -> str:
        configured = self.platform_cookies.get(platform, "").strip()
        if configured:
            return configured
        if self.session_store is not None:
            stored = self.session_store.cookie_header(platform).strip()
            if stored:
                return stored
        return self.cookies.strip() if platform == "douyin" else ""

    async def _douyin(
        self,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        if not self.cookies.strip():
            return _result_error(
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
            "User-Agent": DOUYIN_DESKTOP_USER_AGENT,
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
                detail_response = await self._get_response(
                    DOUYIN_DETAIL_URL,
                    params=common_params,
                    headers=headers,
                    include_cookies=True,
                )
                detail_text = str(getattr(detail_response, "text", "") or "")
                if not detail_text.strip():
                    return _result_error(
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
                    views=_int(statistics.get("play_count")),
                    likes=_int(statistics.get("digg_count")),
                    comments=_int(statistics.get("comment_count")),
                    shares=_int(statistics.get("share_count")),
                    favorites=_int(statistics.get("collect_count")),
                    **{
                        "admire": _int(statistics.get("admire_count")),
                        "recommend": _int(statistics.get("recommend_count")),
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
                    comment_response = await self._get_response(
                        DOUYIN_COMMENT_URL,
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
                    comments, next_cursor = _parse_douyin_comments(comment_payload)
                    total = _int(comment_payload.get("total"))
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
            return _result_error("douyin", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("douyin", url, work_id, "failed", str(exc))

    async def _bilibili(
        self,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        try:
            view = await self._get_json(
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
                    views=_int(stat.get("view")),
                    likes=_int(stat.get("like")),
                    comments=_int(stat.get("reply")),
                    shares=_int(stat.get("share")),
                    favorites=_int(stat.get("favorite")),
                    coins=_int(stat.get("coin")),
                    danmaku=_int(stat.get("danmaku")),
                )
                if include_stats else EngagementStats()
            )
            aid = _int(data.get("aid"))
            comments: list[EngagementComment] = []
            cursor: str | None = None
            comment_source = ""
            if include_comments:
                comments_payload, comment_source = await self._bilibili_comments(aid, page, limit)
                comments, cursor = _parse_bilibili_comments(comments_payload)
                if not include_stats:
                    comment_count = _bilibili_comment_count(comments_payload)
                    stats = EngagementStats(comments=comment_count)
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
            return _result_error("bilibili", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("bilibili", url, work_id, "failed", str(exc))

    async def _bilibili_comments(
        self,
        aid: int | None,
        page: int,
        limit: int,
    ) -> tuple[dict[str, Any], str]:
        """Fetch one requested page through the current web WBI reply API.

        The web endpoint is cursor-based even though our public contract is page-based;
        walk cursors from the first page so callers do not need to know Bilibili's
        internal pagination format.  A legacy endpoint fallback keeps the collector
        useful during short WBI key rotations or API rollouts.
        """

        if aid is None:
            raise PlatformCrawlerError("bilibili view contains no aid")
        try:
            nav = await self._get_json(BILIBILI_NAV_URL)
            mixin_key = _extract_bilibili_wbi_mixin_key(nav)
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
                payload = await self._get_json(
                    BILIBILI_REPLY_WBI_URL,
                    params=_sign_bilibili_wbi_params(base_params, mixin_key=mixin_key),
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
            # The old endpoint is still accepted for some public videos.  It is a
            # compatibility fallback, never the primary implementation.
            return await self._get_json(
                BILIBILI_REPLY_LEGACY_URL,
                params={
                    "type": 1,
                    "oid": aid,
                    "pn": page,
                    "ps": min(limit, COMMENT_PAGE_SIZE),
                    "sort": 2,
                },
            ), "x/v2/reply"

    async def _weibo(
        self,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        headers = {
            "Referer": f"https://m.weibo.cn/detail/{work_id}",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            stats = EngagementStats()
            comments: list[EngagementComment] = []
            cursor: str | None = None
            sources: list[str] = []
            if include_stats:
                payload = await self._get_json(
                    "https://m.weibo.cn/statuses/show",
                    params={"id": work_id},
                    headers=headers,
                )
                data = payload.get("data") or {}
                stats = EngagementStats(
                    likes=_int(data.get("attitudes_count")),
                    comments=_int(data.get("comments_count")),
                    reposts=_int(data.get("reposts_count")),
                )
                sources.append("m.weibo.cn/statuses/show")
            if include_comments:
                request_cursor: str | None = None
                for current_page in range(1, page + 1):
                    params: dict[str, Any] = {
                        "id": work_id,
                        "mid": work_id,
                        "max_id_type": 0,
                    }
                    if request_cursor is not None:
                        params["max_id"] = request_cursor
                    comments_payload = await self._get_json(
                        "https://m.weibo.cn/comments/hotflow",
                        params=params,
                        headers=headers,
                    )
                    comments, cursor = _parse_weibo_comments(comments_payload)
                    if not include_stats:
                        comment_data = comments_payload.get("data") or {}
                        total = _int(comment_data.get("total_number") or comments_payload.get("total_number"))
                        if total is not None:
                            stats = EngagementStats(comments=total)
                    if current_page == page:
                        break
                    if cursor is None:
                        comments = []
                        break
                    request_cursor = cursor
                sources.append("m.weibo.cn/comments/hotflow")
            return EngagementResult(
                platform="weibo",
                canonical_url=f"https://m.weibo.cn/detail/{work_id}",
                work_id=work_id,
                coverage="partial",
                reason=(
                    "微博访客评论接口可能折叠或限流，不能证明评论全集"
                    if include_comments else "微博访客详情接口提供当前公开互动量"
                ),
                source=" + ".join(sources),
                stats=stats,
                comments=comments[:limit],
                next_cursor=cursor,
            )
        except PlatformBlockedError as exc:
            return _result_error("weibo", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("weibo", url, work_id, "failed", str(exc))

    async def _haokan(
        self,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        try:
            payload = await self._get_json(
                "https://haokan.baidu.com/haokan/ui-web/v2/comment/get",
                params={
                    "rn": min(limit, COMMENT_PAGE_SIZE),
                    "url_key": work_id,
                    "pn": page if include_comments else 1,
                    "child_rn": 2,
                },
                headers={"Referer": f"https://haokan.baidu.com/v?vid={work_id}"},
            )
            data = payload.get("data") or {}
            comments = _parse_haokan_comments(data.get("list") or []) if include_comments else []
            count = _int(data.get("comment_count"))
            return EngagementResult(
                platform="haokan",
                canonical_url=f"https://haokan.baidu.com/v?vid={work_id}",
                work_id=work_id,
                coverage="partial",
                reason=(
                    "好看评论接口可匿名读取指定页，不能证明评论全集"
                    if include_comments
                    else "当前稳定协议仅提供评论总数；其他互动量仍需额外 video/read 参数"
                ),
                source="haokan/ui-web/v2/comment/get",
                stats=EngagementStats(comments=count),
                comments=comments[:limit],
                next_cursor=None if data.get("is_over") or not include_comments else str(page + 1),
            )
        except PlatformBlockedError as exc:
            return _result_error("haokan", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("haokan", url, work_id, "failed", str(exc))

    async def _xiaohongshu(
        self,
        url: str,
        work_id: str,
        *,
        page: int = 1,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        parsed = urlparse(url)
        token = parse_qs(parsed.query).get("xsec_token", [""])[0]
        source = parse_qs(parsed.query).get("xsec_source", ["pc_search"])[0] or "pc_search"
        cookie = self._platform_cookie("xiaohongshu")
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
                        feed = await self._xhs_post("/api/sns/web/v1/feed", feed_payload, cookie, url)
                        note = _xhs_note_card(feed)
                        stats = _xhs_stats_from_note(note)
                        if any(value is not None for value in stats.model_dump().values()):
                            sources.append("api/sns/web/v1/feed")
                    except PlatformCrawlerError:
                        pass
                if not any(value is not None for value in stats.model_dump().values()):
                    response = await self._get_response(
                        url,
                        headers={"Referer": "https://www.xiaohongshu.com/explore"},
                    )
                    stats = _parse_xhs_stats(str(getattr(response, "text", "")), work_id)
                    sources.append("note SSR noteDetailMap")

            if include_comments:
                if not cookie:
                    if include_stats:
                        return EngagementResult(
                            platform="xiaohongshu",
                            canonical_url=canonical_url,
                            work_id=work_id,
                            coverage="partial",
                            reason="小红书 SSR 已返回互动统计；评论需要登录态和 xsec_token",
                            source="note SSR noteDetailMap",
                            stats=stats,
                        )
                    return _result_error(
                        "xiaohongshu",
                        url,
                        work_id,
                        "unsupported",
                        "小红书评论需要登录态；请在本机运行 login_platform xiaohongshu 建立会话",
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
                    return _result_error(
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
                    comment_payload = await self._xhs_get(
                        "/api/sns/web/v2/comment/page",
                        params,
                        cookie,
                        url,
                    )
                    comments = _parse_xhs_comments(comment_payload)
                    total = _int(
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

            if not comments and not any(value is not None for value in stats.model_dump().values()):
                return _result_error(
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
                    "小红书登录会话和纯算法动态签名已生效；评论仅返回指定公开页"
                    if include_comments
                    else "小红书互动量来自签名详情接口或笔记 SSR"
                ),
                source=" + ".join(sources),
                stats=stats,
                comments=comments,
                next_cursor=next_cursor,
            )
        except PlatformBlockedError as exc:
            return _result_error("xiaohongshu", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("xiaohongshu", url, work_id, "failed", str(exc))

    async def _xhs_get(
        self,
        uri: str,
        params: dict[str, Any],
        cookie: str,
        referer: str,
    ) -> dict[str, Any]:
        headers = _xhs_signed_headers(uri, cookie, params=params)
        headers.update(_authenticated_headers(cookie, referer))
        query = "&".join(
            f"{quote(str(key), safe='')}={quote(str(value), safe=',')}"
            for key, value in params.items()
        )
        payload = await self._get_json(
            f"https://edith.xiaohongshu.com{uri}?{query}",
            headers=headers,
        )
        return _unwrap_platform_data(payload, "小红书")

    async def _xhs_post(
        self,
        uri: str,
        payload: dict[str, Any],
        cookie: str,
        referer: str,
    ) -> dict[str, Any]:
        headers = _xhs_signed_headers(uri, cookie, payload=payload)
        headers.update(_authenticated_headers(cookie, referer))
        headers["Content-Type"] = "application/json;charset=UTF-8"
        response = await self._post_json(
            f"https://edith.xiaohongshu.com{uri}",
            headers=headers,
            data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        )
        return _unwrap_platform_data(response, "小红书")

    async def _kuaishou(
        self,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        cookie = self._platform_cookie("kuaishou")
        if not cookie:
            return _result_error(
                "kuaishou",
                url,
                work_id,
                "unsupported",
                "快手 GraphQL/评论接口需要登录态，且网页请求会带短期 kww/kwssectoken；匿名评论常见响应为 Need captcha。请在本机运行 login_platform kuaishou 建立会话",
            )
        headers = _authenticated_headers(cookie, url)
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
                    "query": KUAISHOU_DETAIL_QUERY,
                }
                detail_response = await self._post_json(
                    KUAISHOU_GRAPHQL_URL,
                    headers=headers,
                    data=json.dumps(detail_payload, separators=(",", ":")),
                )
                _raise_kuaishou_errors(detail_response)
                detail = (detail_response.get("data") or {}).get("visionVideoDetail") or {}
                photo = detail.get("photo") or {}
                if str(photo.get("id") or "") != work_id:
                    raise PlatformCrawlerError("快手详情响应未匹配目标 photoId")
                stats = EngagementStats(
                    views=_int(photo.get("viewCount")),
                    likes=_int(photo.get("likeCount") or photo.get("realLikeCount")),
                    comments=_int(photo.get("commentCount")),
                )
                sources.append("graphql visionVideoDetail")

            if include_comments:
                cursor = ""
                total: int | None = None
                for current_page in range(1, page + 1):
                    comment_payload = await self._kuaishou_comment_page(
                        work_id,
                        cursor,
                        headers,
                    )
                    comments = _parse_kuaishou_comments(
                        comment_payload.get("rootCommentsV2") or []
                    )
                    total = _int(
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
            return _result_error("kuaishou", url, work_id, "blocked", str(exc))
        except Exception as exc:
            coverage: EngagementCoverage = "blocked" if _is_challenge_text(str(exc)) else "failed"
            return _result_error("kuaishou", url, work_id, coverage, str(exc))

    async def _kuaishou_comment_page(
        self,
        work_id: str,
        cursor: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        rest_payload = {"photoId": work_id, "pcursor": cursor}
        rest = await self._post_json(
            KUAISHOU_COMMENT_URL,
            headers=headers,
            data=json.dumps(rest_payload, separators=(",", ":")),
        )
        if rest.get("result") == 1:
            return rest
        if _is_challenge_text(json.dumps(rest, ensure_ascii=False)):
            raise PlatformBlockedError("快手评论接口要求验证码或会话已失效")
        graphql_payload = {
            "operationName": "commentListQuery",
            "variables": {"photoId": work_id, "pcursor": cursor},
            "query": KUAISHOU_COMMENT_QUERY,
        }
        graphql = await self._post_json(
            KUAISHOU_GRAPHQL_URL,
            headers=headers,
            data=json.dumps(graphql_payload, separators=(",", ":")),
        )
        _raise_kuaishou_errors(graphql)
        comment_list = (graphql.get("data") or {}).get("visionCommentList")
        if not isinstance(comment_list, dict):
            raise PlatformCrawlerError("快手评论响应没有目标数据")
        return comment_list

    async def _wechat(
        self,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        cookie = self._platform_cookie("wechat")
        try:
            article_response = await self._get_response(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Referer": "https://mp.weixin.qq.com/",
                    **({"Cookie": cookie} if cookie else {}),
                },
            )
            text = str(getattr(article_response, "text", "") or "")
            metadata = _parse_wechat_metadata(text, url)
            stats = _parse_wechat_stats(text) if include_stats else EngagementStats()
            if include_stats and not any(value is not None for value in stats.model_dump().values()) and cookie:
                try:
                    ext_payload = await self._wechat_appmsgext(url, metadata, cookie)
                    stats = _parse_wechat_stats_from_payload(ext_payload)
                except PlatformCrawlerError:
                    pass

            if not include_comments:
                has_stats = any(value is not None for value in stats.model_dump().values())
                return EngagementResult(
                    platform="wechat",
                    canonical_url=url,
                    work_id=work_id,
                    coverage="partial" if has_stats else "unsupported",
                    reason=(
                        "公众号文章页面返回了当前会话可见互动量"
                        if has_stats
                        else "公众号匿名文章页未下发阅读/点赞统计；需微信文章会话，普通网页账号登录不一定有效"
                    ),
                    source="mp article appmsgstat/cgiDataNew",
                    stats=stats,
                )

            if metadata.get("show_comment") == "0":
                return EngagementResult(
                    platform="wechat",
                    canonical_url=url,
                    work_id=work_id,
                    coverage="partial",
                    reason="该公众号文章由作者关闭评论，登录账号也不会产生可抓取评论",
                    source="cgiDataNew.show_comment",
                    stats=stats,
                )
            required = ("biz", "mid", "idx", "comment_id")
            if any(not metadata.get(key) for key in required):
                return _result_error(
                    "wechat", url, work_id, "unsupported", "公众号文章没有暴露完整评论参数"
                )
            if not cookie:
                return _result_error(
                    "wechat",
                    url,
                    work_id,
                    "unsupported",
                    "公众号评论需要有效微信文章会话；请在本机运行 login_platform wechat 后再试",
                )
            params = {
                "action": "getcomment",
                "__biz": metadata["biz"],
                "mid": metadata["mid"],
                "idx": metadata["idx"],
                "comment_id": metadata["comment_id"],
                "offset": str((page - 1) * min(limit, COMMENT_PAGE_SIZE)),
                "limit": str(min(limit, COMMENT_PAGE_SIZE)),
                "appmsg_token": metadata.get("appmsg_token", ""),
                "uin": metadata.get("uin", ""),
                "key": metadata.get("key", ""),
                "pass_ticket": metadata.get("pass_ticket", ""),
                "wxtoken": "777",
                "devicetype": metadata.get("devicetype", "Windows 10 x64"),
                "clientversion": metadata.get("clientversion", "63090c11"),
                "f": "json",
            }
            payload = await self._get_json(
                "https://mp.weixin.qq.com/mp/appmsg_comment",
                params=params,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": url,
                    "Cookie": cookie,
                },
            )
            base = payload.get("base_resp") or {}
            raw_ret = payload.get("ret") if payload.get("ret") is not None else base.get("ret")
            try:
                ret = int(raw_ret) if raw_ret not in {None, ""} else None
            except (TypeError, ValueError):
                ret = None
            errmsg = str(payload.get("errmsg") or base.get("errmsg") or "")
            if ret not in {None, 0}:
                coverage: EngagementCoverage = "blocked" if ret == -3 or "session" in errmsg.lower() else "failed"
                return _result_error("wechat", url, work_id, coverage, f"公众号评论接口: {errmsg or ret}")
            comments = _parse_wechat_comments(payload)
            total = _int(
                payload.get("total_count")
                or payload.get("total")
                or _find_nested_value(payload, {"total_count", "total"})
            )
            has_more = bool(payload.get("is_continue") or payload.get("has_more"))
            return EngagementResult(
                platform="wechat",
                canonical_url=url,
                work_id=work_id,
                coverage="partial",
                reason="公众号评论依赖当前微信文章会话；仅返回指定公开页",
                source="mp/appmsg_comment",
                stats=EngagementStats(comments=total),
                comments=comments[:limit],
                next_cursor=str(page + 1) if has_more else None,
            )
        except PlatformBlockedError as exc:
            return _result_error("wechat", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("wechat", url, work_id, "failed", str(exc))

    async def _wechat_appmsgext(
        self,
        article_url: str,
        metadata: dict[str, str],
        cookie: str,
    ) -> dict[str, Any]:
        query = {
            "__biz": metadata.get("biz", ""),
            "mid": metadata.get("mid", ""),
            "idx": metadata.get("idx", "1"),
            "sn": metadata.get("sn", ""),
            "scene": "0",
            "appmsg_token": metadata.get("appmsg_token", ""),
        }
        form = {
            "r": "0",
            "__biz": metadata.get("biz", ""),
            "appmsg_type": "9",
            "mid": metadata.get("mid", ""),
            "sn": metadata.get("sn", ""),
            "idx": metadata.get("idx", "1"),
            "scene": "0",
            "title": "",
            "ct": "0",
            "is_need_ad": "0",
            "is_need_reward": "0",
            "comment_id": metadata.get("comment_id", ""),
            "is_only_read": "0",
            "appmsg_token": metadata.get("appmsg_token", ""),
        }
        payload = await self._post_json(
            "https://mp.weixin.qq.com/mp/getappmsgext",
            params=query,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": article_url,
                "Cookie": cookie,
            },
            data=urlencode(form),
        )
        return payload

    async def _toutiao(
        self,
        url: str,
        work_id: str,
        limit: int,
        *,
        page: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        endpoint = "https://www.toutiao.com/article/v4/tab_comments/"
        try:
            stats = EngagementStats()
            sources: list[str] = []
            reason = ""
            # The article document is the only public source for digg/read/share
            # counters.  Keep it on the interaction path so comments remain a
            # separate paged request as required by the API contract.
            if include_stats and not include_comments:
                try:
                    article_response = await self._get_response(
                        f"https://www.toutiao.com/article/{work_id}/",
                        headers={
                            "Accept": "text/html,application/xhtml+xml",
                            "Referer": f"https://www.toutiao.com/article/{work_id}/",
                        },
                    )
                    stats = _parse_toutiao_stats(str(getattr(article_response, "text", "") or ""))
                    if any(value is not None for value in stats.model_dump().values()):
                        sources.append("article SSR itemCounter")
                    else:
                        reason = "头条文章 SSR 未返回 itemCounter，当前只报告可验证字段"
                except PlatformCrawlerError as exc:
                    reason = f"头条文章 SSR 请求受阻，互动统计不可用: {exc}"

            if not include_comments:
                return EngagementResult(
                    platform="toutiao",
                    canonical_url=f"https://www.toutiao.com/article/{work_id}/",
                    work_id=work_id,
                    coverage="partial",
                    reason=reason or "头条互动统计来自文章 SSR，字段可能随页面挑战而缺失",
                    source=" + ".join(sources) or "article SSR",
                    stats=stats,
                )

            payload = await self._get_json(
                endpoint,
                params={
                    "aid": "24",
                    "app_name": "toutiao_web",
                    "offset": str((page - 1) * min(limit, COMMENT_PAGE_SIZE)) if include_comments else "0",
                    "count": min(limit, COMMENT_PAGE_SIZE),
                    "group_id": work_id,
                    "item_id": work_id,
                },
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"https://www.toutiao.com/article/{work_id}/",
                },
            )
            if payload.get("err_no") != 0:
                raise PlatformCrawlerError("toutiao comments payload is unavailable")
            comments = (
                _parse_toutiao_comments(payload.get("data") or [])
                if include_comments else []
            )
            total = _int(payload.get("total_number"))
            offset = _int(payload.get("offset"))
            has_more = bool(payload.get("has_more"))
            return EngagementResult(
                platform="toutiao",
                canonical_url=f"https://www.toutiao.com/article/{work_id}/",
                work_id=work_id,
                coverage="partial",
                reason=(
                    "头条评论接口可匿名读取指定页，不能证明评论全集"
                    if include_comments else reason
                ),
                source="article/v4/tab_comments",
                stats=EngagementStats(comments=total),
                comments=comments[:limit],
                next_cursor=str(offset) if include_comments and has_more and offset is not None else None,
            )
        except PlatformBlockedError as exc:
            return _result_error("toutiao", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("toutiao", url, work_id, "failed", str(exc))


_UNSUPPORTED_REASONS: dict[EngagementPlatform, str] = {
    "douyin": "匿名详情请求返回空包；提供调用方自己的有效会话 Cookie 后可尝试读取统计，评论仍可能被访客接口折叠",
    "wechat": "公众号匿名 getappmsgext 不下发阅读/点赞统计，appmsg_comment 返回 no session；互动量和评论都需要文章会话参数",
    "kuaishou": (
        "快手作品页纯协议返回错误 JSON；未携带 webWeapon 动态 kww/kwssectoken 时，"
        "visionShortVideoReco 返回的可能是无关推荐流，评论接口返回 Need captcha，不能把推荐作品误报为目标作品"
    ),
}


_MEDIA_ALIASES: dict[str, EngagementPlatform] = {
    "douyin": "douyin",
    "抖音": "douyin",
    "toutiao": "toutiao",
    "头条": "toutiao",
    "今日头条": "toutiao",
    "wechat": "wechat",
    "weixin": "wechat",
    "微信": "wechat",
    "公众号": "wechat",
    "微信公众号": "wechat",
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "小红书": "xiaohongshu",
    "haokan": "haokan",
    "好看": "haokan",
    "好看视频": "haokan",
    "kuaishou": "kuaishou",
    "快手": "kuaishou",
    "bilibili": "bilibili",
    "b站": "bilibili",
    "weibo": "weibo",
    "微博": "weibo",
}


def normalize_media_name(media_name: str) -> EngagementPlatform:
    normalized = re.sub(r"\s+", "", media_name.strip().casefold())
    platform = _MEDIA_ALIASES.get(normalized)
    if platform is None:
        supported = ", ".join((
            "douyin", "toutiao", "wechat", "xiaohongshu",
            "haokan", "kuaishou", "bilibili", "weibo",
        ))
        raise ValueError(f"unsupported media_name; expected one of: {supported}")
    return platform


def validate_media_url(url: str, media_name: str) -> tuple[EngagementPlatform, str]:
    requested_platform = normalize_media_name(media_name)
    detected_platform, work_id = identify_url(url)
    if detected_platform != requested_platform:
        raise ValueError(
            f"media_name '{media_name}' does not match URL platform '{detected_platform}'"
        )
    return detected_platform, work_id


def identify_url(url: str) -> tuple[EngagementPlatform, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path
    query = parse_qs(parsed.query)
    if "bilibili.com" in host:
        match = re.search(r"/(BV[0-9A-Za-z]+|av\d+)(?:/|$)", path)
        work_id = match.group(1) if match else query.get("bvid", [""])[0]
        return "bilibili", work_id[2:] if work_id.startswith("av") else work_id
    if "weibo.com" in host or host == "m.weibo.cn":
        match = re.search(r"/(?:detail/)?(\d{8,})", path)
        return "weibo", (match.group(1) if match else query.get("id", [""])[0])
    if "xiaohongshu.com" in host:
        match = re.search(r"/explore/([0-9a-f]{24})", path, re.I)
        return "xiaohongshu", match.group(1) if match else ""
    if "haokan.baidu.com" in host:
        return "haokan", query.get("vid", [""])[0]
    if "douyin.com" in host or "iesdouyin.com" in host:
        match = re.search(r"/(?:video|share/video)/(\d+)", path)
        return "douyin", match.group(1) if match else ""
    if "toutiao.com" in host:
        match = re.search(r"/(?:article|video)/(\d+)", path)
        return "toutiao", match.group(1) if match else ""
    if "kuaishou.com" in host:
        match = re.search(r"/(?:short-video|profile)/([^/?]+)", path)
        return "kuaishou", match.group(1) if match else ""
    if "mp.weixin.qq.com" in host:
        path_match = re.search(r"/s/([^/?]+)", path)
        return "wechat", (
            query.get("mid", [""])[0]
            or query.get("sn", [""])[0]
            or (path_match.group(1) if path_match else "")
        )
    raise ValueError("unsupported content URL host")


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _result_error(platform: EngagementPlatform, url: str, work_id: str, coverage: EngagementCoverage, reason: str) -> EngagementResult:
    return EngagementResult(platform=platform, canonical_url=url, work_id=work_id, coverage=coverage, reason=reason, source="protocol")


def _parse_bilibili_comments(payload: dict[str, Any]) -> tuple[list[EngagementComment], str | None]:
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
            text=BeautifulSoup(str(item.get("content", {}).get("message") or ""), "html.parser").get_text(" ", strip=True),
            created_at=datetime.fromtimestamp(int(item.get("ctime") or 0), tz=timezone.utc) if item.get("ctime") else None,
            likes=_int(item.get("like")),
            replies=_int(item.get("rcount")),
        ))
    page = data.get("page") or {}
    cursor = data.get("cursor") or {}
    next_cursor = cursor.get("next")
    if next_cursor not in (None, "", 0):
        return result, str(next_cursor)
    return result, str(int(page.get("num", 1)) + 1) if page.get("num") and page.get("count", 0) > page.get("num", 1) * page.get("size", 1) else None


def _bilibili_comment_count(payload: dict[str, Any]) -> int | None:
    data = payload.get("data") or {}
    cursor = data.get("cursor") or {}
    if cursor.get("all_count") is not None:
        return _int(cursor.get("all_count"))
    return _int((data.get("page") or {}).get("count"))


def _bilibili_image_key(url: Any) -> str:
    filename = urlparse(str(url or "")).path.rsplit("/", 1)[-1]
    key = filename.rsplit(".", 1)[0]
    if not key:
        raise PlatformCrawlerError("bilibili nav contains an invalid WBI image URL")
    return key


def _extract_bilibili_wbi_mixin_key(payload: dict[str, Any]) -> str:
    if payload.get("code") not in {0, -101} or not isinstance(payload.get("data"), dict):
        raise PlatformCrawlerError(f"bilibili nav returned code {payload.get('code')}")
    wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
    source = _bilibili_image_key(wbi_img.get("img_url")) + _bilibili_image_key(wbi_img.get("sub_url"))
    if len(source) <= max(BILIBILI_WBI_MIXIN_KEY_ENC_TAB):
        raise PlatformCrawlerError("bilibili nav WBI image keys are invalid")
    return "".join(source[index] for index in BILIBILI_WBI_MIXIN_KEY_ENC_TAB)[:32]


def _sign_bilibili_wbi_params(
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


def _authenticated_headers(cookie: str, referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
        "Referer": referer,
        "User-Agent": DOUYIN_DESKTOP_USER_AGENT,
    }


def _xhs_signed_headers(
    uri: str,
    cookie: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, str]:
    try:
        from xhshow import Xhshow
    except ImportError as exc:
        raise PlatformCrawlerError("小红书签名依赖 xhshow 未安装") from exc
    signer = Xhshow()
    if params is not None:
        signed = signer.sign_headers_get(uri=uri, cookies=cookie, params=params)
    elif payload is not None:
        signed = signer.sign_headers_post(uri=uri, cookies=cookie, payload=payload)
    else:
        raise ValueError("params or payload is required")
    return {
        key: value
        for key, value in {
            "X-S": signed.get("x-s"),
            "X-T": signed.get("x-t"),
            "X-S-Common": signed.get("x-s-common"),
            "X-B3-Traceid": signed.get("x-b3-traceid"),
        }.items()
        if value
    }


def _unwrap_platform_data(payload: dict[str, Any], platform: str) -> dict[str, Any]:
    if payload.get("success") is False:
        raise PlatformCrawlerError(str(payload.get("msg") or f"{platform} response failed"))
    if payload.get("success") is True:
        data = payload.get("data")
        return data if isinstance(data, dict) else {}
    return payload


def _xhs_note_card(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items") or []
    if isinstance(items, list) and items and isinstance(items[0], dict):
        card = items[0].get("note_card") or items[0].get("noteCard") or items[0]
        return card if isinstance(card, dict) else {}
    return payload.get("note_card") or payload.get("noteCard") or payload


def _xhs_stats_from_note(note: dict[str, Any]) -> EngagementStats:
    interact = note.get("interact_info") or note.get("interactInfo") or note
    return EngagementStats(
        likes=_int(interact.get("liked_count") or interact.get("likedCount")),
        favorites=_int(interact.get("collected_count") or interact.get("collectedCount")),
        shares=_int(interact.get("share_count") or interact.get("shareCount")),
        comments=_int(interact.get("comment_count") or interact.get("commentCount")),
    )


def _parse_xhs_comments(payload: dict[str, Any]) -> list[EngagementComment]:
    result: list[EngagementComment] = []
    for item in payload.get("comments") or payload.get("comment_list") or []:
        if not isinstance(item, dict):
            continue
        user = item.get("user_info") or item.get("userInfo") or item.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        comment_id = str(item.get("id") or item.get("comment_id") or item.get("commentId") or "")
        text = str(item.get("content") or item.get("text") or "")
        if not comment_id or not text:
            continue
        timestamp = item.get("create_time") or item.get("createTime") or item.get("timestamp")
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(user.get("nickname") or user.get("nick_name") or user.get("name") or ""),
            text=BeautifulSoup(text, "html.parser").get_text(" ", strip=True),
            created_at=_timestamp(timestamp),
            likes=_int(item.get("like_count") or item.get("likeCount") or item.get("liked_count")),
            replies=_int(item.get("sub_comment_count") or item.get("subCommentCount")),
        ))
    return result


def _parse_kuaishou_comments(items: list[Any]) -> list[EngagementComment]:
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
            created_at=_timestamp(item.get("timestamp")),
            likes=_int(item.get("likedCount") or item.get("liked_count")),
            replies=_int(item.get("subCommentCount") or item.get("sub_comment_count")),
        ))
    return result


def _timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _raise_kuaishou_errors(payload: dict[str, Any]) -> None:
    errors = payload.get("errors")
    if errors:
        message = json.dumps(errors, ensure_ascii=False)
        if _is_challenge_text(message):
            raise PlatformBlockedError("快手接口要求验证码或会话已失效")
        raise PlatformCrawlerError(message)


def _is_challenge_text(value: str) -> bool:
    lowered = value.casefold()
    return any(token in lowered for token in ("need captcha", "captcha", "验证码", "security check", "登录"))


def _parse_wechat_metadata(text: str, url: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in (
        "show_comment", "comment_id", "bizuin", "__biz", "mid", "idx", "sn",
        "appmsg_token", "uin", "key", "pass_ticket", "devicetype", "clientversion",
    ):
        match = re.search(
            rf"(?:[\"']{re.escape(key)}[\"']|\b{re.escape(key)})\s*[:=]\s*[\"']?([^,;\"'\s}}]+)",
            text,
            re.I,
        )
        if match:
            values[key] = html.unescape(match.group(1))
    if "biz" not in values:
        values["biz"] = values.get("bizuin", "")
    parsed = urlparse(url)
    values.setdefault("mid", parse_qs(parsed.query).get("mid", [""])[0])
    values.setdefault("idx", parse_qs(parsed.query).get("idx", ["1"])[0])
    return values


def _parse_wechat_stats(text: str) -> EngagementStats:
    decoded = html.unescape(text).replace(r"\u0022", '"').replace(r'\"', '"')
    values: dict[str, int | None] = {}
    for key in ("read_num", "readCount", "like_num", "old_like_num", "comment_count", "commentCount", "share_count", "shareCount"):
        match = re.search(rf"[\"']{re.escape(key)}[\"']\s*[:=]\s*[\"']?([0-9]+)", decoded)
        if match:
            values[key] = _int(match.group(1))
    return EngagementStats(
        views=values.get("read_num") or values.get("readCount"),
        likes=values.get("like_num") or values.get("old_like_num"),
        comments=values.get("comment_count") or values.get("commentCount"),
        shares=values.get("share_count") or values.get("shareCount"),
    )


def _parse_wechat_stats_from_payload(payload: dict[str, Any]) -> EngagementStats:
    nested = payload.get("appmsgstat") or payload.get("appmsg_stat") or payload
    if not isinstance(nested, dict):
        return EngagementStats()
    return EngagementStats(
        views=_int(nested.get("read_num") or nested.get("readCount")),
        likes=_int(nested.get("like_num") or nested.get("old_like_num") or nested.get("likeCount")),
        comments=_int(nested.get("comment_count") or nested.get("commentCount")),
        shares=_int(nested.get("share_count") or nested.get("shareCount")),
    )


def _parse_wechat_comments(payload: dict[str, Any]) -> list[EngagementComment]:
    candidates = payload.get("elected_comment") or payload.get("comment") or payload.get("comments") or []
    if isinstance(candidates, dict):
        candidates = candidates.get("list") or candidates.get("comment") or []
    result: list[EngagementComment] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        comment_id = str(item.get("comment_id") or item.get("id") or "")
        text = str(item.get("content") or item.get("text") or "")
        if not comment_id or not text:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(item.get("nick_name") or item.get("nickname") or item.get("user_name") or ""),
            text=BeautifulSoup(text, "html.parser").get_text(" ", strip=True),
            created_at=_timestamp(item.get("create_time") or item.get("createTime")),
            likes=_int(item.get("like_num") or item.get("like_count")),
            replies=_int(item.get("reply_count") or item.get("reply_num")),
        ))
    return result


def _find_nested_value(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child is not None and child != "":
                return child
            found = _find_nested_value(child, keys)
            if found is not None and found != "":
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_nested_value(child, keys)
            if found is not None and found != "":
                return found
    return None


def _parse_douyin_comments(payload: dict[str, Any]) -> tuple[list[EngagementComment], str | None]:
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
            created_at=datetime.fromtimestamp(int(item.get("create_time") or 0), tz=timezone.utc) if item.get("create_time") else None,
            likes=_int(item.get("digg_count")),
            replies=_int(item.get("reply_comment_total")),
        ))
    if payload.get("has_more") in {0, False}:
        return result, None
    cursor = payload.get("cursor")
    return result, str(cursor) if cursor not in {None, ""} else None


def _parse_weibo_comments(payload: dict[str, Any]) -> tuple[list[EngagementComment], str | None]:
    data = payload.get("data") or {}
    result = []
    for item in data.get("data") or []:
        user = item.get("user") or {}
        comment_id = str(item.get("idstr") or item.get("id") or "")
        if not comment_id:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(user.get("screen_name") or ""),
            text=BeautifulSoup(str(item.get("text") or ""), "html.parser").get_text(" ", strip=True),
            created_at=_parse_weibo_date(item.get("created_at")),
            likes=_int(item.get("like_count")),
            replies=_int(item.get("total_number")),
        ))
    return result, str(data.get("max_id")) if data.get("max_id") else None


def _parse_haokan_comments(items: list[Any]) -> list[EngagementComment]:
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        comment_id = str(item.get("reply_id") or item.get("id") or "")
        if not comment_id:
            continue
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(item.get("uname") or item.get("user_name") or ""),
            text=BeautifulSoup(str(item.get("content") or ""), "html.parser").get_text(" ", strip=True),
            created_at=datetime.fromtimestamp(int(item.get("create_time") or 0), tz=timezone.utc) if item.get("create_time") else None,
            likes=_int(item.get("like_count")),
            replies=_int(item.get("reply_count")),
        ))
    return result


def _parse_toutiao_comments(items: list[Any]) -> list[EngagementComment]:
    result: list[EngagementComment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        comment = item.get("comment") or {}
        comment_id = str(comment.get("id_str") or comment.get("id") or "")
        if not comment_id:
            continue
        created_at = comment.get("create_time")
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(comment.get("user_name") or ""),
            text=BeautifulSoup(str(comment.get("text") or ""), "html.parser").get_text(" ", strip=True),
            created_at=datetime.fromtimestamp(int(created_at), tz=timezone.utc) if created_at else None,
            likes=_int(comment.get("digg_count")),
            replies=_int(comment.get("reply_count")),
        ))
    return result


def _parse_xhs_stats(text: str, work_id: str) -> EngagementStats:
    decoded = html.unescape(text).replace("\\u002F", "/")
    detail_anchor = decoded.find(f'"noteDetailMap":{{"{work_id}"')
    if detail_anchor >= 0:
        detail_segment = decoded[detail_anchor:detail_anchor + 20000]
        detail_match = re.search(r'"interactInfo"\s*:\s*\{(.*?)\}', detail_segment, re.S)
        if detail_match:
            values = _xhs_interaction_values(detail_match.group(1))
            if values:
                return _xhs_stats(values)
    note_pattern = re.compile(r'"noteId"\s*:\s*"' + re.escape(work_id) + r'"')
    for note_match in reversed(list(note_pattern.finditer(decoded))):
        prefix = decoded[max(0, note_match.start() - 3000):note_match.start()]
        matches = list(re.finditer(r'"interactInfo"\s*:\s*\{(.*?)\}', prefix, re.S))
        if not matches:
            continue
        body = matches[-1].group(1)
        values = _xhs_interaction_values(body)
        if values:
            return _xhs_stats(values)
    return EngagementStats()


def _parse_toutiao_stats(text: str) -> EngagementStats:
    """Parse public article counters from plain or URL-encoded SSR JSON."""

    decoded = html.unescape(text)
    for _ in range(2):
        decoded = unquote(decoded)
    decoded = decoded.replace(r"\u0022", '"').replace(r'\"', '"')
    match = re.search(r'"itemCounter"\s*:\s*\{([^{}]+)\}', decoded, re.S)
    body = match.group(1) if match else decoded
    values = {
        key: _int(value)
        for key, value in re.findall(
            r'"(commentCount|diggCount|readCount|shareCount|showCount)"\s*:\s*"?([^",}\s]+)',
            body,
        )
    }
    like_match = re.search(r'"likeData"\s*:\s*\{[^{}]*"count"\s*:\s*"?([^",}\s]+)', decoded, re.S)
    if values.get("diggCount") is None and like_match:
        values["diggCount"] = _int(like_match.group(1))
    return EngagementStats(
        views=values.get("readCount"),
        likes=values.get("diggCount"),
        comments=values.get("commentCount"),
        shares=values.get("shareCount"),
        **{"show_count": values.get("showCount")},
    )


def _xhs_interaction_values(body: str) -> dict[str, int | None]:
    return {
        key: _int(value)
        for key, value in re.findall(
            r'"(likedCount|collectedCount|shareCount|commentCount)"\s*:\s*"?([^",}]+)',
            body,
        )
    }


def _xhs_stats(values: dict[str, int | None]) -> EngagementStats:
    return EngagementStats(
        likes=values.get("likedCount"),
        favorites=values.get("collectedCount"),
        shares=values.get("shareCount"),
        comments=values.get("commentCount"),
    )


def _parse_weibo_date(value: Any) -> datetime | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime
    try:
        parsed = parsedate_to_datetime(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


async def fetch_engagement(
    url: str,
    *,
    comment_limit: int = 20,
    client: AsyncHttpClient | None = None,
    cookies: str = "",
    proxy_provider: Any | None = None,
    proxy_mode: str = "direct",
) -> EngagementResult:
    """Convenience function for one URL; closes the internally-created client."""
    crawler = EngagementCrawler(
        client=client,
        cookies=cookies,
        proxy_provider=proxy_provider,
        proxy_mode=proxy_mode,
    )
    try:
        return await crawler.fetch(url, comment_limit=comment_limit)
    finally:
        await crawler.aclose()


async def fetch_interactions(
    url: str,
    media_name: str,
    *,
    client: AsyncHttpClient | None = None,
    cookies: str = "",
    proxy_provider: Any | None = None,
    proxy_mode: str = "direct",
) -> InteractionResult:
    crawler = EngagementCrawler(
        client=client,
        cookies=cookies,
        proxy_provider=proxy_provider,
        proxy_mode=proxy_mode,
    )
    try:
        return await crawler.fetch_interactions(url, media_name)
    finally:
        await crawler.aclose()


async def fetch_comments(
    url: str,
    media_name: str,
    page: int,
    *,
    client: AsyncHttpClient | None = None,
    cookies: str = "",
    proxy_provider: Any | None = None,
    proxy_mode: str = "direct",
) -> CommentPageResult:
    crawler = EngagementCrawler(
        client=client,
        cookies=cookies,
        proxy_provider=proxy_provider,
        proxy_mode=proxy_mode,
    )
    try:
        return await crawler.fetch_comments(url, media_name, page)
    finally:
        await crawler.aclose()
