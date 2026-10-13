"""Autohome/Car Home publisher counters and reply cursor protocol."""
from __future__ import annotations
import json
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import comment, require_page, json_or_jsonp
from app.models.engagement import EngagementResult, EngagementStats


def page_metadata(text, work_id, *, chejiahao=False):
    soup = BeautifulSoup(text, "html.parser")
    cfg = re.search(r"var\s+ReplyConfig\s*=\s*\{\s*appid:\s*(\d+),\s*id:\s*(\d+)", text)
    if cfg:
        if cfg[2] != work_id:
            raise PlatformCrawlerError("汽车之家评论配置不是目标作品")
        stats = EngagementStats()
        like = soup.select_one('[data-like-id="' + work_id + '"]')
        if like:
            value = like.select_one('[data-like-count]')
            if value:
                stats.likes = to_int(value.get("data-like-count"))
        return cfg[1], stats
    node = soup.find("script", id="__NEXT_DATA__")
    state = json.loads(node.string) if node and node.string else {}
    props = (state.get("props") or {}).get("pageProps") or {}
    # Current desktop Car Home pages nest the target under pageProps.article.
    target = props.get("article") if chejiahao and isinstance(props.get("article"), dict) else props
    article = target.get("articleContent") or {}
    if target.get("idString") is not None and str(target["idString"]) != work_id:
        raise PlatformCrawlerError("汽车之家页面不是目标作品")
    if str(target.get("idString")) != work_id or not article:
        return None
    if article.get("recycled") is True:
        raise PlatformCrawlerError("汽车之家目标作品已删除")
    # Car Home's own PC client uses appid=21; news articles use appid=1.
    return "21" if chejiahao else "1", EngagementStats(
        views=to_int(article.get("clickCount")), comments=to_int(article.get("replyCount")))


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor, include_stats, include_comments):
    try:
        host = urlparse(url).hostname
        chejiahao = host in {"chejiahao.m.autohome.com.cn", "chejiahao.autohome.com.cn"}
        candidates = [url.replace("http://", "https://", 1)]
        if chejiahao:
            alternate = (f"https://chejiahao.autohome.com.cn/info/{work_id}" if host == "chejiahao.m.autohome.com.cn"
                         else f"https://chejiahao.m.autohome.com.cn/share/info/{work_id}")
            candidates.append(alternate)
        metadata = None
        for index, candidate in enumerate(candidates):
            try:
                response = await crawler._get_response(candidate)
                metadata = page_metadata(response.text, work_id, chejiahao=chejiahao)
                if metadata is not None:
                    break
            except PlatformCrawlerError:
                if index + 1 == len(candidates):
                    raise
        if metadata is None:
            if chejiahao:
                raise PlatformCrawlerError("车家号移动与桌面页面均未返回目标文章配置")
            return result_error("autohome", url, work_id, "unsupported", "该页面未开放文章评论配置（经销商促销/车型报价页不视为车家号文章）").model_copy(update={"retryable":False})
        appid, stats = metadata
        size = min(limit, COMMENT_PAGE_SIZE)
        cursor = comment_cursor or "0"
        # Cursor is opaque. Direct page=N requests must walk preceding pages.
        steps = 1 if comment_cursor is not None else page
        data = None
        for step in range(steps):
            payload = await crawler._get_json("https://reply.autohome.com.cn/api/comments/showmore.json", params={
                "_appid":"m", "appid":appid, "objid":work_id, "next":cursor,
                "pagesize":size, "ordertype":0, "usechat":1,
            }, headers={"Referer":url})
            data = payload.get("result") or {}
            if payload.get("returncode") != 0 or "hasmore" not in data or "list" not in data:
                raise PlatformCrawlerError("汽车之家评论接口未提供有效分页")
            items = data.get("list") or []
            more = data["hasmore"]
            require_page(items, more, cursor, data.get("next"))
            cursor = str(data.get("next"))
            if not more:
                if step + 1 < steps:
                    items = []
                break
        counts_response = await crawler._get_response( "https://reply.autohome.com.cn/api/getData_ReplyCounts.ashx",
            params={"appid":appid,"objids":work_id,"dateType":"jsonp","callback":"collector"})
        # This legacy endpoint emits a JS object with a single-quoted key.
        # Convert only that documented key; never evaluate remote scripts.
        counts = json_or_jsonp(counts_response.text.replace("'commentlist'", '\"commentlist\"'))
        for x in counts.get("commentlist", []):
            if str(x.get("objid")) == work_id:
                stats.comments = to_int(x.get("replycountall"))
        comments = [comment(x.get("ReplyId"), x.get("RMemberName"), x.get("RContent"),
            likes=x.get("RUp"), replies=x.get("FReplyCount"),
            created=(re.search(r"/Date\((\d+)", x.get("RReplyDate") or "")[1] if re.search(r"/Date\((\d+)", x.get("RReplyDate") or "") else None)) for x in items] if include_comments else []
        return EngagementResult(platform="autohome", canonical_url=url, work_id=work_id,
            coverage="partial", stats=stats, comments=comments,
            next_cursor=cursor if more and include_comments else None,
            source="target-page + reply.autohome.com.cn", reason="公开内容的互动字段和一级评论；未公开字段保留 null")
    except PlatformBlockedError as exc:
        return result_error("autohome", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("autohome", url, work_id, "failed", exc)
