"""Resolve public account handles to platform identifiers with exact ownership checks."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.douyin import signed_params
from app.crawlers.platforms.xiaohongshu import signed_headers, authenticated_headers


class AuthorIdentityError(PlatformCrawlerError):
    """A valid search response did not establish exact author ownership."""


def _douyin_params(crawler, params):
    if hasattr(crawler, "captured") or getattr(crawler, "handles_douyin_signing", False):
        return params
    return signed_params(params)


def douyin_matches(user, account):
    return account in {
        str(user.get(k) or "") for k in ("uid", "sec_uid", "unique_id", "short_id")
    }


def select_douyin_user(payload, account):
    if payload.get("status_code") != 0 or not isinstance(
        payload.get("user_list"), list
    ):
        raise PlatformCrawlerError("抖音账号搜索未返回用户列表，无法解析 author_id")
    matches = {}
    for row in payload["user_list"]:
        user = row.get("user_info") or row
        if douyin_matches(user, account) and re.fullmatch(
            r"MS4wLj[A-Za-z0-9_-]{10,250}", str(user.get("sec_uid") or "")
        ):
            matches[user["sec_uid"]] = user
    if len(matches) != 1:
        raise AuthorIdentityError(
            "抖音搜索未找到唯一且账号完全匹配的作者；不会按昵称猜测"
        )
    return next(iter(matches.values()))


async def resolve_douyin(crawler, account, base, headers):
    if account.startswith("MS4wLj"):
        return account
    cache = getattr(crawler, "_author_identity_cache", {})
    if account in cache:
        return cache[account]
    repository = getattr(crawler, "_author_identity_repository", None)
    if repository:
        saved = await repository.get_identity("douyin", account)
        if saved:
            # Handles can be renamed/reassigned. Revalidate a stored mapping
            # through the profile endpoint once per discovery before trusting it.
            profile = await crawler._get_json(
                "https://www.douyin.com/aweme/v1/web/user/profile/other/",
                params=_douyin_params(crawler, {**base, "sec_user_id": saved["user"]["sec_uid"]}),
                headers=headers,
            )
            user = profile.get("user") or {}
            if profile.get("status_code") != 0:
                source_work_id = saved.get("source_work_id")
                if not source_work_id:
                    raise PlatformCrawlerError("抖音缓存作者身份核验失败，需重试会话")
                # Some searchable/active works outlive visibility of a profile.
                # Re-read the original public work; stored metadata alone is
                # never sufficient evidence for a new discovery.
                payload = await crawler._get_json(
                    "https://www.douyin.com/aweme/v1/web/aweme/detail/",
                    params=_douyin_params(crawler, {**base, "aweme_id": source_work_id}),
                    headers=headers,
                )
                detail = payload.get("aweme_detail") or {}
                if payload.get("status_code") != 0 or str(detail.get("aweme_id")) != source_work_id:
                    raise PlatformCrawlerError("抖音来源作品无法核验缓存作者身份")
                user = detail.get("author") or {}
            if douyin_matches(user, account) and user.get("sec_uid") == saved["user"]["sec_uid"]:
                cache[account] = user["sec_uid"]
            else:
                await repository.delete_identity("douyin", account)
    # Both numeric handles and internal numeric UIDs occur in real feeds.
    if account not in cache and account.isdigit():
        try:
            profile = await crawler._get_json(
                "https://www.douyin.com/aweme/v1/web/user/profile/other/",
                params=_douyin_params(crawler, {**base, "user_id": account}),
                headers=headers,
            )
            user = profile.get("user") or {}
            if (
                profile.get("status_code") == 0
                and douyin_matches(user, account)
                and user.get("sec_uid")
            ):
                cache[account] = user["sec_uid"]
        except PlatformCrawlerError:
            pass
    if account not in cache:
        payload = None
        if hasattr(crawler, "captured"):
            payload = await crawler.captured(
                ["/discover/search/"],
                cursor_key="offset",
                cursor="0",
                uid_key="keyword",
                uid=account,
            )
        if payload is None:
            params = {
                **base,
                "search_channel": "aweme_user_web",
                "keyword": account,
                "search_source": "switch_tab",
                "query_correct_type": "1",
                "is_filter_search": "0",
                "from_group_id": "",
                "disable_rs": "0",
                "list_type": "single",
                "need_filter_settings": "0",
                "offset": "0",
                "count": "20",
            }
            payload = await crawler._get_json(
                "https://www.douyin.com/aweme/v1/web/discover/search/",
                params=_douyin_params(crawler, params),
                headers=headers,
            )
        user = select_douyin_user(payload, account)
        cache[account] = user["sec_uid"]
    if repository:
        await repository.save_identity("douyin", account, user)
    crawler._author_identity_cache = cache
    return cache[account]


def select_xhs_user(payload, account):
    if payload.get("success") is not True:
        raise PlatformCrawlerError(
            f"小红书账号搜索受限 code={payload.get('code')}，无法解析小红书号"
        )
    data = payload.get("data") or {}
    rows = data.get("users")
    if not isinstance(rows, list):
        raise PlatformCrawlerError("小红书账号搜索缺少 users 列表")
    matches = {}
    for row in rows:
        user = row.get("user_info") or row
        if account not in {
            str(user.get(k) or "") for k in ("red_id", "redId", "user_id", "userid")
        }:
            continue
        uid = str(user.get("user_id") or user.get("userid") or user.get("id") or "")
        if re.fullmatch(r"[0-9a-f]{24}", uid):
            matches[uid] = uid
    if len(matches) != 1:
        raise PlatformCrawlerError(
            "小红书搜索未找到唯一且小红书号完全匹配的作者；不会按昵称猜测"
        )
    return next(iter(matches))


async def resolve_xiaohongshu(crawler, account):
    if re.fullmatch(r"[0-9a-f]{24}", account):
        return account
    cache = getattr(crawler, "_author_identity_cache", {})
    if account in cache:
        return cache[account]
    origin = "https://www.xiaohongshu.com"
    endpoint = "/api/sns/web/v1/search/usersearch"
    payload = None
    if hasattr(crawler, "captured"):
        # Inspect search response bodies only; exact account matching below is mandatory.
        for request, value in list(crawler.responses):
            if endpoint in request.url:
                payload = value
        if payload is None:
            await crawler.page.wait_for_timeout(2000)
            for request, value in list(crawler.responses):
                if endpoint in request.url:
                    payload = value
    if payload is None:
        from xhshow import Xhshow

        cookie = (
            await crawler.cookie_header(origin)
            if hasattr(crawler, "cookie_header")
            else crawler._platform_cookie("xiaohongshu")
        )
        if not cookie:
            raise PlatformCrawlerError("小红书号解析需要有效的平台会话")
        search_ids = Xhshow()
        body = {
            "search_user_request": {
                "keyword": account,
                "page": 1,
                "page_size": 15,
                "search_id": search_ids.get_search_id(),
                "biz_type": "web_search_user",
                "request_id": search_ids.get_search_request_id(),
            }
        }
        headers = authenticated_headers(
            cookie, origin + "/search_result?keyword=" + quote(account)
        )
        headers.update(signed_headers(endpoint, cookie, payload=body))
        headers["Content-Type"] = "application/json"
        payload = await crawler._post_json(
            "https://edith.xiaohongshu.com" + endpoint,
            headers=headers,
            data=json.dumps(body, separators=(",", ":")),
        )
    cache[account] = select_xhs_user(payload, account)
    crawler._author_identity_cache = cache
    return cache[account]
