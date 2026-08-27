from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.wechat_channels_midu import HttpWeChatChannelsMiduClient


URL_ONE = (
    "https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?"
    "api=openFinderView&extInfo=%7B%22action%22%3A%22openFinderFeed%22%2C"
    "%22feedID%22%3A%22export%2FUzFfBgAAxFirstFeedId1234567890%22%7D"
)
URL_TWO = (
    "https://channels.weixin.qq.com/mobile/commonFinderJsApi.html?"
    "api=openFinderView&extInfo=%7B%22action%22%3A%22openFinderFeed%22%2C"
    "%22feedID%22%3A%22export%2FUzFfBgAAxSecondFeedId123456789%22%7D"
)


def test_midu_client_rejects_unprotected_remote_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        HttpWeChatChannelsMiduClient("http://example.com:8095")


def test_midu_client_batches_url_resolution_and_counter_query() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path == "/history_data/get_md_history_data":
            assert payload["schemeType"] == 3
            assert payload["contentWebpageUrlList"] == [URL_ONE, URL_TWO]
            return httpx.Response(200, json={
                "code": 0,
                "message": "success",
                "data": [
                    {
                        "origin_id": "sk-one",
                        "url": URL_ONE,
                        "like_count": 12,
                        "repost_count": 3,
                    },
                    {
                        "origin_id": "sk-two",
                        "url": URL_TWO,
                        "like_count": 7,
                        "repost_count": 1,
                    },
                ],
            })
        assert request.url.path == "/idata/md/engagement/query"
        assert payload == {"schemeType": 4, "idList": ["sk-one", "sk-two"]}
        return httpx.Response(200, json={
            "code": 200,
            "message": "请求已正常响应",
            "data": [
                {
                    "skId": "sk-one",
                    "skContentExt": {
                        "skViews": 100,
                        "skAttitudesCount": None,
                        "skCommentsCount": 2,
                        "skShareCount": None,
                        "skRepostsCount": 4,
                    },
                },
                {
                    "skId": "sk-two",
                    "skContentExt": {
                        "skViews": None,
                        "skAttitudesCount": 8,
                        "skCommentsCount": 0,
                        "skShareCount": 5,
                        "skRepostsCount": None,
                    },
                },
            ],
        })

    async def scenario() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            source = HttpWeChatChannelsMiduClient(
                "https://127.0.0.1:8095",
                client=client,
            )
            batched = await source.fetch_many([URL_ONE, URL_TWO, URL_ONE])
            cached = await source.fetch_interactions(URL_ONE)
            return batched, cached

    batched, cached = asyncio.run(scenario())

    assert len(calls) == 2
    assert batched[URL_ONE]["stats"] == {
        "views": 100,
        "likes": 12,
        "comments": 2,
        "shares": None,
        "reposts": 4,
    }
    assert batched[URL_TWO]["stats"] == {
        "views": None,
        "likes": 8,
        "comments": 0,
        "shares": 5,
        "reposts": 1,
    }
    assert cached["source_id"] == "sk-one"


def test_midu_client_reports_unindexed_url() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/history_data/get_md_history_data"
        return httpx.Response(200, json={"code": 0, "data": []})

    async def scenario() -> list[str]:
        errors: list[str] = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
        ) as client:
            source = HttpWeChatChannelsMiduClient(
                "https://127.0.0.1:8095",
                client=client,
            )
            for _ in range(2):
                try:
                    await source.fetch_interactions(URL_ONE)
                except PlatformCrawlerError as exc:
                    errors.append(str(exc))
        return errors

    errors = asyncio.run(scenario())

    assert errors == [
        "蜜度近 30 天未收录该视频号链接",
        "蜜度近 30 天未收录该视频号链接",
    ]
    assert calls == 1
