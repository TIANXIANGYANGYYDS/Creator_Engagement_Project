from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from app.crawlers.platforms.registry import MEDIA_ALIASES


AUTHOR_PLATFORMS = {
    "douyin",
    "kuaishou",
    "weibo",
    "bilibili",
    "xiaohongshu",
    "toutiao",
    "sina_blog",
}
AUTHOR_ALIASES = {
    **MEDIA_ALIASES,
    "sina_blog": "sina_blog",
    "新浪博客": "sina_blog",
    "新浪": "sina_blog",
    "新浪微博": "weibo",
    "今日头条微头条": "toutiao",
    "微头条": "toutiao",
}


class AuthorRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, populate_by_name=True
    )

    uid: str = Field(
        min_length=1, max_length=256, validation_alias=AliasChoices("author_id", "uid")
    )
    media_name: str = Field(validation_alias=AliasChoices("media_name", "platform"))

    @model_validator(mode="after")
    def validate_author(self):
        platform = AUTHOR_ALIASES.get(re.sub(r"\s+", "", self.media_name.casefold()))
        if platform not in AUTHOR_PLATFORMS:
            raise ValueError(
                "作者内容支持抖音、快手、微博、B站、小红书、今日头条，以及兼容的新浪博客"
            )
        self.media_name = platform
        if platform in {"douyin", "xiaohongshu", "kuaishou"}:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", self.uid):
                raise ValueError("author_id 请提供平台账号或内部 UID，不是昵称或 URL")
            if platform == "xiaohongshu" and re.fullmatch(r"[0-9a-fA-F]{24}", self.uid):
                self.uid = self.uid.lower()
        elif platform == "toutiao":
            if not re.fullmatch(r"[A-Za-z0-9_=-]{1,256}", self.uid):
                raise ValueError("author_id 请提供数字 UID 或作者主页 token")
        elif not re.fullmatch(r"[0-9]{1,24}", self.uid):
            raise ValueError("该平台 author_id 必须为数字账号 ID")
        return self


class AuthorWork(BaseModel):
    work_id: str
    author_uid: str
    resolved_author_uid: str | None = None
    author_name: str = ""
    media_name: str
    content_type: Literal["video", "article", "short_post", "image_post"]
    url: str
    title: str = ""
    description: str = ""
    text: str = ""
    published_at: datetime | None = None
    cover_url: str | None = None
    media_urls: list[str] = Field(default_factory=list)
    duration_ms: int | None = None
    content_complete: bool = True
    detail_status: Literal["pending", "complete", "failed"] = "complete"
    # Retrieval hints are durable internally, but are not a public raw payload.
    detail_context: dict = Field(default_factory=dict, exclude=True, repr=False)
    error: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @computed_field
    @property
    def author_id(self) -> str:
        return self.author_uid


class AuthorIdentityResponse(BaseModel):
    @computed_field
    @property
    def author_id(self) -> str:
        return self.uid


class AuthorPage(AuthorIdentityResponse):
    uid: str
    media_name: str
    data: list[AuthorWork] = Field(default_factory=list)
    next_cursor: str | None = None
    coverage: Literal["complete", "partial"] = "partial"
    reason: str = ""
    total: int | None = None


class AuthorJobStatus(AuthorIdentityResponse):
    job_id: str
    uid: str
    media_name: str
    status: Literal["queued", "running", "completed", "partial", "failed", "cancelled"]
    saved_count: int = 0
    detail_pending_count: int = 0
    detail_completed_count: int = 0
    detail_failed_count: int = 0
    phase: Literal["queued", "discovery", "retry_wait", "details", "finished"] = "queued"
    retry_count: int = 0
    next_retry_at: datetime | None = None
    discovery_done: bool = False
    pages_completed: int = 0
    complete: bool = False
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    expires_at: datetime | None = None


class AuthorJobResults(BaseModel):
    job: AuthorJobStatus
    data: list[AuthorWork]
    next_cursor: str | None = None


class AuthorBatchItem(BaseModel):
    index: int
    author_id: str | None = None
    media_name: str | None = None
    job_id: str | None = None
    status: Literal["accepted", "duplicate", "rejected"]
    error: str | None = None


class AuthorBatchSubmission(BaseModel):
    total: int
    accepted_count: int
    duplicate_count: int
    rejected_count: int
    data: list[AuthorBatchItem]
