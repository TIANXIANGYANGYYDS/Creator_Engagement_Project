from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.models.engagement import CommentPageResult, EngagementResult, InteractionResult
from app.services.engagement_service import EngagementService


class FakeCrawler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    async def fetch(self, url: str, *, comment_limit: int) -> EngagementResult:
        self.calls.append((url, comment_limit))
        return EngagementResult(
            platform="toutiao",
            canonical_url=url,
            work_id=url.rsplit("/", 1)[-1],
            coverage="partial",
        )

    async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
        self.calls.append((f"{media_name}:{url}", 0))
        return InteractionResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
        )

    async def fetch_comments(self, url: str, media_name: str, page: int) -> CommentPageResult:
        self.calls.append((f"{media_name}:{url}", page))
        return CommentPageResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
            page=page,
        )

    async def aclose(self) -> None:
        self.closed = True


def test_fetch_many_preserves_input_order() -> None:
    crawler = FakeCrawler()
    service = EngagementService(crawler)  # type: ignore[arg-type]

    results = asyncio.run(service.fetch_many(["first", "second"], comment_limit=3, concurrency=2))

    assert [result.canonical_url for result in results] == ["first", "second"]
    assert crawler.calls == [("first", 3), ("second", 3)]


def test_service_exposes_separate_interaction_and_comment_calls() -> None:
    crawler = FakeCrawler()
    service = EngagementService(crawler)  # type: ignore[arg-type]

    interaction = asyncio.run(service.fetch_interactions("url-1", "头条"))
    comments = asyncio.run(service.fetch_comments("url-2", "toutiao", 3))

    assert interaction.canonical_url == "url-1"
    assert comments.page == 3
    assert crawler.calls == [("头条:url-1", 0), ("toutiao:url-2", 3)]


def test_required_proxy_mode_requires_api_url() -> None:
    settings = Settings(_env_file=None, proxy_mode="required", proxy_51_api_url="")

    with pytest.raises(ValueError, match="PROXY_51_API_URL"):
        EngagementService.from_settings(settings)
