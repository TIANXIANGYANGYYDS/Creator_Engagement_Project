"""Small parsing helpers for public publisher pages (no inferred zero counts)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.models.engagement import EngagementComment
from app.crawlers.platforms.common import to_int


def assigned_json(text: str, name: str) -> dict:
    match = re.search(re.escape(name) + r"\s*=\s*", text)
    if not match:
        return {}
    try:
        value = json.JSONDecoder().raw_decode(text[match.end():])[0]
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def json_or_jsonp(text: str) -> dict:
    text = text.strip()
    try:
        value = json.loads(text)
    except ValueError:
        match = re.fullmatch(r"[\w.$]+\s*\((.*)\)\s*;?", text, re.S)
        if not match:
            raise PlatformCrawlerError("公开接口未返回 JSON/JSONP")
        value = json.loads(match[1])
    if not isinstance(value, dict):
        raise PlatformCrawlerError("公开接口 JSON 结构异常")
    return value


async def get_jsonp(crawler, url, *, params=None, headers=None):
    response = await crawler._get_response(url, params=params, headers=headers)
    return json_or_jsonp(response.text)


def comment(cid: Any, author: Any, text: Any, *, likes=None, replies=None, created=None):
    if cid is None or str(cid) == "":
        raise PlatformCrawlerError("评论缺少 ID，不能确认分页完整性")
    stamp = None
    if created not in (None, ""):
        try:
            number = float(created)
            stamp = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            try:
                stamp = datetime.fromisoformat(str(created))
                if stamp.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            except ValueError:
                pass
    return EngagementComment(
        comment_id=str(cid), author=str(author or ""),
        text=BeautifulSoup(str(text or ""), "html.parser").get_text(" ", strip=True),
        likes=to_int(likes), replies=to_int(replies), created_at=stamp,
    )


def require_page(items, has_more, current, following):
    if has_more and (not items or str(following) == str(current) or following is None):
        raise PlatformCrawlerError("评论分页未前进，不能将空页当成末页")
