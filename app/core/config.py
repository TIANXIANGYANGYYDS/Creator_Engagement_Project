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
    proxy_pool_size: int = Field(default=4, alias="PROXY_POOL_SIZE", ge=1, le=200)
    proxy_max_concurrency: int = Field(
        default=2,
        alias="PROXY_MAX_CONCURRENCY",
        ge=1,
    )

    creator_engagement_cookie: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "CREATOR_ENGAGEMENT_COOKIE",
            "DOUYIN_SESSION_COOKIE",
        ),
    )
    request_timeout_seconds: float = Field(
        default=20,
        alias="REQUEST_TIMEOUT_SECONDS",
        gt=0,
    )
    browser_fallback_enabled: bool = Field(default=True, alias="BROWSER_FALLBACK_ENABLED")
    browser_timeout_seconds: float = Field(default=35, alias="BROWSER_TIMEOUT_SECONDS", gt=0)
    browser_challenge_wait_seconds: float = Field(
        default=5,
        alias="BROWSER_CHALLENGE_WAIT_SECONDS",
        ge=0,
    )
    browser_headless: bool = Field(default=True, alias="BROWSER_HEADLESS")
    browser_profile_dir: str = Field(
        default=".local/browser-profiles",
        alias="BROWSER_PROFILE_DIR",
    )
    platform_session_dir: str = Field(
        default=".local/platform-sessions",
        alias="PLATFORM_SESSION_DIR",
    )
    aidata_api_key: SecretStr = Field(default=SecretStr(""), alias="AIDATA_API_KEY")
    aidata_base_url: str = Field(default="https://aidata.vip", alias="AIDATA_BASE_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8200, alias="API_PORT", ge=1, le=65535)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
