"""Autohome/Car Home publisher counters and reply cursor protocol."""
from __future__ import annotations
import json
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import comment, require_page, json_or_jsonp
from app.crawlers.platforms.public_news import PublicDataUnavailable
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
        if host == "club.autohome.com.cn":
            return await fetch_forum(crawler, url, work_id, page, comment_cursor, include_comments)
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
    except PublicDataUnavailable as exc:
        return result_error("autohome", url, work_id, "unsupported", exc).model_copy(update={"retryable": False})
    except PlatformBlockedError as exc:
        return result_error("autohome", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("autohome", url, work_id, "failed", exc)


async def fetch_forum(crawler, url, wid, page, cursor, include_comments):
    current = int(cursor) if cursor else (page if include_comments else 1)
    target_url = re.sub(r"-\d+\.html(?:[?#].*)?$", f"-{current}.html", url)
    response = await crawler._get_response(target_url)
    match = re.search(r"window\[['\"]__TOPICINFO__['\"]\]\s*=\s*\{(.*?)\};", response.text, re.S)
    config = match[1] if match else ""
    def number(key):
        found = re.search(r"\b" + re.escape(key) + r"\s*:\s*(\d+)", config)
        return int(found[1]) if found else None
    if str(number("topicId")) != wid or number("currentPageIdx") != current:
        raise PlatformCrawlerError("汽车之家论坛未返回目标帖子或请求页码")
    if number("topicDelete"):
        raise PublicDataUnavailable("汽车之家论坛目标帖子已删除")
    payload = await crawler._get_json("https://club.autohome.com.cn/frontapi/getclicksandreplys",
        params={"topicids": wid}, headers={"Referer": target_url})
    counts = next((x for x in payload.get("result", []) if str(x.get("topicid")) == wid), None)
    if payload.get("returncode") != 0 or counts is None:
        raise PlatformCrawlerError("汽车之家论坛未返回目标帖子计数")
    stats = EngagementStats(views=to_int(counts.get("views")), comments=to_int(counts.get("allreplys")))
    result, following = [], None
    if include_comments:
        soup = BeautifulSoup(response.text, "html.parser")
        container = soup.select_one("#js-reply-list-container")
        if container is None:
            raise PlatformCrawlerError("汽车之家论坛一级回复容器缺失")
        for root in container.select(":scope > li[data-reply-id][data-status]"):
            # The official client reveals status 20 only to moderators, and
            # status 22 only to moderators/the author. Do not expose their HTML.
            if root["data-status"] in {"20", "22"}:
                continue
            body = root.select_one(".reply-main > .reply-detail")
            if body is None:
                raise PlatformCrawlerError("汽车之家论坛一级回复正文缺失")
            for control in body.select(".reply-detail-oper-btn"):
                control.decompose()
            date = root.select_one(".reply-top .reply-static-text strong:last-child")
            result.append(comment(root["data-reply-id"], "", body.get_text(" ", strip=True),
                created=date.get_text(strip=True) if date else None))
        if current == 1 and not result and stats.comments != 0:
            raise PublicDataUnavailable("汽车之家论坛计数包含非公开回复，当前没有可访问的一级评论")
        next_path = re.sub(r"-\d+\.html$", f"-{current + 1}.html", urlparse(target_url).path)
        for link in soup.select('a[href]'):
            if urlparse(link["href"]).path == next_path:
                following = str(current + 1)
                break
    return EngagementResult(platform="autohome", canonical_url=url, work_id=wid,
        coverage="partial", stats=stats, comments=result, next_cursor=following,
        source="club.autohome.com.cn target topic + getclicksandreplys",
        reason="论坛阅读/回复计数和公开一级回复；审核或仅作者可见的回复不返回正文，未公开字段保留 null")
