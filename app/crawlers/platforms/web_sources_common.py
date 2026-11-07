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


def nuxt_literal_state(html: str) -> dict:
    """Decode Nuxt's literal-only IIFE serialization without executing JavaScript.

    Only JSON arguments, literal properties on argument objects, object keys
    and argument references are accepted. No JavaScript is executed.
    """
    soup = BeautifulSoup(html, "html.parser")
    script = next((node.string for node in soup.select("script")
        if node.string and node.string.strip().startswith("window.__NUXT__=")), "")
    if len(script) > 3_000_000:
        raise PlatformCrawlerError("Nuxt 状态超过解析大小限制")
    match = re.fullmatch(r"window\.__NUXT__=\(function\(([^)]*)\)\{(.*?)return (.*)\}\((.*)\)\);?", script.strip(), re.S)
    if not match:
        raise PlatformCrawlerError("页面未提供可解码的 Nuxt 字面量状态")
    try:
        names = match[1].split(",")
        values = json.loads("[" + match[4] + "]")
        if len(names) != len(values) or len(set(names)) != len(names):
            raise ValueError
        bindings = dict(zip(names, values))
        # Nuxt can initialize shared objects before returning the state.
        # Accept only direct properties with JSON values, never calls or code.
        prefix = match[2].strip()
        while prefix:
            assignment = re.match(r"([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)\s*=\s*", prefix)
            if not assignment or not isinstance(bindings.get(assignment[1]), dict):
                raise ValueError
            value, length = json.JSONDecoder().raw_decode(prefix[assignment.end():])
            tail = prefix[assignment.end() + length:].lstrip()
            if not tail.startswith(";"):
                raise ValueError
            bindings[assignment[1]][assignment[2]] = value
            prefix = tail[1:].lstrip()
        token = re.compile(r'"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z_$][\w$]*|[{}\[\],:]')
        colon = re.compile(r"\s*:")
        source, output, end = match[3], [], 0
        for item in token.finditer(source):
            if source[end:item.start()].strip():
                raise ValueError
            value = item[0]
            if re.fullmatch(r"[A-Za-z_$][\w$]*", value):
                if colon.match(source, item.end()):
                    value = json.dumps(value)
                elif value not in {"true", "false", "null"}:
                    value = json.dumps(bindings[value], ensure_ascii=False)
            output.append(value)
            end = item.end()
        if source[end:].strip():
            raise ValueError
        state = json.loads("".join(output))
        if not isinstance(state, dict):
            raise ValueError
        return state
    except (KeyError, ValueError, TypeError):
        raise PlatformCrawlerError("Nuxt 状态含非字面量表达式或无效引用") from None


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
