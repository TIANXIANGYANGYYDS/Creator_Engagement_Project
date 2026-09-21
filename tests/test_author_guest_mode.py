import asyncio
from types import SimpleNamespace

from app.core.config import Settings
from app.models.author import AuthorRequest
from app.services.author_service import AuthorService, DiscoverySession
from app.services.engagement_service import EngagementService


def test_guest_mode_ignores_all_account_secrets_and_saved_browser_state():
    class ForbiddenStore:
        def scoped_state(self, *args):
            raise AssertionError("Guest collection must never read stored login state")

    parent = SimpleNamespace(
        client=object(), xiaohongshu_cookie_enabled=True,
        platform_cookies={"xiaohongshu": "a1=device; web_session=account"},
    )
    service = EngagementService(
        parent, author_guest_only=True, author_douyin_cookie="sessionid=account",
        author_session_store=ForbiddenStore(),
        author_platform_cookies={p: "account=secret" for p in ("kuaishou", "weibo", "bilibili", "toutiao")},
    )
    authors = AuthorService(service)
    crawler = authors._crawler()
    for platform in ("kuaishou", "weibo", "bilibili", "toutiao", "xiaohongshu", "douyin"):
        assert crawler._platform_cookie(platform) == ""
        assert crawler.browser_state(platform) == {"cookies": [], "origins": []}
    assert crawler._author_douyin_configured_cookie == ""
    for platform, uid in [("douyin", "handle"), ("xiaohongshu", "a" * 24)]:
        assert DiscoverySession(authors, AuthorRequest(author_id=uid, media_name=platform)).browser_mode


def test_guest_is_default_even_when_old_cookie_mode_remains_in_environment():
    settings = Settings(_env_file=None, proxy_51_api_url="https://example.com/proxy", douyin_author_session_mode="cookie", douyin_author_cookie="", author_use_saved_sessions=True)
    service = EngagementService.from_settings(settings)
    try:
        assert service.author_guest_only
        assert service.author_douyin_cookie == ""
        assert AuthorService(service)._crawler().author_session_store is None
    finally:
        asyncio.run(service.aclose())
