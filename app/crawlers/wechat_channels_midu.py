"""No-account WeChat Channels counters from the configured Midu service."""

from __future__ import annotations

from datetime import datetime, timedelta
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import first_present, to_int
from app.crawlers.platforms.registry import extract_wechat_channels_mobile_feed_id


MAX_BATCH_SIZE = 300
LOOKBACK_DAYS = 30
CACHE_TTL_SECONDS = 120
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


class HttpWeChatChannelsMiduClient:
    """Resolve client-jump URLs and read their latest indexed counters."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 70,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("WECHAT_CHANNELS_MIDU_URL must use http or https")
        is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not is_loopback:
            raise ValueError("远程蜜度服务必须使用 HTTPS；HTTP 仅允许回环地址")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=max(70, timeout_seconds),
            trust_env=False,
            verify=not (parsed.scheme == "https" and is_loopback),
        )
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._missing_until: dict[str, float] = {}
        self._failure_cache: dict[str, tuple[float, str]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def fetch_interactions(self, url: str) -> dict[str, Any]:
        cached = self._cached(url)
        if cached is not None:
            return cached
        failure = self._cached_failure(url)
        if failure:
            raise PlatformCrawlerError(failure)
        if self._known_missing(url):
            raise PlatformCrawlerError("蜜度近 30 天未收录该视频号链接")
        results = await self.fetch_many([url])
        result = results.get(url)
        if result is None:
            raise PlatformCrawlerError("蜜度近 30 天未收录该视频号链接")
        return result

    async def fetch_many(self, urls: list[str]) -> dict[str, dict[str, Any]]:
        unique_urls = list(dict.fromkeys(url for url in urls if url.strip()))
        results: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for url in unique_urls:
            cached = self._cached(url)
            if cached is not None:
                results[url] = cached
            elif not self._known_missing(url) and not self._cached_failure(url):
                missing.append(url)

        for offset in range(0, len(missing), MAX_BATCH_SIZE):
            batch_urls = missing[offset : offset + MAX_BATCH_SIZE]
            try:
                batch_results = await self._fetch_batch(batch_urls)
            except (PlatformBlockedError, PlatformCrawlerError) as exc:
                expires_at = monotonic() + CACHE_TTL_SECONDS
                for url in batch_urls:
                    self._failure_cache[url] = (expires_at, str(exc))
                raise
            expires_at = monotonic() + CACHE_TTL_SECONDS
            for url, result in batch_results.items():
                self._missing_until.pop(url, None)
                self._failure_cache.pop(url, None)
                self._cache[url] = (expires_at, result)
                results[url] = dict(result)
            for url in batch_urls:
                if url not in batch_results:
                    self._missing_until[url] = expires_at
        return results

    def _cached(self, url: str) -> dict[str, Any] | None:
        cached = self._cache.get(url)
        if cached is None:
            return None
        if cached[0] <= monotonic():
            self._cache.pop(url, None)
            return None
        return dict(cached[1])

    def _known_missing(self, url: str) -> bool:
        expires_at = self._missing_until.get(url)
        if expires_at is None:
            return False
        if expires_at <= monotonic():
            self._missing_until.pop(url, None)
            return False
        return True

    def _cached_failure(self, url: str) -> str:
        cached = self._failure_cache.get(url)
        if cached is None:
            return ""
        if cached[0] <= monotonic():
            self._failure_cache.pop(url, None)
            return ""
        return cached[1]

    async def _fetch_batch(self, urls: list[str]) -> dict[str, dict[str, Any]]:
        if not urls:
            return {}
        now = datetime.now(BEIJING_TZ)
        start = (now - timedelta(days=LOOKBACK_DAYS)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        history_payload = await self._post(
            "/history_data/get_md_history_data",
            {
                "monitorStartTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "monitorEndTime": now.strftime("%Y-%m-%d %H:%M:%S"),
                "schemeType": 3,
                "contentWebpageUrlList": urls,
                "captureWebsiteNameList": ["微信视频号"],
                "weiboHandleTypeList": [1],
                "useNativeParams": True,
                "params": {"videoUnionContentSwitch": 2},
                "presentResult": "1",
            },
            success_codes={0},
        )
        history_items = history_payload.get("data") or []
        if not isinstance(history_items, list):
            raise PlatformCrawlerError("蜜度 URL 查询响应缺少 data")

        history_by_id: dict[str, dict[str, Any]] = {}
        requested_by_feed_id = {
            extract_wechat_channels_mobile_feed_id(url): url for url in urls
        }
        requested_by_url = {url: url for url in urls}
        resolved_url_by_id: dict[str, str] = {}
        for item in history_items:
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("url") or "").strip()
            feed_id = extract_wechat_channels_mobile_feed_id(source_url)
            requested_url = requested_by_feed_id.get(feed_id) or requested_by_url.get(
                source_url
            )
            source_id = str(item.get("origin_id") or "").strip()
            if not requested_url or not source_id:
                continue
            history_by_id[source_id] = item
            resolved_url_by_id[source_id] = requested_url

        if not history_by_id:
            return {}
        engagement_payload = await self._post(
            "/idata/md/engagement/query",
            {"schemeType": 4, "idList": list(history_by_id)},
            success_codes={200},
        )
        engagement_items = engagement_payload.get("data") or []
        if not isinstance(engagement_items, list):
            raise PlatformCrawlerError("蜜度互动查询响应缺少 data")

        results: dict[str, dict[str, Any]] = {}
        for item in engagement_items:
            if not isinstance(item, dict):
                continue
            source_id = str(item.get("skId") or "").strip()
            requested_url = resolved_url_by_id.get(source_id)
            if not requested_url:
                continue
            history = history_by_id[source_id]
            content_ext = item.get("skContentExt") or {}
            if not isinstance(content_ext, dict):
                content_ext = {}
            stats = {
                "views": _first_int(
                    content_ext.get("skViews"),
                    history.get("view_count"),
                ),
                "likes": _first_int(
                    content_ext.get("skAttitudesCount"),
                    history.get("like_count"),
                ),
                "comments": _first_int(content_ext.get("skCommentsCount")),
                "shares": _first_int(
                    content_ext.get("skShareCount"),
                    history.get("share_count"),
                ),
                "reposts": _first_int(
                    content_ext.get("skRepostsCount"),
                    history.get("repost_count"),
                ),
            }
            if not any(value is not None for value in stats.values()):
                continue
            results[requested_url] = {
                "stats": stats,
                "source_id": source_id,
                "source": "midu/history_data+idata/md/engagement/query",
            }
        return results

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        success_codes: set[int],
    ) -> dict[str, Any]:
        try:
            response = await self.client.post(
                f"{self.base_url}{path}",
                headers={"Accept": "application/json"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise PlatformCrawlerError("蜜度视频号数据源连接失败") from exc
        if response.status_code in {401, 403}:
            raise PlatformBlockedError("蜜度视频号数据源鉴权失败")
        if response.status_code < 200 or response.status_code >= 300:
            raise PlatformCrawlerError(
                f"蜜度视频号数据源返回 HTTP {response.status_code}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise PlatformCrawlerError("蜜度视频号数据源返回了无效 JSON") from exc
        if not isinstance(body, dict):
            raise PlatformCrawlerError("蜜度视频号数据源响应结构错误")
        code = to_int(body.get("code"))
        if code not in success_codes:
            message = first_present(body, "message", "msg") or code
            raise PlatformCrawlerError(f"蜜度视频号数据源业务失败: {message}")
        return body


def _first_int(*values: Any) -> int | None:
    for value in values:
        parsed = to_int(value)
        if parsed is not None:
            return parsed
    return None
