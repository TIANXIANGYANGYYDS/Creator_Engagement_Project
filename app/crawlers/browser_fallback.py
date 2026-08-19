"""Runtime browser fallback for platform pages that reject protocol requests.

The protocol crawler remains the first attempt.  This module is deliberately
small: it opens a persistent Camoufox profile, lets the platform generate its
own cookies/signatures, captures JSON responses, and converts useful public
fields into the project's common result model.  Captured credentials are never
written to source or logs.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.models.engagement import (
    EngagementComment,
    EngagementPlatform,
    EngagementResult,
    EngagementStats,
)


@dataclass(frozen=True)
class BrowserFallbackSettings:
    timeout_seconds: float = 35
    challenge_wait_seconds: float = 5
    headless: bool | str = True
    profile_dir: Path = Path(".local/browser-profiles")


class BrowserFallback:
    """Use one persistent browser profile per platform and request."""

    def __init__(
        self,
        *,
        settings: BrowserFallbackSettings | None = None,
        proxy_provider: Any | None = None,
        cookies: str = "",
    ) -> None:
        self.settings = settings or BrowserFallbackSettings()
        self.proxy_provider = proxy_provider
        self.cookies = cookies
        self._locks: dict[EngagementPlatform, asyncio.Lock] = {}

    async def fetch(
        self,
        url: str,
        platform: EngagementPlatform,
        work_id: str,
        *,
        page: int,
        limit: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        lock = self._locks.setdefault(platform, asyncio.Lock())
        async with lock:
            return await self._fetch_locked(
                url,
                platform,
                work_id,
                page=page,
                limit=limit,
                include_stats=include_stats,
                include_comments=include_comments,
            )

    async def _fetch_locked(
        self,
        url: str,
        platform: EngagementPlatform,
        work_id: str,
        *,
        page: int,
        limit: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        proxy_mapping = None
        lease_ok = False
        try:
            if self.proxy_provider is not None:
                proxy_mapping = await self.proxy_provider.get_requests_proxies()
            proxy = _playwright_proxy(proxy_mapping)
            profile_dir = self.settings.profile_dir / platform
            profile_dir.mkdir(parents=True, exist_ok=True)

            from camoufox.async_api import AsyncCamoufox
            from camoufox.addons import DefaultAddons

            async with AsyncCamoufox(
                headless=self.settings.headless,
                os="windows",
                locale="zh-CN",
                humanize=True,
                block_webrtc=True,
                proxy=proxy,
                persistent_context=True,
                user_data_dir=str(profile_dir),
                exclude_addons=[DefaultAddons.UBO],
                enable_cache=True,
            ) as context:
                await self._seed_cookies(context, url, platform)
                page_obj = context.pages[0] if context.pages else await context.new_page()
                responses: list[Any] = []

                def collect(response: Any) -> None:
                    resource_type = getattr(response, "request", None)
                    resource_type = getattr(resource_type, "resource_type", "")
                    if resource_type in {"document", "xhr", "fetch"}:
                        responses.append(response)

                page_obj.on("response", collect)
                try:
                    await page_obj.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=int(self.settings.timeout_seconds * 1000),
                    )
                except Exception:
                    # A challenge page can keep navigation open; already captured
                    # responses are still useful and are parsed below.
                    pass
                await self._interact_with_page(page_obj, platform, page)
                await page_obj.wait_for_timeout(int(self.settings.challenge_wait_seconds * 1000))
                result = await self._parse_responses(
                    page_obj,
                    responses,
                    url,
                    platform,
                    work_id,
                    page=page,
                    limit=limit,
                    include_stats=include_stats,
                    include_comments=include_comments,
                )
                # Some platforms defer the first API call until the page has
                # settled.  One reload is cheap compared with returning an
                # unexplained empty result from an otherwise valid profile.
                if result.coverage in {"unsupported", "blocked"} and not result.comments and not any(
                    value is not None for value in result.stats.model_dump().values()
                ):
                    responses.clear()
                    try:
                        await page_obj.reload(
                            wait_until="domcontentloaded",
                            timeout=int(self.settings.timeout_seconds * 1000),
                        )
                    except Exception:
                        pass
                    await self._interact_with_page(page_obj, platform, page)
                    await page_obj.wait_for_timeout(int(self.settings.challenge_wait_seconds * 1000))
                    result = await self._parse_responses(
                        page_obj,
                        responses,
                        url,
                        platform,
                        work_id,
                        page=page,
                        limit=limit,
                        include_stats=include_stats,
                        include_comments=include_comments,
                    )
            lease_ok = True
            return result
        except Exception as exc:
            return EngagementResult(
                platform=platform,
                canonical_url=url,
                work_id=work_id,
                coverage="failed",
                reason=f"浏览器兜底执行失败: {type(exc).__name__}: {exc}",
                source="browser_fallback",
            )
        finally:
            if self.proxy_provider is not None and proxy_mapping is not None:
                callback = (
                    getattr(self.proxy_provider, "on_success_for", None)
                    if lease_ok
                    else getattr(self.proxy_provider, "on_failure_for", None)
                )
                if callback is not None:
                    value = callback(proxy_mapping, RuntimeError("browser fallback failed")) if not lease_ok else callback(proxy_mapping)
                    if asyncio.iscoroutine(value):
                        await value

    async def _seed_cookies(self, context: Any, url: str, platform: EngagementPlatform) -> None:
        if not self.cookies.strip():
            return
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        cookie_list = []
        for item in self.cookies.split(";"):
            if "=" not in item:
                continue
            name, value = item.strip().split("=", 1)
            if name and value:
                cookie_list.append({"name": name, "value": value, "url": origin})
        if cookie_list:
            await context.add_cookies(cookie_list)

    async def _interact_with_page(self, page: Any, platform: EngagementPlatform, requested_page: int) -> None:
        # Trigger lazy comment requests.  Selectors are intentionally generic so
        # platform markup changes do not break the protocol path.
        if platform in {"douyin", "xiaohongshu", "kuaishou", "wechat", "bilibili", "weibo"}:
            for _ in range(min(max(requested_page, 1) + 1, 6)):
                try:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(500)
                except Exception:
                    break
        for selector in (
            "button:has-text('评论')",
            "[role='button']:has-text('评论')",
            "button:has-text('重试')",
            "button:has-text('刷新')",
        ):
            try:
                locator = page.locator(selector).first
                if await locator.is_visible(timeout=300):
                    await locator.click(timeout=700)
                    await page.wait_for_timeout(700)
                    break
            except Exception:
                continue

    async def _parse_responses(
        self,
        page_obj: Any,
        responses: list[Any],
        url: str,
        platform: EngagementPlatform,
        work_id: str,
        *,
        page: int,
        limit: int,
        include_stats: bool,
        include_comments: bool,
    ) -> EngagementResult:
        stats = EngagementStats()
        comments: list[EngagementComment] = []
        total_comments: int | None = None
        sources: list[str] = []
        body_text = ""
        try:
            body_text = await page_obj.locator("body").inner_text(timeout=1000)
        except Exception:
            pass

        for response in responses:
            response_url = str(getattr(response, "url", ""))
            try:
                text = await response.text()
            except Exception:
                continue
            if not text:
                continue
            payload: Any = None
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                pass
            if platform == "douyin":
                stats, comments, total_comments, source = _parse_douyin(response_url, payload, work_id, stats, comments)
            elif platform == "toutiao":
                stats, comments, total_comments, source = _parse_toutiao(response_url, text, payload, stats, comments)
            elif platform == "xiaohongshu":
                stats, comments, total_comments, source = _parse_xhs(response_url, text, payload, stats, comments)
            elif platform == "haokan":
                stats, comments, total_comments, source = _parse_haokan(response_url, payload, stats, comments)
            elif platform == "wechat":
                stats, comments, total_comments, source = _parse_wechat(response_url, text, payload, stats, comments)
            elif platform == "kuaishou":
                stats, comments, total_comments, source = _parse_kuaishou(response_url, payload, work_id, stats, comments)
            elif platform == "bilibili":
                stats, comments, total_comments, source = _parse_bilibili(response_url, payload, stats, comments)
            elif platform == "weibo":
                stats, comments, total_comments, source = _parse_weibo(response_url, payload, stats, comments)
            else:
                source = ""
            if source and source not in sources:
                sources.append(source)

        comments = comments[:limit]
        useful_stats = any(value is not None for value in stats.model_dump().values())
        useful_comments = bool(comments)
        challenge = _challenge_reason(body_text)
        if not useful_stats and not useful_comments:
            coverage = "blocked" if challenge else "unsupported"
            reason = challenge or "浏览器已生成会话，但页面未捕获目标公开互动或评论响应"
        else:
            coverage = "partial"
            reason = (
                "浏览器会话捕获平台公开响应；评论仅返回当前加载页"
                if include_comments else "浏览器会话捕获平台公开互动响应"
            )
        if include_stats and not useful_stats:
            reason += "；互动字段未返回"
        if include_comments and not useful_comments:
            reason += "；评论字段未返回"
        return EngagementResult(
            platform=platform,
            canonical_url=url,
            work_id=work_id,
            coverage=coverage,
            reason=reason,
            source="browser:" + ",".join(sources) if sources else "browser_fallback",
            stats=stats if include_stats or total_comments is not None else EngagementStats(),
            comments=comments if include_comments else [],
            next_cursor=str(page + 1) if include_comments and useful_comments else None,
        )


def _playwright_proxy(proxies: dict[str, str] | None) -> dict[str, str] | None:
    if not proxies:
        return None
    server = proxies.get("https") or proxies.get("http")
    return {"server": server} if server else None


def _number(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _comment(item: dict[str, Any], *, default_id: str = "") -> EngagementComment | None:
    comment_id = str(item.get("cid") or item.get("id") or item.get("comment_id") or default_id)
    text = str(item.get("text") or item.get("content") or item.get("message") or "")
    user = item.get("user") or item.get("user_info") or item.get("author") or {}
    if not isinstance(user, dict):
        user = {}
    author = str(user.get("nickname") or user.get("nick_name") or user.get("name") or user.get("uname") or item.get("user_name") or "")
    if not comment_id or not text:
        return None
    created = item.get("create_time") or item.get("created_at") or item.get("time")
    created_at = None
    if created:
        try:
            created_at = datetime.fromtimestamp(float(created), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return EngagementComment(
        comment_id=comment_id,
        author=author,
        text=text,
        created_at=created_at,
        likes=_number(item.get("digg_count") or item.get("like_count") or item.get("like")),
        replies=_number(item.get("reply_count") or item.get("reply_comment_total") or item.get("rcount")),
    )


def _walk_comments(value: Any) -> list[EngagementComment]:
    found: list[EngagementComment] = []
    if isinstance(value, dict):
        parsed = _comment(value)
        if parsed:
            found.append(parsed)
        for child in value.values():
            found.extend(_walk_comments(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_comments(child))
    unique: dict[str, EngagementComment] = {}
    for item in found:
        unique.setdefault(item.comment_id, item)
    return list(unique.values())


def _parse_douyin(url: str, payload: Any, work_id: str, stats: EngagementStats, comments: list[EngagementComment]):
    if "/aweme/v1/web/aweme/detail/" in url and isinstance(payload, dict):
        detail = payload.get("aweme_detail") or {}
        if str(detail.get("aweme_id") or work_id) == work_id:
            values = detail.get("statistics") or {}
            stats = EngagementStats(
                views=_number(values.get("play_count")), likes=_number(values.get("digg_count")),
                comments=_number(values.get("comment_count")), shares=_number(values.get("share_count")),
                favorites=_number(values.get("collect_count")),
            )
            return stats, comments, stats.comments, "aweme/detail"
    if "/aweme/v1/web/comment/list/" in url and isinstance(payload, dict):
        parsed = [_comment(item) for item in payload.get("comments") or []]
        return stats, [x for x in parsed if x], _number(payload.get("total")), "comment/list"
    return stats, comments, stats.comments, ""


def _parse_toutiao(url: str, text: str, payload: Any, stats: EngagementStats, comments: list[EngagementComment]):
    if "tab_comments" in url and isinstance(payload, dict):
        items = payload.get("data") or []
        parsed = []
        for item in items:
            item = item.get("comment") if isinstance(item, dict) and isinstance(item.get("comment"), dict) else item
            if isinstance(item, dict):
                value = _comment(item)
                if value:
                    parsed.append(value)
        return stats, parsed, _number(payload.get("total_number")), "article/tab_comments"
    try:
        from app.crawlers.engagement import _parse_toutiao_stats

        parsed_stats = _parse_toutiao_stats(text)
    except Exception:
        parsed_stats = EngagementStats()
    values = parsed_stats.model_dump()
    if any(value is not None for value in values.values()):
        return parsed_stats, comments, parsed_stats.comments, "article SSR"
    return stats, comments, stats.comments, ""


def _stats_from_text(text: str) -> dict[str, int | None]:
    def match(*names: str) -> int | None:
        for name in names:
            result = re.search(rf'"{name}"\s*:\s*"?([0-9,]+)', text)
            if result:
                return _number(result.group(1))
        return None
    return {
        "views": match("readCount", "read_count"),
        "likes": match("diggCount", "likeCount", "likedCount"),
        "comments": match("commentCount", "comment_count"),
        "shares": match("shareCount", "share_count"),
        "favorites": match("collectedCount", "collectCount"),
    }


def _parse_xhs(url: str, text: str, payload: Any, stats: EngagementStats, comments: list[EngagementComment]):
    if "comment/page" in url:
        parsed = _walk_comments(payload)
        total = _number(_find_key(payload, {"comment_count", "commentCount", "total"}))
        return stats, parsed, total or stats.comments, "comment/page"
    try:
        from app.crawlers.engagement import _parse_xhs_stats

        note_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        parsed_stats = _parse_xhs_stats(text, note_id)
    except Exception:
        parsed_stats = EngagementStats()
    if not any(value is not None for value in parsed_stats.model_dump().values()):
        generic = _stats_from_text(text)
        parsed_stats = EngagementStats(**generic)
    if any(value is not None for value in parsed_stats.model_dump().values()):
        return parsed_stats, comments, parsed_stats.comments, "note SSR"
    return stats, comments, stats.comments, ""


def _parse_haokan(url: str, payload: Any, stats: EngagementStats, comments: list[EngagementComment]):
    if "comment/get" not in url or not isinstance(payload, dict):
        return stats, comments, stats.comments, ""
    data = payload.get("data") or {}
    parsed = _walk_comments(data.get("list") or data.get("comments") or [])
    total = _number(data.get("comment_count"))
    return EngagementStats(comments=total), parsed, total, "haokan/comment/get"


def _parse_wechat(url: str, text: str, payload: Any, stats: EngagementStats, comments: list[EngagementComment]):
    if "appmsg_comment" in url and isinstance(payload, dict):
        parsed = _walk_comments(payload)
        return stats, parsed, _number(_find_key(payload, {"total", "total_count"})), "mp/appmsg_comment"
    values = _stats_from_text(text)
    return (EngagementStats(**values), comments, values.get("comments"), "mp article SSR") if any(value is not None for value in values.values()) else (stats, comments, stats.comments, "")


def _parse_kuaishou(url: str, payload: Any, work_id: str, stats: EngagementStats, comments: list[EngagementComment]):
    if not isinstance(payload, dict):
        return stats, comments, stats.comments, ""
    if "visionCommentList" in url or "comment" in url.lower():
        parsed = _walk_comments(payload)
        return stats, parsed, _number(_find_key(payload, {"commentCount", "comment_count", "total"})), "graphql comments"
    if "visionShortVideoReco" in url or "visionVideoDetail" in url:
        if work_id not in json.dumps(payload, ensure_ascii=False):
            return stats, comments, stats.comments, ""
        values = _stats_from_mapping(payload)
        return EngagementStats(**values), comments, values.get("comments"), "graphql detail"
    return stats, comments, stats.comments, ""


def _parse_bilibili(url: str, payload: Any, stats: EngagementStats, comments: list[EngagementComment]):
    if not isinstance(payload, dict):
        return stats, comments, stats.comments, ""
    if "reply" in url and isinstance(payload.get("data"), dict):
        parsed = _walk_comments(payload.get("data", {}).get("replies") or [])
        return stats, parsed, _number(_find_key(payload, {"all_count", "total"})), "bilibili replies"
    data = payload.get("data") or {}
    stat = data.get("stat") if isinstance(data, dict) else None
    if isinstance(stat, dict):
        return EngagementStats(views=_number(stat.get("view")), likes=_number(stat.get("like")), comments=_number(stat.get("reply")), shares=_number(stat.get("share")), favorites=_number(stat.get("favorite")), coins=_number(stat.get("coin")), danmaku=_number(stat.get("danmaku"))), comments, _number(stat.get("reply")), "bilibili view"
    return stats, comments, stats.comments, ""


def _parse_weibo(url: str, payload: Any, stats: EngagementStats, comments: list[EngagementComment]):
    if not isinstance(payload, dict):
        return stats, comments, stats.comments, ""
    data = payload.get("data") or {}
    if "comments" in url and isinstance(data, dict):
        return stats, _walk_comments(data.get("data") or data), _number(data.get("total_number")), "weibo comments"
    if isinstance(data, dict) and any(key in data for key in ("attitudes_count", "comments_count", "reposts_count")):
        return EngagementStats(likes=_number(data.get("attitudes_count")), comments=_number(data.get("comments_count")), reposts=_number(data.get("reposts_count"))), comments, _number(data.get("comments_count")), "weibo status"
    return stats, comments, stats.comments, ""


def _find_key(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys:
                return child
            found = _find_key(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, keys)
            if found is not None:
                return found
    return None


def _find_key_mapping(value: Any, keys: set[str]) -> dict[str, int | None]:
    found = {key: _number(_find_key(value, {key})) for key in keys}
    return {
        "views": found.get("viewCount") or found.get("playCount"),
        "likes": found.get("likeCount") or found.get("like_count"),
        "comments": found.get("commentCount") or found.get("comment_count"),
        "shares": found.get("shareCount") or found.get("share_count"),
    }


def _stats_from_mapping(value: Any) -> dict[str, int | None]:
    return _find_key_mapping(value, {"viewCount", "playCount", "likeCount", "like_count", "commentCount", "comment_count", "shareCount", "share_count"})


def _challenge_reason(body: str) -> str:
    lowered = body.casefold()
    for marker, reason in (
        ("need captcha", "页面要求验证码，浏览器兜底未自动完成验证码"),
        ("验证码", "页面要求验证码，浏览器兜底未自动完成验证码"),
        ("安全验证", "页面进入安全验证挑战，未获得目标数据"),
        ("登录后", "页面要求登录后才能读取目标数据"),
    ):
        if marker in lowered:
            return reason
    return ""
