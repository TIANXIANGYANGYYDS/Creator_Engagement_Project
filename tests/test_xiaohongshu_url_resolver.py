from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platforms import PLATFORM_HANDLERS
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.xiaohongshu_url_resolver import XiaohongshuUrlResolver, recover_named_author, recover_note
from app.models.engagement import EngagementResult, EngagementStats


NOTE = "6aad6b8c0000000028002d0a"
RAW = f"https://www.xiaohongshu.com/discovery/item/{NOTE}"


def test_note_id_lookup_reaches_handler_without_author_and_reuses_cache(tmp_path, monkeypatch):
    class UnavailableSource:
        def find(self, *args):
            pytest.fail("ID lookup must not need author metadata")

    monkeypatch.setattr("app.crawlers.xiaohongshu_url_resolver.signed_headers", lambda *a, **k: {})
    cache = tmp_path / "cache.json"
    resolver = XiaohongshuUrlResolver(tmp_path / "absent.json", cache,
                                     source_collection=UnavailableSource())
    crawler = EngagementCrawler(client=object(), xiaohongshu_url_resolver=resolver)
    monkeypatch.setattr(crawler, "_platform_cookie", lambda _: "a1=device; web_session=account")
    requests = []
    token = "signed+/value=="

    async def post(url, **kwargs):
        requests.append(url)
        assert url == "https://edith.xiaohongshu.com/api/sns/agentspark/web/note/detail"
        assert json.loads(kwargs["data"]) == {"note_ids": [NOTE]}
        await asyncio.sleep(0)
        return {"success": True, "code": 0, "data": {"content_detail_list": [
            {"id": "2" * 24, "xsec_token": "wrong-note"},
            {"id": NOTE, "xsec_token": token},
        ]}}

    async def handler(crawler, url, work_id, limit, **kwargs):
        assert work_id == NOTE
        assert urlsplit(url).path == f"/explore/{NOTE}"
        assert parse_qs(urlsplit(url).query) == {"xsec_token": [token], "xsec_source": ["pc_dots"]}
        return EngagementResult(platform="xiaohongshu", canonical_url=url, work_id=work_id,
                                coverage="partial", stats=EngagementStats(likes=7))

    monkeypatch.setattr(crawler, "_post_json", post)
    monkeypatch.setitem(PLATFORM_HANDLERS, "xiaohongshu", handler)

    async def run():
        first, second = await asyncio.gather(
            crawler.fetch_interactions(RAW, "xiaohongshu"),
            crawler.fetch_interactions(RAW, "xiaohongshu"),
        )
        assert first.stats.likes == second.stats.likes == 7

    asyncio.run(run())
    assert len(requests) == 1
    saved = json.loads(cache.read_text())[NOTE]
    assert saved["source"] == "agentspark_note_detail"
    assert parse_qs(urlsplit(saved["url"]).query)["xsec_token"] == [token]
    assert cache.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("rows", [
    [], [{"id": "2" * 24, "xsec_token": "wrong-note"}],
    [{"id": NOTE}], [{"id": NOTE, "xsec_token": " "}],
    [{"id": NOTE, "xsec_token": 123}],
])
def test_note_id_lookup_never_accepts_missing_or_wrong_note_token(rows, monkeypatch):
    monkeypatch.setattr("app.crawlers.xiaohongshu_url_resolver.signed_headers", lambda *a, **k: {})

    class Crawler:
        def _platform_cookie(self, platform):
            return "a1=device; web_session=account"

        async def _post_json(self, *args, **kwargs):
            return {"success": True, "code": 0, "data": {"content_detail_list": rows}}

    assert asyncio.run(recover_note(Crawler(), NOTE)) is None


@pytest.mark.parametrize("payload,error", [
    ({"success": False, "code": -100}, PlatformBlockedError),
    ({"success": False, "code": 461}, PlatformBlockedError),
    ({"success": False, "code": -1, "msg": "unavailable"}, PlatformCrawlerError),
    ({"data": {"content_detail_list": []}}, PlatformCrawlerError),
    ({"success": True, "data": []}, PlatformCrawlerError),
    ({"success": True, "data": {"content_detail_list": [None]}}, PlatformCrawlerError),
    ({"success": True, "data": {"content_detail_list": [
        {"id": NOTE, "xsec_token": "one"}, {"id": NOTE, "xsec_token": "two"},
    ]}}, PlatformCrawlerError),
])
def test_note_id_lookup_errors_do_not_query_authors_or_poison_cache(tmp_path, monkeypatch, payload, error):
    monkeypatch.setattr("app.crawlers.xiaohongshu_url_resolver.signed_headers", lambda *a, **k: {})
    calls = []

    class Crawler:
        def _platform_cookie(self, platform):
            return "a1=device; web_session=account"

        async def _post_json(self, *args, **kwargs):
            calls.append(1)
            return payload

    class UnavailableSource:
        def find(self, *args):
            pytest.fail("lookup failure must propagate without author requests")

    cache = tmp_path / "cache.json"
    resolver = XiaohongshuUrlResolver(tmp_path / "absent.json", cache,
                                     source_collection=UnavailableSource())

    async def run():
        for _ in range(2):
            with pytest.raises(error):
                await resolver.resolve(Crawler(), RAW, NOTE)

    asyncio.run(run())
    assert len(calls) == 2 and not cache.exists() and not resolver._not_found


def test_valid_note_cache_needs_no_author_or_source_database(tmp_path):
    class UnavailableSource:
        def find(self, *args):
            pytest.fail("cached note must not query author metadata")

    cache = tmp_path / "cache.json"
    resolved = RAW + "?xsec_token=verified"
    cache.write_text(json.dumps({NOTE: {"url": resolved, "saved_at": time.time(), "author": "old-source"}}))
    resolver = XiaohongshuUrlResolver(tmp_path / "absent.json", cache, source_collection=UnavailableSource())
    assert asyncio.run(resolver.resolve(object(), RAW, NOTE)) == resolved


@pytest.mark.parametrize("entry", [
    {"url": RAW + "?xsec_token=expired", "saved_at": 1},
    {"url": RAW + "?xsec_token=future", "saved_at": 10**12},
    {"url": RAW + "?xsec_token=bad-time", "saved_at": "now"},
    {"url": RAW.replace(NOTE, "2" * 24) + "?xsec_token=wrong-note"},
    {"url": RAW.replace(".com/", ".com.evil.test/") + "?xsec_token=bad-host"},
    {"url": RAW.replace("https://", "http://") + "?xsec_token=bad-scheme"},
    {"url": RAW.replace("https://", "https://user:pass@") + "?xsec_token=credentials"},
    {"url": RAW},
    {"url": "https://[invalid"},
    "malformed-entry",
])
def test_invalid_note_cache_cannot_bypass_context_recovery(tmp_path, entry):
    from app.crawlers.xiaohongshu_url_resolver import XiaohongshuUrlResolutionError
    if isinstance(entry, dict):
        entry = {"saved_at": time.time(), **entry}
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({NOTE: entry}))
    resolver = XiaohongshuUrlResolver(tmp_path / "absent.json", cache)
    with pytest.raises(XiaohongshuUrlResolutionError):
        asyncio.run(resolver.resolve(object(), RAW, NOTE))


def test_production_raw_data_resolves_original_url_using_exact_profile(tmp_path, monkeypatch):
    uid = '5d2b14c9000000001603aa54'
    source = tmp_path / 'production.json'
    source.write_text(json.dumps([{'rawData': {'url': RAW, 'author_id': '49321294179',
        'board_url': f'https://www.xiaohongshu.com/user/profile/{uid}',
        'author_full_info': {'profile_url': f'https://www.xiaohongshu.com/user/profile/{uid}'}}}]))
    resolver = XiaohongshuUrlResolver(source, tmp_path / 'cache.json')
    async def recover(crawler, author, targets, pages):
        assert author == uid and targets == {NOTE}
        return {NOTE: RAW + '?xsec_token=verified'}
    monkeypatch.setattr('app.crawlers.xiaohongshu_url_resolver.recover_author', recover)
    assert asyncio.run(resolver.resolve(object(), RAW, NOTE)) == RAW + '?xsec_token=verified'


@pytest.mark.parametrize('conflicting_rows', [False, True])
def test_production_author_conflicts_do_not_fall_back_to_name(tmp_path, conflicting_rows):
    a = '1' * 24
    b = '2' * 24
    row = {'url': RAW, 'author_name': '同名', 'board_url': f'https://www.xiaohongshu.com/user/profile/{a}'}
    if conflicting_rows:
        rows = [row, {**row, 'board_url': f'https://www.xiaohongshu.com/user/profile/{b}'},
                {'url': RAW, 'author_name': '同名'}]
    else:
        row['author_full_info'] = {'profile_url': f'https://www.xiaohongshu.com/user/profile/{b}'}
        rows = [row]
    source = tmp_path / 'production.json'
    source.write_text(json.dumps([{'rawData': r} for r in rows]))
    resolver = XiaohongshuUrlResolver(source, tmp_path / 'cache.json')
    assert NOTE not in resolver.authors and NOTE not in resolver.names


def test_profile_uid_does_not_accept_lookalike_domain(tmp_path):
    source = tmp_path / 'source.json'
    source.write_text(json.dumps([{'rawData': {'url': RAW,
        'board_url': 'https://www.xiaohongshu.com.evil.test/user/profile/' + '1' * 24}}]))
    resolver = XiaohongshuUrlResolver(source, tmp_path / 'cache.json')
    assert NOTE not in resolver.authors


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
    assert "尚未取得该笔记的有效访问上下文" in result.reason
    assert "调用方无需补充" in result.reason


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


def test_negative_author_scan_is_shared_briefly_then_expires(tmp_path, monkeypatch):
    from app.crawlers.xiaohongshu_url_resolver import XiaohongshuUrlResolutionError
    source = tmp_path / 'source.json'
    source.write_text(json.dumps([{'url': RAW, 'author_id': 'author'}]))
    resolver = XiaohongshuUrlResolver(source, tmp_path / 'cache.json')
    clock = [100.0]
    calls = []
    async def recover(*args):
        calls.append(1)
        raise XiaohongshuUrlResolutionError('达到作者分页上限，目标笔记未找到')
    monkeypatch.setattr('app.crawlers.xiaohongshu_url_resolver.recover_author', recover)
    monkeypatch.setattr('app.crawlers.xiaohongshu_url_resolver.time.monotonic', lambda: clock[0])
    async def run():
        for _ in range(2):
            with pytest.raises(XiaohongshuUrlResolutionError):
                await resolver.resolve(object(), RAW, NOTE)
        assert len(calls) == 1
        clock[0] += 121
        with pytest.raises(XiaohongshuUrlResolutionError):
            await resolver.resolve(object(), RAW, NOTE)
        assert len(calls) == 2
    asyncio.run(run())


class SourceCollection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def find(self, query, projection):
        self.calls.append((query, projection))
        return self

    def max_time_ms(self, value):
        assert value == 3000
        return self

    def limit(self, value):
        return self.rows[:value]


def test_new_raw_url_uses_current_source_without_caller_author_or_token(tmp_path, monkeypatch):
    uid = '607f092b000000000100b5bc'
    collection = SourceCollection([{'raw': {'url': RAW,
        'author_profile_url': f'https://www.xiaohongshu.com/user/profile/{uid}'}}])
    resolver = XiaohongshuUrlResolver(tmp_path / 'absent.json', tmp_path / 'cache.json',
                                     source_collection=collection)
    calls = []
    async def recover(crawler, account, targets, pages):
        calls.append(account)
        return {NOTE: RAW + '?xsec_token=verified'}
    monkeypatch.setattr('app.crawlers.xiaohongshu_url_resolver.recover_author', recover)
    async def run():
        for _ in range(2):
            assert await resolver.resolve(object(), RAW, NOTE) == RAW + '?xsec_token=verified'
    asyncio.run(run())
    assert calls == [uid] and len(collection.calls) == 1
    query, projection = collection.calls[0]
    assert RAW in query['$or'][0]['url']['$in']
    assert not any('count' in key or 'content' in key for key in projection)


def test_current_source_conflicts_cannot_fall_back_to_name(tmp_path, monkeypatch):
    from app.crawlers.xiaohongshu_url_resolver import XiaohongshuUrlResolutionError
    collection = SourceCollection([{'raw': {'url': RAW, 'author_name': '同名',
        'author_profile_url': f'https://www.xiaohongshu.com/user/profile/{uid}'}}
        for uid in ['1' * 24, '2' * 24]])
    resolver = XiaohongshuUrlResolver(tmp_path / 'absent.json', tmp_path / 'cache.json',
                                     source_collection=collection)
    with pytest.raises(XiaohongshuUrlResolutionError):
        asyncio.run(resolver.resolve(object(), RAW, NOTE))
    assert NOTE in resolver.conflicts
    assert NOTE not in resolver.authors and NOTE not in resolver.names


def test_current_source_failure_is_sanitized_and_not_negative_cached(tmp_path):
    from app.crawlers.xiaohongshu_url_resolver import XiaohongshuUrlResolutionError
    class BrokenCollection(SourceCollection):
        def find(self, *args):
            self.calls.append(1)
            raise RuntimeError('secret database address')
    collection = BrokenCollection([])
    resolver = XiaohongshuUrlResolver(tmp_path / 'absent.json', tmp_path / 'cache.json',
                                     source_collection=collection)
    async def run():
        for _ in range(2):
            with pytest.raises(XiaohongshuUrlResolutionError, match='来源查询暂不可用') as error:
                await resolver.resolve(object(), RAW, NOTE)
            assert 'secret' not in str(error.value)
    asyncio.run(run())
    assert len(collection.calls) == 2


def test_new_batch_source_is_loaded_without_restarting_worker(tmp_path, monkeypatch):
    from app.crawlers.xiaohongshu_url_resolver import XiaohongshuUrlResolutionError
    source = tmp_path / 'source.json'
    source.write_text('[]')
    resolver = XiaohongshuUrlResolver(source, tmp_path / 'cache.json')
    calls = []

    async def recover(crawler, name, note_id, pages):
        calls.append((name, note_id))
        return {NOTE: RAW + '?xsec_token=verified'}

    monkeypatch.setattr('app.crawlers.xiaohongshu_url_resolver.recover_named_author', recover)

    async def run():
        with pytest.raises(XiaohongshuUrlResolutionError):
            await resolver.resolve(object(), RAW, NOTE)
        source.write_text(json.dumps([{'url': RAW, 'author_name': '新批次作者'}]))
        assert await resolver.resolve(object(), RAW, NOTE) == RAW + '?xsec_token=verified'

    asyncio.run(run())
    assert calls == [('新批次作者', NOTE)]


def test_partial_source_update_can_be_retried_and_retains_known_authors(tmp_path, monkeypatch):
    import os
    from app.crawlers.xiaohongshu_url_resolver import XiaohongshuUrlResolutionError
    source = tmp_path / 'source.json'
    old_note = '1' * 24
    source.write_text(json.dumps([{'url': RAW.replace(NOTE, old_note), 'author_id': 'old'}]))
    resolver = XiaohongshuUrlResolver(source, tmp_path / 'cache.json')
    original_stat = source.stat()

    async def recover(crawler, account, targets, pages):
        assert account == 'new' and targets == {NOTE}
        return {NOTE: RAW + '?xsec_token=verified'}

    monkeypatch.setattr('app.crawlers.xiaohongshu_url_resolver.recover_author', recover)

    async def run():
        source.write_text('[{')
        with pytest.raises(XiaohongshuUrlResolutionError):
            await resolver.resolve(object(), RAW, NOTE)
        assert resolver.authors[old_note] == 'old'
        source.write_text(json.dumps([{'url': RAW, 'author_id': 'new'}]))
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        assert await resolver.resolve(object(), RAW, NOTE) == RAW + '?xsec_token=verified'

    asyncio.run(run())


def test_canceled_source_read_does_not_skip_update_on_next_request(tmp_path, monkeypatch):
    import threading
    source = tmp_path / 'source.json'
    source.write_text('[]')
    resolver = XiaohongshuUrlResolver(source, tmp_path / 'cache.json')
    source.write_text(json.dumps([{'url': RAW, 'author_id': 'new'}]))
    original_read = resolver._read_updated_source
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def delayed_read():
        entered.set()
        release.wait(timeout=2)
        try:
            return original_read()
        finally:
            finished.set()

    async def recover(crawler, account, targets, pages):
        return {NOTE: RAW + '?xsec_token=verified'}

    monkeypatch.setattr(resolver, '_read_updated_source', delayed_read)
    monkeypatch.setattr('app.crawlers.xiaohongshu_url_resolver.recover_author', recover)

    async def run():
        task = asyncio.create_task(resolver.resolve(object(), RAW, NOTE))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
        monkeypatch.setattr(resolver, '_read_updated_source', original_read)
        assert await resolver.resolve(object(), RAW, NOTE) == RAW + '?xsec_token=verified'

    asyncio.run(run())
