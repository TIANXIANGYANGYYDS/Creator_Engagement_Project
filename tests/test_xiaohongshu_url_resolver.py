from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platforms import PLATFORM_HANDLERS
from app.crawlers.xiaohongshu_url_resolver import XiaohongshuUrlResolver, recover_named_author
from app.models.engagement import EngagementResult, EngagementStats


NOTE = "6aad6b8c0000000028002d0a"
RAW = f"https://www.xiaohongshu.com/discovery/item/{NOTE}"


def test_raw_url_reaches_handler_with_recovered_token(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps([{"url": RAW, "author_id": "27247934734"}]))
    resolver = XiaohongshuUrlResolver(source, tmp_path / "cache.json")
    resolved = f"https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=token&xsec_source=pc_user"
    calls = []

    async def fake_recover(crawler, author, targets, pages):
        calls.append((author, targets))
        return {NOTE: resolved}

    async def handler(crawler, url, work_id, limit, **kwargs):
        calls.append(url)
        return EngagementResult(
            platform="xiaohongshu", canonical_url=url, work_id=work_id,
            coverage="partial", stats=EngagementStats(likes=1),
        )

    monkeypatch.setattr("app.crawlers.xiaohongshu_url_resolver.recover_author", fake_recover)
    monkeypatch.setitem(PLATFORM_HANDLERS, "xiaohongshu", handler)
    crawler = EngagementCrawler(client=object(), xiaohongshu_url_resolver=resolver)

    async def run():
        first = await crawler.fetch_interactions(RAW, "xiaohongshu")
        second = await crawler.fetch_interactions(RAW, "xiaohongshu")
        return first, second

    first, second = asyncio.run(run())
    assert first.stats.likes == second.stats.likes == 1
    assert calls == [("27247934734", {NOTE}), resolved, resolved]


def test_unknown_author_reports_unavailable_without_guessing(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps([{"url": RAW, "author_id": ""}]))
    resolver = XiaohongshuUrlResolver(source, tmp_path / "cache.json")
    crawler = EngagementCrawler(client=object(), xiaohongshu_url_resolver=resolver)
    result = asyncio.run(crawler.fetch_interactions(RAW, "xiaohongshu"))
    assert result.coverage == "unsupported"
    assert "作者信息" in result.reason


def test_named_author_requires_note_membership(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps([{"url": RAW, "author_name": "同名作者"}]))
    resolver = XiaohongshuUrlResolver(source, tmp_path / "cache.json")
    token_url = f"https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=verified"

    async def fake_named(crawler, name, note_id, pages):
        assert (name, note_id) == ("同名作者", NOTE)
        return {NOTE: token_url}

    monkeypatch.setattr("app.crawlers.xiaohongshu_url_resolver.recover_named_author", fake_named)
    assert asyncio.run(resolver.resolve(object(), RAW, NOTE)) == token_url


def test_named_author_checks_exact_name_and_target_note(monkeypatch) -> None:
    monkeypatch.setattr("app.crawlers.xiaohongshu_url_resolver.signed_headers", lambda *a, **k: {})
    first = "111111111111111111111111"
    second = "222222222222222222222222"
    token_url = f"https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=verified"
    checked = []

    class FakeCrawler:
        def _platform_cookie(self, platform):
            return "a1=device; web_session=account"

        async def _post_json(self, url, **kwargs):
            return {"success": True, "data": {"users": [
                {"id": first, "name": "同名作者"},
                {"id": second, "name": "同名作者"},
                {"id": "333333333333333333333333", "name": "其他作者"},
            ]}}

    async def fake_recover(crawler, uid, targets, max_pages):
        checked.append(uid)
        return {NOTE: token_url} if uid == second else {}

    monkeypatch.setattr("app.crawlers.xiaohongshu_url_resolver.recover_author", fake_recover)
    found = asyncio.run(recover_named_author(FakeCrawler(), "同名作者", NOTE, 20))
    assert found == {NOTE: token_url}
    assert checked == [first, second]
