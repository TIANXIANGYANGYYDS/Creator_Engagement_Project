from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

from app.crawlers.authors.common import date, encode_cursor, pending, require_list, urls
from app.crawlers.authors.common import AuthorCursorError
from app.crawlers.http_client import PlatformCrawlerError
from app.models.author import AuthorPage, AuthorWork


PHOTO_FIELDS = "id caption photoUrl coverUrl duration timestamp"
PHOTO_SELECTION = "... on PhotoEntity{" + PHOTO_FIELDS + "} ... on recoPhotoEntity{" + PHOTO_FIELDS + "}"


async def fetch_page(crawler, request, state, *, include_details=False):
    cursor = state.get("pcursor", "")
    if not isinstance(cursor, str) or len(cursor) > 512:
        raise AuthorCursorError("快手 pcursor 无效")
    resolved_uid = await resolve_author(crawler, request)
    endpoint = "/rest/v/profile/feed"
    payload = None
    if hasattr(crawler, "captured"):
        payload = await crawler.captured(
            [endpoint],
            cursor_key="pcursor",
            cursor=cursor,
            uid_key="user_id",
            uid=resolved_uid,
        )
    if payload is None:
        payload = await crawler._post_json(
            "https://www.kuaishou.com" + endpoint,
            headers={"Content-Type": "application/json"},
            data=json.dumps(
                {"user_id": resolved_uid, "pcursor": cursor, "page": "profile"}
            ),
        )
    if payload.get("result") != 1:
        rest_result = payload.get("result")
        # The official site uses both REST and GraphQL in different rollouts.
        graphql = await crawler._post_json(
            "https://www.kuaishou.com/graphql",
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "operationName": "visionProfilePhotoList",
                "variables": {"userId": resolved_uid, "pcursor": cursor, "page": "profile"},
                "query": "query visionProfilePhotoList($userId:String,$pcursor:String,$page:String){visionProfilePhotoList(userId:$userId,pcursor:$pcursor,page:$page){result pcursor feeds{author{id name} photo{" + PHOTO_SELECTION + "}}}}",
            }),
        )
        payload = (graphql.get("data") or {}).get("visionProfilePhotoList") or {}
        if payload.get("result") != 1:
            raise PlatformCrawlerError(
                f"快手作者列表受限 REST={rest_result} GraphQL={payload.get('result')}，当前公开入口未返回可核验作品"
            )
    entries = require_list(payload.get("feeds"), "快手作者")
    if "pcursor" not in payload:
        raise PlatformCrawlerError("快手作者列表缺少分页游标")
    works = []
    for entry in entries:
        photo = entry.get("photo") or {}
        author = entry.get("author") or {}
        if str(author.get("id") or author.get("user_id") or "") not in {request.uid, resolved_uid}:
            raise PlatformCrawlerError("快手作品作者不匹配")
        wid = str(photo.get("id") or photo.get("photo_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", wid):
            raise PlatformCrawlerError("快手作品 ID 缺失")
        caption = str(photo.get("caption") or "")
        work = AuthorWork(
            work_id=wid,
            author_uid=request.uid,
            resolved_author_uid=resolved_uid,
            media_name="kuaishou",
            author_name=str(author.get("name") or ""),
            content_type="video",
            url=f"https://www.kuaishou.com/short-video/{wid}",
            title=caption,
            description=caption,
            text=caption,
            cover_url=photo.get("coverUrl"),
            duration_ms=photo.get("duration"),
            media_urls=urls([photo.get("photoUrl")]),
        )
        timestamp = photo.get("timestamp")
        work.published_at = date(
            int(timestamp) // 1000
            if timestamp and int(timestamp) > 10**11
            else timestamp
        )
        # A list can carry the complete requested public video metadata already.
        if not photo.get("photoUrl") or "caption" not in photo:
            pending(work)
        works.append(work)
    next_value = str(payload.get("pcursor") or "")
    more = next_value not in {"", "no_more", "0"}
    if more and next_value == cursor:
        raise PlatformCrawlerError("快手作者分页游标没有推进")
    return AuthorPage(
        uid=request.uid,
        media_name="kuaishou",
        data=works,
        next_cursor=encode_cursor(request, {"pcursor": next_value}) if more else None,
        reason="快手当前会话可见作品；登录限制或隐藏作品可能影响历史覆盖",
    )


async def fetch_detail(crawler, request, work):
    # The public mobile share page embeds complete video metadata even when
    # the desktop detail API requires a session. Keep the same proxy lease.
    if not hasattr(crawler, "page"):
        try:
            return await mobile_detail(crawler, request, work)
        except PlatformCrawlerError:
            pass
    payload = await crawler._post_json(
        "https://www.kuaishou.com/graphql",
        headers={"Content-Type": "application/json"},
        data=json.dumps(
            {
                "operationName": "visionVideoDetail",
                "variables": {"photoId": work.work_id, "page": "detail"},
                "query": "query visionVideoDetail($photoId:String,$page:String){visionVideoDetail(photoId:$photoId,page:$page){status author{id name} photo{id caption photoUrl coverUrl duration timestamp}}}",
            }
        ),
    )
    detail = (payload.get("data") or {}).get("visionVideoDetail") or {}
    photo = detail.get("photo") or {}
    if (
        str(photo.get("id")) != work.work_id
        or str((detail.get("author") or {}).get("id")) not in {request.uid, work.resolved_author_uid}
    ):
        raise PlatformCrawlerError("快手详情受限或作者/作品身份不匹配")
    if "caption" not in photo:
        raise PlatformCrawlerError("快手详情缺少视频文案")
    work.text = work.description = work.title = str(photo.get("caption") or "")
    work.media_urls = urls([photo.get("photoUrl")])
    work.cover_url = photo.get("coverUrl")
    work.duration_ms = photo.get("duration")
    return work


async def resolve_author(crawler, request):
    cache = getattr(crawler, "_author_identity_cache", {})
    if request.uid in cache:
        return cache[request.uid]
    repository = getattr(crawler, "_author_identity_repository", None)
    if request.uid.isdigit() and repository:
        saved = await repository.get_identity("kuaishou", request.uid)
        if saved and re.fullmatch(r"[A-Za-z0-9_-]+", saved.get("source_work_id") or ""):
            work = AuthorWork(work_id=saved["source_work_id"], author_uid=request.uid, media_name="kuaishou", content_type="video", url=f"https://www.kuaishou.com/short-video/{saved['source_work_id']}")
            # Re-read the public source work; stored metadata alone cannot prove
            # ownership for a new discovery.
            protocol = getattr(crawler, "crawler", crawler)
            detail = await mobile_detail(protocol, request, work)
            cache[request.uid] = detail.resolved_author_uid
            crawler._author_identity_cache = cache
            return detail.resolved_author_uid
    return request.uid


async def mobile_detail(crawler, request, work):
    response = await crawler._get_response(
        work.url,
        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile Safari/537.36"},
    )
    return parse_mobile_detail(response.text, request, work)


def parse_mobile_detail(body, request, work):
    match = re.search(r"window\.INIT_STATE\s*=\s*", body)
    if not match:
        raise PlatformCrawlerError("快手移动详情缺少作品状态")
    try:
        state, _ = json.JSONDecoder().raw_decode(body[match.end() :])
    except ValueError as exc:
        raise PlatformCrawlerError("快手移动详情状态无效") from exc
    if not isinstance(state, dict):
        raise PlatformCrawlerError("快手移动详情状态无效")
    # Top-level state keys vary; never evaluate JavaScript from a remote page.
    for value in state.values():
        if not isinstance(value, dict) or not isinstance(value.get("photo"), dict):
            continue
        photo = value["photo"]
        share = parse_qs(str(photo.get("share_info") or ""))
        if share.get("photoId", [""])[0] != work.work_id:
            continue
        owner = str(photo.get("userId") or "")
        encoded_owner = share.get("userId", [""])[0]
        if request.uid not in {owner, encoded_owner}:
            raise PlatformCrawlerError("快手移动详情作者不匹配")
        if "caption" not in photo:
            raise PlatformCrawlerError("快手移动详情缺少视频文案")
        work.title = work.description = work.text = str(photo["caption"] or "")
        work.resolved_author_uid = encoded_owner or owner
        work.author_name = str(photo.get("userName") or work.author_name)
        work.media_urls = urls(
            v.get("url") for v in photo.get("mainMvUrls") or [] if isinstance(v, dict)
        )
        covers = urls(
            v.get("url") for v in photo.get("coverUrls") or [] if isinstance(v, dict)
        )
        work.cover_url = covers[0] if covers else work.cover_url
        work.duration_ms = photo.get("duration")
        if photo.get("timestamp"):
            work.published_at = date(int(photo["timestamp"]) // 1000)
        return work
    raise PlatformCrawlerError("快手移动详情没有匹配的作品 ID")
