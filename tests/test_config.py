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
    assert settings.proxy_max_concurrency == 2
