"""Recover a note's access URL from its exact author in the supplied dataset."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from app.crawlers.authors.identity import resolve_xiaohongshu
from app.crawlers.platforms.xiaohongshu import _get, authenticated_headers, signed_headers


NOTE_ID = re.compile(r"[0-9a-f]{24}")


class XiaohongshuUrlResolutionError(RuntimeError):
    pass


async def recover_author(crawler, account: str, targets: set[str], max_pages: int) -> dict[str, str]:
    uid = await resolve_xiaohongshu(crawler, account)
    cookie = crawler._platform_cookie("xiaohongshu")
    if not cookie:
        raise XiaohongshuUrlResolutionError("小红书登录态不可用，请重新扫码授权")
    found: dict[str, str] = {}
    cursor = ""
    has_more = False
    for _ in range(max_pages):
        payload = await _get(
            crawler,
            "/api/sns/web/v1/user_posted",
            {"num": "30", "cursor": cursor, "user_id": uid,
             "image_formats": "jpg,webp,avif"},
            cookie,
            f"https://www.xiaohongshu.com/user/profile/{uid}",
        )
        notes = payload.get("notes")
        if not isinstance(notes, list):
            raise XiaohongshuUrlResolutionError("作者笔记列表缺失")
        for note in notes:
            owner = note.get("user") or {}
            if str(owner.get("user_id") or owner.get("userId") or "") != uid:
                raise XiaohongshuUrlResolutionError("作者笔记归属校验失败")
            note_id = str(note.get("note_id") or note.get("noteId") or "")
            token = note.get("xsec_token") or note.get("xsecToken")
            if note_id in targets and token:
                found[note_id] = (
                    f"https://www.xiaohongshu.com/explore/{note_id}?"
                    + urlencode({"xsec_token": token, "xsec_source": "pc_user"})
                )
        has_more = bool(payload.get("has_more"))
        if targets <= found.keys() or not has_more:
            break
        next_cursor = str(payload.get("cursor") or "")
        if not next_cursor or next_cursor == cursor:
            raise XiaohongshuUrlResolutionError("作者分页游标未推进")
        cursor = next_cursor
    if has_more and not targets <= found.keys():
        raise XiaohongshuUrlResolutionError("达到作者分页上限，目标笔记未找到")
    return found


async def recover_named_author(crawler, name: str, note_id: str, max_pages: int) -> dict[str, str]:
    from xhshow import Xhshow

    cookie = crawler._platform_cookie("xiaohongshu")
    if not cookie:
        raise XiaohongshuUrlResolutionError("小红书登录态不可用，请重新扫码授权")
    signer = Xhshow()
    endpoint = "/api/sns/web/v1/search/usersearch"
    body = {"search_user_request": {
        "keyword": name, "page": 1, "page_size": 15,
        "search_id": signer.get_search_id(),
        "biz_type": "web_search_user",
        "request_id": signer.get_search_request_id(),
    }}
    headers = authenticated_headers(cookie, "https://www.xiaohongshu.com/search_result?keyword=" + quote(name))
    headers.update(signed_headers(endpoint, cookie, payload=body))
    headers["Content-Type"] = "application/json"
    payload = await crawler._post_json(
        "https://edith.xiaohongshu.com" + endpoint,
        headers=headers,
        data=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    )
    if payload.get("success") is not True:
        raise XiaohongshuUrlResolutionError("小红书作者名搜索受限")
    rows = (payload.get("data") or {}).get("users") or []
    candidates = []
    for row in rows:
        user = row.get("user_info") or row
        uid = str(user.get("user_id") or user.get("userid") or user.get("id") or "")
        nickname = str(user.get("name") or user.get("nickname") or "").strip()
        if nickname == name and NOTE_ID.fullmatch(uid) and uid not in candidates:
            candidates.append(uid)
    if len(candidates) > 5:
        raise XiaohongshuUrlResolutionError("同名作者过多，无法限定核验范围")
    matches = []
    for uid in candidates:
        found = await recover_author(crawler, uid, {note_id}, max_pages)
        if note_id in found:
            matches.append(found[note_id])
    if len(matches) > 1:
        raise XiaohongshuUrlResolutionError("多个同名账号包含目标笔记，归属不唯一")
    return {note_id: matches[0]} if matches else {}


class XiaohongshuUrlResolver:
    def __init__(self, source_path: Path, cache_path: Path, *, max_pages: int = 20) -> None:
        self.cache_path = cache_path
        self.max_pages = max_pages
        self._lock = asyncio.Lock()
        self._not_found: dict[tuple[str, str], tuple[float, str]] = {}
        self.authors: dict[str, str] = {}
        self.names: dict[str, str] = {}
        conflicts: set[str] = set()
        try:
            rows = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rows = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("rawData"), dict):
                row = row["rawData"]
            parsed = urlsplit(str(row.get("url") or ""))
            note_id = parsed.path.rsplit("/", 1)[-1]
            account = str(row.get("author_id") or "").strip()
            if parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com"} or not NOTE_ID.fullmatch(note_id):
                continue
            # Production exports include the exact profile URL as well as a
            # numeric red ID. Prefer the profile UID to avoid account search.
            info = row.get("author_full_info") or {}
            profiles = set()
            for value in [row.get("board_url"), info.get("profile_url") if isinstance(info, dict) else None]:
                profile = urlsplit(str(value or ""))
                match = re.fullmatch(r"/user/profile/([0-9a-f]{24})/?", profile.path)
                if profile.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"} and match:
                    profiles.add(match[1])
            if len(profiles) > 1:
                conflicts.add(note_id)
                self.authors.pop(note_id, None)
                self.names.pop(note_id, None)
            if note_id in conflicts:
                continue
            if profiles:
                account = next(iter(profiles))
            if not account:
                name = str(row.get("author_name") or "").strip()
                if name:
                    self.names[note_id] = name
                continue
            if account:
                if note_id in self.authors and self.authors[note_id] != account:
                    self.authors.pop(note_id)
                    self.names.pop(note_id, None)
                    conflicts.add(note_id)
                elif note_id not in conflicts:
                    self.authors[note_id] = account
        try:
            self.cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(self.cache, dict):
                self.cache = {}
        except (OSError, ValueError):
            self.cache = {}

    async def resolve(self, crawler, url: str, note_id: str) -> str:
        if parse_qs(urlsplit(url).query).get("xsec_token"):
            return url
        account = self.authors.get(note_id)
        name = self.names.get(note_id)
        if not account and not name:
            raise XiaohongshuUrlResolutionError(
                "原始链接缺少 xsec_token，且来源数据没有可核对的作者信息"
            )
        identity = account or f"name:{name}"
        async with self._lock:
            # A comments request should not repeat the interactions request's
            # unsuccessful author scan moments later. Do not cache auth errors.
            now = time.monotonic()
            self._not_found = {key: value for key, value in self._not_found.items() if value[0] > now}
            missing = self._not_found.get((identity, note_id))
            if missing:
                raise XiaohongshuUrlResolutionError(missing[1])
            entry = self.cache.get(note_id) or {}
            if (
                entry.get("author") == identity
                and time.time() - entry.get("saved_at", 0) < 1800
                and note_id == urlsplit(str(entry.get("url") or "")).path.rsplit("/", 1)[-1]
                and parse_qs(urlsplit(str(entry.get("url") or "")).query).get("xsec_token")
            ):
                return entry["url"]
            try:
                found = (
                    await recover_author(crawler, account, {note_id}, self.max_pages)
                    if account else await recover_named_author(crawler, name, note_id, self.max_pages)
                )
                if note_id not in found:
                    raise XiaohongshuUrlResolutionError("已核对作者列表，目标笔记当前不可见")
            except XiaohongshuUrlResolutionError as exc:
                if str(exc) in {"达到作者分页上限，目标笔记未找到", "已核对作者列表，目标笔记当前不可见"}:
                    self._not_found[(identity, note_id)] = (time.monotonic() + 120, str(exc))
                raise
            self.cache[note_id] = {"author": identity, "url": found[note_id], "saved_at": time.time()}
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_name(self.cache_path.name + ".tmp")
            temporary.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self.cache_path)
            return found[note_id]
