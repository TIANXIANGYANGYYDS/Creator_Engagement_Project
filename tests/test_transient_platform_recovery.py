from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.crawlers.browser_fallback import BrowserFallback, _should_reload_page
from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platforms.toutiao import unavailable_article


TARGET = "7702018971821736502"
URL = f"https://www.toutiao.com/article/{TARGET}/"
MISSING_HTML = '<div class="error-content"><p class="error-tips">抱歉，你访问的内容不存在</p></div>'


def response(payload=None, *, status=200, text=""):
    return SimpleNamespace(status_code=status, text=text or json.dumps(payload), json=lambda: payload)


def comment_payload(*, target=TARGET, total=0, items=None, offset=20, more=False):
    return {"err_no": 0, "data": items or [], "total_number": total,
            "has_more": more, "offset": offset,
            "group": {"group_id": target, "user_id": 123}}


class RotatingClient:
    proxy_mode = "required"

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.leases = 0
        self.active = 0
        self.calls = []
        self.invalidations = []

    @asynccontextmanager
    async def lease_scope(self):
        self.leases += 1
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1

    def invalidate_active_lease(self, reason):
        self.invalidations.append(reason)

    async def get(self, url, **kwargs):
        self.calls.append((self.leases, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    post = get


class NoBrowser:
    async def fetch(self, *args, **kwargs):
        pytest.fail("A recovered protocol request must not launch a browser")


def test_weibo_recovers_after_three_dead_exits_and_keeps_target_comments():
    client = RotatingClient(
        TimeoutError(), TimeoutError(), TimeoutError(),
        response(text='<script>var request_id = "0123456789abcdef";</script>'),
        response(text='visitor_gray_callback({"retcode":20000000,"data":{"sub":"guest"}});'),
        response({"idstr": "5360144752052327", "mblogid": "RpeDF8BU3", "comments_count": 1}),
        response({"ok": 1, "data": {"data": [{"id": "c1", "text": "根评论", "total_number": 2}], "max_id": 0}}),
    )
    result = asyncio.run(EngagementCrawler(
        client=client, max_protocol_attempts=3, browser_fallback=NoBrowser(),
    ).fetch_comments("http://weibo.com/8404950668/RpeDF8BU3", "微博", 1))
    assert result.coverage == "partial" and result.protocol_attempts == 4
    assert result.work_id == "5360144752052327" and result.total_comments == 1
    assert result.comments[0].text == "根评论"
    assert client.leases == 4 and client.active == 0 and len(client.invalidations) == 3
    assert all(lease == 4 for lease, _, _ in client.calls[3:])


@pytest.mark.parametrize("failure", [TimeoutError(), ConnectionError(), response(status=403), response(status=429)])
def test_toutiao_transient_failure_recovers_before_browser(failure):
    client = RotatingClient(failure, response(comment_payload()))
    result = asyncio.run(EngagementCrawler(
        client=client, platform_protocol_max_attempts={"toutiao": 1}, browser_fallback=NoBrowser(),
    ).fetch_comments(URL, "今日头条", 1))
    assert result.coverage == "partial" and result.total_comments == 0
    assert result.protocol_attempts == 2 and client.leases == 2
    assert len(client.invalidations) == 1 and client.active == 0


@pytest.mark.parametrize("failure", [response(status=401), response({}), response(comment_payload(target="999"))])
def test_toutiao_login_and_invalid_business_data_do_not_get_extra_exits(failure):
    client = RotatingClient(failure)
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(URL, "今日头条", 1))
    assert result.coverage in {"failed", "blocked"}
    assert result.total_comments is None and client.leases == 1


@pytest.mark.parametrize("platform,url,attempts", [
    ("toutiao", URL, 1), ("weibo", "https://weibo.com/8404950668/RpeDF8BU3", 3),
])
def test_transient_recovery_has_a_finite_attempt_limit(platform, url, attempts):
    client = RotatingClient(*(TimeoutError() for _ in range(attempts + 2)))
    result = asyncio.run(EngagementCrawler(client=client, max_protocol_attempts=attempts,
        protocol_retry_base_seconds=0).fetch_comments(url, platform, 1))
    assert result.coverage == "failed" and result.protocol_attempts == attempts + 2
    assert client.leases == attempts + 2 and client.active == 0


def test_fixed_direct_exit_does_not_receive_proxy_recovery():
    client = RotatingClient(TimeoutError())
    client.proxy_mode = "direct"
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(URL, "今日头条", 1))
    assert result.coverage == "failed" and client.leases == 1


def test_spent_protocol_budget_does_not_start_extra_attempt(monkeypatch):
    clock = [0]
    monkeypatch.setattr("app.crawlers.engagement.monotonic", lambda: clock[0])
    client = RotatingClient(TimeoutError())
    original = client.get

    async def get(*args, **kwargs):
        clock[0] = 26
        return await original(*args, **kwargs)

    client.get = get
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(URL, "今日头条", 1))
    assert result.coverage == "failed" and client.leases == 1


def test_extra_attempt_is_cancelled_at_recovery_deadline_and_releases_lease(monkeypatch):
    shift = [0]
    monkeypatch.setattr("app.crawlers.engagement.monotonic", lambda: monotonic() + shift[0])
    client = RotatingClient()

    async def get(*args, **kwargs):
        if client.leases == 1:
            shift[0] = 24.99
            raise TimeoutError()
        await asyncio.sleep(10)
        pytest.fail("Recovery deadline should cancel the request")

    client.get = get
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(URL, "今日头条", 1))
    assert result.coverage == "failed" and "恢复超时" in result.reason
    assert client.leases == 2 and client.active == 0
    assert len(client.invalidations) == 2


def test_toutiao_browser_requests_the_requested_page_in_its_own_session():
    page = SimpleNamespace(url=URL, content=AsyncMock(return_value="<html></html>"),
        evaluate=AsyncMock(return_value={"status": 200, "text": json.dumps(comment_payload(
            total=41, offset=40, more=True, items=[{"comment": {
                "id": "c21", "text": "第二页一级评论", "reply_count": 3,
                "replies": [{"text": "不要抓取回复正文"}],
            }}]))}))
    result = asyncio.run(BrowserFallback()._parse_responses(page, [], URL, "toutiao", TARGET,
        page=2, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "partial" and result.stats.comments == 41
    assert result.next_cursor == "40" and len(result.comments) == 1
    assert result.comments[0].text == "第二页一级评论" and result.comments[0].replies == 3
    query = parse_qs(urlsplit(page.evaluate.call_args.args[1]).query)
    assert query["offset"] == ["20"] and query["group_id"] == [TARGET]
    assert query["item_id"] == [TARGET]


def test_toutiao_browser_rejects_wrong_target_in_api_response():
    page = SimpleNamespace(url=URL, content=AsyncMock(return_value="<html></html>"),
        evaluate=AsyncMock(return_value={"status": 200,
            "text": json.dumps(comment_payload(target="999"))}))
    result = asyncio.run(BrowserFallback()._parse_responses(page, [], URL, "toutiao", TARGET,
        page=1, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "failed" and result.stats.comments is None


def test_toutiao_browser_does_not_click_publish_or_login_controls():
    page = SimpleNamespace(locator=lambda *args: pytest.fail("Do not click the publish control"))
    asyncio.run(BrowserFallback()._interact_with_page(page, "toutiao", 2, include_comments=True))


def test_toutiao_page_challenge_does_not_hide_an_accessible_public_comment_api():
    page = SimpleNamespace(url=URL,
        content=AsyncMock(return_value="<html><body>安全验证</body></html>"),
        evaluate=AsyncMock(return_value={"status": 200, "text": json.dumps(comment_payload())}))
    result = asyncio.run(BrowserFallback()._parse_responses(page, [], URL, "toutiao", TARGET,
        page=1, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "partial" and result.stats.comments == 0


def test_toutiao_challenge_stays_blocked_when_target_api_is_also_blocked():
    page = SimpleNamespace(url=URL,
        content=AsyncMock(return_value="<html><body>安全验证</body></html>"),
        locator=lambda *args: SimpleNamespace(inner_text=AsyncMock(return_value="安全验证")),
        evaluate=AsyncMock(return_value={"status": 403, "text": "denied"}))
    result = asyncio.run(BrowserFallback()._parse_responses(page, [], URL, "toutiao", TARGET,
        page=1, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "blocked" and result.stats.comments is None
    assert "安全验证" in result.reason


def test_toutiao_missing_target_stops_browser_reloads_and_retries():
    payload = comment_payload()
    payload["group"]["user_id"] = 0
    page = SimpleNamespace(url=URL, content=AsyncMock(return_value=MISSING_HTML),
        evaluate=AsyncMock(return_value={"status": 200, "text": json.dumps(payload)}))
    browser = BrowserFallback()
    result = asyncio.run(browser._parse_responses(page, [], URL, "toutiao", TARGET,
        page=1, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "unsupported" and result.retryable is False
    assert result.stats.comments is None and not _should_reload_page("toutiao", result)
    browser.fetch = AsyncMock(return_value=result)
    crawler = EngagementCrawler(client=RotatingClient(), browser_fallback=browser, max_browser_attempts=3)
    asyncio.run(crawler._run_browser_fallback(URL, "toutiao", TARGET, page=1, limit=20,
        include_stats=False, include_comments=True))
    assert browser.fetch.await_count == 1


def test_toutiao_missing_article_route_does_not_hide_accessible_comments():
    page = SimpleNamespace(url=URL, content=AsyncMock(return_value=MISSING_HTML),
        evaluate=AsyncMock(return_value={"status": 200, "text": json.dumps(comment_payload(
            total=1, items=[{"comment": {"id": "c1", "text": "公开评论"}}]))}))
    result = asyncio.run(BrowserFallback()._parse_responses(page, [], URL, "toutiao", TARGET,
        page=1, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "partial" and result.stats.comments == 1
    assert result.comments[0].text == "公开评论"


def test_toutiao_empty_later_page_does_not_mark_whole_work_unavailable():
    payload = comment_payload(offset=60)
    payload["group"]["user_id"] = "0"
    page = SimpleNamespace(url=URL, content=AsyncMock(return_value=MISSING_HTML),
        evaluate=AsyncMock(return_value={"status": 200, "text": json.dumps(payload)}))
    result = asyncio.run(BrowserFallback()._parse_responses(page, [], URL, "toutiao", TARGET,
        page=3, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "failed" and result.stats.comments is None
    assert getattr(result, "retryable", None) is not False


def test_toutiao_browser_recovery_deadline_cancels_and_cleans_up(monkeypatch):
    calls = [0]

    def clock():
        calls[0] += 1
        return monotonic() + (59.99 if calls[0] > 1 else 0)

    monkeypatch.setattr("app.crawlers.engagement.monotonic", clock)
    cleaned = []

    class SlowBrowser:
        async def fetch(self, *args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cleaned.append(True)

    crawler = EngagementCrawler(client=RotatingClient(), browser_fallback=SlowBrowser(), max_browser_attempts=3)
    result = asyncio.run(crawler._run_browser_fallback(URL, "toutiao", TARGET,
        page=1, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "failed" and "60 秒" in result.reason
    assert cleaned == [True]


@pytest.mark.parametrize("html,url", [
    (MISSING_HTML, "https://www.toutiao.com/"),
    (MISSING_HTML, "https://www.toutiao.com/article/999/"),
    (MISSING_HTML, f"https://other.example/article/{TARGET}/"),
    ("<article>抱歉，你访问的内容不存在</article>", URL),
    ('<script>"抱歉，你访问的内容不存在"</script>', URL),
])
def test_unavailable_requires_target_error_page_evidence(html, url):
    assert unavailable_article(html, TARGET, url) is None


def test_toutiao_explicit_invalid_parameters_stop_retry_and_browser():
    client = RotatingClient(response({"err_no": 1, "message": "params illegal"}))
    result = asyncio.run(EngagementCrawler(client=client, browser_fallback=NoBrowser(),
        max_protocol_attempts=3).fetch_comments(URL, "今日头条", 1))
    assert result.coverage == "unsupported" and result.total_comments is None
    assert "params illegal" in result.reason
    assert client.leases == 1 and len(client.calls) == 1


@pytest.mark.parametrize("verified", [False, True])
def test_toutiao_article_404_requires_comment_evidence(verified):
    article = response(status=404, text=MISSING_HTML)
    article.url = URL
    payload = comment_payload()
    payload["group"]["user_id"] = 123 if verified else 0
    client = RotatingClient(article, response(payload))
    result = asyncio.run(EngagementCrawler(client=client, browser_fallback=NoBrowser())
        .fetch_interactions(URL, "今日头条"))
    assert result.coverage == ("partial" if verified else "unsupported")
    assert result.stats.comments == (0 if verified else None)
    assert len(client.calls) == 2


def test_toutiao_captured_document_keeps_its_url_after_redirect():
    from app.crawlers.platforms.browser_page_context import BrowserPageContext

    page = SimpleNamespace(url="https://www.toutiao.com/",
        content=AsyncMock(return_value="<html>homepage</html>"))
    context = BrowserPageContext(page, URL, MISSING_HTML)
    captured = asyncio.run(context._get_response(URL))
    assert captured.url == URL and captured.text == MISSING_HTML
    assert unavailable_article(captured.text, TARGET, captured.url) is not None
    current = asyncio.run(BrowserPageContext(page, URL)._get_response(URL))
    assert current.url == page.url
    assert unavailable_article(current.text, TARGET, current.url) is None


@pytest.mark.parametrize("coverage,target,stats,enriched", [
    ("partial", TARGET, {"likes": 0, "shares": 3}, True),
    ("failed", TARGET, {}, False),
    ("blocked", TARGET, {}, False),
    ("partial", "999", {"likes": 99}, False),
    ("partial", TARGET, {"comments": 99}, False),
])
def test_toutiao_enriches_comment_only_stats_without_losing_verified_fields(coverage, target, stats, enriched):
    from app.models.engagement import EngagementResult, EngagementStats

    client = RotatingClient(response(text="<html>challenge</html>"),
        response(comment_payload(total=2, items=[{"comment": {"id": "c1", "text": "root"}}])))
    browser = SimpleNamespace(fetch=AsyncMock(return_value=EngagementResult(
        platform="toutiao", canonical_url=URL, work_id=target, coverage=coverage,
        source="browser: article SSR itemCounter", stats=EngagementStats(**stats))))
    result = asyncio.run(EngagementCrawler(client=client, browser_fallback=browser,
        max_browser_attempts=3).fetch_interactions(URL, "头条"))
    assert result.coverage == "partial" and result.stats.comments == 2
    assert result.stats.likes == (0 if enriched else None)
    assert result.stats.shares == (3 if enriched else None)
    assert result.stats.views is None
    assert browser.fetch.await_count == 1 and client.leases == 1
    assert ("browser:" in result.source) is enriched


def test_toutiao_optional_enrichment_has_one_bounded_attempt(monkeypatch):
    calls = [0]
    cleaned = []

    def clock():
        calls[0] += 1
        return monotonic() + (24.99 if calls[0] > 1 else 0)

    class SlowBrowser:
        async def fetch(self, *args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cleaned.append(True)

    crawler = EngagementCrawler(client=RotatingClient(), browser_fallback=SlowBrowser(),
        max_browser_attempts=3)
    monkeypatch.setattr("app.crawlers.engagement.monotonic", clock)
    result = asyncio.run(crawler._run_browser_fallback(URL, "toutiao", TARGET,
        page=1, limit=20, include_stats=True, include_comments=False, enrich_stats=True))
    assert result.coverage == "failed" and "25 秒" in result.reason
    assert cleaned == [True]


def test_toutiao_browser_preserves_login_redirect_as_missing_stats_reason():
    page = SimpleNamespace(url="https://sso.toutiao.com/login/?service=target",
        content=AsyncMock(return_value="<html>登录</html>"))
    page.context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=
        SimpleNamespace(status=200, text=AsyncMock(return_value=json.dumps(comment_payload()))))))
    result = asyncio.run(BrowserFallback()._parse_responses(page, [], URL, "toutiao", TARGET,
        page=1, limit=20, include_stats=True, include_comments=False))
    assert result.coverage == "partial" and result.stats.comments == 0
    assert "跳转到登录页" in result.reason and result.stats_block_reason
