"""Local WeChat-client session bridge for article metrics and elected comments.

The public API server never receives raw WeChat credentials.  A bridge running
on the user's desktop captures a short-lived article session from the local
WeChat WebView, keeps it in memory, and performs the protocol requests locally.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import re
from secrets import compare_digest
from threading import RLock
from time import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.wechat import (
    find_nested_value,
    parse_comments,
    parse_metadata,
    parse_stats,
    parse_stats_payload,
)
from app.models.engagement import EngagementStats


SESSION_TTL_SECONDS = 25 * 60
MAX_CAPTURE_BODY_CHARS = 6 * 1024 * 1024
MAX_WECHAT_RESPONSE_BYTES = 6 * 1024 * 1024
MAX_COMMENT_ENDPOINT_PAGES = 10
REQUIRED_SESSION_FIELDS = ("uin", "key", "pass_ticket", "appmsg_token", "cookie")


class BridgeCaptureRequest(BaseModel):
    request_url: str = Field(min_length=1, max_length=32_768)
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: str = Field(default="", max_length=1024 * 1024)
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: str = Field(default="", max_length=MAX_CAPTURE_BODY_CHARS)


class BridgeFetchRequest(BaseModel):
    url: str = Field(min_length=1, max_length=32_768)
    metadata: dict[str, str] = Field(default_factory=dict)
    page: int = Field(default=1, ge=1, le=500)
    limit: int = Field(default=20, ge=1, le=100)


@dataclass
class _SessionRecord:
    raw: dict[str, str]
    captured_at: float
    expires_at: float


class WeChatSessionStore:
    """Thread-safe, memory-only store keyed by public-account ``__biz``."""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._records: dict[str, _SessionRecord] = {}
        self._url_biz: dict[str, str] = {}
        self._lock = RLock()

    def capture(self, capture: BridgeCaptureRequest) -> tuple[bool, str]:
        parsed_request = urlparse(capture.request_url)
        if (
            parsed_request.scheme != "https"
            or parsed_request.hostname != "mp.weixin.qq.com"
            or not _allowed_capture_path(parsed_request.path)
        ):
            return False, ""
        request_values = _request_values(capture.request_url, capture.request_body)
        response_metadata = parse_metadata(capture.response_body, capture.request_url)
        biz = str(
            request_values.get("__biz")
            or response_metadata.get("biz")
            or response_metadata.get("bizuin")
            or ""
        ).strip()
        if not biz:
            return False, ""

        values = {
            key: str(request_values.get(key) or response_metadata.get(key) or "").strip()
            for key in ("uin", "key", "pass_ticket", "appmsg_token")
        }
        cookie = _header(capture.request_headers, "cookie")
        if not cookie:
            cookie = _cookies_from_set_cookie(_header(capture.response_headers, "set-cookie"))
        user_agent = _header(capture.request_headers, "user-agent")
        optional = {
            "devicetype": str(request_values.get("devicetype") or "").strip(),
            "clientversion": str(request_values.get("clientversion") or "").strip(),
            "user_agent": user_agent,
        }
        if not any(values.values()) and not cookie:
            return False, biz

        now = time()
        with self._lock:
            self._purge(now)
            previous = self._records.get(biz)
            merged = dict(previous.raw) if previous else {}
            merged.update({key: value for key, value in values.items() if value})
            merged.update({key: value for key, value in optional.items() if value})
            if cookie:
                merged["cookie"] = cookie
            self._records[biz] = _SessionRecord(
                raw=merged,
                captured_at=now,
                expires_at=now + self.ttl_seconds,
            )
            self._url_biz[_clean_article_url(capture.request_url)] = biz
        return True, biz

    def credential(self, biz: str) -> dict[str, str] | None:
        now = time()
        with self._lock:
            self._purge(now)
            record = self._records.get(biz)
            if record is None or not all(record.raw.get(key) for key in REQUIRED_SESSION_FIELDS):
                return None
            return dict(record.raw)

    def biz_for_url(self, url: str) -> str:
        with self._lock:
            self._purge(time())
            return self._url_biz.get(_clean_article_url(url), "")

    def status(self) -> dict[str, Any]:
        now = time()
        with self._lock:
            self._purge(now)
            sessions = [
                {
                    "biz": biz,
                    "status": (
                        "valid"
                        if all(record.raw.get(key) for key in REQUIRED_SESSION_FIELDS)
                        else "partial"
                    ),
                    "available_fields": [
                        key for key in REQUIRED_SESSION_FIELDS if record.raw.get(key)
                    ],
                    "expires_in_seconds": max(0, int(record.expires_at - now)),
                }
                for biz, record in sorted(self._records.items())
            ]
        return {"ok": True, "sessions": sessions}

    def _purge(self, now: float) -> None:
        expired = {
            biz for biz, record in self._records.items() if record.expires_at <= now
        }
        for biz in expired:
            self._records.pop(biz, None)
        if expired:
            self._url_biz = {
                url: biz for url, biz in self._url_biz.items() if biz not in expired
            }


class WeChatSessionBridge:
    """Executes WeChat article requests from the credential-owning desktop."""

    def __init__(
        self,
        *,
        store: WeChatSessionStore | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        self.store = store or WeChatSessionStore()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._stats_observations: dict[tuple[str, str, str], EngagementStats] = {}
        self._comment_observations: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def capture(self, capture: BridgeCaptureRequest) -> dict[str, Any]:
        captured, biz = self.store.capture(capture)
        if captured:
            self._capture_observation(capture, biz)
        status = self.store.status()
        matching = [item for item in status["sessions"] if item["biz"] == biz]
        return {
            "ok": captured,
            "biz": biz,
            "session": matching[0] if matching else None,
        }

    async def interactions(self, request: BridgeFetchRequest) -> dict[str, Any]:
        if not _allowed_article_url(request.url):
            return _bridge_error("invalid_target", "只允许 mp.weixin.qq.com 公开文章 URL")
        metadata, biz = self._resolve_metadata(request)
        if not biz:
            return _bridge_error("missing_article_context", "文章未暴露 __biz")
        key = _article_key(biz, metadata)
        observed = self._stats_observations.get(key)
        stats = observed or EngagementStats()
        sources: list[str] = ["wechat_client_observed_response"] if observed else []

        credential = self.store.credential(biz)
        if credential is None:
            if _has_stats(stats):
                return {
                    "ok": True,
                    "source": "+".join(sources),
                    "stats": stats.model_dump(),
                }
            return _bridge_error("waiting_session", "请在本地微信中打开该公众号任意文章")
        article_session_url = _with_session_query(request.url, biz, credential)
        headers = _wechat_headers(credential, article_session_url)

        if not _has_stats(stats):
            article_response = await self._get_article_response(
                article_session_url,
                biz,
                credential,
                headers,
            )
            stats = parse_stats(article_response.text)
            if _has_stats(stats):
                sources.append("credentialized_article_html")

        if stats.views is None or stats.likes is None:
            ext_response = await self.client.post(
                "https://mp.weixin.qq.com/mp/getappmsgext",
                params={
                    "__biz": biz,
                    "mid": metadata.get("mid", ""),
                    "idx": metadata.get("idx", "1"),
                    "sn": metadata.get("sn", ""),
                    "scene": "0",
                    "appmsg_token": credential["appmsg_token"],
                },
                headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                data={
                    "r": "0",
                    "__biz": biz,
                    "appmsg_type": "9",
                    "mid": metadata.get("mid", ""),
                    "sn": metadata.get("sn", ""),
                    "idx": metadata.get("idx", "1"),
                    "scene": "0",
                    "comment_id": metadata.get("comment_id", ""),
                    "is_need_ad": "0",
                    "is_only_read": "0",
                    "appmsg_token": credential["appmsg_token"],
                },
            )
            _raise_wechat_response(ext_response)
            payload = _response_json(ext_response)
            _raise_wechat_payload(payload)
            ext_stats = parse_stats_payload(payload)
            if _has_stats(ext_stats):
                stats = _merge_stats(stats, ext_stats)
                sources.append("mp/getappmsgext")

        if not _has_stats(stats):
            return _bridge_error("no_data", "当前微信会话未返回互动计数")
        return {"ok": True, "source": "+".join(sources), "stats": stats.model_dump()}

    async def comments(self, request: BridgeFetchRequest) -> dict[str, Any]:
        if not _allowed_article_url(request.url):
            return _bridge_error("invalid_target", "只允许 mp.weixin.qq.com 公开文章 URL")
        metadata, biz = self._resolve_metadata(request)
        if not biz or not metadata.get("mid") or not metadata.get("comment_id"):
            return _bridge_error("missing_article_context", "文章未暴露完整评论参数")
        key = _article_key(biz, metadata)
        if request.page == 1 and key in self._comment_observations:
            return self._comment_page(
                self._comment_observations[key], request.page, request.limit,
                source="wechat_client_observed_response",
            )

        credential = self.store.credential(biz)
        if credential is None:
            return _bridge_error("waiting_session", "请在本地微信中打开该公众号任意文章")
        headers = _wechat_headers(
            credential,
            _with_session_query(request.url, biz, credential),
        )
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        buffer = ""
        has_more = False
        total: int | None = None
        wanted = request.page * request.limit

        for _ in range(MAX_COMMENT_ENDPOINT_PAGES):
            params = {
                "action": "getcomment",
                "__biz": biz,
                "appmsgid": metadata["mid"],
                "mid": metadata["mid"],
                "idx": metadata.get("idx", "1"),
                "comment_id": metadata["comment_id"],
                "limit": "100",
                "appmsg_token": credential["appmsg_token"],
                "uin": credential["uin"],
                "key": credential["key"],
                "pass_ticket": credential["pass_ticket"],
                "wxtoken": "777",
                "devicetype": credential.get("devicetype", "Windows 10 x64"),
                "clientversion": credential.get("clientversion", "63090c11"),
                "f": "json",
            }
            if buffer:
                params["buffer"] = buffer
            response = await self.client.get(
                "https://mp.weixin.qq.com/mp/appmsg_comment",
                params=params,
                headers=headers,
            )
            _raise_wechat_response(response)
            payload = _response_json(response)
            _raise_wechat_payload(payload)
            for comment in parse_comments(payload):
                if comment.comment_id in seen:
                    continue
                seen.add(comment.comment_id)
                rows.append(comment.model_dump(mode="json"))
            total = _comment_total(payload, total)
            has_more = bool(payload.get("continue_flag") or payload.get("is_continue"))
            buffer = str(payload.get("buffer") or "")
            if len(rows) >= wanted or not has_more or not buffer:
                break

        start = (request.page - 1) * request.limit
        page_rows = rows[start:start + request.limit]
        next_page = bool(start + len(page_rows) < len(rows) or has_more)
        return {
            "ok": True,
            "source": "mp/appmsg_comment",
            "comment_scope": "elected",
            "comments": page_rows,
            "total_comments": total,
            "has_more": next_page,
        }

    async def _get_article_response(
        self,
        url: str,
        biz: str,
        credential: dict[str, str],
        headers: dict[str, str],
    ) -> httpx.Response:
        current = url
        for _ in range(3):
            response = await self.client.get(current, headers=headers)
            if response.status_code not in {301, 302, 303, 307, 308}:
                _raise_wechat_response(response)
                return response
            location = urljoin(str(response.url), str(response.headers.get("location") or ""))
            if "/mp/wappoc_appmsgcaptcha" in location:
                raise PlatformBlockedError("微信会话触发访问环境验证")
            if not _allowed_article_url(location):
                raise PlatformBlockedError("微信文章跳转到了非文章目标")
            current = _with_session_query(location, biz, credential)
        raise PlatformCrawlerError("微信文章重定向次数过多")

    def _resolve_metadata(self, request: BridgeFetchRequest) -> tuple[dict[str, str], str]:
        metadata = parse_metadata("", request.url)
        metadata.update({key: str(value) for key, value in request.metadata.items() if value})
        biz = metadata.get("biz") or metadata.get("bizuin") or self.store.biz_for_url(request.url)
        metadata["biz"] = biz
        return metadata, biz

    def _capture_observation(self, capture: BridgeCaptureRequest, biz: str) -> None:
        values = _request_values(capture.request_url, capture.request_body)
        response_metadata = parse_metadata(capture.response_body, capture.request_url)
        metadata = {
            "mid": str(values.get("appmsgid") or values.get("mid") or response_metadata.get("mid") or ""),
            "idx": str(values.get("idx") or response_metadata.get("idx") or "1"),
        }
        key = _article_key(biz, metadata)
        path = urlparse(capture.request_url).path
        if path == "/mp/getappmsgext":
            payload = _loads_json(capture.response_body)
            if payload:
                stats = parse_stats_payload(payload)
                if _has_stats(stats):
                    self._stats_observations[key] = stats
        elif path == "/mp/appmsg_comment":
            payload = _loads_json(capture.response_body)
            if payload:
                self._comment_observations[key] = payload
        elif path == "/s" or path.startswith("/s/") or path == "/mp/appmsg/show":
            stats = parse_stats(capture.response_body)
            if _has_stats(stats):
                self._stats_observations[key] = stats

    @staticmethod
    def _comment_page(
        payload: dict[str, Any], page: int, limit: int, *, source: str,
    ) -> dict[str, Any]:
        rows = [comment.model_dump(mode="json") for comment in parse_comments(payload)]
        start = (page - 1) * limit
        selected = rows[start:start + limit]
        total = _comment_total(payload, None)
        has_more = bool(
            start + len(selected) < len(rows)
            or payload.get("continue_flag")
            or payload.get("is_continue")
        )
        return {
            "ok": True,
            "source": source,
            "comment_scope": "elected",
            "comments": selected,
            "total_comments": total,
            "has_more": has_more,
        }


class HttpWeChatSessionBridgeClient:
    """Client used by the main API process; no WeChat secrets cross this link."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 20,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token.strip()
        if not self.base_url or not self.token:
            raise ValueError("微信会话桥 URL 和 token 必须同时配置")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("远程微信会话桥必须使用 HTTPS；HTTP 仅允许回环地址")
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def fetch(
        self,
        operation: str,
        *,
        url: str,
        metadata: dict[str, str],
        page: int,
        limit: int,
    ) -> dict[str, Any]:
        if operation not in {"interactions", "comments"}:
            raise ValueError("unsupported WeChat bridge operation")
        try:
            response = await self.client.post(
                f"{self.base_url}/v1/{operation}",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"url": url, "metadata": metadata, "page": page, "limit": limit},
            )
        except httpx.HTTPError as exc:
            raise PlatformCrawlerError("本地微信会话桥连接失败") from exc
        if response.status_code in {401, 403}:
            raise PlatformBlockedError("本地微信会话桥鉴权失败")
        if response.status_code < 200 or response.status_code >= 300:
            raise PlatformCrawlerError(f"本地微信会话桥返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise PlatformCrawlerError("本地微信会话桥返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise PlatformCrawlerError("本地微信会话桥响应结构错误")
        return payload


def create_wechat_bridge_app(
    token: str,
    *,
    bridge: WeChatSessionBridge | None = None,
) -> FastAPI:
    """Create a loopback sidecar API with bearer authentication."""

    secret = token.strip()
    if len(secret) < 24:
        raise ValueError("WECHAT_SESSION_BRIDGE_TOKEN 至少需要 24 个字符")
    active_bridge = bridge or WeChatSessionBridge()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await active_bridge.aclose()

    app = FastAPI(
        title="Creator Engagement WeChat Session Bridge",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.wechat_bridge = active_bridge

    def authorize(authorization: str = Header(default="")) -> None:
        expected = f"Bearer {secret}"
        if not compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/health")
    async def health(authorization: str = Header(default="")) -> dict[str, Any]:
        authorize(authorization)
        return active_bridge.store.status()

    @app.post("/v1/capture")
    async def capture(
        body: BridgeCaptureRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        authorize(authorization)
        return active_bridge.capture(body)

    @app.post("/v1/interactions")
    async def interactions(
        body: BridgeFetchRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        authorize(authorization)
        return await active_bridge.interactions(body)

    @app.post("/v1/comments")
    async def comments(
        body: BridgeFetchRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        authorize(authorization)
        return await active_bridge.comments(body)

    return app


def _request_values(request_url: str, request_body: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for source in (urlparse(request_url).query, request_body):
        for key, children in parse_qs(source, keep_blank_values=True).items():
            if children and children[0] != "":
                values[key] = children[0]
    return values


def _header(headers: dict[str, str], name: str) -> str:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return str(value or "").strip()
    return ""


def _cookies_from_set_cookie(value: str) -> str:
    pairs = re.findall(r"(?:^|,\s*)([!#$%&'*+.^_`|~0-9A-Za-z-]+)=([^;,]*)", value)
    return "; ".join(f"{name}={content}" for name, content in pairs)


def _clean_article_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    for key in (
        "uin",
        "key",
        "pass_ticket",
        "appmsg_token",
        "wxtoken",
        "devicetype",
        "clientversion",
    ):
        query.pop(key, None)
    return urlunparse((
        parsed.scheme,
        parsed.netloc.lower(),
        parsed.path,
        "",
        urlencode(query, doseq=True),
        "",
    ))


def _article_key(biz: str, metadata: dict[str, str]) -> tuple[str, str, str]:
    return biz, str(metadata.get("mid") or ""), str(metadata.get("idx") or "1")


def _allowed_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "mp.weixin.qq.com"
        and (
            parsed.path == "/s"
            or parsed.path.startswith("/s/")
            or parsed.path == "/mp/appmsg/show"
        )
    )


def _allowed_capture_path(path: str) -> bool:
    return (
        path == "/s"
        or path.startswith("/s/")
        or path in {"/mp/appmsg/show", "/mp/getappmsgext", "/mp/appmsg_comment"}
    )


def _with_session_query(url: str, biz: str, credential: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["__biz"] = [biz]
    for key in ("uin", "key", "pass_ticket", "appmsg_token"):
        query[key] = [credential[key]]
    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        urlencode(query, doseq=True),
        "",
    ))


def _wechat_headers(credential: dict[str, str], referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Cookie": credential["cookie"],
        "Referer": referer,
        "User-Agent": credential.get("user_agent") or "Mozilla/5.0 MicroMessenger",
    }


def _raise_wechat_response(response: httpx.Response) -> None:
    if len(response.content) > MAX_WECHAT_RESPONSE_BYTES:
        raise PlatformCrawlerError("微信响应超过安全大小限制")
    location = str(response.headers.get("location") or "")
    if "/mp/wappoc_appmsgcaptcha" in location or "/mp/wappoc_appmsgcaptcha" in str(response.url):
        raise PlatformBlockedError("微信会话触发访问环境验证")
    if response.status_code in {401, 403, 406, 412, 418, 429}:
        raise PlatformBlockedError(f"微信会话请求被拒绝: HTTP {response.status_code}")
    if response.status_code < 200 or response.status_code >= 300:
        raise PlatformCrawlerError(f"微信会话请求返回 HTTP {response.status_code}")


def _response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise PlatformCrawlerError("微信会话接口返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise PlatformCrawlerError("微信会话接口响应结构错误")
    return payload


def _raise_wechat_payload(payload: dict[str, Any]) -> None:
    base = payload.get("base_resp") if isinstance(payload.get("base_resp"), dict) else {}
    raw_ret = payload.get("ret") if payload.get("ret") is not None else base.get("ret")
    try:
        ret = int(raw_ret) if raw_ret not in {None, ""} else 0
    except (TypeError, ValueError):
        ret = 0
    if ret == 0:
        return
    message = str(payload.get("errmsg") or base.get("errmsg") or ret)
    if ret == -3 or "session" in message.lower():
        raise PlatformBlockedError(f"微信短时会话已失效: {message}")
    raise PlatformCrawlerError(f"微信会话接口返回 {ret}: {message}")


def _loads_json(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _comment_total(payload: dict[str, Any], previous: int | None) -> int | None:
    raw = payload.get("total_count")
    if raw is None:
        raw = payload.get("total")
    if raw is None:
        raw = find_nested_value(payload, {"total_count", "elected_comment_total_cnt"})
    try:
        return int(raw) if raw is not None and raw != "" else previous
    except (TypeError, ValueError):
        return previous


def _has_stats(stats: EngagementStats) -> bool:
    return any(value is not None for value in stats.model_dump().values())


def _merge_stats(primary: EngagementStats, secondary: EngagementStats) -> EngagementStats:
    first = primary.model_dump()
    second = secondary.model_dump()
    return EngagementStats.model_validate({
        key: first.get(key) if first.get(key) is not None else second.get(key)
        for key in first
    })


def _bridge_error(status: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "status": status, "reason": reason}
