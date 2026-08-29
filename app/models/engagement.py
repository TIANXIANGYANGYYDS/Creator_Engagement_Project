from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EngagementPlatform = Literal[
    "douyin",
    "toutiao",
    "wechat",
    "wechat_channels",
    "xiaohongshu",
    "haokan",
    "kuaishou",
    "bilibili",
    "weibo",
]
EngagementCoverage = Literal[
    "complete",
    "partial",
    "blocked",
    "failed",
    "unsupported",
]
CommentPaginationMode = Literal[
    "all_public_pages",
    "paged_until_blocked",
    "first_public_page",
    "unavailable",
]
CollectOperation = Literal["interactions", "comments"]
CollectStatus = Literal["success", "failed"]
JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
WebhookStatus = Literal["pending", "sent", "failed"]


class EngagementStats(BaseModel):
    model_config = ConfigDict(extra="allow")

    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    favorites: int | None = Field(default=None, ge=0)
    coins: int | None = Field(default=None, ge=0)
    danmaku: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    recommendations: int | None = Field(default=None, ge=0)


class EngagementComment(BaseModel):
    model_config = ConfigDict(extra="allow")

    comment_id: str = Field(min_length=1)
    author: str = ""
    text: str = ""
    created_at: datetime | None = None
    likes: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)


class CommentCapabilities(BaseModel):
    """Anonymous comment coverage exposed by the current deployment."""

    root_comments: CommentPaginationMode
    anonymous: bool
    note: str = ""


def unavailable_comment_capabilities() -> CommentCapabilities:
    return CommentCapabilities(
        root_comments="unavailable",
        anonymous=False,
        note="capability metadata was not supplied",
    )


class CollectionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: EngagementPlatform
    canonical_url: str
    work_id: str
    coverage: EngagementCoverage
    reason: str = ""
    source: str = ""
    protocol_attempts: int = Field(default=1, ge=1)


class InteractionResult(CollectionResult):
    model_config = ConfigDict(extra="ignore")

    stats: EngagementStats = Field(default_factory=EngagementStats)


class CommentPageResult(CollectionResult):
    model_config = ConfigDict(extra="ignore")

    page: int = Field(ge=1)
    comments: list[EngagementComment] = Field(default_factory=list)
    next_page: int | None = Field(default=None, ge=1)
    next_cursor: str | None = None
    total_comments: int | None = Field(default=None, ge=0)
    capabilities: CommentCapabilities = Field(
        default_factory=unavailable_comment_capabilities
    )


class InteractionDataResponse(BaseModel):
    media_name: str
    data: EngagementStats


class CommentDataResponse(BaseModel):
    media_name: str
    comments: list[EngagementComment] = Field(default_factory=list)


class CollectItemRequest(BaseModel):
    url: str = Field(min_length=1)
    media_name: str = Field(min_length=1)
    type: CollectOperation
    page: int | None = Field(default=None, ge=1, strict=True)


class CollectRequest(BaseModel):
    items: list[CollectItemRequest] = Field(min_length=1)


class CollectResultData(BaseModel):
    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    total_comments: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    favorites: int | None = Field(default=None, ge=0)
    coins: int | None = Field(default=None, ge=0)
    danmaku: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    recommendations: int | None = Field(default=None, ge=0)
    comments: list[EngagementComment] | None = None


class CollectItemResponse(BaseModel):
    url: str
    media_name: str
    type: CollectOperation
    status: CollectStatus
    complete: bool
    result: CollectResultData
    error: str | None = None


class CollectResponse(BaseModel):
    data: list[CollectItemResponse]
    duration_ms: int = Field(ge=0)
    cost_yuan: float = Field(ge=0)


class JobItemRequest(CollectItemRequest):
    item_id: str = Field(min_length=1)


class CreateJobRequest(BaseModel):
    items: list[JobItemRequest] = Field(min_length=1)
    webhook_url: str | None = Field(default=None, min_length=1)


class JobProgress(BaseModel):
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    success: int = Field(ge=0)
    failed: int = Field(ge=0)


class JobSubmitResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: JobProgress
    duration_ms: int = Field(ge=0)
    cost_yuan: float = Field(ge=0)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    webhook_status: WebhookStatus | None = None
    error: str | None = None


class JobItemResponse(CollectItemResponse):
    item_id: str
    duration_ms: int = Field(default=0, ge=0)


class JobResultsResponse(BaseModel):
    job_id: str
    status: JobStatus
    data: list[JobItemResponse] = Field(default_factory=list)
    next_cursor: str | None = None
    available_count: int = Field(ge=0)
    total: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    cost_yuan: float = Field(ge=0)


class EngagementResult(CollectionResult):
    """Legacy combined result retained for internal compatibility."""

    stats: EngagementStats = Field(default_factory=EngagementStats)
    comments: list[EngagementComment] = Field(default_factory=list)
    next_cursor: str | None = None
    resume_cursor: str | None = Field(default=None, exclude=True)
