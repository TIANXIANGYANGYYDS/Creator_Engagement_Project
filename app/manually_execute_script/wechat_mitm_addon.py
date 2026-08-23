"""mitmproxy addon that forwards only WeChat article-session flows to localhost.

Run this file with ``mitmdump -s``.  It deliberately does not write flow files,
cookies, or tokens to disk.  The receiving bridge must be bound to loopback.
"""

from __future__ import annotations

import json
import os
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener


MAX_RESPONSE_CHARS = 6 * 1024 * 1024
ALLOWED_PATHS = {
    "/mp/getappmsgext",
    "/mp/appmsg_comment",
    "/mp/appmsg/show",
}


class WeChatSessionCaptureAddon:
    def __init__(self) -> None:
        self.bridge_url = os.environ.get(
            "WECHAT_SESSION_BRIDGE_URL",
            "http://127.0.0.1:8210",
        ).rstrip("/")
        self.token = os.environ.get("WECHAT_SESSION_BRIDGE_TOKEN", "").strip()

    def response(self, flow: object) -> None:
        if not self.token:
            return
        request = getattr(flow, "request", None)
        response = getattr(flow, "response", None)
        if request is None or response is None:
            return
        host = str(getattr(request, "host", "") or "").lower()
        path = str(getattr(request, "path", "") or "").split("?", 1)[0]
        if host != "mp.weixin.qq.com" or not _allowed_path(path):
            return
        try:
            response_body = str(response.get_text(strict=False) or "")
        except Exception:
            response_body = ""
        if len(response_body) > MAX_RESPONSE_CHARS:
            response_body = ""
        try:
            request_body = str(request.get_text(strict=False) or "")
        except Exception:
            request_body = ""
        payload = {
            "request_url": str(getattr(request, "url", "") or ""),
            "request_headers": _selected_headers(
                getattr(request, "headers", {}),
                {"cookie", "user-agent"},
            ),
            "request_body": request_body,
            "response_headers": _selected_headers(
                getattr(response, "headers", {}),
                {"set-cookie", "content-type"},
            ),
            "response_body": response_body,
        }
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            target = urlparse(self.bridge_url)
            if target.scheme != "http" or target.hostname not in {
                "127.0.0.1", "localhost", "::1",
            }:
                return
            outbound = Request(
                f"{self.bridge_url}/v1/capture",
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
            # The desktop system proxy points at mitmproxy during capture.  A
            # no-proxy opener prevents this loopback callback from recursing.
            with build_opener(ProxyHandler({})).open(outbound, timeout=1):
                pass
        except Exception:
            # Capture must never break article rendering in WeChat.
            return


def _allowed_path(path: str) -> bool:
    return path in ALLOWED_PATHS or path == "/s" or path.startswith("/s/")


def _selected_headers(headers: object, allowed: set[str]) -> dict[str, str]:
    selected: dict[str, str] = {}
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except AttributeError:
        return selected
    for key, value in items:
        if str(key).lower() in allowed:
            selected[str(key)] = str(value)
    return selected


addons = [WeChatSessionCaptureAddon()]
