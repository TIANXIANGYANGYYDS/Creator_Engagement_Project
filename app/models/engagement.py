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
    data: EngagementStats


class CommentDataResponse(BaseModel):
    data: list[EngagementComment] = Field(default_factory=list)


class EngagementResult(CollectionResult):
    """Legacy combined result retained for internal compatibility."""

    stats: EngagementStats = Field(default_factory=EngagementStats)
    comments: list[EngagementComment] = Field(default_factory=list)
    next_cursor: str | None = None
    resume_cursor: str | None = Field(default=None, exclude=True)
