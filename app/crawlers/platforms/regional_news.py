"""Public article counters and discussions from regional news publishers."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.article_communities import article_stats, zero_or_unavailable
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import checked_list
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.crawlers.platforms.article_communities import next_data
from app.models.engagement import EngagementStats


async def shobserver(crawler, url, wid, limit, page, cursor, comments):
    base = "https://www.shobserver.cn"
    payload = await crawler._get_json(base + "/news/getNewsDetail", params={"id": wid})
    if payload.get("success") is not True:
        raise PlatformCrawlerError("上观文章接口返回业务错误")
    item = payload.get("object") or {}
    stats = article_stats(item, wid, "id", {"views": "viewcount", "likes": "praise",
        "comments": "replay", "favorites": "collectcount"})
    result, following = [], None
    if comments:
        current = cursor or ""
        for step in range(1 if cursor else page):
            payload = await crawler._post_json(base + "/reply/getReplyListByPage",
                data={"nid": wid, "lastReplyId": current}, headers={"Referer": url})
            if payload.get("success") is not True:
                raise PlatformCrawlerError("上观评论接口返回业务错误")
            rows = checked_list(payload.get("object"), stats.comments, first=not current)
            more = len(rows) >= 10
            following = str(rows[-1].get("id") or "") if more else None
            require_page(rows, more, current, following)
            if not more:
                if step + 1 < (1 if cursor else page):
                    rows = []
                break
            current = following
        for row in rows:
            if row.get("newsid") is not None and str(row["newsid"]) != wid:
                raise PlatformCrawlerError("上观评论不属于目标文章")
            if row.get("quoteReplay") or row.get("quoteid") or str(row.get("slock")) == "9":
                continue
            result.append(comment(row.get("id"), row.get("username"), row.get("content"),
                likes=row.get("praisecount"), created=row.get("formatAddTime")))
    return stats, result, following, "shobserver.cn news/getNewsDetail + reply/getReplyListByPage"


def ding_headers(app_id, secret, timestamp):
    fields = {"app-id": app_id, "authorization": "", "timestamp": str(timestamp)}
    material = "".join(f"{key}={fields[key]}" for key in sorted(fields)) + secret
    return {**fields, "sign": hashlib.md5(material.encode("utf-8")).hexdigest()}


async def ding_config(crawler, url):
    # Public web configuration comes from the current official bundle. No login
    # token, browser identity or site signing secret is embedded in this module.
    response = await crawler._get_response(url)
    soup = BeautifulSoup(response.text, "html.parser")
    scripts = [node.get("src", "") for node in soup.select("script[src]")]
    script = next((src for src in scripts if re.fullmatch(
        r"https://nmstatic\.dingxinwen\.cn/nm-sharepage/assets/index-[a-z0-9]+\.js", src)), None)
    if not script:
        raise PlatformCrawlerError("顶端页面未提供当前公共配置脚本")
    text = (await crawler._get_response(script)).text
    prefix = urlsplit(url).hostname.split(".")[0] + "."
    match = re.search(r'name:"' + re.escape(prefix) + r'"[^}]+', text)
    config = dict(re.findall(r'(new_app_id|new_app_secret):"([^"]+)"', match[0])) if match else {}
    if not all(config.get(k) for k in ("new_app_id", "new_app_secret")):
        raise PlatformCrawlerError("顶端页面公共应用配置缺失")
    return config


async def dingnews(crawler, url, wid, limit, page, cursor, comments):
    config = await ding_config(crawler, url)
    base = "https://cms-api.topnews.cn"
    def headers():
        return {**ding_headers(config["new_app_id"], config["new_app_secret"], int(time.time() * 1000)),
                "Referer": url}
    payload = await crawler._get_json(base + "/api/new/query", params={"nmId": wid}, headers=headers())
    if payload.get("code") != 0:
        raise PlatformCrawlerError("顶端文章接口返回业务错误")
    item = payload.get("data") or {}
    stats = article_stats(item, wid, "nmId", {"views": "readNum", "likes": "likeNum", "comments": "commentNum"})
    zero_or_unavailable(stats, comments, "顶端")
    if comments:
        payload = await crawler._post_json(base + "/api/ddComment/list", headers=headers(),
            json_body={"bizId": wid, "comment_type": 1, "type": 1, "size": 10})
        if payload.get("code") != 0:
            raise PlatformCrawlerError("顶端评论接口返回业务错误")
        checked_list(payload.get("data"), stats.comments, first=True)
    return stats, [], None, "cms-api.topnews.cn api/new/query + api/ddComment/list"


async def thepaper(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://m.thepaper.cn/newsDetail_forward_{wid}")
    detail = next_data(response.text).get("props", {}).get("pageProps", {}).get("detailData", {})
    if str((detail.get("contentDetail") or {}).get("contId")) != wid:
        raise PlatformCrawlerError("澎湃页面内容 ID 不匹配")
    base = "https://api.thepaper.cn"
    headers = {"Client-Type": "2", "Referer": url}
    payload = await crawler._get_json(base + "/contentapi/article/detail/interaction/state",
        params={"contId": wid}, headers=headers)
    if payload.get("code") != 200:
        raise PlatformCrawlerError("澎湃互动接口返回业务错误")
    data = payload.get("data") or {}
    stats = EngagementStats(likes=to_int(data.get("praiseTimes")))
    current = (int(cursor) if cursor else page) if comments else 1
    payload = await crawler._post_json(base + "/comment/news/comment/webList", headers=headers,
        json_body={"contId": wid, "pageNum": current, "pageSize": min(limit, 20)})
    data = payload.get("data") or {}
    if payload.get("code") != 200 or data.get("pageNum") != current:
        raise PlatformCrawlerError("澎湃评论接口返回业务错误或页码不匹配")
    stats.comments = to_int(data.get("total"))
    rows = checked_list(data.get("list"), stats.comments, first=current == 1)
    result, following = [], None
    if comments:
        more = data.get("hasNext")
        if not isinstance(more, bool) or (more and data.get("nextPageNum") != current + 1):
            raise PlatformCrawlerError("澎湃评论缺少有效分页标记")
        require_page(rows, more, current, data.get("nextPageNum"))
        following = str(current + 1) if more else None
        for row in rows:
            if str(row.get("objectId")) != wid:
                raise PlatformCrawlerError("澎湃评论不属于目标文章")
            if row.get("parentId") not in (None, 0, "0"):
                continue
            # foldNum controls UI folding; it is not a reply counter. Nested
            # commentReply is a preview, so neither is used as a total.
            result.append(comment(row.get("commentId"), (row.get("userInfo") or {}).get("sname"),
                row.get("content"), likes=row.get("originPraiseTimes"), created=row.get("originCreateTime")))
    return stats, result, following, "thepaper target contentDetail + interaction/state + comment/webList"


async def yzwb(crawler, url, wid, limit, page, cursor, comments):
    base = "https://api.yzwb.net/wap/"
    payload = await crawler._post_json(base + "getDetail?vno=3.5.0&",
        data={"data": json.dumps({"news_id": wid})}, headers={"Referer": url})
    if payload.get("status") != 0:
        raise PlatformCrawlerError("紫牛新闻文章接口返回业务错误")
    stats = article_stats(payload.get("data"), wid, "news_id", {
        "views": "review_count", "likes": "praise_count", "comments": "reply_count"})
    zero_or_unavailable(stats, comments, "紫牛新闻")
    return stats, [], None, "api.yzwb.net target getDetail.news_id + review_count/praise_count/reply_count"


COLLECTORS = {"shobserver": shobserver, "dingnews": dingnews, "thepaper": thepaper, "yzwb": yzwb}
