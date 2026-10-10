"""Local authenticated platform session storage.

Files are Playwright-compatible storage-state JSON and live under `.local/`,
which is ignored by Git. Only Cookie headers are exposed to protocol clients.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.models.engagement import EngagementPlatform


class PlatformSessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def state_path(self, platform: EngagementPlatform) -> Path:
        return self.root / f"{platform}.json"

    def scoped_state(self, platform: EngagementPlatform, domains: tuple[str, ...]) -> dict:
        """Read fresh state, preserving cookie scope and excluding expired tokens."""
        try:
            state = json.loads(self.state_path(platform).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"cookies": [], "origins": []}
        if not isinstance(state, dict):
            return {"cookies": [], "origins": []}

        def allowed(host):
            return any(host == domain or host.endswith("." + domain) for domain in domains)

        cookies = []
        for cookie in state.get("cookies", []):
            if not isinstance(cookie, dict) or not cookie.get("name") or not cookie.get("value"):
                continue
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            expires = cookie.get("expires", -1)
            if not isinstance(expires, (int, float)) or (expires > 0 and expires <= time.time()):
                continue
            if allowed(domain):
                cookies.append(cookie)
        origins = [
            item for item in state.get("origins", [])
            if isinstance(item, dict)
            and urlsplit(str(item.get("origin") or "")).scheme == "https"
            and allowed(urlsplit(str(item.get("origin") or "")).hostname or "")
        ]
        return {"cookies": cookies, "origins": origins}

    def cookie_header(self, platform: EngagementPlatform) -> str:
        path = self.state_path(platform)
        if not path.exists():
            return ""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return ""
        cookies = payload.get("cookies", []) if isinstance(payload, dict) else []
        values = []
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if name and value:
                values.append(f"{name}={value}")
        return "; ".join(values)

    def cookie_header_for_url(self, platform: EngagementPlatform, url: str) -> str:
        """Return unexpired cookies that a browser would send to this URL."""
        target = urlsplit(url)
        host = (target.hostname or "").lower()
        if target.scheme != "https" or not host:
            return ""
        try:
            state = json.loads(self.state_path(platform).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(state, dict):
            return ""
        cookies = state.get("cookies")
        if not isinstance(cookies, list):
            return ""
        values = []
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name, value = cookie.get("name"), cookie.get("value")
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            path = str(cookie.get("path") or "/")
            expires = cookie.get("expires", -1)
            if not isinstance(name, str) or not isinstance(value, str) or not name or not value:
                continue
            if not domain or not (host == domain or host.endswith("." + domain)):
                continue
            if not target.path.startswith(path):
                continue
            if not isinstance(expires, (int, float)) or (expires > 0 and expires <= time.time()):
                continue
            values.append(f"{name}={value}")
        return "; ".join(values)

    async def save_context(self, platform: EngagementPlatform, context: Any) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.state_path(platform)
        await context.storage_state(path=str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
