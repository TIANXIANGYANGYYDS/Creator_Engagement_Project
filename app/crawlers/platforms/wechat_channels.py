"""WeChat Channels public counters and authorized-client comments.

WeChat Channels is independent from Official Account articles.  Its public
preview page exposes display-formatted counters through one JSON request.
Comment bodies can optionally be read through a separately authorized desktop
WeChat sidecar; the API process never receives the WeChat account session.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.base import PlatformCrawlerContext
from app.crawlers.platforms.common import (
    first_present,
    result_error,
    timestamp,
    to_int,
)
from app.crawlers.platforms.registry import extract_wechat_channels_mobile_feed_id
from app.models.engagement import EngagementComment, EngagementResult, EngagementStats


FEED_INFO_URL = (
    "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
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
    try:
        bridge_issue = ""
        midu_issue = ""
        mobile_feed_id = extract_wechat_channels_mobile_feed_id(url)
        if include_comments:
            try:
                bridge_payload = await crawler._wechat_channels_bridge_comments(
                    url=url,
                    page=page,
                    limit=limit,
                )
            except (PlatformBlockedError, PlatformCrawlerError) as exc:
                bridge_payload = None
                bridge_issue = str(exc)
            if bridge_payload is not None:
                comments = parse_bridge_comments(bridge_payload.get("comments"))
                total = _to_int(bridge_payload.get("total_comments"))
                next_marker = str(bridge_payload.get("next_marker") or "").strip()
                exhausted = bool(bridge_payload.get("exhausted"))
                if comments or total == 0 or exhausted:
                    return EngagementResult(
                        platform="wechat_channels",
                        canonical_url=url,
                        work_id=work_id,
                        coverage="complete" if exhausted or total == 0 else "partial",
                        reason=(
                            "已授权微信客户端会话返回视频号一级评论；返回内容受平台公开、"
                            "删除和折叠规则影响"
                        ),
                        source=str(
                            bridge_payload.get("source")
                            or "wx_channel/finderGetCommentList"
                        ),
                        stats=EngagementStats(comments=total),
                        comments=comments[:limit],
                        next_cursor=next_marker or None,
                    )
                bridge_issue = "视频号会话桥成功响应但没有返回目标评论正文"

        if mobile_feed_id and include_stats:
            try:
                bridge_payload = await crawler._wechat_channels_bridge_interactions(
                    url=url
                )
            except (PlatformBlockedError, PlatformCrawlerError) as exc:
                bridge_payload = None
                bridge_issue = str(exc)
            if bridge_payload is not None:
                raw_stats = bridge_payload.get("stats") or {}
                stats = EngagementStats.model_validate(raw_stats)
                if any(value is not None for value in stats.model_dump().values()):
                    return EngagementResult(
                        platform="wechat_channels",
                        canonical_url=url,
                        work_id=work_id,
                        coverage="partial",
                        reason=(
                            "已授权微信客户端会话返回视频号精确互动量；平台未提供的字段为空"
                        ),
                        source=str(
                            bridge_payload.get("source")
                            or "wx_channel/finderGetCommentDetail"
                        ),
                        stats=stats,
                    )
                bridge_issue = "视频号会话桥详情响应没有返回互动量"

            try:
                midu_payload = await crawler._wechat_channels_midu_interactions(
                    url=url
                )
            except (PlatformBlockedError, PlatformCrawlerError) as exc:
                midu_payload = None
                midu_issue = str(exc)
            if midu_payload is not None:
                raw_stats = midu_payload.get("stats") or {}
                stats = EngagementStats.model_validate(raw_stats)
                if any(value is not None for value in stats.model_dump().values()):
                    return EngagementResult(
                        platform="wechat_channels",
                        canonical_url=url,
                        work_id=work_id,
                        coverage="partial",
                        reason=(
                            "无微信账号模式通过蜜度已收录数据返回视频号互动量；"
                            "平台或供应端未提供的字段为空"
                        ),
                        source=str(
                            midu_payload.get("source")
                            or "midu/history_data+idata/md/engagement/query"
                        ),
                        stats=stats,
                    )
                midu_issue = "蜜度视频号数据源没有返回互动量"

        if mobile_feed_id:
            operation = "评论正文" if include_comments else "互动量"
            return result_error(
                "wechat_channels",
                url,
                work_id,
                "unsupported",
                (
                    "该视频号客户端跳转链接无法通过公开预览接口读取"
                    f"{operation}；互动量可配置 WECHAT_CHANNELS_MIDU_URL，"
                    "评论正文仍需要支持 encrypted_object_id 的授权微信客户端侧车"
                    + (f"；会话桥: {bridge_issue}" if bridge_issue else "")
                    + (f"；蜜度: {midu_issue}" if midu_issue else "")
                ),
            )

        body, referer, page_url = build_feed_request(url)

        payload = await crawler._post_json(
            FEED_INFO_URL,
            params={
                "_rid": f"{int(time.time()):x}-{secrets.token_hex(4)}",
                "_pageUrl": page_url,
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://channels.weixin.qq.com",
                "Referer": referer,
            },
            json_body=body,
            force_direct=True,
        )
        err_code = _to_int(payload.get("errCode")) or 0
        if err_code != 0:
            raise PlatformBlockedError(
                f"视频号公开预览接口返回 {err_code}: {payload.get('errMsg') or 'unknown'}"
            )
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise PlatformCrawlerError("视频号公开预览接口缺少 data")
        error = data.get("errMsg") or {}
        if isinstance(error, dict) and (
            (_to_int(error.get("type")) or 0) != 0
            or str(error.get("title") or "").strip()
            or str(error.get("content") or "").strip()
        ):
            detail = str(error.get("content") or error.get("title") or "内容不可用")
            return result_error("wechat_channels", url, work_id, "unsupported", detail)

        feed_info = data.get("feedInfo") or {}
        if not isinstance(feed_info, dict):
            feed_info = {}
        stats = parse_stats(feed_info)
        has_feed = bool(
            feed_info
            and (
                str(feed_info.get("description") or "").strip()
                or any(value is not None for value in stats.model_dump().values())
            )
        )
        if not has_feed:
            return result_error(
                "wechat_channels",
                url,
                work_id,
                "unsupported",
                "视频号分享链接已失效或公开预览接口未返回目标内容",
            )

        if include_comments:
            return EngagementResult(
                platform="wechat_channels",
                canonical_url=url,
                work_id=work_id,
                coverage="unsupported",
                reason=(
                    "视频号公开预览只下发评论总数，不下发评论正文；正文由微信客户端 "
                    "finderGetCommentList 会话接口提供"
                    + (
                        f"；会话桥: {bridge_issue}"
                        if bridge_issue
                        else "；未配置 WECHAT_CHANNELS_BRIDGE_URL"
                    )
                ),
                source="finder-preview/api/feed/get_feed_info",
                stats=EngagementStats(comments=stats.comments),
            )

        return EngagementResult(
            platform="wechat_channels",
            canonical_url=url,
            work_id=work_id,
            coverage="partial",
            reason=(
                "视频号公开分享页互动量可匿名获取；“万/亿/+”字段按页面展示值换算，"
                "带加号时返回可验证下限"
            ),
            source="finder-preview/api/feed/get_feed_info",
            stats=stats if include_stats else EngagementStats(),
        )
    except PlatformBlockedError as exc:
        return result_error("wechat_channels", url, work_id, "blocked", str(exc))
    except Exception as exc:
        return result_error("wechat_channels", url, work_id, "failed", str(exc))


def build_feed_request(url: str) -> tuple[dict[str, Any], str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    host = parsed.hostname or ""
    short_uri = ""
    if host == "weixin.qq.com":
        match = re.search(r"/sph/([0-9A-Za-z_-]+)(?:/|$)", parsed.path)
        short_uri = match.group(1) if match else ""
    elif parsed.path.startswith("/finder-preview/pages/sph"):
        short_uri = query.get("id", [""])[0]

    if short_uri:
        referer = (
            "https://channels.weixin.qq.com/finder-preview/pages/sph"
            f"?id={short_uri}"
        )
        return (
            {"baseReq": {"generalToken": ""}, "shortUri": short_uri},
            referer,
            "https://channels.weixin.qq.com/finder-preview/pages/sph",
        )

    export_id = query.get("eid", [""])[0]
    token = query.get("token", [""])[0]
    if not export_id:
        raise ValueError("视频号 URL 缺少分享 shortUri 或 eid")
    referer = (
        "https://channels.weixin.qq.com/finder-preview/pages/feed"
        f"?token={token}&eid={export_id}"
    )
    return (
        {"baseReq": {"generalToken": token}, "exportId": export_id},
        referer,
        "https://channels.weixin.qq.com/finder-preview/pages/feed",
    )


def parse_stats(feed_info: dict[str, Any]) -> EngagementStats:
    return EngagementStats(
        likes=parse_formatted_count(feed_info.get("likeCountFmt")),
        comments=parse_formatted_count(feed_info.get("commentCountFmt")),
        shares=parse_formatted_count(feed_info.get("forwardCountFmt")),
        favorites=parse_formatted_count(feed_info.get("favCountFmt")),
    )


def parse_formatted_count(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("+", "")
    if not text or text in {"-", "--"}:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kKwW万亿]?)", text)
    if match is None:
        return None
    multipliers = {
        "": Decimal(1),
        "k": Decimal(1_000),
        "K": Decimal(1_000),
        "w": Decimal(10_000),
        "W": Decimal(10_000),
        "万": Decimal(10_000),
        "亿": Decimal(100_000_000),
    }
    try:
        return int(Decimal(match.group(1)) * multipliers[match.group(2)])
    except (InvalidOperation, ValueError):
        return None


def parse_bridge_comments(value: Any) -> list[EngagementComment]:
    if not isinstance(value, list):
        return []
    comments: list[EngagementComment] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        comment_id = str(
            first_present(item, "commentId", "comment_id", "id") or ""
        ).strip()
        if not comment_id:
            continue
        replies = first_present(
            item,
            "expandCommentCount",
            "subCommentCount",
            "replyCount",
        )
        comments.append(
            EngagementComment(
                comment_id=comment_id,
                author=str(
                    first_present(item, "nickname", "authorName", "username") or ""
                ),
                text=str(first_present(item, "content", "text") or ""),
                created_at=timestamp(
                    first_present(item, "createtime", "createTime", "timestamp")
                ),
                likes=to_int(
                    first_present(item, "likeCount", "likedCount", "like_count")
                ),
                replies=to_int(replies),
            )
        )
    return comments


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
