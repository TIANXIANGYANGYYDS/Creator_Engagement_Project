"""Baijiahao and Yoojia page metadata plus Baidu's public comment service."""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import assigned_json, comment, get_jsonp, require_page
from app.models.engagement import EngagementResult, EngagementStats

COMMENT_URL = "https://ext.baidu.com/api/comment/v2/comment/list"


def page_metadata(text, platform, work_id=None):
    if platform == "yoojia":
        state = assigned_json(text, "window.__INITIAL_STATE__")
        page_id = ((state.get("route") or {}).get("params") or {}).get("nid")
        if work_id and str(page_id) != work_id:
            raise PlatformCrawlerError("有驾页面不是目标作品")
        detail = state.get("articleDetail") or {}
        if not (detail.get("article") or {}).get("title"):
            detail = state.get("videoDetail") or {}
        article = detail.get("article") or detail.get("videoData") or {}
        tid = article.get("comment_thread_id") or detail.get("commentId")
        if not article.get("title") or not tid:
            raise PlatformCrawlerError("有驾页面缺少目标内容/评论标识")
        return str(tid), EngagementStats(), bool(detail.get("isDelete"))
    state = assigned_json(text, "window.jsonData")
    if work_id and work_id.startswith(("news_", "dt_", "sv_")):
        target_id = (state.get("bsData") or {}).get("nid") or ((state.get("data") or {}).get("pageInfo") or {}).get("nid") or (state.get("curVideoMeta") or {}).get("id")
        if str(target_id).removeprefix("news_").removeprefix("dt_").removeprefix("sv_") != work_id.split("_", 1)[1]:
            raise PlatformCrawlerError("百家号页面不是目标作品")
    video = state.get("curVideoMeta") or {}
    if video.get("thread_id") and video.get("title"):
        return str(video["thread_id"]), EngagementStats(
            views=to_int(state.get("playCount")), likes=to_int((state.get("like") or {}).get("count"))), False
    data = state.get("bsData") or {}
    if data:
        tid = (data.get("comment") or {}).get("tid")
        stats = EngagementStats(likes=to_int((data.get("like") or {}).get("count")), comments=to_int(data.get("commentNum")))
    else:
        data = (state.get("data") or {}).get("pageInfo") or {}
        tid = (data.get("common") or {}).get("thread_id") or ((data.get("extra") or {}).get("comment") or {}).get("tid")
        counts = data.get("interaction_data") or {}
        stats = EngagementStats(likes=to_int((counts.get("praise") or {}).get("praise_num")),
            views=to_int((counts.get("readNum") or {}).get("count")), shares=to_int(counts.get("forwardNum")), comments=to_int(counts.get("commentNum")))
    if not data or not tid:
        raise PlatformCrawlerError("百家号页面缺少目标内容/评论标识")
    return str(tid), stats, False


def parse_comments(items):
    result = []
    for x in items:
        user = x.get("user") or {}
        result.append(comment(x.get("reply_id"), x.get("uname") or user.get("uname") or user.get("user_name"),
            x.get("content"), likes=x.get("like_count"), replies=x.get("reply_count"), created=x.get("create_time")))
    return result


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor,
                include_stats, include_comments, platform="baijiahao"):
    try:
        target_url = url.replace("http://", "https://", 1)
        if platform == "yoojia" and urlparse(url).hostname == "youjia.baidu.com":
            target_url = f"https://www.yoojia.com/article/{work_id}.html"
        response = await crawler._get_response(target_url,
            **({"allow_statuses": {404, 500}} if platform == "yoojia" else {}))
        if platform == "yoojia" and response.status_code == 500:
            # The public page intermittently returns 500 for otherwise valid
            # content. Retry once; deleted/empty pages and blocks are not retried.
            await asyncio.sleep(0.5)
            response = await crawler._get_response(target_url, allow_statuses={404})
        tid, stats, deleted = page_metadata(response.text, platform, work_id)
        size = min(limit, COMMENT_PAGE_SIZE)
        start = int(comment_cursor) if comment_cursor is not None else (page - 1) * size
        payload = await get_jsonp(crawler, COMMENT_URL, params={
            "thread_id": tid, "reply_id": "", "start": start, "num": size,
            "appid": "22862035", "order": 12, "inner_order": 9,
            "use_list": 1, "source": "feed", "out_reply_id": "", "use_uk": 1,
        }, headers={"Referer": url})
        data = payload.get("ret") or {}
        if str(payload.get("errno")) != "0" or str(data.get("thread_id")) != tid:
            raise PlatformCrawlerError("百度评论接口失败或返回了其他文章")
        if not isinstance(data.get("list"), list) or "is_over" not in data:
            raise PlatformCrawlerError("百度评论响应缺少列表/分页标记")
        total = to_int(data.get("comment_count"))
        if total is not None:
            stats.comments = total
        items = data["list"]
        more = not data["is_over"]
        following = start + len(items)
        require_page(items, more, start, following)
        return EngagementResult(platform=platform, canonical_url=url, work_id=work_id,
            coverage="partial", stats=stats, comments=parse_comments(items) if include_comments else [],
            next_cursor=str(following) if more and include_comments else None,
            source="target-page metadata + ext.baidu.com/api/comment/v2/comment/list",
            reason="公开一级评论；未公开的互动字段保留 null" + ("；有驾将页面标为已删除，评论来自该页保留的目标 thread_id" if deleted else ""))
    except PlatformBlockedError as exc:
        return result_error(platform, url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error(platform, url, work_id, "failed", exc)
