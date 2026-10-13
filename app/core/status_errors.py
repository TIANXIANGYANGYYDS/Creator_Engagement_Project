"""Positive status-error evidence; missing content alone is never an alert."""
from __future__ import annotations

import re

import httpx

def status_error(error: Exception | str) -> str | None:
    from app.crawlers.http_client import PlatformCrawlerError

    reason = str(error)
    # Explicit response status takes precedence over missing-data explanations.
    codes = re.findall(r"\bHTTP\s*[:=]?\s*(\d{3})\b", reason, re.I)
    if "401" in codes or re.search(
        r"reauthorization_required|登录(?:态|状态)?(?:已)?失效|会话(?:已)?失效|登录过期|未登录|请先登录|重新登录|not logged in|session expired|access_token.*expired|官方 API (?:40001|40014|42001)\b", reason, re.I
    ):
        return "login_expired"
    if set(codes) & {"403", "406", "412", "418", "429", "432", "461", "471"} or re.search(
        r"risk_limited|ArgusSecurityPlugin|要求验证码|要求验证|触发.{0,12}(?:验证码|访问验证|访问限制|风控)|请求被限制|访问过于频繁|请求过于频繁|captcha required|rate limit|官方 API (?:40164|45009|45011)\b", reason, re.I
    ):
        return "risk_limited"
    if re.search(r"要求登录|会话尚未建立|会话初始化未完成|跳转到登录页", reason):
        return "session_rejected"
    if any(int(code) >= 500 for code in codes):
        return "upstream_error"
    if re.search(r"transport_stopped|owner_unavailable|常驻浏览器不可用|browser.*(?:closed|disconnected|crashed)", reason, re.I):
        return "transport_error"
    if re.search(r"non-JSON|非 JSON|invalid JSON|返回无效 JSON|HTTP\s*200\s*空包", reason, re.I):
        return "protocol_error"
    if re.search(r"engagement request failed|timed?\s*out|timeout|超时|连接失败|队列繁忙|connection (?:refused|reset)|proxy.*unavailable|代理.*不可用", reason, re.I):
        return "transport_error"
    if re.search(r"(?:returned code|业务码)\s*[:=]?\s*-(?:101|100)\b", reason):
        return "login_expired"
    if re.search(r"(?:returned code|业务码)\s*[:=]?\s*-(?:352|412|509|104|103|13002)\b", reason):
        return "risk_limited"
    if isinstance(error, Exception):
        # Keep explicit transport/runtime causes even if a collector wraps them.
        if error.__cause__ is not None:
            category = status_error(error.__cause__)
            if category:
                return category
        if isinstance(error, (httpx.TransportError, TimeoutError, ConnectionError)):
            return "transport_error"
        if type(error).__name__ in {"ProxyUnavailableError", "ProxyError", "RequestsError", "CurlError", "TargetClosedError"}:
            return "transport_error"
        if type(error).__name__ == "AuthorSessionError":
            return "session_rejected"
        # Collector/data-validation errors have no alert without positive status
        # evidence. KeyError/AttributeError/etc. are actual program failures.
        if not isinstance(error, (PlatformCrawlerError, ValueError)):
            return "program_error"
    return None
