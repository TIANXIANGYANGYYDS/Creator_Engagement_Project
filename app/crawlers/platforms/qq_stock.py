"""Tencent stock news public counters and combined news discussion feed."""
from __future__ import annotations
import hashlib
import json
import secrets
import time
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms.common import COMMENT_PAGE_SIZE, result_error, to_int
from app.crawlers.platforms.web_sources_common import comment, require_page
from app.models.engagement import EngagementResult, EngagementStats

# Public H5 application signing material from the publisher's JS, not an account credential.
H5_APP_KEY = "9c8e247b438b7d0ae845f9931810a387"


def sign_params():
    nonce = secrets.randbelow(10000)
    sign = hashlib.md5(f"zxg_h5{H5_APP_KEY}{nonce}".encode()).hexdigest()
    return {"zappid":"zxg_h5", "nonce":nonce, "sign":sign}


def detail_params(news_id, *, timestamp=None):
    params = {"openid":"", "fskey":"", "appid":"wxcbc3ab3807acb685", "check":10,
        "device_id":"1", "app":"zxg", "operationName":"QueryNewsInfo",
        "variables":json.dumps({"input":{"newsId":news_id,"reserve":1572995}}, separators=(",",":")),
        "extensions":json.dumps({"persistedQuery":{"version":1,
            "sha256Hash":"207391e4b0ec552d8f199c428a7a1e9a604ab6c9e820c82671d465a0923031aa"}}, separators=(",",":"))}
    params.update(sign_detail_request(params, timestamp=timestamp))
    return params


def sign_detail_request(params, *, timestamp=None):
    """V4 GET signature reproduced from the public H5 client (captured-vector tested)."""
    stamp = str(timestamp if timestamp is not None else int(time.time()))
    encoded = "&".join(f"{key}={params[key]}" for key in sorted(params) if params[key] not in (None, ""))
    method_hash = hashlib.md5(b"zxgGET").hexdigest().upper()
    signature = hashlib.md5((stamp + encoded + method_hash).encode()).hexdigest()
    return {"x-appid":"zxg", "x-sa-v":"4", "x-timestamp":stamp, "x-sa-sign":signature}


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor, include_stats, include_comments):
    try:
        if work_id.startswith("community:"):
            return await fetch_community(crawler, url, work_id, limit, page=page,
                comment_cursor=comment_cursor, include_comments=include_comments)
        news_id = work_id.removeprefix("nes")
        payload = await crawler._get_json("https://snp.tenpay.com/zxg/news/graphql", params=detail_params(news_id), headers={"Referer":url})
        detail = (payload.get("data") or {}).get("queryNewsInfo") or {}
        info = detail.get("news_info") or {}
        canonical_id = info.get("news_id")
        if canonical_id and news_id == canonical_id + "00":
            news_id = canonical_id  # Legacy share URLs append a known 00 suffix.
        if detail.get("code") != 0 or canonical_id != news_id or not info.get("title"):
            raise PlatformCrawlerError("腾讯自选股未返回目标资讯详情")
        stats = EngagementStats()
        if include_stats:
            payload = await crawler._get_json("https://snp.tenpay.com/cgi/cgi-bin/snp/news/countnum", params={
                "news_id":news_id,"kind":"like",**sign_params(),
            }, headers={"Referer":url})
            if payload.get("code") != 0:
                raise PlatformCrawlerError("腾讯自选股点赞接口拒绝请求")
            stats.likes = to_int((payload.get("data") or {}).get("likeNum"))
        size = min(limit, COMMENT_PAGE_SIZE)
        cursor = json.loads(comment_cursor) if comment_cursor else {"begin":"", "begin_news":""}
        steps = 1 if comment_cursor else page
        for step in range(steps):
            payload = await crawler._get_json("https://proxy.finance.qq.com/group/newstockgroup/rssNewsService/newsRssList", params={
                "id":news_id,"visible":1,"limit":size,"map_id":"news_" + news_id,
                "comment_id":info.get("comment_id") or "","content_link":1,"openid":"","fskey":"",
                "appid":"wxcbc3ab3807acb685","check":10,"app":"zxg",**cursor,
            }, headers={"Referer":url})
            data = payload.get("data") or {}
            if payload.get("code") != 0 or not isinstance(data.get("rss_list"), list) or "more_flag" not in data:
                raise PlatformCrawlerError("腾讯自选股评论列表不可用")
            items = data["rss_list"]
            more = bool(data["more_flag"])
            following = {"begin":data.get("min_id") or "", "begin_news":data.get("news_min_id") or ""}
            require_page(items, more, cursor, following)
            cursor = following
            if not more:
                if step + 1 < steps:
                    items = []
                break
        stats.comments = to_int(data.get("comment_cnt"))
        comments = []
        if include_comments:
            subjects = data.get("subject_dict") or {}
            users = data.get("userinfo_list") or {}
            for x in items:
                cid = str(x.get("comment_id") or x.get("subject_id") or "")
                body = {**subjects.get(cid, {}), **x} if isinstance(subjects, dict) else x
                user = users.get(str(body.get("user_id")), {}) if isinstance(users, dict) else {}
                comments.append(comment(cid, body.get("user_name") or user.get("nick"), body.get("content"),
                    likes=body.get("like_num"), replies=body.get("comment_cnt"), created=body.get("created_at")))
        return EngagementResult(platform="qq_stock", canonical_url=url, work_id=work_id,
            coverage="partial", stats=stats, comments=comments,
            next_cursor=json.dumps(cursor) if more and include_comments else None,
            source="snp/news/countnum + rssNewsService/newsRssList", reason="腾讯自选股公开点赞、评论数和讨论列表")
    except PlatformBlockedError as exc:
        return result_error("qq_stock", url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error("qq_stock", url, work_id, "failed", exc)


async def fetch_community(crawler, url, work_id, limit, *, page, comment_cursor, include_comments):
    subject_id = work_id.split(":", 1)[1]
    base = "https://proxy.finance.qq.com/group/newstockgroup/"
    payload = await crawler._get_json(base + "RssService2/getRssSubjectById", params={
        "subjectid":subject_id, "subject_id":subject_id, "visible":1, "content_link":1,
    }, headers={"Referer":url})
    content = (payload.get("data") or {}).get("content") or {}
    if payload.get("code") != 0 or content.get("subject_id") != subject_id:
        raise PlatformCrawlerError("腾讯自选股社区未返回目标帖子")
    stats = EngagementStats(views=to_int(content.get("view_num")), likes=to_int(content.get("like_num")),
        comments=to_int(content.get("comment_cnt")), reposts=to_int(content.get("retweet_count")))
    comments, following = [], None
    if include_comments:
        size = min(limit, COMMENT_PAGE_SIZE)
        cursor = comment_cursor or ""
        steps = 1 if comment_cursor else page
        for step in range(steps):
            payload = await crawler._get_json(base + "RssService/getRssCommentListBySubjectId", params={
                "subjectid":subject_id, "subject_id":subject_id, "num":size, "limit":size, "begin":cursor, "last":cursor,
            }, headers={"Referer":url})
            data = payload.get("data") or {}
            if payload.get("code") != 0 or not isinstance(data.get("comment"), list) or "more_flag" not in data:
                raise PlatformCrawlerError("腾讯自选股社区评论响应异常")
            items = data["comment"]
            next_id = items[-1].get("comment_id") if items else None
            more = bool(data["more_flag"])
            require_page(items, more, cursor, next_id)
            cursor = str(next_id) if next_id else ""
            if not more:
                if step + 1 < steps:
                    items = []
                break
        comments = [comment(x.get("comment_id"), x.get("user_name"), x.get("content"),
            likes=x.get("like_num"), replies=x.get("comment_cnt"), created=x.get("created_at")) for x in items]
        following = cursor if more else None
    return EngagementResult(platform="qq_stock", canonical_url=url, work_id=work_id,
        coverage="partial", stats=stats, comments=comments, next_cursor=following,
        source="RssService2/getRssSubjectById + RssService/getRssCommentListBySubjectId",
        reason="腾讯自选股社区公开互动和评论")
