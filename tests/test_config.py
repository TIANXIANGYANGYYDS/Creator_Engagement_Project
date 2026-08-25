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
    assert settings.strict_anonymous_mode is True
    assert settings.xiaohongshu_session_mode == "disabled"
    assert settings.xiaohongshu_cookie.get_secret_value() == ""
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


def test_wechat_session_bridge_credentials_are_optional_secrets(monkeypatch) -> None:
    monkeypatch.setenv("WECHAT_SESSION_BRIDGE_URL", "http://127.0.0.1:8210")
    monkeypatch.setenv("WECHAT_SESSION_BRIDGE_TOKEN", "local-bridge-token-secret")

    settings = Settings(_env_file=None)

    assert settings.wechat_session_bridge_url == "http://127.0.0.1:8210"
    assert (
        settings.wechat_session_bridge_token.get_secret_value()
        == "local-bridge-token-secret"
    )


def test_wechat_channels_bridge_credentials_are_optional_secrets(monkeypatch) -> None:
    monkeypatch.setenv("WECHAT_CHANNELS_BRIDGE_URL", "http://127.0.0.1:2026")
    monkeypatch.setenv("WECHAT_CHANNELS_BRIDGE_TOKEN", "channels-local-secret")

    settings = Settings(_env_file=None)

    assert settings.wechat_channels_bridge_url == "http://127.0.0.1:2026"
    assert (
        settings.wechat_channels_bridge_token.get_secret_value()
        == "channels-local-secret"
    )


def test_wechat_article_cookie_is_an_independent_secret(monkeypatch) -> None:
    monkeypatch.setenv("CREATOR_ENGAGEMENT_COOKIE", "douyin-cookie")
    monkeypatch.setenv("WECHAT_ARTICLE_COOKIE", "wap_sid2=wechat-session")

    settings = Settings(_env_file=None)

    assert settings.creator_engagement_cookie.get_secret_value() == "douyin-cookie"
    assert (
        settings.wechat_article_cookie.get_secret_value()
        == "wap_sid2=wechat-session"
    )


def test_xiaohongshu_cookie_is_an_independent_opt_in_secret(monkeypatch) -> None:
    monkeypatch.setenv("XIAOHONGSHU_SESSION_MODE", "cookie")
    monkeypatch.setenv(
        "XIAOHONGSHU_COOKIE",
        "a1=xhs-device; web_session=xhs-session",
    )

    settings = Settings(_env_file=None)

    assert settings.xiaohongshu_session_mode == "cookie"
    assert (
        settings.xiaohongshu_cookie.get_secret_value()
        == "a1=xhs-device; web_session=xhs-session"
    )
