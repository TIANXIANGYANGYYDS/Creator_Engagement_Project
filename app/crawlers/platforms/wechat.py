"""WeChat article metadata, counters, and session-bound comment flow."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import (
    COMMENT_PAGE_SIZE,
    first_present,
    result_error,
    timestamp,
    to_int,
)
from app.models.engagement import (
    EngagementComment,
    EngagementCoverage,
    EngagementResult,
    EngagementStats,
)


async def fetch(
    crawler: PlatformCrawlerContext,
    url: str,
    work_id: str,
    limit: int,
    *,
    page: int,
    comment_cursor: str | None,
    include_stats: bool,
    include_comments: bool,
) -> EngagementResult:
    cookie = crawler._platform_cookie("wechat")
    cookie_issue = ""
    official_issue = ""
    bridge_issue = ""
    official_stats: EngagementStats | None = None
    bridge_stats: EngagementStats | None = None
    try:
        article_response = await crawler._get_response(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://mp.weixin.qq.com/",
                **({"Cookie": cookie} if cookie else {}),
            },
        )
        text = str(getattr(article_response, "text", "") or "")
        response_url = str(getattr(article_response, "url", "") or "")
        if "/mp/wappoc_appmsgcaptcha" in response_url:
            raise PlatformBlockedError("公众号文章触发访问环境验证码")
        metadata = merge_cookie_metadata(parse_metadata(text, url), cookie)
        stats = parse_stats(text) if include_stats else EngagementStats()
        if include_stats and not _has_stats(stats) and cookie:
            try:
                ext_payload = await fetch_appmsgext(crawler, url, metadata, cookie)
                stats = parse_stats_payload(ext_payload)
                if not _has_stats(stats):
                    cookie_issue = "Cookie 会话响应未包含互动统计"
            except PlatformCrawlerError as exc:
                cookie_issue = str(exc)
        if include_stats:
            try:
                bridge_payload = await crawler._wechat_session_bridge_request(
                    "interactions",
                    url=url,
                    metadata=metadata,
                    page=page,
                    limit=limit,
                )
                if bridge_payload and bridge_payload.get("ok"):
                    bridge_stats = EngagementStats.model_validate(
                        bridge_payload.get("stats") or {}
                    )
                    if _has_stats(bridge_stats):
                        stats = bridge_stats
                elif bridge_payload:
                    bridge_issue = str(
                        bridge_payload.get("reason")
                        or bridge_payload.get("status")
                        or "会话桥未返回数据"
                    )
            except PlatformCrawlerError as exc:
                bridge_issue = str(exc)
            if bridge_stats is None or not _has_stats(bridge_stats):
                try:
                    official_stats = await fetch_official_stats(crawler, metadata)
                    if official_stats is not None:
                        stats = official_stats
                except PlatformCrawlerError as exc:
                    official_issue = str(exc)

        if not include_comments:
            has_stats = _has_stats(stats)
            official = official_stats is not None
            bridged = bridge_stats is not None and _has_stats(bridge_stats)
            return EngagementResult(
                platform="wechat",
                canonical_url=url,
                work_id=work_id,
                coverage="partial" if has_stats else "unsupported",
                reason=(
                    "本地微信短时会话返回该公众号文章的公开互动量"
                    if bridged
                    else (
                        "自有公众号官方接口返回阅读人数、赞、分享、留言和收藏数据"
                        if official
                        else (
                            "公众号文章页面返回了当前会话可见互动量"
                            if has_stats
                            else (
                                "公众号页面未下发阅读/点赞统计；任意第三方文章需要完整的"
                                "文章会话 Cookie"
                                + cookie_capability_suffix(metadata, cookie)
                                + (f"；Cookie 协议: {cookie_issue}" if cookie_issue else "")
                                + (f"；会话桥: {bridge_issue}" if bridge_issue else "")
                                + (f"；官方接口: {official_issue}" if official_issue else "")
                            )
                        )
                    )
                ),
                source=(
                    str(bridge_payload.get("source") or "wechat_session_bridge")
                    if bridged
                    else (
                        "api.weixin.qq.com/datacube/getarticletotaldetail"
                        if official
                        else "mp article appmsgstat/cgiDataNew"
                    )
                ),
                stats=stats,
            )

        preloaded_payload = parse_preloaded_comment_payload(text)
        preloaded_comments = parse_comments(preloaded_payload)

        try:
            bridge_comments = await crawler._wechat_session_bridge_request(
                "comments",
                url=url,
                metadata=metadata,
                page=page,
                limit=limit,
            )
        except PlatformCrawlerError as exc:
            bridge_issue = str(exc)
            bridge_comments = None
        if bridge_comments and bridge_comments.get("ok"):
            comments = parse_comments({"comments": bridge_comments.get("comments") or []})
            total = to_int(bridge_comments.get("total_comments"))
            return EngagementResult(
                platform="wechat",
                canonical_url=url,
                work_id=work_id,
                coverage="partial",
                reason="本地微信短时会话返回指定页；微信文章只公开精选评论，不代表全量评论",
                source=str(bridge_comments.get("source") or "wechat_session_bridge"),
                stats=EngagementStats(comments=total),
                comments=comments[:limit],
                next_cursor=str(page + 1) if bridge_comments.get("has_more") else None,
            )
        if bridge_comments:
            bridge_issue = str(
                bridge_comments.get("reason")
                or bridge_comments.get("status")
                or "会话桥未返回数据"
            )

        try:
            official_comments = await fetch_official_comments(
                crawler,
                metadata,
                page=page,
                limit=limit,
            )
        except PlatformCrawlerError as exc:
            official_issue = str(exc)
            official_comments = None
        if official_comments is not None:
            comments = parse_comments(official_comments)
            total = to_int(official_comments.get("total"))
            offset = (page - 1) * min(limit, COMMENT_PAGE_SIZE)
            has_more = total is not None and offset + len(comments) < total
            return EngagementResult(
                platform="wechat",
                canonical_url=url,
                work_id=work_id,
                coverage="complete",
                reason="自有公众号官方留言接口返回该文章指定页",
                source="api.weixin.qq.com/cgi-bin/comment/list",
                stats=EngagementStats(comments=total),
                comments=comments[:limit],
                next_cursor=str(page + 1) if has_more else None,
            )

        if page == 1 and preloaded_comments:
            total = (
                parse_preloaded_comment_total(text)
                or to_int(find_nested_value(
                    preloaded_payload,
                    {"total_count", "total", "elected_comment_total_cnt"},
                ))
            )
            return EngagementResult(
                platform="wechat",
                canonical_url=url,
                work_id=work_id,
                coverage="partial",
                reason="公众号文章 HTML 预载了当前精选评论首屏",
                source="preload_comment_list",
                stats=EngagementStats(comments=total),
                comments=preloaded_comments[:limit],
                next_cursor=(
                    "2"
                    if total is not None and total > len(preloaded_comments)
                    else None
                ),
            )

        required = ("biz", "mid", "idx", "comment_id")
        if any(not metadata.get(key) for key in required):
            return result_error(
                "wechat", url, work_id, "unsupported", "公众号文章没有暴露完整评论参数"
            )
        if not cookie:
            page_state = (
                "当前匿名文章页未显示评论区；show_comment=0 不能单独证明作者关闭评论"
                if metadata.get("show_comment") == "0"
                else "当前文章页没有预载精选评论"
            )
            return result_error(
                "wechat",
                url,
                work_id,
                "unsupported",
                page_state
                + "；公众号任意第三方文章评论需要完整的文章会话 Cookie"
                + (f"；会话桥: {bridge_issue}" if bridge_issue else "")
                + (f"；官方接口尝试失败: {official_issue}" if official_issue else ""),
            )
        payload = await fetch_cookie_comments(
            crawler,
            url,
            metadata,
            cookie,
            page=page,
            limit=limit,
        )
        base = payload.get("base_resp") or {}
        raw_ret = payload.get("ret") if payload.get("ret") is not None else base.get("ret")
        try:
            ret = int(raw_ret) if raw_ret not in {None, ""} else None
        except (TypeError, ValueError):
            ret = None
        errmsg = str(payload.get("errmsg") or base.get("errmsg") or "")
        if ret not in {None, 0}:
            coverage: EngagementCoverage = "unsupported" if (
                ret == -3 or "session" in errmsg.lower()
            ) else "failed"
            return result_error(
                "wechat",
                url,
                work_id,
                coverage,
                f"公众号评论 Cookie 会话无效: {errmsg or ret}"
                + cookie_capability_suffix(metadata, cookie),
            )
        if to_int(payload.get("enabled")) == 0:
            return EngagementResult(
                platform="wechat",
                canonical_url=url,
                work_id=work_id,
                coverage="complete",
                reason="公众号评论接口明确返回 enabled=0，该文章未开放评论",
                source="mp/appmsg_comment.enabled",
                stats=EngagementStats(comments=0),
            )
        comments = parse_comments(payload)
        total = to_int(
            payload.get("total_count")
            or payload.get("total")
            or payload.get("elected_comment_total_cnt")
            or find_nested_value(
                payload,
                {"total_count", "total", "elected_comment_total_cnt"},
            )
        )
        has_more = bool(
            payload.get("is_continue")
            or payload.get("has_more")
            or payload.get("continue_flag")
        )
        return EngagementResult(
            platform="wechat",
            canonical_url=url,
            work_id=work_id,
            coverage="partial",
            reason="公众号文章 Cookie 纯协议返回指定精选评论页",
            source="mp/appmsg_comment",
            stats=EngagementStats(comments=total),
            comments=comments[:limit],
            next_cursor=str(page + 1) if has_more else None,
        )
    except PlatformBlockedError as exc:
        return result_error("wechat", url, work_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("wechat", url, work_id, "failed", str(exc))


async def fetch_appmsgext(
    crawler: PlatformCrawlerContext,
    article_url: str,
    metadata: dict[str, str],
    cookie: str,
) -> dict[str, Any]:
    query = {
        "__biz": metadata.get("biz", ""),
        "mid": metadata.get("mid", ""),
        "idx": metadata.get("idx", "1"),
        "sn": metadata.get("sn", ""),
        "scene": "0",
        "appmsg_token": metadata.get("appmsg_token", ""),
        "uin": metadata.get("uin", ""),
        "key": metadata.get("key", ""),
        "pass_ticket": metadata.get("pass_ticket", ""),
        "wxtoken": metadata.get("wxtoken", "777"),
        "devicetype": metadata.get("devicetype", ""),
        "clientversion": metadata.get("clientversion", ""),
        "f": "json",
    }
    form = {
        "r": "0",
        "__biz": metadata.get("biz", ""),
        "appmsg_type": "9",
        "mid": metadata.get("mid", ""),
        "sn": metadata.get("sn", ""),
        "idx": metadata.get("idx", "1"),
        "scene": "0",
        "title": "",
        "ct": metadata.get("ct", "0"),
        "is_need_ad": "0",
        "is_need_reward": "0",
        "comment_id": metadata.get("comment_id", ""),
        "is_only_read": metadata.get("is_only_read", "0"),
        "appmsg_token": metadata.get("appmsg_token", ""),
    }
    response = await crawler._post_response(
        "https://mp.weixin.qq.com/mp/getappmsgext",
        params=query,
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": article_url,
            "Cookie": cookie,
        },
        data=urlencode(form),
    )
    payload = response_json(response, "公众号互动接口")
    base = payload.get("base_resp") or {}
    raw_ret = payload.get("ret") if payload.get("ret") is not None else base.get("ret")
    try:
        ret = int(raw_ret) if raw_ret not in {None, ""} else 0
    except (TypeError, ValueError):
        ret = 0
    if ret != 0:
        errmsg = str(payload.get("errmsg") or base.get("errmsg") or ret)
        if ret == -3 or "session" in errmsg.lower():
            raise PlatformCrawlerError(f"公众号互动 Cookie 会话无效: {errmsg}")
        raise PlatformCrawlerError(f"公众号互动接口返回错误: {errmsg}")
    return payload


async def fetch_cookie_comments(
    crawler: PlatformCrawlerContext,
    article_url: str,
    metadata: dict[str, str],
    cookie: str,
    *,
    page: int,
    limit: int,
) -> dict[str, Any]:
    """Translate numbered API pages to WeChat's opaque ``buffer`` cursor."""

    page_size = min(limit, COMMENT_PAGE_SIZE)
    base_params = {
        "action": "getcomment",
        "scene": "0",
        "__biz": metadata.get("biz", ""),
        "appmsgid": metadata.get("mid", ""),
        "mid": metadata.get("mid", ""),
        "idx": metadata.get("idx", "1"),
        "comment_id": metadata.get("comment_id", ""),
        "limit": str(page_size),
        "appmsg_token": metadata.get("appmsg_token", ""),
        "uin": metadata.get("uin", ""),
        "key": metadata.get("key", ""),
        "pass_ticket": metadata.get("pass_ticket", ""),
        "wxtoken": metadata.get("wxtoken", "777"),
        "devicetype": metadata.get("devicetype", ""),
        "clientversion": metadata.get("clientversion", ""),
        "f": "json",
    }
    buffer = ""
    payload: dict[str, Any] = {}
    for current_page in range(1, page + 1):
        params = {
            **base_params,
            "offset": str((current_page - 1) * page_size),
        }
        if buffer:
            params["buffer"] = buffer
        response = await crawler._get_response(
            "https://mp.weixin.qq.com/mp/appmsg_comment",
            params=params,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": article_url,
                "Cookie": cookie,
            },
        )
        payload = response_json(response, "公众号评论接口")
        base = payload.get("base_resp") or {}
        raw_ret = (
            payload.get("ret")
            if payload.get("ret") is not None
            else base.get("ret")
        )
        try:
            ret = int(raw_ret) if raw_ret not in {None, ""} else 0
        except (TypeError, ValueError):
            ret = 0
        if ret != 0 or current_page == page:
            return payload

        has_more = bool(
            payload.get("continue_flag")
            or payload.get("is_continue")
            or payload.get("has_more")
        )
        buffer = str(payload.get("buffer") or "")
        if not has_more:
            return {
                **payload,
                "elected_comment": [],
                "comment": [],
                "comments": [],
                "continue_flag": 0,
                "is_continue": 0,
                "has_more": False,
                "buffer": "",
            }
        if not buffer:
            # Older variants honor offset without returning a cursor. Continue
            # with the numbered offset instead of fabricating a token.
            continue
    return payload


def response_json(response: Any, endpoint_name: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        text = str(getattr(response, "text", "") or "")
        lowered = text.lower()
        if (
            "<title>verify</title>" in lowered
            or "wappoc_appmsgcaptcha" in lowered
            or "访问过于频繁" in text
        ):
            raise PlatformBlockedError(f"{endpoint_name}触发微信访问验证") from exc
        raise PlatformCrawlerError(f"{endpoint_name}返回非 JSON") from exc
    if not isinstance(payload, dict):
        raise PlatformCrawlerError(f"{endpoint_name}返回结构异常")
    return payload


async def fetch_official_stats(
    crawler: PlatformCrawlerContext,
    metadata: dict[str, str],
) -> EngagementStats | None:
    mid = metadata.get("mid", "")
    idx = metadata.get("idx", "1")
    publish_date = metadata_publish_date(metadata)
    if not mid or not publish_date:
        return None
    payload = await _post_official_api(
        crawler,
        "/datacube/getarticletotaldetail",
        {"begin_date": publish_date, "end_date": publish_date},
    )
    if payload is None:
        return None
    target = f"{mid}_{idx}"
    for item in payload.get("list") or []:
        if not isinstance(item, dict) or str(item.get("msgid") or "") != target:
            continue
        details = [
            detail
            for detail in (item.get("detail_list") or [])
            if isinstance(detail, dict)
        ]
        if not details:
            return None
        latest = max(details, key=lambda value: str(value.get("stat_date") or ""))
        return EngagementStats(
            views=to_int(latest.get("read_user")),
            likes=to_int(latest.get("like_user")),
            comments=to_int(latest.get("comment_count")),
            shares=to_int(latest.get("share_user")),
            favorites=to_int(latest.get("collection_user")),
            recommendations=to_int(latest.get("zaikan_user")),
        )
    return None


async def fetch_official_comments(
    crawler: PlatformCrawlerContext,
    metadata: dict[str, str],
    *,
    page: int,
    limit: int,
) -> dict[str, Any] | None:
    mid = metadata.get("mid", "")
    if not mid.isdigit():
        return None
    try:
        index = max(0, int(metadata.get("idx", "1")) - 1)
    except ValueError:
        index = 0
    page_size = min(limit, COMMENT_PAGE_SIZE)
    return await _post_official_api(
        crawler,
        "/cgi-bin/comment/list",
        {
            "msg_data_id": int(mid),
            "index": index,
            "begin": (page - 1) * page_size,
            "count": page_size,
            "type": 0,
        },
    )


async def _post_official_api(
    crawler: PlatformCrawlerContext,
    path: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    token = await crawler._wechat_mp_access_token()
    if not token:
        return None
    for attempt in range(2):
        payload = await crawler._post_json(
            f"https://api.weixin.qq.com{path}",
            params={"access_token": token},
            headers={"Content-Type": "application/json"},
            json_body=body,
            force_direct=True,
        )
        errcode = to_int(payload.get("errcode")) or 0
        if errcode == 0:
            return payload
        if errcode in {40001, 40014, 42001} and attempt == 0:
            crawler._invalidate_wechat_mp_access_token()
            token = await crawler._wechat_mp_access_token()
            if token:
                continue
        message = str(payload.get("errmsg") or "unknown error")
        if errcode in {40164, 45009, 45011}:
            raise PlatformBlockedError(f"公众号官方 API {errcode}: {message}")
        raise PlatformCrawlerError(f"公众号官方 API {errcode}: {message}")
    return None


def metadata_publish_date(metadata: dict[str, str]) -> str:
    raw = metadata.get("ct") or metadata.get("publish_time") or ""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return ""
    china_tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(value, tz=china_tz).date().isoformat()


def parse_metadata(text: str, url: str) -> dict[str, str]:
    """Read article constants without matching unrelated SDK placeholders."""

    keys = (
        "show_comment",
        "comment_id",
        "bizuin",
        "__biz",
        "mid",
        "idx",
        "sn",
        "appmsg_token",
        "uin",
        "key",
        "pass_ticket",
        "wxtoken",
        "devicetype",
        "clientversion",
        "ct",
        "publish_time",
        "is_only_read",
    )
    values: dict[str, str] = {}
    cgi_data = extract_js_object(text, "window.cgiDataNew")
    if cgi_data:
        for key in (
            "show_comment",
            "comment_id",
            "bizuin",
            "__biz",
            "mid",
            "idx",
            "sn",
            "wxtoken",
            "devicetype",
            "clientversion",
            "ct",
            "publish_time",
            "is_only_read",
        ):
            value = extract_js_scalar(cgi_data, key, separator=":")
            if value != "":
                values[key] = value

    # Some page versions expose session fields and timestamps as standalone
    # constants. Only accept quoted strings or primitive literals, never
    # expressions such as ``mid = this.mid`` or template placeholders.
    session_keys = {"appmsg_token", "uin", "key", "pass_ticket"}
    for key in keys:
        if values.get(key):
            continue
        value = (
            extract_window_scalar(
                text,
                key,
                allow_var=key == "appmsg_token",
            )
            if key in session_keys
            else extract_js_scalar(text, key, separator="=")
        )
        if value != "":
            values[key] = value

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query_keys = {
        "bizuin": "__biz",
        "__biz": "__biz",
        "mid": "mid",
        "idx": "idx",
        "sn": "sn",
        "appmsg_token": "appmsg_token",
        "uin": "uin",
        "key": "key",
        "pass_ticket": "pass_ticket",
        "wxtoken": "wxtoken",
        "devicetype": "devicetype",
        "clientversion": "clientversion",
    }
    for target_key, query_key in query_keys.items():
        query_value = str((query.get(query_key) or [""])[0]).strip()
        if query_value and not values.get(target_key):
            values[target_key] = html.unescape(query_value)

    values["biz"] = values.get("bizuin") or values.get("__biz") or ""
    values.setdefault("idx", "1")
    return values


def extract_js_object(text: str, marker: str) -> str:
    """Return one balanced JavaScript object assigned after ``marker``."""

    marker_at = text.find(marker)
    if marker_at < 0:
        return ""
    start = text.find("{", marker_at + len(marker))
    if start < 0:
        return ""
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            continue
        if character in {"'", '"', "`"}:
            quote = character
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def extract_js_scalar(text: str, key: str, *, separator: str) -> str:
    escaped_key = re.escape(key)
    name = rf"(?:[\"']{escaped_key}[\"']|(?<![\w]){escaped_key}(?![\w]))"
    pattern = re.compile(
        name
        + rf"\s*{re.escape(separator)}\s*"
        + r"(?:[\"'](?P<quoted>[^\"']*)[\"']|"
        + r"(?P<bare>-?[0-9]+|true|false|null))",
        re.I,
    )
    match = pattern.search(text)
    if match is None:
        return ""
    value = match.group("quoted")
    if value is None:
        value = match.group("bare") or ""
    if value.lower() == "null":
        return ""
    return html.unescape(value.strip())


def extract_window_scalar(text: str, key: str, *, allow_var: bool = False) -> str:
    owner = r"(?:window\.|\bvar\s+)" if allow_var else r"window\."
    pattern = re.compile(
        owner + rf"{re.escape(key)}\s*=\s*"
        r"[\"'](?P<value>[^\"']*)[\"']",
        re.I,
    )
    match = pattern.search(text)
    if match is None:
        return ""
    return html.unescape(match.group("value").strip())


def parse_cookie_metadata(cookie: str) -> dict[str, str]:
    """Extract only named session parameters; never expose their values."""

    pairs: dict[str, str] = {}
    for segment in cookie.split(";"):
        name, separator, value = segment.strip().partition("=")
        if not separator or not name:
            continue
        pairs[name.lower()] = value.strip()
    aliases = {
        "appmsg_token": "appmsg_token",
        "pass_ticket": "pass_ticket",
        "key": "key",
        "uin": "uin",
        "wxuin": "uin",
        "wxtoken": "wxtoken",
        "wxtokenkey": "wxtoken",
        "devicetype": "devicetype",
        "clientversion": "clientversion",
        "version": "clientversion",
    }
    values: dict[str, str] = {}
    for cookie_name, metadata_name in aliases.items():
        value = pairs.get(cookie_name, "")
        if value and not values.get(metadata_name):
            values[metadata_name] = unquote(value)
    return values


def merge_cookie_metadata(metadata: dict[str, str], cookie: str) -> dict[str, str]:
    merged = dict(metadata)
    for key, value in parse_cookie_metadata(cookie).items():
        if value and not merged.get(key):
            merged[key] = value
    return merged


def cookie_capability_suffix(metadata: dict[str, str], cookie: str) -> str:
    if not cookie:
        return "；未配置 WECHAT_ARTICLE_COOKIE"
    required = ("uin", "key", "pass_ticket", "appmsg_token")
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        return "；Cookie 缺少会话字段: " + ", ".join(missing)
    return "；Cookie 会话字段齐全但可能已过期或不属于该文章会话"


def parse_stats(text: str) -> EngagementStats:
    decoded = html.unescape(text).replace(r"\u0022", '"').replace(r'\"', '"')
    values: dict[str, int | None] = {}
    for key in (
        "read_num", "read_num_v2", "appmsg_read_num", "readCount",
        "like_num", "like_num_v2", "appmsg_like_num",
        "old_like_num", "old_like_num_v2", "appmsg_old_like_num", "likeCount",
        "comment_count", "commentCount", "share_count", "shareCount",
    ):
        match = re.search(
            rf"(?<![\w])[\"']?{re.escape(key)}[\"']?\s*[:=]\s*[\"']?([0-9]+)",
            decoded,
        )
        if match:
            values[key] = to_int(match.group(1))
    return EngagementStats(
        views=to_int(first_present(
            values,
            "read_num",
            "read_num_v2",
            "appmsg_read_num",
            "readCount",
        )),
        likes=to_int(first_present(
            values,
            "like_num",
            "like_num_v2",
            "appmsg_like_num",
            "old_like_num",
            "old_like_num_v2",
            "appmsg_old_like_num",
            "likeCount",
        )),
        comments=to_int(first_present(values, "comment_count", "commentCount")),
        shares=to_int(first_present(values, "share_count", "shareCount")),
    )


def parse_stats_payload(payload: dict[str, Any]) -> EngagementStats:
    nested = payload.get("appmsgstat") or payload.get("appmsg_stat") or payload
    if isinstance(nested, str):
        try:
            nested = json.loads(html.unescape(nested))
        except (TypeError, ValueError, json.JSONDecodeError):
            nested = {}
    if not isinstance(nested, dict):
        return EngagementStats()
    return EngagementStats(
        views=to_int(first_present(nested, "read_num", "read_count", "readCount")),
        likes=to_int(first_present(
            nested,
            "like_num",
            "like_count",
            "old_like_num",
            "old_like_count",
            "likeCount",
        )),
        comments=to_int(first_present(nested, "comment_count", "commentCount")),
        shares=to_int(first_present(nested, "share_count", "shareCount")),
        favorites=to_int(first_present(nested, "collect_count", "favorite_count")),
    )


def parse_comments(payload: dict[str, Any]) -> list[EngagementComment]:
    candidates = (
        payload.get("elected_comment")
        or payload.get("comment")
        or payload.get("comments")
        or find_nested_value(
            payload,
            {"elected_comment", "comment_list", "comments"},
        )
        or []
    )
    if isinstance(candidates, dict):
        candidates = candidates.get("list") or candidates.get("comment") or []
    result: list[EngagementComment] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        comment_id = str(
            item.get("comment_id")
            or item.get("content_id")
            or item.get("user_comment_id")
            or item.get("id")
            or ""
        )
        text = str(item.get("content") or item.get("text") or "")
        if not comment_id or not text:
            continue
        user = item.get("user") or {}
        if not isinstance(user, dict):
            user = {}
        result.append(EngagementComment(
            comment_id=comment_id,
            author=str(
                item.get("nick_name")
                or item.get("nickname")
                or item.get("user_name")
                or item.get("openid")
                or user.get("nickname")
                or ""
            ),
            text=BeautifulSoup(text, "html.parser").get_text(" ", strip=True),
            created_at=timestamp(
                item.get("create_time") or item.get("createTime") or item.get("created_at")
            ),
            likes=to_int(first_present(item, "like_num", "like_count", "liked_count")),
            replies=(
                to_int(first_present(item, "reply_count", "reply_num"))
                or (1 if isinstance(item.get("reply"), dict)
                    and str(item["reply"].get("content") or "").strip() else None)
            ),
        ))
    return result


def parse_preloaded_comment_payload(text: str) -> dict[str, Any]:
    assignment = re.search(
        r"(?:\bvar\s+|\bwindow\.)?preload_comment_list\s*=\s*",
        text,
        re.I,
    )
    if assignment is None:
        return {}
    tail = text[assignment.end():].lstrip()
    if not tail:
        return {}
    try:
        if tail[0] in {"'", '"'}:
            quote = tail[0]
            string_match = re.match(
                rf"{re.escape(quote)}((?:\\.|[^{re.escape(quote)}])*){re.escape(quote)}",
                tail,
                re.S,
            )
            if string_match is None:
                return {}
            decoded = ast.literal_eval(quote + string_match.group(1) + quote)
            decoded = html.unescape(decoded)
            value = json.loads(decoded) if decoded else {}
        elif tail[0] in {"{", "["}:
            value, _ = json.JSONDecoder().raw_decode(tail)
        else:
            return {}
    except (SyntaxError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    if isinstance(value, list):
        return {"elected_comment": value}
    return value if isinstance(value, dict) else {}


def parse_preloaded_comment_total(text: str) -> int | None:
    match = re.search(
        r"(?:\bvar\s+|\bwindow\.)?"
        r"(?:preload_comment_total_cnt|elected_comment_total_cnt)"
        r"\s*=\s*[\"']?([0-9]+)",
        text,
        re.I,
    )
    return to_int(match.group(1)) if match else None


def find_nested_value(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child is not None and child != "":
                return child
            found = find_nested_value(child, keys)
            if found is not None and found != "":
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_nested_value(child, keys)
            if found is not None and found != "":
                return found
    return None


def _has_stats(stats: EngagementStats) -> bool:
    return any(value is not None for value in stats.model_dump().values())
