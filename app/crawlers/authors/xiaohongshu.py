from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from app.crawlers.authors.common import date, encode_cursor, pending, require_list, urls
from app.crawlers.authors.common import AuthorCursorError
from app.crawlers.authors.identity import resolve_xiaohongshu
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.xiaohongshu import _get, signed_headers, authenticated_headers, unwrap_data
from app.models.author import AuthorPage, AuthorWork


async def fetch_page(crawler, request, state, *, include_details=False):
    resolved_uid = await resolve_xiaohongshu(crawler, request.uid)
    cursor = state.get("cursor", "")
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise AuthorCursorError("小红书 cursor 无效")
    origin = "https://www.xiaohongshu.com"
    endpoint = "/api/sns/web/v1/user_posted"
    payload = None
    if hasattr(crawler, "captured"):
        captured = await crawler.captured(
            [endpoint],
            cursor_key="cursor",
            cursor=cursor,
            uid_key="user_id",
            uid=resolved_uid,
        )
        if captured:
            if captured.get("success") is not True:
                raise PlatformCrawlerError("小红书作者列表受限，需要有效会话")
            payload = captured.get("data")
    if payload is None:
        cookie = (
            await crawler.cookie_header(origin)
            if hasattr(crawler, "cookie_header")
            else crawler._platform_cookie("xiaohongshu")
        )
        if not any(part.strip().startswith("a1=") for part in cookie.split(";")):
            raise PlatformCrawlerError(
                "小红书未建立有效游客会话，作者列表需要登录或验证"
            )
        payload = await _get(
            crawler,
            endpoint,
            {
                "num": "30",
                "cursor": cursor,
                "user_id": resolved_uid,
                "image_formats": "jpg,webp,avif",
            },
            cookie,
            f"{origin}/user/profile/{resolved_uid}",
        )
    notes = require_list(payload.get("notes"), "小红书作者")
    if type(payload.get("has_more")) not in (bool, int):
        raise PlatformCrawlerError("小红书列表缺少分页状态")
    works = []
    for note in notes:
        owner = note.get("user") or {}
        if str(owner.get("user_id") or owner.get("userId") or "") != resolved_uid:
            raise PlatformCrawlerError("小红书作品作者不匹配")
        wid = str(note.get("note_id") or note.get("noteId") or "")
        if not re.fullmatch(r"[0-9a-f]{24}", wid):
            raise PlatformCrawlerError("小红书作品 ID 无效")
        token = str(note.get("xsec_token") or note.get("xsecToken") or "")
        cover = note.get("cover") or {}
        url = f"{origin}/explore/{wid}"
        if token:
            url += "?" + urlencode({"xsec_token": token, "xsec_source": "pc_user"})
        work = AuthorWork(
            work_id=wid,
            author_uid=request.uid,
            resolved_author_uid=resolved_uid,
            media_name="xiaohongshu",
            author_name=str(owner.get("nickname") or ""),
            content_type="video" if note.get("type") == "video" else "image_post",
            url=url,
            title=str(note.get("display_title") or note.get("title") or ""),
            cover_url=cover.get("url_default") or cover.get("url"),
        )
        works.append(pending(work, xsec_token=token, resolved_uid=resolved_uid))
    next_value = str(payload.get("cursor") or "")
    if payload.get("has_more") and (not next_value or next_value == cursor):
        raise PlatformCrawlerError("小红书分页游标没有推进")
    return AuthorPage(
        uid=request.uid,
        media_name="xiaohongshu",
        data=works,
        next_cursor=encode_cursor(request, {"cursor": next_value})
        if payload.get("has_more")
        else None,
        reason="作者公开笔记列表；游客权限可能隐藏作品，不能证明历史全集",
    )


def parse_note(body, work_id):
    # Extract a single JSON value, never evaluate remote JavaScript on the server.
    match = re.search(r"(?:window\.)?__INITIAL_STATE__\s*=\s*", body)
    if not match:
        raise PlatformCrawlerError("小红书正文缺少初始状态，访问 token 可能失效")
    raw = body[match.end() :]
    # JSON JS undefined occurs outside strings; preserve prose containing that word.
    raw = re.sub(
        r'("(?:\\.|[^"\\])*"|\bundefined\b)',
        lambda m: "null" if m[0] == "undefined" else m[0],
        raw,
    )
    try:
        state, _ = json.JSONDecoder().raw_decode(raw)
        detail = (state.get("note") or {}).get("noteDetailMap") or {}
        note = (detail.get(work_id) or {}).get("note")
        if not isinstance(note, dict) or str(note.get("noteId") or "") != work_id:
            raise ValueError()
        return note
    except (ValueError, TypeError, AttributeError) as exc:
        raise PlatformCrawlerError("小红书详情没有匹配目标笔记") from exc


async def fetch_detail(crawler, request, work):
    note = None
    token = work.detail_context.get("xsec_token")
    cookie = await crawler.cookie_header("https://www.xiaohongshu.com") if hasattr(crawler, "cookie_header") else crawler._platform_cookie("xiaohongshu")
    if token and cookie:
        endpoint = "/api/sns/web/v1/feed"
        body = {"source_note_id": work.work_id, "image_formats": ["jpg", "webp", "avif"], "extra": {"need_body_topic": "1"}, "xsec_source": "pc_user", "xsec_token": token}
        for sign_format in ("xys", "xyw"):
            headers = authenticated_headers(cookie, work.url)
            headers.update(signed_headers(endpoint, cookie, payload=body, sign_format=sign_format))
            headers["Content-Type"] = "application/json"
            try:
                payload = unwrap_data(await crawler._post_json(
                    "https://edith.xiaohongshu.com" + endpoint,
                    data=json.dumps(body, ensure_ascii=False, separators=(",", ":")), headers=headers,
                ))
                for item in payload.get("items") or []:
                    candidate = item.get("note_card") or {}
                    if str(candidate.get("note_id") or item.get("id") or "") == work.work_id:
                        note = candidate
                        break
                if note is not None:
                    break
            except PlatformCrawlerError:
                continue
    if note is None:
        response = await crawler._get_response(work.url)
        note = parse_note(response.text, work.work_id)
    if str((note.get("user") or {}).get("userId") or (note.get("user") or {}).get("user_id") or "") != (
        work.resolved_author_uid or request.uid
    ):
        raise PlatformCrawlerError("小红书笔记详情作者不匹配")
    work.title = str(note.get("title") or work.title)
    if "desc" not in note:
        raise PlatformCrawlerError("小红书详情缺少正文，不能视作完整笔记")
    work.text = work.description = str(note.get("desc") or "")
    work.published_at = date(int(note.get("time") or 0) // 1000)
    media = [
        img.get("urlDefault") or img.get("url_default") or img.get("url")
        for img in note.get("imageList", note.get("image_list", []))
    ]
    video = note.get("video") or {}
    for streams in ((video.get("media") or {}).get("stream") or {}).values():
        if isinstance(streams, list):
            media.extend(s.get("masterUrl") or s.get("master_url") for s in streams)
    work.media_urls = urls(media)
    duration = (video.get("basic") or {}).get("duration")
    work.duration_ms = int(float(duration) * 1000) if duration else None
    return work
