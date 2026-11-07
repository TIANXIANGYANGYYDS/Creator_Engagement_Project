import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.crawlers.browser_fallback import BrowserFallback, _parse_kuaishou, _parse_kuaishou_guest
from app.models.engagement import EngagementStats


def test_nested_apollo_fields_survive_partial_detail_and_comment_failure():
    stats, comments, total, next_page, _ = _parse_kuaishou_guest({
        "apolloState": {"defaultClient": {
            "VisionVideoDetailPhoto:target": {"id": "target", "viewCount": 214, "realLikeCount": 0},
            "VisionVideoDetailPhoto:other": {"id": "other", "viewCount": 9999},
        }},
        "detailPayload": {"data": {"visionVideoDetail": {"status": 1,
            "photo": {"id": "target", "viewCount": None}}}},
        "commentError": "AbortError", "reached": False,
    }, "target")
    assert stats.views == 214 and stats.likes == 0
    assert total is None and not comments and next_page is None


def test_captured_comments_require_matching_request_and_exclude_reply_bodies():
    payload = {"result": 1, "commentCountV2": 4, "pcursorV2": "no_more",
        "rootCommentsV2": [{"commentId": "root", "content": "root text",
            "subCommentCount": 3, "replies": [{"commentId": "reply", "content": "nested"}]}]}
    url = "https://www.kuaishou.com/rest/v/photo/comment/list"
    for request in (None, '{"photoId":"other"}'):
        stats, comments, total, source = _parse_kuaishou(url, payload, "target",
            EngagementStats(), [], request_body=request)
        assert total is None and comments == [] and source == ""
    stats, comments, total, source = _parse_kuaishou(url, payload, "target",
        EngagementStats(), [], request_body='{"photoId":"target"}')
    assert total == 4 and [c.comment_id for c in comments] == ["root"]
    assert comments[0].replies == 3


def test_graphql_capture_reads_exact_detail_but_never_recommendation_counters():
    url = "https://www.kuaishou.com/graphql"
    detail = {"data": {"visionVideoDetail": {"status": 1,
        "photo": {"id": "target", "viewCount": 5, "realLikeCount": 0}},
        "visionShortVideoReco": {"feeds": [{"id": "other", "viewCount": 99999}]}}}
    stats, _, _, source = _parse_kuaishou(url, detail, "target", EngagementStats(), [])
    assert stats.views == 5 and stats.likes == 0 and source
    detail["data"].pop("visionVideoDetail")
    stats, _, _, source = _parse_kuaishou(url, detail, "target", EngagementStats(), [])
    assert stats.views is None and source == ""


def test_detail_success_cannot_turn_missing_comment_page_into_success():
    browser = BrowserFallback()
    browser._kuaishou_guest_payload = AsyncMock(return_value={
        "apolloPhoto": {"id": "target", "viewCount": 5, "commentCount": 4},
        "reached": False, "commentError": "AbortError",
    })
    page = SimpleNamespace(locator=lambda _: SimpleNamespace(inner_text=AsyncMock(return_value="")))
    result = asyncio.run(browser._parse_responses(page, [],
        "https://www.kuaishou.com/short-video/target", "kuaishou", "target",
        page=2, limit=20, include_stats=False, include_comments=True))
    assert result.coverage == "failed" and result.comments == []
    assert "一级评论页" in result.reason


@pytest.mark.parametrize("payload", [
    {"errors": [{"message": "Need captcha"}]},
    {"data": {"result": 400002, "url": "https://captcha.zt.kuaishou.com/iframe/index.html?type=1"}},
])
def test_graphql_captcha_is_reported_instead_of_generic_missing_data(payload):
    browser = BrowserFallback()
    browser._kuaishou_guest_payload = AsyncMock(return_value={
        "detailPayload": payload,
        "commentPage": {"result": 1, "pcursorV2": "no_more"}, "reached": False,
    })
    page = SimpleNamespace(locator=lambda _: SimpleNamespace(inner_text=AsyncMock(return_value="")))
    result = asyncio.run(browser._parse_responses(page, [],
        "https://www.kuaishou.com/short-video/target", "kuaishou", "target",
        page=1, limit=20, include_stats=True, include_comments=False))
    assert result.coverage == "blocked" and "要求验证码" in result.reason
    assert result.stats.comments is None


def test_saved_kuaishou_guest_cookies_are_scoped_and_expiry_checked(tmp_path):
    from app.crawlers.engagement import EngagementCrawler
    from app.crawlers.platform_session import PlatformSessionStore

    store = PlatformSessionStore(tmp_path)
    store.state_path("kuaishou").write_text(json.dumps({"cookies": [
        {"name": "did", "value": "current", "domain": ".kuaishou.com", "path": "/", "expires": -1},
        {"name": "kww", "value": "old", "domain": ".kuaishou.com", "path": "/", "expires": 1},
        {"name": "other", "value": "secret", "domain": ".example.com", "path": "/", "expires": -1},
    ]}))
    crawler = EngagementCrawler(client=SimpleNamespace(), session_store=store)
    assert crawler._platform_cookie("kuaishou") == "did=current"


@pytest.mark.parametrize("has_comments", [False, True])
def test_guest_login_gate_keeps_verified_comments_and_stops_detail_retries(has_comments):
    browser = BrowserFallback()
    browser._kuaishou_guest_payload = AsyncMock(return_value={
        "detailPayload": {"data": {"visionVideoDetail": {"status": 1040, "photo": None}}},
        "commentPage": ({"result": 1, "rootCommentsV2": [], "commentCountV2": 0,
                         "pcursorV2": "no_more"} if has_comments else {"result": 1}),
        "reached": has_comments,
    })
    page = SimpleNamespace(locator=lambda _: SimpleNamespace(inner_text=AsyncMock(return_value="")))
    result = asyncio.run(browser._parse_responses(page, [],
        "https://www.kuaishou.com/short-video/target", "kuaishou", "target",
        page=1, limit=20, include_stats=True, include_comments=has_comments))
    assert "status=1040" in result.reason and "登录查看更多作品" in result.reason
    assert result.retryable is False
    assert result.stats.comments == (0 if has_comments else None)
    assert result.coverage == ("partial" if has_comments else "unsupported")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required for browser JS contract")
def test_browser_js_aborts_comments_without_discarding_apollo():
    class Page:
        async def evaluate(self, script, arguments):
            # Run the actual browser function with a stalled fetch and a short
            # clock; verify abort/cleanup rather than matching source text.
            harness = r'''
const nativeSetTimeout = setTimeout;
globalThis.location = {hostname: "www.kuaishou.com"};
let activeTimers = new Set();
globalThis.setTimeout = (cb, ms) => {
  const id = nativeSetTimeout(cb, 1); activeTimers.add(id); return id;
};
const nativeClearTimeout = clearTimeout;
globalThis.clearTimeout = id => { activeTimers.delete(id); nativeClearTimeout(id); };
globalThis.__APOLLO_STATE__ = {defaultClient: {
  "VisionVideoDetailPhoto:target": {id:"target", viewCount:214, realLikeCount:0}
}};
let paths = [];
globalThis.fetch = async (path, options) => {
  paths.push(path);
  return await new Promise((resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("stalled", "AbortError")));
  });
};
'''
            harness += "(" + script + ")(" + json.dumps(arguments) + ").then(value => console.log(JSON.stringify({value,paths,timers:activeTimers.size})));"
            result = subprocess.run(["node", "-"], input=harness, text=True,
                capture_output=True, check=True, timeout=5)
            data = json.loads(result.stdout)
            assert data["paths"] == ["/rest/v/photo/comment/list"]
            assert data["timers"] == 0
            return data["value"]

    payload = asyncio.run(BrowserFallback()._kuaishou_guest_payload(Page(), "target", 1, False))
    assert payload["apolloPhoto"]["viewCount"] == 214
    assert payload["reached"] is False and "AbortError" in payload["commentError"]
