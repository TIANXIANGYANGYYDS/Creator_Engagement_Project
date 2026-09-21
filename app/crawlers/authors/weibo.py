from __future__ import annotations

from app.crawlers.authors.common import (
    date,
    encode_cursor,
    page_number,
    require_list,
    text,
    urls,
    pending,
    fill_details,
)
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.authors.common import AuthorCursorError, AuthorSessionError
from app.models.author import AuthorPage, AuthorWork


def flatten(cards):
    for card in cards:
        if not isinstance(card, dict):
            continue
        if isinstance(card.get("mblog"), dict):
            yield card["mblog"]
        yield from flatten(card.get("card_group") or [])


async def fetch_page(crawler, request, state, *, include_details=True):
    page = page_number(state)
    referer = f"https://weibo.com/u/{request.uid}"
    cookie = getattr(crawler, "cookie_for_url", lambda url: "")("https://weibo.com/")
    source = state.get("source", "desktop" if cookie else "mobile")
    if source not in {"desktop", "mobile"}:
        raise AuthorCursorError("微博列表来源无效")
    if source == "desktop":
        if not cookie:
            raise PlatformCrawlerError("微博登录态缺失，请更新微博会话后续跑")
        headers = {"Referer": referer, "Cookie": cookie, "X-Requested-With": "XMLHttpRequest"}
        payload = await crawler._get_json(
            "https://weibo.com/ajax/statuses/mymblog",
            params={"uid": request.uid, "page": page, "feature": 0}, headers=headers,
        )
    else:
        mobile_cookie = getattr(crawler, "cookie_for_url", lambda url: "")("https://m.weibo.cn/")
        if mobile_cookie:
            headers = {"Referer": referer, "Cookie": mobile_cookie}
        else:
            await crawler._ensure_weibo_visitor_session(referer, entry="weibo")
            headers = {"Referer": referer, **crawler._weibo_visitor_headers()}
        payload = await crawler._get_json(
            "https://m.weibo.cn/api/container/getIndex",
            params={"type": "uid", "value": request.uid, "containerid": f"107603{request.uid}", "page": page},
            headers=headers,
        )
    data = payload.get("data") or {}
    if source == "mobile" and any(
        "登录" in str(card.get("desc") or "")
        for card in data.get("cards", []) if isinstance(card, dict)
    ):
        raise AuthorSessionError("微博时间线返回登录限制，当前访客入口无法继续访问；已保留分页进度")
    if payload.get("ok") != 1:
        if payload.get("ok") == 0 and "这里还没有内容" in str(payload.get("msg", "")):
            if page == 1:
                raise PlatformCrawlerError(
                    "微博首屏访客不可见，无法区分空账号与登录限制"
                )
            return AuthorPage(
                uid=request.uid,
                media_name="weibo",
                reason="访客时间线已结束，平台可能隐藏历史作品，不能证明全集",
            )
        raise PlatformCrawlerError("微博作者列表返回失败或需要登录，不能当作空列表")
    posts = require_list(data.get("list"), "微博账号时间线") if source == "desktop" else list(flatten(require_list(data.get("cards"), "微博")))
    if not posts and page == 1:
        user = data.get("userInfo") or {}
        verified_empty = str(user.get("id")) == request.uid and user.get("statuses_count") == 0
        if not verified_empty:
            raise PlatformCrawlerError("微博首屏没有可核验作品，不能将登录限制或隐藏时间线视为空账号")
    page_ids = [str(p.get("idstr") or p.get("id") or "") for p in posts]
    if page_ids and state.get("last_page_ids") == page_ids:
        raise PlatformCrawlerError("微博重复返回上一页，历史访问可能受限")
    works = {}
    for post in posts:
        user = post.get("user") or {}
        if str(user.get("idstr") or user.get("id", "")) != request.uid:
            continue
        work_id = str(post.get("idstr") or post.get("id") or "")
        if not work_id:
            raise PlatformCrawlerError("微博作品缺少 ID")
        info = post.get("page_info") or {}
        video = info.get("type") == "video" or bool(info.get("media_info"))
        article = info.get("type") == "article"
        work = AuthorWork(
            work_id=work_id,
            author_uid=request.uid,
            author_name=user.get("screen_name") or "",
            media_name="weibo",
            content_type="video" if video else "article" if article else "short_post",
            url=f"https://m.weibo.cn/detail/{work_id}",
            published_at=date(post.get("created_at")),
            text=text(post.get("text_raw") or post.get("raw_text") or post.get("text")),
        )
        if post.get("isLongText") or article:
            pending(
                work,
                long_text=bool(post.get("isLongText")),
                article_url=info.get("page_url") if article else None,
                source=source,
            )
        media = info.get("media_info") or {}
        work.media_urls = urls(
            [
                media.get(k)
                for k in ("stream_url_hd", "stream_url", "mp4_hd_url", "mp4_sd_url")
            ]
            + [
                (p.get("large") or {}).get("url")
                for p in post.get("pics", [])
                if isinstance(p, dict)
            ]
        )
        work.cover_url = (
            (info.get("page_pic") or {}).get("url")
            if isinstance(info.get("page_pic"), dict)
            else None
        )
        work.title = text(info.get("title")) or work.text[:80]
        work.description = work.text
        works[work_id] = work
    if include_details:
        await fill_details(crawler, request, list(works.values()), fetch_detail)
    return AuthorPage(
        uid=request.uid,
        media_name="weibo",
        data=list(works.values()),
        next_cursor=encode_cursor(request, {"page": page + 1, "source": source, "last_page_ids": page_ids}) if posts else None,
        reason="当前会话可见的微博时间线；折叠或隐藏作品可能影响历史覆盖",
    )


async def fetch_detail(crawler, request, work):
    referer = f"https://weibo.com/u/{request.uid}"
    desktop = work.detail_context.get("source") == "desktop"
    origin = "https://weibo.com/" if desktop else "https://m.weibo.cn/"
    cookie = getattr(crawler, "cookie_for_url", lambda url: "")(origin)
    if cookie:
        headers = {"Referer": referer, "Cookie": cookie}
    elif desktop:
        raise PlatformCrawlerError("微博长正文需要恢复账号登录态")
    else:
        await crawler._ensure_weibo_visitor_session(referer, entry="weibo")
        headers = {"Referer": referer, **crawler._weibo_visitor_headers()}
    if work.detail_context.get("long_text"):
        extended = await crawler._get_json(
            "https://weibo.com/ajax/statuses/longtext" if desktop else "https://m.weibo.cn/statuses/extend",
            params={"id": work.work_id},
            headers=headers,
        )
        full = (extended.get("data") or {}).get("longTextContent")
        if extended.get("ok") != 1 or not full:
            raise PlatformCrawlerError("微博长正文接口未返回完整文本")
        work.text = text(full)
    if work.content_type == "article":
        import re
        from bs4 import BeautifulSoup
        from urllib.parse import urlsplit, parse_qs

        link = str(work.detail_context.get("article_url") or "")
        parsed = urlsplit(link)
        article_id = parse_qs(parsed.query).get("id", [""])[0]
        if not re.fullmatch(r"(?:230940)?[0-9]{8,32}", article_id):
            raise PlatformCrawlerError("微博独立文章链接缺少可核验的文章 ID")
        response = await crawler._get_response(
            f"https://weibo.com/ttarticle/p/show?id={article_id}", headers=headers
        )
        soup = BeautifulSoup(response.text, "html.parser")
        body = soup.select_one(".WB_editor_iframe_new, .article-content")
        if body is None or not body.get_text(strip=True):
            raise PlatformCrawlerError("微博独立文章正文受限，已保留附文")
        work.text = body.get_text("\n", strip=True)
        work.media_urls = urls([img.get("src") for img in body.select("img")])
    work.description = work.text
    return work
