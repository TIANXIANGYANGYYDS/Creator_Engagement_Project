"""Verify author mappings from supplied public work links before storing them.

Usage: python -m app.manually_execute_script.seed_douyin_authors INPUT.json
       --author-id HANDLE [--author-id OTHER_HANDLE]
Uses a fresh guest browser by default; never loads an account in guest-only mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from app.core.config import Settings
from app.crawlers.authors.douyin_session import DouyinProtocolTransport
from app.crawlers.authors.identity import douyin_matches
from app.crawlers.authors.kuaishou import mobile_detail
from app.crawlers.authors.browser import BrowserTransport
from app.models.author import AuthorRequest, AuthorWork
from app.repositories.author_repository import AuthorRepository
from app.services.author_service import AuthorService
from app.services.engagement_service import EngagementService


async def seed(path: Path, selected: set[str], settings: Settings, *, platform="douyin"):
    if platform not in {"douyin", "kuaishou"}:
        raise ValueError("仅支持抖音和快手来源作品核验")
    if platform == "douyin" and not settings.author_guest_only and settings.douyin_author_session_mode != "cookie":
        raise ValueError("请先配置 DOUYIN_AUTHOR_SESSION_MODE=cookie 和 DOUYIN_AUTHOR_COOKIE")
    rows = json.loads(path.read_text())
    candidates = {}
    for row in rows:
        names = {"抖音", "douyin"} if platform == "douyin" else {"快手", "kuaishou"}
        if row.get("media_name") not in names or not row.get("author_id"):
            continue
        request = AuthorRequest(author_id=str(row["author_id"]), media_name=platform)
        if selected and request.uid not in selected:
            continue
        link = urlsplit(row.get("url") or "")
        hosts = {"www.douyin.com", "www.iesdouyin.com", "douyin.com", "iesdouyin.com"} if platform == "douyin" else {"www.kuaishou.com", "kuaishou.com"}
        if link.hostname not in hosts:
            continue
        pattern = r"/(?:video|note)/(\d+)(?:/|$)" if platform == "douyin" else r"/short-video/([A-Za-z0-9_-]+)(?:/|$)"
        match = re.search(pattern, link.path)
        if match:
            candidates.setdefault(request.uid, set()).add(match.group(1))
    repository = AuthorRepository(settings.mongo_uri, settings.mongo_db_name)
    service = EngagementService.from_settings(settings)
    authors = AuthorService(service)
    results = []
    try:
        # Only identities are written; importing references does not create jobs
        # or alter existing works. Outbound requests use fixed platform hosts.
        for account, work_ids in candidates.items():
            record = {"author_id": account, "verified": False}
            # Retry each reference on fresh leases so one dead proxy does not
            # skip an author's only supplied work link.
            for work_id in (wid for wid in work_ids for _ in range(3)):
                try:
                    async with authors._slot(platform), service.crawler.client.isolated_session_scope(), service.crawler.client.lease_scope():
                        crawler = authors._crawler()
                        crawler._author_identity_cache = {}
                        browser = None
                        try:
                            if platform == "kuaishou":
                                request = AuthorRequest(author_id=account, media_name=platform)
                                work = AuthorWork(work_id=work_id, author_uid=account, media_name=platform, content_type="video", url=f"https://www.kuaishou.com/short-video/{work_id}")
                                work = await mobile_detail(crawler, request, work)
                                if not re.fullmatch(r"[A-Za-z0-9_-]+", work.resolved_author_uid or ""):
                                    continue
                                await repository.save_identity(platform, account, {"uid": account, "profile_id": work.resolved_author_uid}, source_work_id=work_id)
                                record = {"author_id": account, "verified": True, "source_work_id": work_id}
                                break
                            if settings.author_guest_only:
                                browser = BrowserTransport(crawler, authors.browser_slots)
                                transport = await browser.open(f"https://www.douyin.com/video/{work_id}")
                            else:
                                transport = DouyinProtocolTransport(crawler, service.author_douyin_cookie)
                            payload = await transport._get_json(
                                "https://www.douyin.com/aweme/v1/web/aweme/detail/",
                                params={"aweme_id": work_id},
                            )
                            detail = payload.get("aweme_detail") or {}
                            user = detail.get("author") or {}
                            if (payload.get("status_code") != 0 or str(detail.get("aweme_id")) != work_id
                                    or not douyin_matches(user, account)
                                    or not re.fullmatch(r"MS4wLj[A-Za-z0-9_-]{10,250}", str(user.get("sec_uid") or ""))):
                                continue
                            await repository.save_identity("douyin", account, user, source_work_id=work_id)
                            record = {"author_id": account, "verified": True, "source_work_id": work_id}
                            break
                        finally:
                            if browser:
                                await browser.close()
                            await crawler.aclose()
                except Exception as exc:
                    record["error_type"] = type(exc).__name__
            results.append(record)
    finally:
        repository.client.close()
        await service.aclose()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--author-id", action="append", default=[])
    args = parser.parse_args()
    result = asyncio.run(seed(args.input, set(args.author_id), Settings()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
