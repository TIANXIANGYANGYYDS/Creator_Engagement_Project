"""HTTP client for an authorized desktop WeChat Channels sidecar."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError


class HttpWeChatChannelsBridgeClient:
    """Read public-video comments through a connected WeChat client page."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        timeout_seconds: float = 70,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("WECHAT_CHANNELS_BRIDGE_URL must use http or https")
        self.token = token.strip()
        parsed = urlparse(self.base_url)
        is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not is_loopback:
            raise ValueError("远程视频号会话桥必须使用 HTTPS；HTTP 仅允许回环地址")
        if not is_loopback and not self.token:
            raise ValueError("远程视频号会话桥必须配置鉴权 token")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=max(70, timeout_seconds),
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def fetch_comments(
        self,
        url: str,
        *,
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        profile = await self._get(
            "/api/channels/feed/profile",
            params={"url": url},
        )
        object_id, nonce_id = _profile_identity(profile)
        if not object_id or not nonce_id:
            title, author = _profile_title_author(profile)
            if not title:
                raise PlatformCrawlerError(
                    "视频号会话桥未从分享 URL 恢复 object_id/nonce_id"
                )
            search = await self._get(
                "/api/channels/feed/search",
                params={"keyword": title[:64]},
            )
            object_id, nonce_id = _matching_search_identity(
                search,
                title=title,
                author=author,
            )
        if not object_id or not nonce_id:
            raise PlatformCrawlerError(
                "视频号会话桥无法把分享 URL 唯一映射到 object_id/nonce_id"
            )

        marker = ""
        total: int | None = None
        for current_page in range(1, page + 1):
            payload = await self._get(
                "/api/channels/feed/comment/list",
                params={
                    # wx_channel uses object_id/nonce_id; the older compatible
                    # implementation uses oid/nid. Sending both is harmless.
                    "object_id": object_id,
                    "nonce_id": nonce_id,
                    "oid": object_id,
                    "nid": nonce_id,
                    "next_marker": marker,
                },
            )
            comments, current_total, next_marker = _comment_page(payload)
            if current_total is not None:
                total = current_total
            if current_page == page:
                return {
                    "comments": comments[:limit],
                    "total_comments": total,
                    "next_marker": next_marker,
                    "exhausted": not bool(next_marker),
                    "object_id": object_id,
                    "source": "wx_channel/finderGetCommentList",
                }
            if not next_marker:
                return {
                    "comments": [],
                    "total_comments": total,
                    "next_marker": "",
                    "exhausted": True,
                    "object_id": object_id,
                    "source": "wx_channel/finderGetCommentList",
                }
            marker = next_marker

        raise AssertionError("unreachable comment page loop")

    async def _get(self, path: str, *, params: dict[str, str]) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Local-Auth"] = self.token
        try:
            response = await self.client.get(
                f"{self.base_url}{path}",
                params=params,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise PlatformCrawlerError("视频号会话桥连接失败") from exc
        if response.status_code in {401, 403}:
            raise PlatformBlockedError("视频号会话桥鉴权失败")
        if response.status_code == 503:
            raise PlatformCrawlerError("视频号会话桥没有可用的微信页面")
        if response.status_code < 200 or response.status_code >= 300:
            raise PlatformCrawlerError(
                f"视频号会话桥返回 HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PlatformCrawlerError("视频号会话桥返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformCrawlerError("视频号会话桥响应结构错误")
        code = _to_int(payload.get("code"))
        if code not in {None, 0}:
            message = payload.get("message") or payload.get("msg") or code
            raise PlatformCrawlerError(f"视频号会话桥业务失败: {message}")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise PlatformCrawlerError("视频号会话桥响应缺少 data")
        err_code = _to_int(data.get("errCode"))
        if err_code not in {None, 0}:
            raise PlatformCrawlerError(
                f"视频号客户端接口失败: {data.get('errMsg') or err_code}"
            )
        return data


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _profile_identity(payload: dict[str, Any]) -> tuple[str, str]:
    for item in _walk_dicts(payload):
        object_id = str(item.get("id") or item.get("objectId") or "").strip()
        nonce_id = str(
            item.get("objectNonceId") or item.get("nonceId") or ""
        ).strip()
        if object_id and nonce_id:
            return object_id, nonce_id
    return "", ""


def _profile_title_author(payload: dict[str, Any]) -> tuple[str, str]:
    title = ""
    author = ""
    for item in _walk_dicts(payload):
        if not title and item.get("description"):
            title = str(item["description"]).strip()
        if not author and item.get("nickname"):
            author = str(item["nickname"]).strip()
        if title and author:
            break
    return title, author


def _matching_search_identity(
    payload: dict[str, Any],
    *,
    title: str,
    author: str,
) -> tuple[str, str]:
    normalized_title = _normalize_text(title)
    normalized_author = _normalize_text(author)
    matches: list[tuple[str, str]] = []
    for item in _walk_dicts(payload):
        object_id = str(item.get("id") or item.get("objectId") or "").strip()
        nonce_id = str(
            item.get("objectNonceId") or item.get("nonceId") or ""
        ).strip()
        if not object_id or not nonce_id:
            continue
        candidate_title, candidate_author = _profile_title_author(item)
        if _normalize_text(candidate_title) != normalized_title:
            continue
        if normalized_author and _normalize_text(candidate_author) != normalized_author:
            continue
        identity = (object_id, nonce_id)
        if identity not in matches:
            matches.append(identity)
    return matches[0] if len(matches) == 1 else ("", "")


def _comment_page(
    payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], int | None, str]:
    for item in _walk_dicts(payload):
        comments = item.get("commentInfo")
        if not isinstance(comments, list):
            continue
        count_info = item.get("countInfo") or {}
        total = (
            _to_int(count_info.get("commentCount"))
            if isinstance(count_info, dict)
            else None
        )
        return (
            [comment for comment in comments if isinstance(comment, dict)],
            total,
            str(item.get("lastBuffer") or "").strip(),
        )
    raise PlatformCrawlerError("视频号会话桥响应缺少 commentInfo")


def _normalize_text(value: str) -> str:
    return "".join(str(value or "").split()).casefold()


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
