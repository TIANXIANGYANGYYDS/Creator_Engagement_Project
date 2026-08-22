"""WeChat article metadata, counters, and session-bound comment flow."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import html
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

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
    include_stats: bool,
    include_comments: bool,
) -> EngagementResult:
    cookie = crawler._platform_cookie("wechat")
    official_issue = ""
    official_stats: EngagementStats | None = None
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
        metadata = parse_metadata(text, url)
        stats = parse_stats(text) if include_stats else EngagementStats()
        if include_stats and not _has_stats(stats) and cookie:
            try:
                ext_payload = await fetch_appmsgext(crawler, url, metadata, cookie)
                stats = parse_stats_payload(ext_payload)
            except PlatformCrawlerError:
                pass
        if include_stats:
            try:
                official_stats = await fetch_official_stats(crawler, metadata)
                if official_stats is not None:
                    stats = official_stats
            except PlatformCrawlerError as exc:
                official_issue = str(exc)

        if not include_comments:
            has_stats = _has_stats(stats)
            official = official_stats is not None
            return EngagementResult(
                platform="wechat",
                canonical_url=url,
                work_id=work_id,
                coverage="partial" if has_stats else "unsupported",
                reason=(
                    "自有公众号官方接口返回阅读人数、赞、分享、留言和收藏数据"
                    if official
                    else (
                        "公众号文章页面返回了当前会话可见互动量"
                        if has_stats
                        else (
                            "公众号匿名文章页未下发阅读/点赞统计；任意第三方文章仍需微信文章短时会话，"
                            "自有公众号可配置官方 API 授权"
                            + (f"；官方接口尝试失败: {official_issue}" if official_issue else "")
                        )
                    )
                ),
                source=(
                    "api.weixin.qq.com/datacube/getarticletotaldetail"
                    if official
                    else "mp article appmsgstat/cgiDataNew"
                ),
                stats=stats,
            )

        preloaded_payload = parse_preloaded_comment_payload(text)
        preloaded_comments = parse_comments(preloaded_payload)
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

        if metadata.get("show_comment") == "0":
            return EngagementResult(
                platform="wechat",
                canonical_url=url,
                work_id=work_id,
                coverage="complete",
                reason="该公众号文章由作者关闭评论，登录账号也不会产生可抓取评论",
                source="cgiDataNew.show_comment",
                stats=stats,
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
            return result_error(
                "wechat",
                url,
                work_id,
                "unsupported",
                "公众号任意第三方文章评论需要微信文章短时会话；自有公众号可用官方留言授权"
                + (f"；官方接口尝试失败: {official_issue}" if official_issue else ""),
            )
        params = {
            "action": "getcomment",
            "__biz": metadata["biz"],
            "mid": metadata["mid"],
            "idx": metadata["idx"],
            "comment_id": metadata["comment_id"],
            "offset": str((page - 1) * min(limit, COMMENT_PAGE_SIZE)),
            "limit": str(min(limit, COMMENT_PAGE_SIZE)),
            "appmsg_token": metadata.get("appmsg_token", ""),
            "uin": metadata.get("uin", ""),
            "key": metadata.get("key", ""),
            "pass_ticket": metadata.get("pass_ticket", ""),
            "wxtoken": "777",
            "devicetype": metadata.get("devicetype", "Windows 10 x64"),
            "clientversion": metadata.get("clientversion", "63090c11"),
            "f": "json",
        }
        payload = await crawler._get_json(
            "https://mp.weixin.qq.com/mp/appmsg_comment",
            params=params,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": url,
                "Cookie": cookie,
            },
        )
        base = payload.get("base_resp") or {}
        raw_ret = payload.get("ret") if payload.get("ret") is not None else base.get("ret")
        try:
            ret = int(raw_ret) if raw_ret not in {None, ""} else None
        except (TypeError, ValueError):
            ret = None
        errmsg = str(payload.get("errmsg") or base.get("errmsg") or "")
        if ret not in {None, 0}:
            coverage: EngagementCoverage = (
                "blocked" if ret == -3 or "session" in errmsg.lower() else "failed"
            )
            return result_error(
                "wechat", url, work_id, coverage, f"公众号评论接口: {errmsg or ret}"
            )
        comments = parse_comments(payload)
        total = to_int(
            payload.get("total_count")
            or payload.get("total")
            or find_nested_value(payload, {"total_count", "total"})
        )
        has_more = bool(payload.get("is_continue") or payload.get("has_more"))
        return EngagementResult(
            platform="wechat",
            canonical_url=url,
            work_id=work_id,
            coverage="partial",
            reason="公众号评论依赖当前微信文章会话；仅返回指定公开页",
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
        "ct": "0",
        "is_need_ad": "0",
        "is_need_reward": "0",
        "comment_id": metadata.get("comment_id", ""),
        "is_only_read": "0",
        "appmsg_token": metadata.get("appmsg_token", ""),
    }
    return await crawler._post_json(
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
    values: dict[str, str] = {}
    for key in (
        "show_comment", "comment_id", "bizuin", "__biz", "mid", "idx", "sn",
        "appmsg_token", "uin", "key", "pass_ticket", "devicetype", "clientversion",
        "ct", "publish_time",
    ):
        match = re.search(
            rf"(?:[\"']{re.escape(key)}[\"']|\b{re.escape(key)})\s*[:=]\s*[\"']?([^,;\"'\s}}]+)",
            text,
            re.I,
        )
        if match:
            values[key] = html.unescape(match.group(1))
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if not values.get("biz"):
        values["biz"] = values.get("bizuin") or query.get("__biz", [""])[0]
    values.setdefault("sn", query.get("sn", [""])[0])
    values.setdefault("mid", query.get("mid", [""])[0])
    values.setdefault("idx", query.get("idx", ["1"])[0])
    return values


def parse_stats(text: str) -> EngagementStats:
    decoded = html.unescape(text).replace(r"\u0022", '"').replace(r'\"', '"')
    values: dict[str, int | None] = {}
    for key in (
        "read_num", "read_num_v2", "readCount",
        "like_num", "like_num_v2", "old_like_num", "old_like_num_v2", "likeCount",
        "comment_count", "commentCount", "share_count", "shareCount",
    ):
        match = re.search(
            rf"(?<![\w])[\"']?{re.escape(key)}[\"']?\s*[:=]\s*[\"']?([0-9]+)",
            decoded,
        )
        if match:
            values[key] = to_int(match.group(1))
    return EngagementStats(
        views=to_int(first_present(values, "read_num", "read_num_v2", "readCount")),
        likes=to_int(first_present(
            values,
            "like_num",
            "like_num_v2",
            "old_like_num",
            "old_like_num_v2",
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
