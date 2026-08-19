from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EngagementPlatform = Literal[
    "douyin",
    "toutiao",
    "wechat",
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


class EngagementComment(BaseModel):
    model_config = ConfigDict(extra="allow")

    comment_id: str = Field(min_length=1)
    author: str = ""
    text: str = ""
    created_at: datetime | None = None
    likes: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)


class CollectionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: EngagementPlatform
    canonical_url: str
    work_id: str
    coverage: EngagementCoverage
    reason: str = ""
    source: str = ""


class InteractionResult(CollectionResult):
    model_config = ConfigDict(extra="ignore")

    stats: EngagementStats = Field(default_factory=EngagementStats)


class CommentPageResult(CollectionResult):
    model_config = ConfigDict(extra="ignore")

    page: int = Field(ge=1)
    comments: list[EngagementComment] = Field(default_factory=list)
    next_page: int | None = Field(default=None, ge=1)
    total_comments: int | None = Field(default=None, ge=0)


class EngagementResult(CollectionResult):
    """Legacy combined result retained for internal compatibility."""

    stats: EngagementStats = Field(default_factory=EngagementStats)
    comments: list[EngagementComment] = Field(default_factory=list)
    next_cursor: str | None = None
