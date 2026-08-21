from __future__ import annotations

from app.core.config import Settings


def test_settings_support_stock_cookie_name_as_fallback(monkeypatch) -> None:
    monkeypatch.delenv("CREATOR_ENGAGEMENT_COOKIE", raising=False)
    monkeypatch.setenv("DOUYIN_SESSION_COOKIE", "caller-owned-cookie")

    settings = Settings(_env_file=None)

    assert settings.creator_engagement_cookie.get_secret_value() == "caller-owned-cookie"


def test_proxy_defaults_are_ready_for_managed_pool() -> None:
    settings = Settings(_env_file=None)

    assert settings.proxy_mode == "prefer"
    assert settings.proxy_pool_size == 2
    assert settings.proxy_max_concurrency == 2
    assert settings.collection_max_concurrency == 4
    assert settings.engagement_cache_ttl_seconds == 120
    assert settings.engagement_cache_max_entries == 1000
    assert settings.browser_max_concurrency == 2
