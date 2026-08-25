"""Small value-conversion helpers shared by platform-specific collectors."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.engagement import EngagementCoverage, EngagementPlatform, EngagementResult


COMMENT_PAGE_SIZE = 20
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def to_int(value: Any) -> int | None:
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


def first_present(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None and value != "":
            return value
    return None


def timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def result_error(
    platform: EngagementPlatform,
    url: str,
    work_id: str,
    coverage: EngagementCoverage,
    reason: str,
) -> EngagementResult:
    return EngagementResult(
        platform=platform,
        canonical_url=url,
        work_id=work_id,
        coverage=coverage,
        reason=reason,
        source="protocol",
    )
