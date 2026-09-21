from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.crawlers.authors.common import (
    date,
    encode_cursor,
    require_list,
    text,
    urls,
    pending,
    fill_details,
    page_number,
)
from app.crawlers.authors.common import AuthorCursorError
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.bilibili import parse_opus_initial_state, extract_wbi_mixin_key, sign_wbi_params
from app.models.author import AuthorPage, AuthorWork


async def fetch_page(crawler, request, state, *, include_details=True):
    # Existing saved cursors containing offset continue their dynamic traversal.
    source = state.get("source", "dynamic" if "offset" in state else "video")
    if source == "dynamic":
        return await fetch_dynamic_page(crawler, request, state, include_details=include_details)
    if source not in {"video", "article"}:
        raise AuthorCursorError("B 站列表来源无效")
    number = page_number(state)
    headers, key = await prepare_session(crawler, request)
    params = {"mid": request.uid, "pn": number, "ps": 30}
    if source == "video":
        # Public web list parameters also used by RSSHub's Bilibili route.
        # An HTTP visitor has no DOM/WebGL samples; do not invent interaction
        # coordinates or reuse a different browser's device payload.
        params.update({
            "order": "pubdate", "tid": 0, "keyword": "", "platform": "web",
            "web_location": "1550101", "order_avoided": "true",
            "dm_img_list": "[]", "dm_img_str": "bm8gd2ViZ2",
            "dm_cover_img_str": "bm8gd2ViZ2",
            "dm_img_inter": '{"ds":[],"wh":[],"of":[]}',
        })
    else:
        params["sort"] = "publish_time"
    endpoint = "/x/space/wbi/arc/search" if source == "video" else "/x/space/wbi/article"
    payload = await crawler._get_json(
        "https://api.bilibili.com" + endpoint,
        params=sign_wbi_params(params, mixin_key=key), headers=headers,
    )
    data = payload.get("data") or {}
    if payload.get("code") != 0 or data.get("is_risk") or data.get("gaia_res_type"):
        raise PlatformCrawlerError(f"B 站{source}投稿列表受限 code={payload.get('code')}")
    entries = (data.get("list") or {}).get("vlist") if source == "video" else data.get("articles")
    total = (data.get("page") or {}).get("count") if source == "video" else data.get("count")
    if source == "article" and entries is None and total in (None, 0):
        # Some empty article responses contain only pn/ps. Confirm against the
        # independent category count; an omitted list alone is not evidence.
        counts = await crawler._get_json(
            "https://api.bilibili.com/x/space/navnum", params={"mid": request.uid}, headers=headers,
        )
        if counts.get("code") == 0 and (counts.get("data") or {}).get("article") == 0:
            entries, total = [], 0
    entries = require_list(entries, "B 站投稿")
    if type(total) is not int or total < 0:
        raise PlatformCrawlerError("B 站投稿列表缺少有效总数")
    if not entries and (number - 1) * 30 < total:
        raise PlatformCrawlerError("B 站投稿列表意外截断，可重试当前页")
    works = []
    for item in entries:
        owner = item if source == "video" else item.get("author") or {}
        if str(owner.get("mid") or "") != request.uid:
            raise PlatformCrawlerError("B 站投稿作者不匹配")
        wid = str(item.get("bvid") if source == "video" else item.get("id") or "")
        if not re.fullmatch(r"BV[0-9A-Za-z]+" if source == "video" else r"[0-9]+", wid):
            raise PlatformCrawlerError("B 站投稿缺少作品 ID")
        work = AuthorWork(
            work_id=wid if source == "video" else "cv" + wid,
            author_uid=request.uid, media_name="bilibili", content_type=source,
            author_name=str(item.get("author") or "") if source == "video" else str(owner.get("name") or ""),
            url=f"https://www.bilibili.com/video/{wid}" if source == "video" else f"https://www.bilibili.com/read/cv{wid}",
            title=text(item.get("title")), description=text(item.get("description") or item.get("summary")),
            cover_url=item.get("pic") or item.get("banner_url"),
            published_at=date(item.get("created") or item.get("publish_time")),
        )
        pending(work, **({"archive": {"bvid": wid, "title": work.title}} if source == "video" else {"article": {"id": wid, "title": work.title}}))
        works.append(work)
    if include_details:
        await fill_details(crawler, request, works, fetch_detail)
    next_state = {"source": source, "page": number + 1} if number * 30 < total else {"source": "article" if source == "video" else "dynamic"}
    return AuthorPage(
        uid=request.uid, media_name="bilibili", data=works,
        next_cursor=encode_cursor(request, next_state),
        reason="依次遍历视频投稿、专栏和动态；后续来源尚未遍历完，请继续游标",
    )


async def prepare_session(crawler, request):
    cached = getattr(crawler, "_author_bili_session", None)
    if cached:
        return cached
    headers = {"Referer": f"https://space.bilibili.com/{request.uid}/", "Origin": "https://space.bilibili.com"}
    cookie = (await crawler.cookie_header("https://api.bilibili.com")) if hasattr(crawler, "cookie_header") else crawler._platform_cookie("bilibili")
    # Historical guest device cookies belong to a different proxy lease. Keep
    # real account credentials, but obtain guest buvids on the current egress.
    if not any(part.strip().startswith("SESSDATA=") for part in cookie.split(";")):
        cookie = ""
        headers["Cookie"] = ""
    if cookie:
        headers["Cookie"] = cookie
    spi = await crawler._get_json("https://api.bilibili.com/x/frontend/finger/spi", headers=headers)
    if spi.get("code") != 0:
        raise PlatformCrawlerError("B 站访客设备会话初始化失败")
    parts = [cookie] if cookie else []
    names = {p.strip().split("=", 1)[0] for p in cookie.split(";")}
    for name, field in (("buvid3", "b_3"), ("buvid4", "b_4")):
        value = (spi.get("data") or {}).get(field)
        if name not in names and value:
            parts.append(f"{name}={value}")
    if parts:
        headers["Cookie"] = "; ".join(parts)
    nav = await crawler._get_json("https://api.bilibili.com/x/web-interface/nav", headers=headers)
    key = extract_wbi_mixin_key(nav)
    crawler._author_bili_session = (headers, key)
    return headers, key


async def fetch_dynamic_page(crawler, request, state, *, include_details=True):
    offset = state.get("offset", "")
    if (
        not isinstance(offset, str)
        or len(offset) > 512
        or (offset and not re.fullmatch(r"[A-Za-z0-9_-]+", offset))
    ):
        raise AuthorCursorError("B 站 cursor offset 无效")
    headers = {"Referer": f"https://space.bilibili.com/{request.uid}/dynamic"}
    if getattr(crawler, "_author_bili_session", None):
        headers.update(crawler._author_bili_session[0])
    payload = None
    if hasattr(crawler, "captured"):
        payload = await crawler.captured(
            ["/x/polymer/web-dynamic/v1/feed/space"],
            cursor_key="offset",
            cursor=offset,
            uid_key="host_mid",
            uid=request.uid,
        )
    if payload is None:
        payload = await crawler._get_json(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
            params={"host_mid": request.uid, "offset": offset},
            headers=headers,
        )
    data = payload.get("data") or {}
    if payload.get("code") != 0:
        raise PlatformCrawlerError(
            f"B 站作者动态列表返回错误 code={payload.get('code')}"
        )
    entries = require_list(data.get("items"), "B 站作者动态")
    if data.get("has_more") not in (True, False, 0, 1):
        raise PlatformCrawlerError("B 站作者列表缺少分页状态")
    works = []
    for item in entries:
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        if str(author.get("mid") or "") != request.uid:
            continue
        # Reposts reference a different creator's work and are not original uploads.
        if item.get("type") == "DYNAMIC_TYPE_FORWARD":
            continue
        dynamic = modules.get("module_dynamic") or {}
        major = dynamic.get("major") or {}
        archive = major.get("archive") or {}
        article = major.get("article") or {}
        opus = major.get("opus") or {}
        dynamic_id = str(item.get("id_str") or "")
        if not dynamic_id.isdigit():
            raise PlatformCrawlerError("B 站动态 ID 缺失")
        description = str((dynamic.get("desc") or {}).get("text") or "")
        work = AuthorWork(
            work_id="opus:" + dynamic_id,
            author_uid=request.uid,
            media_name="bilibili",
            author_name=author.get("name") or "",
            content_type="short_post",
            url=f"https://www.bilibili.com/opus/{dynamic_id}",
            text=description,
            description=description,
            published_at=date(author.get("pub_ts")),
        )
        if archive or article or opus:
            if archive:
                bvid = str(archive.get("bvid") or "")
                if not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
                    raise PlatformCrawlerError("B站视频缺少 BV 号")
                work.work_id = bvid
                work.content_type = "video"
                work.url = f"https://www.bilibili.com/video/{bvid}"
                work.title = text(archive.get("title"))
                work.cover_url = archive.get("cover")
            elif article:
                aid = str(article.get("id") or "")
                if not aid.isdigit():
                    raise PlatformCrawlerError("B站文章缺少专栏 ID")
                work.work_id = "cv" + aid
                work.content_type = "article"
                work.url = f"https://www.bilibili.com/read/cv{aid}"
                work.title = text(article.get("title"))
            else:
                work.content_type = "article" if opus.get("title") else "image_post"
                work.title = text(opus.get("title"))
            pending(
                work, archive=archive, article=article, opus=opus, dynamic_id=dynamic_id
            )
        else:
            work.title = description[:80]
            work.media_urls = urls(
                [p.get("src") for p in (major.get("draw") or {}).get("items", [])]
            )
        works.append(work)
    if include_details:
        await fill_details(crawler, request, works, fetch_detail)
    next_cursor = None
    if data.get("has_more"):
        next_offset = str(data.get("offset") or "")
        if not next_offset or next_offset == offset:
            raise PlatformCrawlerError("B 站作者分页游标未推进")
        next_cursor = encode_cursor(request, {"source": "dynamic", "offset": next_offset})
    return AuthorPage(
        uid=request.uid,
        media_name="bilibili",
        data=works,
        next_cursor=next_cursor,
        reason="作者公开动态中的视频、文章和图文；作品 ID 与投稿列表重复时由任务存储去重",
    )


async def fetch_detail(crawler, request, work):
    headers = {"Referer": f"https://space.bilibili.com/{request.uid}/dynamic"}
    archive = work.detail_context.get("archive") or {}
    article = work.detail_context.get("article") or {}
    opus = work.detail_context.get("opus") or {}
    dynamic_id = work.detail_context.get("dynamic_id")
    if archive:
        bvid = str(archive.get("bvid") or "")
        if not re.fullmatch(r"BV[0-9A-Za-z]+", bvid):
            raise PlatformCrawlerError("B 站视频缺少 BV 号")
        work.work_id = bvid
        work.content_type = "video"
        work.url = f"https://www.bilibili.com/video/{bvid}"
        work.title = text(archive.get("title"))
        work.cover_url = archive.get("cover")
        view = await crawler._get_json(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bvid},
            headers=headers,
        )
        video = view.get("data") or {}
        if (
            view.get("code") != 0
            or str((video.get("owner") or {}).get("mid")) != request.uid
            or video.get("bvid") != bvid
        ):
            raise PlatformCrawlerError("B 站视频详情不可用或作者不匹配")
        work.description = str(video.get("desc") or "")
        work.text = work.description
        work.duration_ms = int(video.get("duration") or 0) * 1000
    elif article:
        aid = str(article.get("id") or "")
        if not aid.isdigit():
            raise PlatformCrawlerError("B 站文章缺少专栏 ID")
        work.work_id = "cv" + aid
        work.content_type = "article"
        work.url = f"https://www.bilibili.com/read/cv{aid}"
        work.title = text(article.get("title"))
        # Confirm ownership before accepting an independently fetched body.
        info = await crawler._get_json(
            "https://api.bilibili.com/x/article/viewinfo",
            params={"id": aid},
            headers=headers,
        )
        if (
            info.get("code") != 0
            or str(((info.get("data") or {}).get("author") or {}).get("mid"))
            != request.uid
        ):
            raise PlatformCrawlerError("B 站专栏作者不可核验")
        response = await crawler._get_response(work.url, headers=headers)
        soup = BeautifulSoup(response.text, "html.parser")
        body = soup.select_one("#read-article-holder, .article-holder")
        if body is None or not body.get_text(" ", strip=True):
            raise PlatformCrawlerError("B 站文章正文不可用，可能迁移为 Opus")
        work.text = body.get_text("\n", strip=True)
        work.description = work.text[:300]
        work.media_urls = urls(
            [img.get("data-src") or img.get("src") for img in body.select("img")]
        )
    elif opus:
        work.content_type = "article" if opus.get("title") else "image_post"
        work.title = text(opus.get("title"))
        response = await crawler._get_response(work.url, headers=headers)
        detail = parse_opus_initial_state(response.text).get("detail") or {}
        if str(detail.get("id_str")) != dynamic_id:
            raise PlatformCrawlerError("B 站 Opus 详情 ID 不匹配")
        parts = detail.get("modules") or []
        if not isinstance(parts, list):
            raise PlatformCrawlerError("B 站 Opus 正文结构变化")
        owner = next(
            (p.get("module_author") for p in parts if p.get("module_author")),
            {},
        )
        if str(owner.get("mid")) != request.uid:
            raise PlatformCrawlerError("B 站 Opus 详情作者不匹配")
        paragraphs = next(
            (
                p["module_content"].get("paragraphs")
                for p in parts
                if p.get("module_content")
            ),
            None,
        )
        if not isinstance(paragraphs, list):
            raise PlatformCrawlerError("B 站 Opus 缺少完整正文")
        work.text = "\n".join(
            "".join(
                str(n.get("word", {}).get("words") or "")
                for n in p.get("text", {}).get("nodes", [])
            )
            for p in paragraphs
        )
        work.description = work.text[:300]
        work.media_urls = urls(
            [p.get("url") or p.get("src") for p in opus.get("pics", [])]
        )

    return work
