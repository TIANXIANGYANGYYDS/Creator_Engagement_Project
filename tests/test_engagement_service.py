from __future__ import annotations

import asyncio

import pytest

from app.core.config import Settings
from app.models.engagement import EngagementResult
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

    async def aclose(self) -> None:
        self.closed = True


def test_fetch_many_preserves_input_order() -> None:
    crawler = FakeCrawler()
    service = EngagementService(crawler)  # type: ignore[arg-type]

    results = asyncio.run(service.fetch_many(["first", "second"], comment_limit=3, concurrency=2))

    assert [result.canonical_url for result in results] == ["first", "second"]
    assert crawler.calls == [("first", 3), ("second", 3)]


def test_required_proxy_mode_requires_api_url() -> None:
    settings = Settings(_env_file=None, proxy_mode="required", proxy_51_api_url="")

    with pytest.raises(ValueError, match="PROXY_51_API_URL"):
        EngagementService.from_settings(settings)
