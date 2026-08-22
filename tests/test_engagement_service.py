from __future__ import annotations

import asyncio
from time import monotonic

import pytest

from app.core.config import Settings
from app.models.engagement import CommentPageResult, EngagementComment, EngagementResult, InteractionResult
from app.services.engagement_service import (
    EngagementService,
    PlatformTrafficPolicy,
)


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
            stats={"likes": 7, "comments": 1},
        )

    async def fetch_comments(self, url: str, media_name: str, page: int) -> CommentPageResult:
        self.calls.append((f"{media_name}:{url}", page))
        return CommentPageResult(
            platform="toutiao",
            canonical_url=url,
            work_id="123",
            coverage="partial",
            page=page,
            comments=[EngagementComment(comment_id="c1", text="comment request")],
            next_page=page + 1,
            total_comments=1,
        )

    async def aclose(self) -> None:
        self.closed = True


def test_fetch_many_preserves_input_order() -> None:
    crawler = FakeCrawler()
    service = EngagementService(crawler)  # type: ignore[arg-type]

    results = asyncio.run(service.fetch_many(["first", "second"], comment_limit=3, concurrency=2))

    assert [result.canonical_url for result in results] == ["first", "second"]
    assert crawler.calls == [("first", 3), ("second", 3)]


def test_separate_apis_use_independent_platform_requests() -> None:
    crawler = FakeCrawler()
    service = EngagementService(crawler)  # type: ignore[arg-type]

    interaction = asyncio.run(service.fetch_interactions("url-1", "头条"))
    comments = asyncio.run(service.fetch_comments("url-1", "toutiao", 1))

    assert interaction.canonical_url == "url-1"
    assert interaction.stats.likes == 7
    assert comments.comments[0].text == "comment request"
    assert comments.next_page == 2
    assert crawler.calls == [
        ("头条:url-1", 0),
        ("toutiao:url-1", 1),
    ]


def test_deeper_comment_page_remains_an_independent_collection() -> None:
    crawler = FakeCrawler()
    service = EngagementService(crawler)  # type: ignore[arg-type]

    comments = asyncio.run(service.fetch_comments("url-2", "toutiao", 3))

    assert comments.page == 3
    assert crawler.calls == [("toutiao:url-2", 3)]


def test_concurrent_identical_interaction_requests_are_coalesced() -> None:
    class SlowCrawler(FakeCrawler):
        async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
            await asyncio.sleep(0.01)
            return await super().fetch_interactions(url, media_name)

    async def run() -> tuple[InteractionResult, InteractionResult, SlowCrawler]:
        crawler = SlowCrawler()
        service = EngagementService(crawler)  # type: ignore[arg-type]
        first, second = await asyncio.gather(
            service.fetch_interactions("same-url", "头条"),
            service.fetch_interactions("same-url", "toutiao"),
        )
        return first, second, crawler

    first, second, crawler = asyncio.run(run())

    assert first.stats.likes == 7
    assert second.stats.likes == 7
    assert crawler.calls == [("头条:same-url", 0)]


def test_collection_concurrency_is_bounded() -> None:
    class CountingCrawler(FakeCrawler):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_active = 0

        async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return await super().fetch_interactions(url, media_name)

    async def run() -> CountingCrawler:
        crawler = CountingCrawler()
        service = EngagementService(  # type: ignore[arg-type]
            crawler,
            collection_max_concurrency=2,
        )
        await asyncio.gather(*(
            service.fetch_interactions(f"url-{index}", "toutiao")
            for index in range(4)
        ))
        return crawler

    crawler = asyncio.run(run())

    assert crawler.max_active == 2


def test_required_proxy_mode_requires_api_url() -> None:
    settings = Settings(_env_file=None, proxy_mode="required", proxy_51_api_url="")

    with pytest.raises(ValueError, match="PROXY_51_API_URL"):
        EngagementService.from_settings(settings)


def test_retryable_failure_is_not_cached() -> None:
    class BlockedCrawler(FakeCrawler):
        async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
            self.calls.append((f"{media_name}:{url}", 0))
            return InteractionResult(
                platform="toutiao",
                canonical_url=url,
                work_id="123",
                coverage="blocked",
                reason="temporary challenge",
            )

    crawler = BlockedCrawler()
    service = EngagementService(crawler)  # type: ignore[arg-type]

    asyncio.run(service.fetch_interactions("same-url", "toutiao"))
    asyncio.run(service.fetch_interactions("same-url", "toutiao"))

    assert len(crawler.calls) == 2


def test_meaningful_result_is_cached() -> None:
    crawler = FakeCrawler()
    service = EngagementService(crawler)  # type: ignore[arg-type]

    asyncio.run(service.fetch_interactions("same-url", "toutiao"))
    asyncio.run(service.fetch_interactions("same-url", "toutiao"))

    assert crawler.calls == [("toutiao:same-url", 0)]


def test_platform_policy_serializes_and_spaces_request_starts() -> None:
    class TimedCrawler(FakeCrawler):
        def __init__(self) -> None:
            super().__init__()
            self.started: list[float] = []
            self.active = 0
            self.max_active = 0

        async def fetch_interactions(self, url: str, media_name: str) -> InteractionResult:
            self.started.append(monotonic())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.005)
            self.active -= 1
            return await super().fetch_interactions(url, media_name)

    async def run() -> TimedCrawler:
        crawler = TimedCrawler()
        service = EngagementService(  # type: ignore[arg-type]
            crawler,
            collection_max_concurrency=4,
            platform_policies={
                "xiaohongshu": PlatformTrafficPolicy(
                    max_concurrency=1,
                    min_interval_seconds=1,
                    interactions_min_interval_seconds=0.02,
                ),
            },
        )
        await asyncio.gather(
            service.fetch_interactions("url-1", "xiaohongshu"),
            service.fetch_interactions("url-2", "xiaohongshu"),
        )
        return crawler

    crawler = asyncio.run(run())

    assert crawler.max_active == 1
    assert crawler.started[1] - crawler.started[0] >= 0.018
