"""Local authenticated platform session storage.

Files are Playwright-compatible storage-state JSON and live under `.local/`,
which is ignored by Git. Only Cookie headers are exposed to protocol clients.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.models.engagement import EngagementPlatform


class PlatformSessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def state_path(self, platform: EngagementPlatform) -> Path:
        return self.root / f"{platform}.json"

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

    async def save_context(self, platform: EngagementPlatform, context: Any) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.state_path(platform)
        await context.storage_state(path=str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
