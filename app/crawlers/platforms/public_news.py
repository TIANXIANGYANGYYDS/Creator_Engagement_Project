"""Target-bound public news APIs, with explicit checks before accepting empty lists."""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.web_sources_common import assigned_json, comment, require_page
from app.models.engagement import EngagementStats


class PublicDataUnavailable(PlatformCrawlerError):
    """The public page does not expose the requested operation."""


def checked_list(value, total, *, first=True):
    if not isinstance(value, list) or total is None:
        raise PlatformCrawlerError("公开评论接口缺少列表或计数")
    if first and total > 0 and not value:
        raise PlatformCrawlerError("平台声明存在评论但首屏没有返回评论")
    return value


async def gelonghui(crawler, url, wid, limit, page, cursor, comments):
    base = "https://www.gelonghui.com"
    payload = await crawler._get_json(base + f"/api/post/{wid}/v3", headers={"Accept": "application/json"})
    item = payload.get("result") or {}
    if payload.get("statusCode") != 200 or str(item.get("id")) != wid or not item.get("title"):
        raise PlatformCrawlerError("格隆汇详情未返回目标文章")
    counts = item.get("count") or {}
    stats = EngagementStats(**{k: to_int(counts.get(v)) for k, v in {
        "views": "read", "comments": "comment", "likes": "like", "favorites": "favorite", "shares": "share"}.items()})
    result, following = [], None
    if comments:
        current = cursor or str(int(time.time()))
        for step in range(1 if cursor else page):
            payload = await crawler._get_json(base + f"/api/comments/article/{wid}/v2",
                params={"createTimestamp": current}, headers={"Accept": "application/json"})
            if payload.get("statusCode") != 200:
                raise PlatformCrawlerError("格隆汇评论接口返回失败")
            rows = payload.get("result")
            if rows is None and payload.get("totalCount") == 0:
                rows = []
            checked_list(rows, stats.comments, first=not cursor and step == 0)
            following = str(rows[-1].get("createTimestamp")) if rows else None
            require_page(rows, bool(rows), current, following)
            if not rows:
                break
            current = following
        result = [comment(x.get("commentId"), (x.get("user") or {}).get("nick"),
            x.get("content"), created=x.get("createTimestamp")) for x in rows]
    return stats, result, following, base + "/api/comments/article"


async def yidian(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://www.yidianzixun.com/article/{wid}")
    text = re.sub(r"\\x([0-9a-fA-F]{2})", r"\\u00\1", response.text)
    doc = assigned_json(text, "window.yidian.docinfo").get("doc") or {}
    if str(doc.get("docid")) != wid or not doc.get("title") or doc.get("removed"):
        raise PlatformCrawlerError("一点资讯未返回目标文章")
    stats = EngagementStats(likes=to_int(doc.get("up")), comments=to_int(doc.get("comment_count")))
    result, following = [], None
    if comments:
        current = cursor or ""
        for step in range(1 if cursor else page):
            payload = await crawler._get_json("https://www.yidianzixun.com/home/q/getcomments",
                params={"docid": wid, "s": "", "count": min(limit, 20), "last_comment_id": current},
                headers={"Referer": url})
            if payload.get("status") != "success" or str(payload.get("docid")) != wid:
                raise PlatformCrawlerError("一点资讯评论返回失败或目标不匹配")
            stats.comments = to_int(payload.get("total"))
            rows = checked_list(payload.get("comments"), stats.comments, first=not cursor and step == 0)
            more = len(rows) >= min(limit, 20)
            following = str(rows[-1].get("comment_id")) if more else None
            require_page(rows, more, current, following)
            if not more:
                if step + 1 < (1 if cursor else page):
                    rows = []
                break
            current = following
        result = [comment(x.get("comment_id"), x.get("nickname"), x.get("comment"),
            likes=x.get("up"), created=x.get("createAt")) for x in rows]
    return stats, result, following, "www.yidianzixun.com/home/q/getcomments"


async def nowcoder(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    data = assigned_json(response.text, "window.__INITIAL_STATE__").get("prefetchData") or {}
    article = data.get("2") or {}
    target = (article.get("ssrCommonData") or {}).get("contentData") or {}
    cid = str(article.get("contentId") or "")
    if not cid or str(target.get("id")) != cid or not target.get("title"):
        raise PlatformCrawlerError("牛客页面缺少目标内容映射")
    counts = target.get("frequencyData") or {}
    stats = EngagementStats(**{k: to_int(counts.get(v)) for k, v in {
        "views": "viewCnt", "likes": "likeCnt", "comments": "totalCommentCnt", "shares": "shareCnt", "favorites": "followCnt"}.items()})
    result, following = [], None
    if comments:
        current = int(cursor) if cursor else page
        payload = await crawler._get_json("https://gw-c.nowcoder.com/api/sparta/comment/list-by-page",
            params={"entityId": cid, "entityType": article["contentType"], "order": 1,
                    "pageNo": current, "pageSize": min(limit, 20)}, headers={"Referer": url})
        if payload.get("code") != 0 or payload.get("success") is not True:
            raise PlatformCrawlerError("牛客评论接口返回失败")
        data = payload.get("data") or {}
        rows = checked_list(data.get("records"), to_int(data.get("total")), first=current == 1)
        stats.comments = to_int(data.get("totalCommentCnt"))
        more = current < int(data.get("totalPage") or 0)
        require_page(rows, more, current, current + 1)
        following = str(current + 1) if more else None
        result = [comment(x.get("id"), (x.get("userBrief") or {}).get("nickname"),
            x.get("content"), likes=(x.get("frequencyData") or {}).get("likeCnt"),
            created=x.get("createTime")) for x in rows]
    return stats, result, following, "nowcoder.com target state + gw-c.nowcoder.com comments"


async def zaker(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(f"https://app.myzaker.com/news/article.php?pk={wid}")
    binding = re.search(r"CONFIG\[['\"]pkid['\"]\]\s*=\s*['\"]([^'\"]+)", response.text)
    if not binding or binding[1] != wid:
        raise PlatformCrawlerError("ZAKER 页面未返回目标文章")
    endpoint = "https://app.myzaker.com/news/comment.php"
    params = {"act": "get_comments", "pk": wid, "_version": "8.50"}
    current = cursor
    rows, following, data = [], None, {}
    for step in range(1 if cursor or not comments else page):
        if current:
            parsed = urlsplit(current)
            if parsed.hostname not in {"app.myzaker.com", "app3.myzaker.com", "c.myzaker.com", "iphone.myzaker.com"}:
                raise PlatformCrawlerError("ZAKER 评论游标域名不匹配")
        payload = await crawler._get_json(current or endpoint, params=None if current else params,
            headers={"Referer": url})
        if str(payload.get("stat")) != "1":
            raise PlatformCrawlerError("ZAKER 评论接口返回失败")
        data = payload.get("data") or {}
        groups = data.get("comments")
        if not isinstance(groups, list) or not groups or not isinstance(groups[0].get("list"), list):
            raise PlatformCrawlerError("ZAKER 评论列表缺失")
        rows = checked_list(groups[0]["list"], to_int(data.get("comment_counts")), first=not cursor and step == 0)
        following = data.get("next_url") or None
        require_page(rows, bool(following), current, following)
        if not following:
            if comments and step + 1 < (1 if cursor else page):
                rows = []
            break
        current = following
    result = [comment(x.get("pk"), (x.get("auther") or {}).get("name"), x.get("content"),
        likes=x.get("like_num"), replies=x.get("reply_num"),
        created=" ".join(str(x.get(k) or "") for k in ("date", "time")).strip()) for x in rows] if comments else []
    return EngagementStats(likes=to_int(data.get("article_like_counts")), comments=to_int(data.get("comment_counts"))), result, following if comments else None, endpoint


async def ifeng(crawler, url, wid, limit, page, cursor, comments):
    response = await crawler._get_response(url)
    state = assigned_json(response.text, "var allData")
    doc = state.get("docData") or {}
    target = doc.get("base62Id") or doc.get("id")
    encrypted_share = urlsplit(url).hostname == "ishare.ifeng.com" and wid.startswith("v006")
    if (target != wid and not encrypted_share) or not target or not doc.get("title"):
        raise PlatformCrawlerError("凤凰页面未返回目标文章")
    if doc.get("allowComment") is False:
        raise PlatformCrawlerError("凤凰文章关闭评论")
    thread = doc.get("commentUrl")
    if thread != "ucms_" + target:
        raise PlatformCrawlerError("凤凰评论标识与目标文章不匹配")
    host = state.get("__cd__") or "c01049em.ifeng.com"
    if not re.fullmatch(r"[a-zA-Z0-9-]+\.ifeng\.com", host):
        raise PlatformCrawlerError("凤凰评论接口域名不匹配")
    current = int(cursor) if cursor else page
    response = await crawler._get_response(f"https://{host}/get", params={
        "docUrl": thread, "format": "js", "job": 1, "p": current, "pageSize": min(limit, 20),
        "orderby": "ctime", "callback": "collector"}, headers={"Referer": url})
    # The JSON is assigned inside a JavaScript closure; decode the literal only.
    data = assigned_json(response.text, "commentJsonVarStr___")
    if not data or data.get("allow_comment") == 0:
        raise PlatformCrawlerError("凤凰评论接口未返回有效数据或评论已关闭")
    total = to_int(data.get("count"))
    rows = checked_list(data.get("comments"), total, first=current == 1)
    result = [comment(x.get("comment_id") or x.get("id"), x.get("uname"), x.get("comment_contents"),
        likes=x.get("uptimes"), created=x.get("comment_date")) for x in rows] if comments else []
    more = len(rows) >= min(limit, 20)
    return EngagementStats(comments=total), result, str(current + 1) if comments and more else None, f"{host}/get"


async def dayu(crawler, url, wid, limit, page, cursor, comments):
    canonical = f"https://iflow.uc.cn/webview/news?app=webapp&aid={wid}"
    response = await crawler._get_response(canonical)
    doc = assigned_json(response.text, "var xissJsonData")
    if str(doc.get("id")) != wid or not doc.get("title"):
        raise PlatformCrawlerError("大鱼号页面未返回目标文章")
    stats = EngagementStats(comments=to_int(doc.get("participant_cnt")),
        likes=to_int((doc.get("wemedia_info") or {}).get("like_count_smallvideo")))
    result, following = [], None
    if comments:
        if doc.get("cmt_enabled") is not True:
            raise PlatformCrawlerError("大鱼号文章未开放评论")
        current = cursor or "0"
        for step in range(1 if cursor else page):
            payload = await crawler._get_json(f"https://iflow.uc.cn/iflow/api/v1/cmt/article/{wid}/comments",
                params={"cursor": current, "count": min(limit, 20)}, headers={"Referer": canonical})
            data = payload.get("data") or {}
            if payload.get("status") != 0 or str(data.get("aid")) != wid:
                raise PlatformCrawlerError("大鱼号评论响应失败或目标不匹配")
            stats.comments = to_int(data.get("comment_cnt"))
            ids = checked_list(data.get("latest_comments"), stats.comments, first=not cursor and step == 0)
            mapping = data.get("comments_map") or {}
            rows = [mapping.get(str(x), x) for x in ids]
            if any(not isinstance(x, dict) for x in rows):
                raise PlatformCrawlerError("大鱼号评论映射缺少正文")
            more = bool(data.get("has_more")) or len(rows) >= min(limit, 20)
            following = data.get("cursor") if more else None
            require_page(rows, more, current, following)
            if not more:
                if step + 1 < (1 if cursor else page):
                    rows = []
                break
            current = following
        result = [comment(x.get("id") or x.get("comment_id"), (x.get("user") or {}).get("nickname"),
            x.get("content"), likes=x.get("support_cnt"), created=x.get("create_time")) for x in rows]
    return stats, result, following, "iflow.uc.cn target article + comments"


async def ximalaya(crawler, url, wid, limit, page, cursor, comments):
    base = "https://m.ximalaya.com"
    payload = await crawler._get_json(base + f"/m-revision/page/track/queryTrackPage/{wid}")
    data = payload.get("data") or {}
    detail = data.get("trackDetailInfo") or {}
    item = detail.get("trackInfo") or {}
    if payload.get("ret") != 0 or str(data.get("id")) != wid or str(item.get("id")) != wid or not item.get("title") or item.get("isOffline"):
        raise PlatformCrawlerError("喜马拉雅未返回目标声音")
    stats = EngagementStats(views=to_int((detail.get("statCountInfo") or {}).get("playCount")))
    current = int(cursor) if cursor else page
    payload = await crawler._get_json(base + "/m-revision/common/track/queryTrackCommentsFirstPage",
        params={"trackId": wid, "pageSize": min(limit, 20), "page": current if comments else 1},
        headers={"Referer": url})
    data = payload.get("data") or {}
    if payload.get("ret") != 0 or str(data.get("trackId")) != wid:
        raise PlatformCrawlerError("喜马拉雅评论响应失败或目标不匹配")
    stats.comments = to_int(data.get("totalCount"))
    rows = checked_list(data.get("comments"), stats.comments, first=current == 1)
    more = current * min(limit, 20) < stats.comments
    require_page(rows, more, current, current + 1)
    result = [comment(x.get("id"), x.get("nickname"), x.get("content"),
        likes=x.get("likes"), replies=x.get("replyCount"), created=x.get("createdAt")) for x in rows] if comments else []
    return stats, result, str(current + 1) if comments and more else None, base + "/m-revision"


COLLECTORS = {"gelonghui": gelonghui, "yidian": yidian,
              "nowcoder": nowcoder, "zaker": zaker, "ifeng": ifeng, "ifeng_dafeng": ifeng,
              "dayu": dayu, "ximalaya": ximalaya}
