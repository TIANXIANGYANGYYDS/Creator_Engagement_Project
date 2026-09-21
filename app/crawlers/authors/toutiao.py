from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, unquote

from bs4 import BeautifulSoup

from app.crawlers.authors.common import (
    date,
    encode_cursor,
    pending,
    require_list,
    text,
    urls,
)
from app.crawlers.authors.common import AuthorCursorError, AuthorSessionError
from app.crawlers.http_client import PlatformCrawlerError
from app.models.author import AuthorPage, AuthorWork


async def fetch_page(crawler, request, state, *, include_details=False):
    cursor = state.get("max_behot_time", "0")
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise AuthorCursorError("头条 cursor 无效")
    token = request.uid if not request.uid.isdigit() else ""
    if hasattr(crawler, "page") and request.uid.isdigit():
        current = urlsplit(crawler.page.url)
        if current.hostname == "www.toutiao.com" and current.path.startswith(
            "/c/user/token/"
        ):
            token = unquote(current.path.split("/c/user/token/", 1)[1].rstrip("/"))
    endpoint = "/api/pc/list/user/feed"
    payload = None
    if hasattr(crawler, "captured"):
        payload = await crawler.captured(
            [endpoint, "/app/api/pc/feed/"],
            cursor_key="max_behot_time",
            cursor=cursor,
            uid_key="token" if token else "visit_user_id",
            uid=token or request.uid,
        )
    if payload is None:
        params = {
            "category": "profile_all",
            "aid": "24",
            "app_name": "toutiao_web",
            "max_behot_time": cursor,
            "token" if token else "visit_user_id": token or request.uid,
        }
        payload = await crawler._get_json(
            "https://www.toutiao.com" + endpoint, params=params
        )
    return parse_page(payload, request, cursor, profile_token=token)


def parse_page(payload, request, cursor, *, profile_token=""):
    if (
        payload.get("message") not in (None, "success")
        or payload.get("error_code", 0) != 0
    ):
        raise PlatformCrawlerError("头条作者列表受限或请求失败")
    entries = require_list(payload.get("data"), "头条作者")
    if "has_more" not in payload:
        raise PlatformCrawlerError("头条列表缺少分页状态")
    works = []
    for entry in entries:
        if isinstance(entry.get("data"), dict):
            entry = entry["data"]
        cell = entry.get("itemCell") or {}
        cell_owner = cell.get("userInfo") or {}
        owner = entry.get("user_info") or {}
        owner_id = str(entry.get("user_id") or owner.get("user_id") or "")
        owner_token = str(entry.get("user_token") or owner.get("user_token") or "")
        numeric_owner = str(
            ((entry.get("itemCell") or {}).get("userInfo") or {}).get("userID") or ""
        )
        if request.uid not in {owner_id, owner_token, numeric_owner}:
            raise PlatformCrawlerError("头条列表作品作者不可核验")
        wid = str(entry.get("item_id") or entry.get("group_id") or "")
        if not wid.isdigit():
            raise PlatformCrawlerError("头条作品 ID 缺失")
        # Articles may embed a video_id while has_video is explicitly false.
        ugc = bool(cell.get("ugcPost")) or entry.get("cell_type") in {30, 32}
        short_video = entry.get("video") or {}
        video = bool(short_video.get("play_addr")) or (
            bool(entry.get("has_video"))
            if "has_video" in entry
            else bool(entry.get("video_id"))
        )
        work = AuthorWork(
            work_id=wid,
            author_uid=request.uid,
            media_name="toutiao",
            author_name=str(
                entry.get("source") or owner.get("name") or cell_owner.get("name") or ""
            ),
            content_type="short_post" if ugc else "video" if video else "article",
            url=f"https://www.toutiao.com/{'w' if ugc else 'video' if video else 'article'}/{wid}/",
            title=str(entry.get("title") or ""),
            description=text(entry.get("abstract")),
            published_at=date(entry.get("publish_time") or entry.get("behot_time")),
        )
        if video and short_video.get("play_addr") and "title" in entry:
            # Short-video feeds contain the requested public caption and playback
            # links. Reading an article page would lose this content.
            work.text = work.description = str(entry["title"] or "")
            work.media_urls = urls((short_video["play_addr"] or {}).get("url_list", []))
            covers = urls((short_video.get("origin_cover") or {}).get("url_list", []))
            work.cover_url = covers[0] if covers else None
            duration = short_video.get("duration")
            work.duration_ms = int(float(duration) * 1000) if duration else None
        else:
            pending(
                work,
                owner_id=numeric_owner or owner_id,
                owner_token=owner_token or owner_id,
                owner_aliases=list(
                    {owner_id, owner_token, numeric_owner, profile_token} - {""}
                ),
            )
        works.append(work)
    next_value = str((payload.get("next") or {}).get("max_behot_time") or "")
    if payload.get("has_more") and (not next_value.isdigit() or next_value == cursor):
        raise PlatformCrawlerError("头条作者分页游标没有推进")
    return AuthorPage(
        uid=request.uid,
        media_name="toutiao",
        data=works,
        next_cursor=encode_cursor(request, {"max_behot_time": next_value})
        if payload.get("has_more")
        else None,
        reason="今日头条作者公开综合列表；平台可见范围不等同于历史全集",
    )


async def fetch_detail(crawler, request, work):
    response = await crawler._get_response(work.url)
    soup = BeautifulSoup(response.text, "html.parser")
    if soup.title and "头条登录" in soup.title.get_text():
        raise AuthorSessionError("头条作品跳转到登录页，当前访客入口无法取得正文；已保留作品记录")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical and not re.search(
        r"/(?:article|video|group|w)/" + re.escape(work.work_id) + r"/?$",
        urlsplit(str(canonical.get("href"))).path,
    ):
        raise PlatformCrawlerError("头条详情跳转到其他作品")
    body = soup.select_one(".article-content, article")
    schema = None
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.get_text())
            if isinstance(value, dict) and value.get("@type") in {
                "NewsArticle",
                "Article",
                "VideoObject",
            }:
                schema = value
                break
        except ValueError:
            pass
    if schema is None:
        raise PlatformCrawlerError("头条详情缺少可核验的作品结构")
    identity = schema.get("mainEntityOfPage") or {}
    identity = identity.get("@id") if isinstance(identity, dict) else identity
    identity = str(identity or schema.get("url") or "")
    if not re.search(r"/" + re.escape(work.work_id) + r"(?:/|$|\?)", identity):
        raise PlatformCrawlerError("头条详情作品 ID 不匹配")
    author = schema.get("author") or {}
    if isinstance(author, dict) and author.get("url"):
        author_path = urlsplit(str(author["url"])).path.rstrip("/")
        author_id = author_path.rsplit("/", 1)[-1]
        expected_authors = {
            request.uid,
            work.detail_context.get("owner_id"),
            work.detail_context.get("owner_token"),
            *work.detail_context.get("owner_aliases", []),
        }
        if author_id not in expected_authors:
            raise PlatformCrawlerError("头条详情作者与列表不匹配")
    title = soup.select_one("h1")
    work.title = (
        title.get_text(" ", strip=True)
        if title
        else str(schema.get("headline") or schema.get("name") or work.title)
    )
    if work.content_type == "article":
        if body is None or not body.get_text(strip=True):
            raise PlatformCrawlerError("头条文章正文不可用")
        work.text = body.get_text("\n", strip=True)
        work.description = work.text[:300]
        work.media_urls = urls([img.get("src") for img in body.select("img")])
    else:
        work.text = work.description = str(
            schema.get("description") or work.description
        )
        work.media_urls = urls([schema.get("contentUrl")])
    return work
