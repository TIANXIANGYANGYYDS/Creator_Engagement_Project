from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError


class AuthorCursorError(ValueError):
    pass


class AuthorSessionError(PlatformCrawlerError):
    """The selected public route explicitly refuses the current session."""


def text(value) -> str:
    return BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)


def date(value) -> datetime | None:
    if not value:
        return None
    try:
        if str(value).isdigit():
            return datetime.fromtimestamp(int(value), timezone.utc)
        return parsedate_to_datetime(str(value))
    except (ValueError, TypeError, OverflowError):
        return None


def urls(values) -> list[str]:
    return list(
        dict.fromkeys(
            v
            for v in values
            if isinstance(v, str) and urlsplit(v).scheme in {"http", "https"}
        )
    )


def encode_cursor(request, value: dict) -> str:
    payload = {"uid": request.uid, "media_name": request.media_name, **value}
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()


def decode_cursor(request, cursor: str | None) -> dict:
    if cursor is None:
        return {}
    try:
        if len(cursor) > 4096:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(value, dict)
            or value.get("uid") != request.uid
            or value.get("media_name") != request.media_name
        ):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise AuthorCursorError("cursor 无效或不属于此作者及平台") from exc


def page_number(state: dict) -> int:
    page = state.get("page", 1)
    if type(page) is not int or page < 1:
        raise AuthorCursorError("cursor 页码无效")
    return page


def require_list(value, label: str) -> list:
    if not isinstance(value, list):
        raise PlatformCrawlerError(f"{label} 缺少作品列表，不能视作空账号")
    return value


def pending(work, **context):
    work.content_complete = False
    work.detail_status = "pending"
    work.detail_context = context
    return work


async def fill_details(crawler, request, works, handler):
    """Compatibility for direct adapter callers; jobs use independent workers."""
    for work in works:
        if work.detail_status != "pending":
            continue
        try:
            await handler(crawler, request, work)
            work.content_complete = True
            work.detail_status = "complete"
            work.error = None
        except PlatformCrawlerError as exc:
            work.detail_status = "failed"
            work.error = str(exc)
