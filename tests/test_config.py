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
    assert settings.proxy_pool_size == 4
    assert settings.proxy_max_concurrency == 1
    assert settings.collection_max_concurrency == 4
    assert settings.reliability_mode == "enterprise"
    assert settings.protocol_max_attempts == 3
    assert settings.protocol_retry_base_seconds == 1
    assert settings.engagement_cache_ttl_seconds == 120
    assert settings.engagement_cache_max_entries == 1000
    assert settings.browser_max_concurrency == 1
    assert settings.browser_reset_guest_state_on_proxy_change is True


def test_wechat_official_api_credentials_are_optional_secrets(monkeypatch) -> None:
    monkeypatch.setenv("WECHAT_MP_APP_ID", "wx-app-id")
    monkeypatch.setenv("WECHAT_MP_APP_SECRET", "app-secret")

    settings = Settings(_env_file=None)

    assert settings.wechat_mp_app_id == "wx-app-id"
    assert settings.wechat_mp_app_secret.get_secret_value() == "app-secret"
