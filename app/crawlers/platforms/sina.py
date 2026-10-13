"""Sina Kandian/finance public comment threads, validated against article identity."""
from __future__ import annotations

import re
from urllib.parse import urlparse, parse_qs
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import assigned_json, comment, get_jsonp
from app.models.engagement import EngagementResult, EngagementStats

COMMENT_URL = "https://comment5.news.sina.com.cn/page/info"


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor,
                include_stats, include_comments, platform="sina"):
    try:
        nested = parse_qs(urlparse(url).query).get("url")
        if urlparse(url).path == "/article/norm_detail" and nested:
            from app.crawlers.platforms.registry import identify_url
            if identify_url(nested[0])[0] != "sina_finance":
                raise PlatformCrawlerError("新浪财经分享链接目标不匹配")
            url = nested[0]
        channel = "mp"
        newsid = work_id
        if work_id.startswith("comos-") or not re.fullmatch(r"\d+-[0-9a-z]+", work_id):
            response = await crawler._get_response(url)
            text = response.text
            config = assigned_json(text, "commentConfig")
            if config.get("channel") and config.get("newsid"):
                channel, newsid = config["channel"], config["newsid"]
            else:
                match = re.search(r"(?:comment_channel:|channel\s*:\s*['\"])([a-z]+)", text)
                nid = re.search(r"(?:comment_id:|newsid\s*:\s*['\"])([\w-]+)", text)
                if not match or not nid:
                    raise PlatformCrawlerError("新浪页面没有公开评论标识")
                channel, newsid = match[1], nid[1]
            if work_id.startswith("comos-") and newsid != work_id:
                raise PlatformCrawlerError("新浪页面评论标识不是目标文章")
        size = min(limit, COMMENT_PAGE_SIZE)
        payload = await get_jsonp(crawler, COMMENT_URL, params={
            "format": "json", "channel": channel, "newsid": newsid,
            "group": 0, "compress": 0, "ie": "utf-8", "oe": "utf-8",
            "page": page if include_comments else 1, "page_size": size,
        }, headers={"Referer": url})
        data = payload.get("result") or {}
        if (data.get("status") or {}).get("code") != 0:
            raise PlatformCrawlerError("新浪评论接口未成功返回")
        news = data.get("news") or {}
        if str(news.get("newsid")) != newsid or not news.get("title"):
            raise PlatformCrawlerError("新浪评论响应未绑定到目标文章")
        total = to_int((data.get("count") or {}).get("show"))
        if total is None or not isinstance(data.get("cmntlist"), list):
            raise PlatformCrawlerError("新浪评论响应缺少列表或计数")
        items = data["cmntlist"]
        comments = [comment(x.get("mid"), x.get("nick"), x.get("content"),
                    likes=x.get("agree"), replies=x.get("replynum"), created=x.get("time")) for x in items] if include_comments else []
        more = include_comments and len(items) >= size
        return EngagementResult(platform=platform, canonical_url=url, work_id=work_id,
            coverage="partial", stats=EngagementStats(comments=total), comments=comments,
            next_cursor=str(page + 1) if more else None,
            source="comment5.news.sina.com.cn/page/info",
            reason="公开评论数和一级评论；阅读、点赞、收藏、转发未公开，保留 null")
    except PlatformBlockedError as exc:
        return result_error(platform, url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error(platform, url, work_id, "failed", exc)
