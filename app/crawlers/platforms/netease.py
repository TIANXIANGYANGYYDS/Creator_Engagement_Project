"""NetEase article-owned discussion product keys and public comment pagination."""
from __future__ import annotations
import re
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementResult, EngagementStats


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor, include_stats, include_comments):
    canonical = f"https://www.163.com/dy/article/{work_id}.html"
    thread_id = work_id
    try:
        if work_id.startswith("video:"):
            canonical = f"https://c.m.163.com/news/v/{work_id[6:]}.html"
        response = await crawler._get_response(canonical)
        text = response.text
        if work_id.startswith("video:"):
            binding = re.search(r"https://comment\.tie\.163\.com/([A-Z0-9]{16})\.html", text)
            if not binding or not binding[1].startswith(work_id[7:]):
                raise PlatformCrawlerError("网易视频页缺少目标跟帖 ID")
            thread_id = binding[1]
            # The public NetEase web discussion product used by article/video widgets.
            product_key = "a2869674571f77b5a0867c3d71db5856"
        else:
            key = re.search(r'"productKey"\s*:\s*"([a-f0-9]{32})"', text)
            if not key or not re.search(r'"docId"\s*:\s*"' + re.escape(work_id) + '"', text):
                raise PlatformCrawlerError("网易页面未提供目标文章的跟帖配置")
            product_key = key[1]
        endpoint = f"https://comment.api.163.com/api/v1/products/{product_key}/threads/{thread_id}"
        meta = await crawler._get_json(endpoint, headers={"Referer": canonical})
        if meta.get("docId") != thread_id or not meta.get("title"):
            raise PlatformCrawlerError("网易跟帖响应不属于目标文章")
        stats = EngagementStats(comments=to_int(meta.get("cmtCount")))
        comments, following = [], None
        if include_comments:
            size = min(limit, COMMENT_PAGE_SIZE)
            offset = int(comment_cursor) if comment_cursor is not None else (page - 1) * size
            data = await crawler._get_json(endpoint + "/comments/newList", params={
                "offset": offset, "limit": size, "headLimit": 1, "tailLimit": 2, "ibc": "newspc",
            }, headers={"Referer": canonical})
            if not isinstance(data.get("commentIds"), list) or not isinstance(data.get("comments"), dict):
                raise PlatformCrawlerError("网易跟帖列表缺失")
            for ids in data["commentIds"]:
                cid = str(ids).split(",")[-1]  # Earlier IDs are quoted ancestors.
                x = data["comments"].get(cid)
                if not isinstance(x, dict):
                    raise PlatformCrawlerError("网易跟帖正文缺失")
                comments.append(comment(cid, (x.get("user") or {}).get("nickname"), x.get("content"),
                    likes=x.get("vote"), created=x.get("createTime")))
            count = to_int(data.get("newListSize"))
            more = offset + len(comments) < count if count is not None else len(comments) >= size
            require_page(comments, more, offset, offset + len(comments))
            following = str(offset + len(comments)) if more else None
        return EngagementResult(platform="netease", canonical_url=canonical, work_id=work_id,
            coverage="partial", stats=stats, comments=comments, next_cursor=following,
            source="comment.api.163.com", reason="公开跟帖数和跟帖正文；跟帖赞同数不冒充文章点赞数")
    except PlatformBlockedError as exc:
        return result_error("netease", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("netease", url, work_id, "failed", exc)
