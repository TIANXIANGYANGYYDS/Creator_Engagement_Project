"""Refresh an existing XHS browser session and write a credential-free health status."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

from app.core.config import PROJECT_ROOT
from app.manually_execute_script.login_platform import run


STATUS = PROJECT_ROOT / ".local/analysis/xhs-session-health.json"


def write_status(status: str) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATUS.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(STATUS)


def main() -> None:
    try:
        asyncio.run(run("xiaohongshu", headless=True, check_only=True))
    except Exception as exc:
        status = "reauthorization_required" if any(
            marker in str(exc) for marker in ("登录态已失效", "reauthorization_required")
        ) else "check_failed"
        write_status(status)
        raise SystemExit(2) from None
    write_status("valid")


if __name__ == "__main__":
    main()
