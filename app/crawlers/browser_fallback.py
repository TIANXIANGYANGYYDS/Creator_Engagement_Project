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
    max_concurrency: int = 2
    geoip: bool = False
    profile_dir: Path = Path(".local/browser-profiles")
    reset_guest_state_on_proxy_change: bool = False


class BrowserFallback:
    """Use one persistent browser profile per platform and request."""

    def __init__(
        self,
        *,
        settings: BrowserFallbackSettings | None = None,
        proxy_provider: Any | None = None,
        session_store: Any | None = None,
        cookies: str = "",
    ) -> None:
        self.settings = settings or BrowserFallbackSettings()
        if self.settings.max_concurrency <= 0:
            raise ValueError("browser max_concurrency must be greater than zero")
        self.proxy_provider = proxy_provider
        self.session_store = session_store
        self.cookies = cookies
        self._profile_slots: dict[EngagementPlatform, asyncio.Queue[int]] = {}
        self._session_locks: dict[EngagementPlatform, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrency)
        self._proxy_identities: dict[tuple[EngagementPlatform, int], str] = {}

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
        slots = self._profile_slots.get(platform)
        if slots is None:
            slots = asyncio.Queue()
            for slot in range(self.settings.max_concurrency):
                slots.put_nowait(slot)
            self._profile_slots[platform] = slots
        async with self._semaphore:
            profile_slot = await slots.get()
            try:
                return await self._fetch_locked(
                    url,
                    platform,
                    work_id,
                    page=page,
                    limit=limit,
                    include_stats=include_stats,
                    include_comments=include_comments,
                    profile_slot=profile_slot,
                )
            finally:
                slots.put_nowait(profile_slot)

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
        profile_slot: int = 0,
    ) -> EngagementResult:
        proxy_mapping = None
        lease_ok = False
        try:
            if self.proxy_provider is not None and _browser_uses_proxy(platform):
                proxy_mapping = await self.proxy_provider.get_requests_proxies()
            proxy = _playwright_proxy(proxy_mapping)
            profile_dir = self.settings.profile_dir / platform
            if profile_slot:
                profile_dir = self.settings.profile_dir / f"{platform}-worker-{profile_slot}"
            profile_dir.mkdir(parents=True, exist_ok=True)
            target_url = _browser_target_url(url, platform, work_id)

            from camoufox.async_api import AsyncCamoufox
            from camoufox.addons import DefaultAddons

            async with AsyncCamoufox(
                headless=self.settings.headless,
                os="windows",
                locale="zh-CN",
                humanize=True,
                block_webrtc=True,
                proxy=proxy,
                geoip=bool(proxy) and self.settings.geoip,
                persistent_context=True,
                user_data_dir=str(profile_dir),
                exclude_addons=[DefaultAddons.UBO],
                enable_cache=True,
            ) as context:
                await self._prepare_proxy_bound_guest_state(
                    context,
                    platform,
                    proxy_mapping,
                    profile_slot,
                )
                await self._seed_cookies(context, target_url, platform)
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
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=int(self.settings.timeout_seconds * 1000),
                    )
                except Exception:
                    # A challenge page can keep navigation open; already captured
                    # responses are still useful and are parsed below.
                    pass
                await self._interact_with_page(
                    page_obj,
                    platform,
                    page,
                    include_comments=include_comments,
                )
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
                if _should_reload_page(platform, result):
                    responses.clear()
                    try:
                        await page_obj.reload(
                            wait_until="domcontentloaded",
                            timeout=int(self.settings.timeout_seconds * 1000),
                        )
                    except Exception:
                        pass
                    await self._interact_with_page(
                        page_obj,
                        platform,
                        page,
                        include_comments=include_comments,
                    )
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
                await self._persist_session(platform, context)
            stats_ok = not include_stats or any(
                value is not None for value in result.stats.model_dump().values()
            )
            comments_ok = (
                not include_comments
                or bool(result.comments)
                or result.stats.comments == 0
            )
            lease_ok = (
                result.coverage in {"complete", "partial"}
                and stats_ok
                and comments_ok
            )
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

    async def _persist_session(self, platform: EngagementPlatform, context: Any) -> None:
        """Persist guest/login state without making collection depend on it."""
        if self.session_store is None:
            return
        try:
            lock = self._session_locks.setdefault(platform, asyncio.Lock())
            async with lock:
                await self.session_store.save_context(platform, context)
        except Exception:
            # A read-only profile or interrupted write must not discard an
            # otherwise valid collection result.
            pass

    async def _prepare_proxy_bound_guest_state(
        self,
        context: Any,
        platform: EngagementPlatform,
        proxy_mapping: dict[str, str] | None,
        profile_slot: int = 0,
    ) -> None:
        """Reset an explicitly disposable Kuaishou guest profile after IP rotation."""
        if (
            not self.settings.reset_guest_state_on_proxy_change
            or platform != "kuaishou"
            or not proxy_mapping
        ):
            return
        identity = proxy_mapping.get("https") or proxy_mapping.get("http") or ""
        identity_key = (platform, profile_slot)
        if not identity or self._proxy_identities.get(identity_key) == identity:
            return
        await context.clear_cookies()
        await context.add_init_script(
            script="""
            if (location.hostname === 'kuaishou.com' || location.hostname.endsWith('.kuaishou.com')) {
              try { localStorage.clear(); } catch (_) {}
              try { sessionStorage.clear(); } catch (_) {}
            }
            """
        )
        self._proxy_identities[identity_key] = identity

    async def _seed_cookies(self, context: Any, url: str, platform: EngagementPlatform) -> None:
        # The compatibility setting is a Douyin session cookie.  Reusing the
        # same name/value pairs on unrelated domains can poison their guest
        # device sessions and trigger a challenge.
        if platform != "douyin" or not self.cookies.strip():
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

    async def _interact_with_page(
        self,
        page: Any,
        platform: EngagementPlatform,
        requested_page: int,
        *,
        include_comments: bool = True,
    ) -> None:
        if not include_comments:
            return
        # Open the comment panel before scrolling.  Several desktop pages keep
        # comments in an inner modal, so scrolling only ``window`` never emits
        # the next-page request.
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
        if platform in {"douyin", "xiaohongshu", "kuaishou", "wechat", "bilibili", "weibo"}:
            for _ in range(min(max(requested_page, 1) + 2, 8)):
                try:
                    await page.evaluate(
                        """
                        () => {
                          window.scrollTo(0, document.body.scrollHeight);
                          const candidates = Array.from(document.querySelectorAll('*'))
                            .filter((element) => {
                              const style = getComputedStyle(element);
                              return /(auto|scroll)/.test(style.overflowY)
                                && element.scrollHeight > element.clientHeight + 40;
                            })
                            .sort((left, right) =>
                              (right.scrollHeight - right.clientHeight)
                              - (left.scrollHeight - left.clientHeight)
                            )
                            .slice(0, 3);
                          for (const element of candidates) {
                            element.scrollTop = element.scrollHeight;
                          }
                        }
                        """
                    )
                    await page.wait_for_timeout(700)
                except Exception:
                    break

    async def _kuaishou_guest_payload(
        self,
        page_obj: Any,
        work_id: str,
        requested_page: int,
        include_comments: bool,
    ) -> dict[str, Any]:
        """Read the target cache and comments through Kuaishou's guest page session."""
        value = await page_obj.evaluate(
            """
            async ({workId, requestedPage, includeComments}) => {
              const apolloState = globalThis.__APOLLO_STATE__ || {};
              const cachedPhoto = apolloState[`VisionVideoDetailPhoto:${workId}`] || null;
              const apolloPhoto = cachedPhoto ? {
                id: cachedPhoto.id,
                viewCount: cachedPhoto.viewCount,
                likeCount: cachedPhoto.likeCount,
                realLikeCount: cachedPhoto.realLikeCount,
                commentCount: cachedPhoto.commentCount,
                shareCount: cachedPhoto.shareCount,
              } : null;
              const query = `query visionVideoDetail($photoId: String) {
                visionVideoDetail(photoId: $photoId) {
                  status
                  photo { id viewCount likeCount realLikeCount }
                }
              }`;
              let detailPayload = null;
              try {
                const detailResponse = await fetch("/graphql", {
                  method: "POST",
                  credentials: "include",
                  headers: {"content-type": "application/json"},
                  body: JSON.stringify({
                    operationName: "visionVideoDetail",
                    variables: {photoId: workId},
                    query,
                  }),
                });
                detailPayload = await detailResponse.json();
              } catch (_) {}
              let cursor = "";
              const seenCommentIds = new Set();
              const targetPage = includeComments ? requestedPage : 1;
              for (let currentPage = 1; currentPage <= targetPage; currentPage += 1) {
                const response = await fetch("/rest/v/photo/comment/list", {
                  method: "POST",
                  credentials: "include",
                  headers: {"content-type": "application/json"},
                  body: JSON.stringify({photoId: workId, pcursor: cursor}),
                });
                const commentPage = await response.json();
                if (!response.ok || commentPage.result !== 1) {
                  return {apolloPhoto, detailPayload, commentPage, reached: false};
                }
                if (currentPage === targetPage) {
                  if (Array.isArray(commentPage.rootCommentsV2)) {
                    commentPage.rootCommentsV2 = commentPage.rootCommentsV2.filter(
                      (comment) => !seenCommentIds.has(String(comment.commentId || ""))
                    );
                  }
                  return {apolloPhoto, detailPayload, commentPage, reached: true};
                }
                for (const comment of commentPage.rootCommentsV2 || []) {
                  seenCommentIds.add(String(comment.commentId || ""));
                }
                const next = commentPage.pcursorV2 || commentPage.pcursor || "";
                if (!next || next === "0" || next === "no_more") {
                  return {apolloPhoto, detailPayload, commentPage: null, reached: false};
                }
                cursor = String(next);
              }
              return {apolloPhoto, detailPayload, commentPage: null, reached: false};
            }
            """,
            {
                "workId": work_id,
                "requestedPage": requested_page,
                "includeComments": include_comments,
            },
        )
        return value if isinstance(value, dict) else {}

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
        next_available: bool | None = None
        body_text = ""
        try:
            body_text = await page_obj.locator("body").inner_text(timeout=1000)
        except Exception:
            pass

        if platform == "kuaishou":
            try:
                guest = await self._kuaishou_guest_payload(
                    page_obj,
                    work_id,
                    page,
                    include_comments,
                )
                guest_stats, guest_comments, guest_total, guest_next, source = _parse_kuaishou_guest(
                    guest,
                    work_id,
                )
                if any(value is not None for value in guest_stats.model_dump().values()):
                    stats = guest_stats
                if guest_total is not None:
                    stats.comments = guest_total
                if include_comments and guest.get("reached"):
                    comments = guest_comments
                    total_comments = guest_total
                    if guest_total is not None:
                        stats.comments = guest_total
                    next_available = guest_next
                if source:
                    sources.append(source)
            except Exception:
                # Captured network responses below remain a valid fallback.
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
                stats, comments, total_comments, source = _parse_toutiao(
                    response_url,
                    text,
                    payload,
                    work_id,
                    stats,
                    comments,
                )
            elif platform == "xiaohongshu":
                stats, comments, total_comments, source = _parse_xhs(response_url, text, payload, stats, comments)
                if "comment/page" in response_url and isinstance(payload, dict):
                    has_more = _find_key(payload, {"has_more", "hasMore"})
                    if has_more is not None:
                        next_available = bool(has_more)
            elif platform == "haokan":
                stats, comments, total_comments, source = _parse_haokan(response_url, payload, stats, comments)
            elif platform == "wechat":
                stats, comments, total_comments, source = _parse_wechat(response_url, text, payload, stats, comments)
            elif platform == "kuaishou":
                stats, comments, total_comments, source = _parse_kuaishou(response_url, payload, work_id, stats, comments)
            elif platform == "bilibili":
                stats, comments, total_comments, source = _parse_bilibili(
                    response_url,
                    text,
                    payload,
                    stats,
                    comments,
                )
            elif platform == "weibo":
                stats, comments, total_comments, source = _parse_weibo(response_url, payload, stats, comments)
            else:
                source = ""
            if source and source not in sources:
                sources.append(source)

        comments = comments[:limit]
        xhs_guest_gate = platform == "xiaohongshu" and "登录查看全部评论" in body_text
        xhs_guest_page_blocked = xhs_guest_gate and page > 1
        if xhs_guest_gate:
            next_available = False
        if xhs_guest_page_blocked:
            comments = []
        useful_stats = any(value is not None for value in stats.model_dump().values())
        useful_comments = bool(comments)
        challenge = _challenge_reason(body_text)
        if xhs_guest_page_blocked:
            coverage = "unsupported"
            reason = "小红书游客态仅开放首屏评论；页码大于 1 需要调用方自己的有效平台会话"
        elif not useful_stats and not useful_comments:
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
            next_cursor=(
                str(page + 1)
                if include_comments and useful_comments and next_available is not False
                else None
            ),
        )


def _playwright_proxy(proxies: dict[str, str] | None) -> dict[str, str] | None:
    if not proxies:
        return None
    server = proxies.get("https") or proxies.get("http")
    return {"server": server} if server else None


def _browser_uses_proxy(platform: EngagementPlatform) -> bool:
    # XHS guest comment signatures are tied to the browser-generated session
    # and egress. Rotating purchased proxies returned counters but no bodies in
    # production, while the same anonymous profile succeeded over direct egress.
    return platform != "xiaohongshu"


def _should_reload_page(
    platform: EngagementPlatform,
    result: EngagementResult,
) -> bool:
    """Retry deferred pages, except Kuaishou where an explicit guest API already ran."""
    if platform == "kuaishou":
        return False
    return (
        result.coverage in {"unsupported", "blocked"}
        and not result.comments
        and not any(value is not None for value in result.stats.model_dump().values())
    )


def _browser_target_url(
    url: str,
    platform: EngagementPlatform,
    work_id: str,
) -> str:
    """Use the platform's full content page for share and desktop URL variants."""

    if platform == "kuaishou":
        return f"https://www.kuaishou.com/short-video/{work_id}"
    if platform == "toutiao":
        return f"https://www.toutiao.com/article/{work_id}/"
    if platform == "weibo":
        return f"https://m.weibo.cn/detail/{work_id}"
    if platform == "xiaohongshu" and "xsec_token=" in url and "xsec_source=" not in url:
        parsed = urlparse(url)
        query = f"{parsed.query}&xsec_source=pc_feed"
        return parsed._replace(query=query).geturl()
    return url


def _number(value: Any) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace(",", "")
    multiplier = 1
    if text.endswith("万"):
        text = text[:-1]
        multiplier = 10_000
    elif text.endswith("亿"):
        text = text[:-1]
        multiplier = 100_000_000
    try:
        return max(0, int(float(text) * multiplier))
    except (TypeError, ValueError):
        return None


def _first_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


def _comment(item: dict[str, Any], *, default_id: str = "") -> EngagementComment | None:
    comment_id = str(
        item.get("cid")
        or item.get("id")
        or item.get("comment_id")
        or item.get("commentId")
        or default_id
    )
    text = str(item.get("text") or item.get("content") or item.get("message") or "")
    user = item.get("user") or item.get("user_info") or item.get("author") or {}
    if not isinstance(user, dict):
        user = {}
    author = str(
        user.get("nickname")
        or user.get("nick_name")
        or user.get("name")
        or user.get("uname")
        or item.get("user_name")
        or item.get("authorName")
        or item.get("author_name")
        or ""
    )
    if not comment_id or not text:
        return None
    created = _first_value(item, "create_time", "created_at", "timestamp", "time")
    created_at = None
    if created:
        try:
            timestamp = float(created)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            created_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return EngagementComment(
        comment_id=comment_id,
        author=author,
        text=text,
        created_at=created_at,
        likes=_number(_first_value(item, "digg_count", "like_count", "likedCount", "like")),
        replies=_number(
            _first_value(
                item,
                "reply_count",
                "reply_comment_total",
                "subCommentCount",
                "commentCount",
                "rcount",
            )
        ),
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
        from app.crawlers.platforms.douyin import public_view_count

        detail = payload.get("aweme_detail") or {}
        if str(detail.get("aweme_id") or work_id) == work_id:
            values = detail.get("statistics") or {}
            stats = EngagementStats(
                views=public_view_count(values), likes=_number(values.get("digg_count")),
                comments=_number(values.get("comment_count")), shares=_number(values.get("share_count")),
                favorites=_number(values.get("collect_count")),
            )
            return stats, comments, stats.comments, "aweme/detail"
    if "/aweme/v1/web/comment/list/" in url and isinstance(payload, dict):
        parsed = [_comment(item) for item in payload.get("comments") or []]
        return stats, [x for x in parsed if x], _number(payload.get("total")), "comment/list"
    return stats, comments, stats.comments, ""


def _parse_toutiao(
    url: str,
    text: str,
    payload: Any,
    work_id: str,
    stats: EngagementStats,
    comments: list[EngagementComment],
):
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
    if f"/article/{work_id}" not in url and f"/video/{work_id}" not in url:
        return stats, comments, stats.comments, ""
    try:
        from app.crawlers.platforms.toutiao import parse_stats

        parsed_stats = parse_stats(text)
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
        from app.crawlers.platforms.xiaohongshu import parse_stats

        note_id = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
        parsed_stats = parse_stats(text, note_id)
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
        return stats, parsed, _number(_find_key(payload, {"commentCount", "comment_count", "total"})), "Kuaishou comments"
    if "visionShortVideoReco" in url or "visionVideoDetail" in url:
        if work_id not in json.dumps(payload, ensure_ascii=False):
            return stats, comments, stats.comments, ""
        values = _stats_from_mapping(payload)
        return EngagementStats(**values), comments, values.get("comments"), "graphql detail"
    return stats, comments, stats.comments, ""


def _parse_kuaishou_guest(
    payload: dict[str, Any],
    work_id: str,
) -> tuple[EngagementStats, list[EngagementComment], int | None, bool | None, str]:
    apollo = payload.get("apolloState") or {}
    photo_value = payload.get("apolloPhoto")
    photo: dict[str, Any] = photo_value if isinstance(photo_value, dict) else {}
    if isinstance(apollo, dict):
        exact = apollo.get(f"VisionVideoDetailPhoto:{work_id}")
        if isinstance(exact, dict):
            photo = exact
        else:
            for key, value in apollo.items():
                if (
                    str(key).startswith("VisionVideoDetailPhoto:")
                    and isinstance(value, dict)
                    and str(value.get("id") or "") == work_id
                ):
                    photo = value
                    break

    detail_payload = payload.get("detailPayload") or {}
    if isinstance(detail_payload, dict):
        detail = (detail_payload.get("data") or {}).get("visionVideoDetail") or {}
        candidate = detail.get("photo") or {}
        if isinstance(candidate, dict) and str(candidate.get("id") or "") == work_id:
            photo = candidate

    stats = EngagementStats()
    if str(photo.get("id") or "") == work_id:
        stats = EngagementStats(
            views=_number(photo.get("viewCount")),
            likes=_number(_first_value(photo, "realLikeCount", "likeCount")),
            comments=_number(photo.get("commentCount")),
            shares=_number(photo.get("shareCount")),
        )

    comment_page = payload.get("commentPage") or {}
    comments: list[EngagementComment] = []
    total = stats.comments
    next_available: bool | None = None
    if isinstance(comment_page, dict) and comment_page.get("result") == 1:
        comments = _walk_comments(comment_page.get("rootCommentsV2") or [])
        total = _number(_first_value(comment_page, "commentCountV2", "commentCount"))
        cursor = _first_value(comment_page, "pcursorV2", "pcursor")
        next_available = cursor not in {None, "", "0", "no_more"}
        if total is not None:
            stats.comments = total
    source = "guest GraphQL detail + REST comments" if photo or comments or total is not None else ""
    return stats, comments, total, next_available, source


def _parse_bilibili(
    url: str,
    text: str,
    payload: Any,
    stats: EngagementStats,
    comments: list[EngagementComment],
):
    if "window.__INITIAL_STATE__" in text:
        try:
            from app.crawlers.platforms.bilibili import (
                parse_opus_initial_state,
                parse_opus_stats,
            )

            detail = parse_opus_initial_state(text).get("detail") or {}
            parsed_stats = parse_opus_stats(detail)
            if any(value is not None for value in parsed_stats.model_dump().values()):
                return (
                    parsed_stats,
                    comments,
                    parsed_stats.comments,
                    "bilibili opus SSR",
                )
        except Exception:
            pass
    if not isinstance(payload, dict):
        return stats, comments, stats.comments, ""
    if "reply" in url and isinstance(payload.get("data"), dict):
        parsed = _walk_comments(payload.get("data", {}).get("replies") or [])
        return stats, parsed, _number(_find_key(payload, {"all_count", "total"})), "bilibili replies"
    data = payload.get("data") or {}
    stat = (
        data.get("stat") or data.get("stats")
        if isinstance(data, dict)
        else None
    )
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
