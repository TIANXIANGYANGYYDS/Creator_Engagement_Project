"""Protocol-only engagement and comment collection by public content URL.

This module deliberately keeps platform-specific parsing small and explicit.  A
platform that needs a browser-only token is reported as ``unsupported`` instead
of returning an empty successful result.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup
from app.crawlers.http_client import (
    AsyncHttpClient,
    CurlAsyncHttpClient,
    PlatformBlockedError,
    PlatformCrawlerError,
)
from app.models.engagement import (
    EngagementComment,
    EngagementCoverage,
    EngagementPlatform,
    EngagementResult,
    EngagementStats,
)


DOUYIN_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
DOUYIN_COMMENT_URL = "https://www.douyin.com/aweme/v1/web/comment/list/"
DOUYIN_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


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

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()  # type: ignore[attr-defined]

    async def fetch(self, url: str, *, comment_limit: int = 20) -> EngagementResult:
        if comment_limit <= 0:
            raise ValueError("comment_limit must be greater than zero")
        platform, work_id = identify_url(url)
        if not work_id:
            raise ValueError(f"cannot extract {platform} content id from URL")
        if platform == "bilibili":
            return await self._bilibili(url, work_id, comment_limit)
        if platform == "douyin":
            return await self._douyin(url, work_id, comment_limit)
        if platform == "weibo":
            return await self._weibo(url, work_id, comment_limit)
        if platform == "haokan":
            return await self._haokan(url, work_id, comment_limit)
        if platform == "xiaohongshu":
            return await self._xiaohongshu(url, work_id, comment_limit)
        if platform == "toutiao":
            return await self._toutiao(url, work_id, comment_limit)
        return EngagementResult(
            platform=platform,
            canonical_url=url,
            work_id=work_id,
            coverage="unsupported",
            reason=_UNSUPPORTED_REASONS[platform],
            source="protocol_probe",
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
        if status in {403, 412, 418, 429, 432, 471}:
            raise PlatformBlockedError(f"engagement endpoint blocked with HTTP {status}")
        if status < 200 or status >= 300:
            raise PlatformCrawlerError(f"engagement endpoint returned HTTP {status}")
        return response

    async def _douyin(self, url: str, work_id: str, limit: int) -> EngagementResult:
        if not self.cookies.strip():
            return _result_error(
                "douyin",
                url,
                work_id,
                "unsupported",
                "抖音详情/评论请求需要 a_bogus 之外的动态设备会话；匿名协议请求返回 HTTP 200 空包，需调用方提供自己的有效会话 Cookie，不能硬编码临时 Cookie",
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
            comment_params = {
                **common_params,
                "cursor": "0",
                "count": str(min(limit, 20)),
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
            comments: list[EngagementComment] = []
            next_cursor: str | None = None
            if comment_text.strip():
                try:
                    comment_payload = comment_response.json()
                except Exception as exc:
                    raise PlatformCrawlerError("抖音评论接口返回非 JSON") from exc
                comments, next_cursor = _parse_douyin_comments(comment_payload)
            reason = ""
            if not comment_text.strip():
                reason = "抖音访客评论接口返回 HTTP 200 空包；统计来自详情接口，评论未伪装为空成功"
            return EngagementResult(
                platform="douyin",
                canonical_url=f"https://www.douyin.com/video/{work_id}",
                work_id=work_id,
                coverage="partial",
                reason=reason or "抖音评论仅获取当前公开页，不能证明评论全集",
                source="aweme/v1/web/aweme/detail + comment/list",
                stats=stats,
                comments=comments[:limit],
                next_cursor=next_cursor,
            )
        except PlatformBlockedError as exc:
            return _result_error("douyin", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("douyin", url, work_id, "failed", str(exc))

    async def _bilibili(self, url: str, work_id: str, limit: int) -> EngagementResult:
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
            stats = EngagementStats(
                views=_int(stat.get("view")),
                likes=_int(stat.get("like")),
                comments=_int(stat.get("reply")),
                shares=_int(stat.get("share")),
                favorites=_int(stat.get("favorite")),
                coins=_int(stat.get("coin")),
                danmaku=_int(stat.get("danmaku")),
            )
            aid = _int(data.get("aid"))
            comments_payload = await self._get_json(
                "https://api.bilibili.com/x/v2/reply",
                params={"type": 1, "oid": aid, "pn": 1, "ps": min(limit, 20), "sort": 2},
            )
            comments, cursor = _parse_bilibili_comments(comments_payload)
            return EngagementResult(
                platform="bilibili",
                canonical_url=f"https://www.bilibili.com/video/{actual_id}",
                work_id=actual_id,
                coverage="partial",
                reason="B 站评论仅获取当前公开页，不能证明评论全集",
                source="x/web-interface/view + x/v2/reply",
                stats=stats,
                comments=comments[:limit],
                next_cursor=cursor,
            )
        except PlatformBlockedError as exc:
            return _result_error("bilibili", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("bilibili", url, work_id, "failed", str(exc))

    async def _weibo(self, url: str, work_id: str, limit: int) -> EngagementResult:
        headers = {
            "Referer": f"https://m.weibo.cn/detail/{work_id}",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
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
            comments_payload = await self._get_json(
                "https://m.weibo.cn/comments/hotflow",
                params={"id": work_id, "mid": work_id, "max_id_type": 0},
                headers=headers,
            )
            comments, cursor = _parse_weibo_comments(comments_payload)
            return EngagementResult(
                platform="weibo",
                canonical_url=f"https://m.weibo.cn/detail/{work_id}",
                work_id=work_id,
                coverage="partial",
                reason="微博访客时间线/评论接口可能折叠或限流，不能证明评论全集",
                source="m.weibo.cn/statuses/show + comments/hotflow",
                stats=stats,
                comments=comments[:limit],
                next_cursor=cursor,
            )
        except PlatformBlockedError as exc:
            return _result_error("weibo", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("weibo", url, work_id, "failed", str(exc))

    async def _haokan(self, url: str, work_id: str, limit: int) -> EngagementResult:
        try:
            payload = await self._get_json(
                "https://haokan.baidu.com/haokan/ui-web/v2/comment/get",
                params={"rn": min(limit, 20), "url_key": work_id, "pn": 1, "child_rn": 2},
                headers={"Referer": f"https://haokan.baidu.com/v?vid={work_id}"},
            )
            data = payload.get("data") or {}
            comments = _parse_haokan_comments(data.get("list") or [])
            count = _int(data.get("comment_count"))
            return EngagementResult(
                platform="haokan",
                canonical_url=f"https://haokan.baidu.com/v?vid={work_id}",
                work_id=work_id,
                coverage="partial",
                reason="好看评论接口可匿名读取；详情页互动统计需额外 video/read 参数，当前未稳定复现",
                source="haokan/ui-web/v2/comment/get",
                stats=EngagementStats(comments=count),
                comments=comments[:limit],
                next_cursor=None if data.get("is_over") else "2",
            )
        except PlatformBlockedError as exc:
            return _result_error("haokan", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("haokan", url, work_id, "failed", str(exc))

    async def _xiaohongshu(self, url: str, work_id: str, limit: int) -> EngagementResult:
        parsed = urlparse(url)
        token = parse_qs(parsed.query).get("xsec_token", [""])[0]
        try:
            response = await self._get_response(
                url,
                headers={"Referer": "https://www.xiaohongshu.com/explore"},
            )
            text = str(getattr(response, "text", ""))
            stats = _parse_xhs_stats(text, work_id)
            reason = "SSR 已提供互动统计；评论接口需要每次生成 x-s/x-t 签名，未硬编码临时签名"
            return EngagementResult(
                platform="xiaohongshu",
                canonical_url=f"https://www.xiaohongshu.com/explore/{work_id}",
                work_id=work_id,
                coverage="partial",
                reason=reason,
                source="note SSR noteDetailMap",
                stats=stats,
                comments=[],
                next_cursor=None,
                xsec_token=token,
                comment_endpoint="https://edith.xiaohongshu.com/api/sns/web/v2/comment/page",
            )
        except PlatformBlockedError as exc:
            return _result_error("xiaohongshu", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("xiaohongshu", url, work_id, "failed", str(exc))

    async def _toutiao(self, url: str, work_id: str, limit: int) -> EngagementResult:
        endpoint = "https://www.toutiao.com/article/v4/tab_comments/"
        try:
            payload = await self._get_json(
                endpoint,
                params={
                    "aid": "24",
                    "app_name": "toutiao_web",
                    "offset": "0",
                    "count": min(limit, 20),
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
            comments = _parse_toutiao_comments(payload.get("data") or [])
            total = _int(payload.get("total_number"))
            offset = _int(payload.get("offset"))
            has_more = bool(payload.get("has_more"))
            return EngagementResult(
                platform="toutiao",
                canonical_url=f"https://www.toutiao.com/article/{work_id}/",
                work_id=work_id,
                coverage="partial",
                reason="头条评论接口可匿名协议读取；当前接口稳定提供评论总数和评论列表，点赞/转发详情需文章 SSR 或会话签名",
                source="article/v4/tab_comments",
                stats=EngagementStats(comments=total),
                comments=comments[:limit],
                next_cursor=str(offset) if has_more and offset is not None else None,
            )
        except PlatformBlockedError as exc:
            return _result_error("toutiao", url, work_id, "blocked", str(exc))
        except Exception as exc:
            return _result_error("toutiao", url, work_id, "failed", str(exc))


_UNSUPPORTED_REASONS: dict[EngagementPlatform, str] = {
    "douyin": "匿名详情请求返回空包；提供调用方自己的有效会话 Cookie 后可尝试读取统计，评论仍可能被访客接口折叠",
    "wechat": "公众号文章正文可从 RSS 获取，但阅读/点赞/评论接口受文章会话与验证码参数保护",
    "kuaishou": (
        "快手作品页纯协议返回错误 JSON；未携带 webWeapon 动态 kww/kwssectoken 时，"
        "visionShortVideoReco 返回的可能是无关推荐流，评论接口返回 Need captcha，不能把推荐作品误报为目标作品"
    ),
}


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
        return "wechat", query.get("mid", [""])[0] or query.get("sn", [""])[0]
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
    return result, str(int(page.get("num", 1)) + 1) if page.get("num") and page.get("count", 0) > page.get("num", 1) * page.get("size", 1) else None


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
