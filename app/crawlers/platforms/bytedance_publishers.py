"""Micro Toutiao and Xigua: target SSR fields and their own public comment APIs."""
from __future__ import annotations
import json
from urllib.parse import unquote
from bs4 import BeautifulSoup
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.toutiao import fetch_comment_page, parse_comments, public_read_count
from app.crawlers.platforms.web_sources_common import assigned_json, require_page
from app.models.engagement import EngagementResult, EngagementStats


def canonical_url(platform, work_id):
    return (f"https://www.toutiao.com/w/{work_id}/" if platform == "weitoutiao"
            else f"https://m.ixigua.com/video/{work_id}")


def parse_stats(text, platform, work_id):
    if platform == "weitoutiao":
        node = BeautifulSoup(text, "html.parser").find("script", id="RENDER_DATA")
        if not node or not node.string:
            raise PlatformBlockedError("微头条未返回 RENDER_DATA，可能需要浏览器验证")
        data = json.loads(unquote(node.string)).get("data") or {}
        cell = data.get("itemCell") or {}
        gid = data.get("groupId") or (cell.get("articleBase") or {}).get("groupID")
        if str(gid) != work_id:
            raise PlatformCrawlerError("微头条 SSR 不是目标作品")
        counts = cell.get("itemCounter") or {}
        return EngagementStats(views=public_read_count(counts.get("readCount")), likes=to_int(counts.get("diggCount") if counts.get("diggCount") is not None else (data.get("likeData") or {}).get("count")),
            comments=to_int(counts.get("commentCount")), shares=to_int(counts.get("shareCount")),
            favorites=to_int(counts.get("repinCount")), reposts=to_int(counts.get("forwardCount")))
    state = assigned_json(text, "window._SSR_DATA")
    detail = (((state.get("data") or {}).get("storeState") or {}).get("detail") or {})
    video = (detail.get("videoData") or {}).get("result") or {}
    if str(video.get("gid")) != work_id:
        raise PlatformCrawlerError("西瓜视频未返回目标视频数据，可能已删除或访问受限")
    return EngagementStats(views=to_int(video.get("play_count")), comments=to_int(video.get("comment_count")))


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor,
                include_stats, include_comments, platform="weitoutiao"):
    canonical = canonical_url(platform, work_id)
    try:
        stats = EngagementStats()
        # Xigua's mobile page also confirms that the video still exists.
        if include_stats or platform == "ixigua":
            try:
                response = await crawler._get_response(canonical)
                stats = parse_stats(response.text, platform, work_id)
            except PlatformCrawlerError:
                if platform != "weitoutiao":
                    raise
        comments, following = [], None
        if platform == "weitoutiao" and (include_comments or stats.comments is None):
            comments, stats.comments, following = await fetch_comment_page(
                crawler, work_id, limit, page=page, comment_cursor=comment_cursor)
        elif platform != "weitoutiao" and (include_comments or stats.comments is None):
            size = min(limit, COMMENT_PAGE_SIZE)
            offset = int(comment_cursor) if comment_cursor is not None else (page - 1) * size
            endpoint = "https://m.ixigua.com/article/v1/tab_comments/"
            params = {"aid":3586, "group_id":work_id,"offset":offset,"count":size,"tab_index":1}
            payload = await crawler._get_json(endpoint, params=params, headers={"Referer":canonical})
            group = payload.get("group") or {}
            if payload.get("err_no") != 0 or not isinstance(payload.get("data"), list) or "has_more" not in payload:
                raise PlatformCrawlerError("评论接口未返回有效公开列表")
            if group and str(group.get("group_id")) != work_id:
                raise PlatformCrawlerError("评论列表不是目标作品")
            stats.comments = to_int(payload.get("total_number"))
            comments = parse_comments(payload["data"])
            next_offset = to_int(payload.get("offset"))
            require_page(comments, payload["has_more"], offset, next_offset)
            following = str(next_offset) if payload["has_more"] else None
        return EngagementResult(platform=platform, canonical_url=canonical, work_id=work_id,
            coverage="partial", stats=stats, comments=comments if include_comments else [], next_cursor=following if include_comments else None,
            source="target SSR + tab_comments", reason="目标页面公开互动字段和一级评论；缺失字段保留 null")
    except PlatformBlockedError as exc:
        return result_error(platform, url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error(platform, url, work_id, "failed", exc)
