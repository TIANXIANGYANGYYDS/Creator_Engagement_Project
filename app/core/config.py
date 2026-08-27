from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE_ENV_FILE = PROJECT_ROOT / ".local" / "env" / ".env"


class Settings(BaseSettings):
    """Deployment settings shared by the API, CLI and crawlers."""

    model_config = SettingsConfigDict(
        env_file=ACTIVE_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    llm_api_key: SecretStr = Field(default=SecretStr(""), alias="LLM_API_KEY")
    llm_api_base_url: str = Field(default="", alias="LLM_API_BASE_URL")
    llm_timeout: int = Field(default=30, alias="LLM_TIMEOUT", ge=1)

    mongo_uri: str = Field(default="mongodb://localhost:27017", alias="MONGO_URI")
    mongo_db_name: str = Field(default="creator_engagement", alias="MONGO_DB_NAME")

    proxy_51_api_url: str = Field(default="", alias="PROXY_51_API_URL")
    proxy_mode: Literal["direct", "prefer", "required"] = Field(
        default="prefer",
        alias="PROXY_MODE",
    )
    proxy_pool_size: int = Field(default=8, alias="PROXY_POOL_SIZE", ge=1, le=200)
    proxy_max_concurrency: int = Field(
        default=1,
        alias="PROXY_MAX_CONCURRENCY",
        ge=1,
    )

    strict_anonymous_mode: bool = Field(
        default=True,
        alias="STRICT_ANONYMOUS_MODE",
    )
    xiaohongshu_session_mode: Literal["disabled", "cookie"] = Field(
        default="disabled",
        alias="XIAOHONGSHU_SESSION_MODE",
    )
    xiaohongshu_cookie: SecretStr = Field(
        default=SecretStr(""),
        alias="XIAOHONGSHU_COOKIE",
    )

    creator_engagement_cookie: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "CREATOR_ENGAGEMENT_COOKIE",
            "DOUYIN_SESSION_COOKIE",
        ),
    )
    wechat_article_cookie: SecretStr = Field(
        default=SecretStr(""),
        alias="WECHAT_ARTICLE_COOKIE",
    )
    wechat_mp_app_id: str = Field(default="", alias="WECHAT_MP_APP_ID")
    wechat_mp_app_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="WECHAT_MP_APP_SECRET",
    )
    wechat_mp_access_token: SecretStr = Field(
        default=SecretStr(""),
        alias="WECHAT_MP_ACCESS_TOKEN",
    )
    wechat_session_bridge_url: str = Field(
        default="",
        alias="WECHAT_SESSION_BRIDGE_URL",
    )
    wechat_session_bridge_token: SecretStr = Field(
        default=SecretStr(""),
        alias="WECHAT_SESSION_BRIDGE_TOKEN",
    )
    wechat_channels_bridge_url: str = Field(
        default="",
        alias="WECHAT_CHANNELS_BRIDGE_URL",
    )
    wechat_channels_bridge_token: SecretStr = Field(
        default=SecretStr(""),
        alias="WECHAT_CHANNELS_BRIDGE_TOKEN",
    )
    wechat_channels_midu_url: str = Field(
        default="",
        alias="WECHAT_CHANNELS_MIDU_URL",
    )
    request_timeout_seconds: float = Field(
        default=20,
        alias="REQUEST_TIMEOUT_SECONDS",
        gt=0,
    )
    collection_max_concurrency: int = Field(
        default=8,
        alias="COLLECTION_MAX_CONCURRENCY",
        ge=1,
    )
    reliability_mode: Literal["economy", "enterprise"] = Field(
        default="enterprise",
        alias="RELIABILITY_MODE",
    )
    protocol_max_attempts: int = Field(
        default=3,
        alias="PROTOCOL_MAX_ATTEMPTS",
        ge=1,
        le=10,
    )
    protocol_retry_base_seconds: float = Field(
        default=1,
        alias="PROTOCOL_RETRY_BASE_SECONDS",
        ge=0,
        le=30,
    )
    toutiao_protocol_max_attempts: int = Field(
        default=1,
        alias="TOUTIAO_PROTOCOL_MAX_ATTEMPTS",
        ge=1,
        le=10,
    )
    douyin_protocol_max_attempts: int = Field(
        default=5,
        alias="DOUYIN_PROTOCOL_MAX_ATTEMPTS",
        ge=1,
        le=10,
    )
    engagement_cache_ttl_seconds: float = Field(
        default=120,
        alias="ENGAGEMENT_CACHE_TTL_SECONDS",
        ge=0,
    )
    engagement_cache_max_entries: int = Field(
        default=1000,
        alias="ENGAGEMENT_CACHE_MAX_ENTRIES",
        ge=1,
    )
    browser_fallback_enabled: bool = Field(default=True, alias="BROWSER_FALLBACK_ENABLED")
    browser_timeout_seconds: float = Field(default=35, alias="BROWSER_TIMEOUT_SECONDS", gt=0)
    browser_challenge_wait_seconds: float = Field(
        default=5,
        alias="BROWSER_CHALLENGE_WAIT_SECONDS",
        ge=0,
    )
    browser_headless: bool = Field(default=True, alias="BROWSER_HEADLESS")
    browser_max_concurrency: int = Field(
        default=3,
        alias="BROWSER_MAX_CONCURRENCY",
        ge=1,
    )
    browser_max_attempts: int = Field(
        default=3,
        alias="BROWSER_MAX_ATTEMPTS",
        ge=1,
        le=5,
    )
    browser_geoip_enabled: bool = Field(default=False, alias="BROWSER_GEOIP_ENABLED")
    browser_reset_guest_state_on_proxy_change: bool = Field(
        default=True,
        alias="BROWSER_RESET_GUEST_STATE_ON_PROXY_CHANGE",
    )
    browser_profile_dir: str = Field(
        default=".local/browser-profiles",
        alias="BROWSER_PROFILE_DIR",
    )
    platform_session_dir: str = Field(
        default=".local/platform-sessions",
        alias="PLATFORM_SESSION_DIR",
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8200, alias="API_PORT", ge=1, le=65535)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
