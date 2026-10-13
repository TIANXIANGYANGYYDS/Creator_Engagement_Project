import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.status_errors import status_error
from app.crawlers.engagement import EngagementCrawler
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.crawlers.platforms import PLATFORM_HANDLERS
from app.crawlers.platforms.common import result_error
from app.crawlers.authors.common import AuthorSessionError, AuthorCursorError
from app.models.engagement import InteractionResult
from app.models.author import AuthorRequest
from app.services import status_alerts as alerts_module
from app.services.status_alerts import StatusAlerts
from app.services.engagement_service import EngagementService
from app.services.author_service import AuthorService, DiscoverySession


@pytest.mark.parametrize('error,category', [
    ('engagement endpoint blocked with HTTP 401', 'login_expired'),
    ('小红书登录态已失效，请重新扫码授权', 'login_expired'),
    ('engagement endpoint blocked with HTTP 429', 'risk_limited'),
    ('抖音接口被 ArgusSecurityPlugin 拒绝：访客请求缺少有效设备标识或签名', 'risk_limited'),
    ('抖音详情接口返回 HTTP 200 空包；当前会话缺少可验证的设备风控字段或已失效', 'protocol_error'),
    ('engagement endpoint returned HTTP 503', 'upstream_error'),
    ('bilibili view returned code -101', 'login_expired'),
    ('bilibili WBI reply returned code -352', 'risk_limited'),
    (httpx.ConnectError(''), 'transport_error'),
    (TimeoutError(), 'transport_error'),
    (AttributeError('boom'), 'program_error'),
    (AuthorSessionError('current route refuses session'), 'session_rejected'),
    ('HTTP 404', None),
    ('HTTP 410', None),
    ('数据缺少点赞、收藏，评论为空', None),
    ('评论未返回有效列表，可能需要登录/验证', None),
    (PlatformBlockedError('微博视频详情未返回 MID'), None),
    (PlatformCrawlerError('bilibili view payload is unavailable'), None),
    (ValueError('invalid URL'), None),
    (AuthorCursorError('bad cursor'), None),
])
def test_only_positive_status_evidence(error, category):
    assert status_error(error) == category


def rows(alerts):
    with alerts.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM alerts')]


def test_cross_process_dedup_retry_and_cooldown(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=1800000000.0)
    monkeypatch.setattr(alerts_module.time, 'time', lambda: clock.now)
    alerts = StatusAlerts(tmp_path, enabled=True)
    alerts.record('douyin', 'comments', 'HTTP 429')
    original = rows(alerts)[0]['incident_id']
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda _: StatusAlerts(tmp_path, enabled=True).record('douyin', 'comments', 'HTTP 429'), range(8)))
    assert len(rows(alerts)) == 1 and rows(alerts)[0]['occurrences'] == 9
    notify = AsyncMock(side_effect=[RuntimeError('HTTP 503'), 'om-ok'])
    async def run():
        assert await alerts.dispatch(notify) == 0
        assert rows(alerts)[0]['delivery_error'] == 'HTTP 503'
        assert await alerts.dispatch(notify) == 0
        clock.now += 300
        restarted = StatusAlerts(tmp_path, enabled=True)
        assert await restarted.dispatch(notify) == 1
        assert notify.await_args.args[1] == original
        restarted.record('douyin', 'comments', 'HTTP 429')
        assert rows(alerts)[0]['incident_id'] == original
        clock.now += 1800
        restarted.record('douyin', 'comments', 'HTTP 429')
        assert rows(alerts)[0]['incident_id'] != original
    asyncio.run(run())
    assert (tmp_path / 'alerts.sqlite3').stat().st_mode & 0o777 == 0o600


def test_missing_data_and_cached_results_do_not_alert(tmp_path):
    alerts = StatusAlerts(tmp_path, enabled=True)
    result = InteractionResult(platform='douyin', canonical_url='https://www.douyin.com/video/123',
                               work_id='123', coverage='partial', reason='未取得完整互动字段')
    crawler = SimpleNamespace(fetch_interactions=AsyncMock(return_value=result))
    service = EngagementService(crawler, status_alerts=alerts)
    async def run():
        await service.fetch_interactions(result.canonical_url, '抖音')
        assert rows(alerts) == []
        result.coverage = 'blocked'
        result.reason = 'HTTP 429'
        service._result_cache.clear()
        await service.fetch_interactions(result.canonical_url, '抖音')
        await service.fetch_interactions(result.canonical_url, '抖音')
        assert len(rows(alerts)) == 1 and rows(alerts)[0]['occurrences'] == 1
    asyncio.run(run())


@pytest.mark.parametrize('platform', list(PLATFORM_HANDLERS))
@pytest.mark.parametrize('operation', ['interactions', 'comments'])
def test_every_registered_platform_queues_status_errors(tmp_path, platform, operation):
    alerts = StatusAlerts(tmp_path, enabled=True)
    result = result_error(platform, 'https://example.com/item', '123', 'failed', AttributeError('test crash'))
    assert result._status_error == 'program_error'
    alerts.result(result, operation)
    stored = rows(alerts)
    assert len(stored) == 1
    report = json.loads(stored[0]['report'])
    assert (report['platform'], report['operation'], report['status']) == (platform, operation, 'program_error')
    assert '_status_error' not in result.model_dump()


def test_exception_metadata_survives_real_collector_conversion(tmp_path, monkeypatch):
    crawler = EngagementCrawler(client=SimpleNamespace(), max_protocol_attempts=1)
    async def fail(*args, **kwargs):
        return result_error('douyin', 'https://www.douyin.com/video/123', '123', 'failed', AttributeError('test'))
    monkeypatch.setattr(crawler, '_fetch', fail)
    async def run():
        interaction = await crawler.fetch_interactions('https://www.douyin.com/video/123', 'douyin')
        comments = await crawler.fetch_comments('https://www.douyin.com/video/123', 'douyin', 1)
        assert interaction._status_error == comments._status_error == 'program_error'
        assert '_status_error' not in comments.model_dump()
    asyncio.run(run())


def test_xhs_session_faults_delegated_to_existing_monitor(tmp_path):
    alerts = StatusAlerts(tmp_path, enabled=True, xhs_monitor=True)
    alerts.record('xiaohongshu', 'comments', '小红书登录态已失效，请重新扫码授权')
    alerts.record('xiaohongshu', 'interactions', '小红书会话暂停：risk_limited HTTP 471')
    alerts.record('xiaohongshu', 'interactions', '小红书常驻浏览器不可用')
    assert rows(alerts) == []
    alerts.record('xiaohongshu', 'comments', AttributeError('bug'))
    assert len(rows(alerts)) == 1


def test_author_background_discovery_and_detail_are_observed(tmp_path, monkeypatch):
    alerts = StatusAlerts(tmp_path, enabled=True)
    service = AuthorService(EngagementService(SimpleNamespace(), status_alerts=alerts))
    request = AuthorRequest(author_id='123', media_name='weibo')
    session = DiscoverySession(service, request)
    monkeypatch.setattr(session, '_fetch_page', AsyncMock(side_effect=AuthorSessionError('会话已失效')))
    monkeypatch.setattr(service, '_fetch_detail', AsyncMock(side_effect=TimeoutError()))
    async def run():
        with pytest.raises(AuthorSessionError):
            await session.fetch_page(request)
        with pytest.raises(TimeoutError):
            await service.fetch_detail(request, None)
    asyncio.run(run())
    assert {json.loads(r['report'])['operation'] for r in rows(alerts)} == {'author_list', 'author_detail'}


def test_store_failure_does_not_replace_original_exception(tmp_path):
    root = tmp_path / 'file'
    root.write_text('not a directory')
    alerts = StatusAlerts(root, enabled=True)
    with pytest.raises(TimeoutError):
        with alerts.observe('weibo', 'comments'):
            raise TimeoutError('original')


def test_card_redacts_secrets_and_keeps_request_trace(tmp_path):
    alerts = StatusAlerts(tmp_path, enabled=True)
    alerts.record('weibo', 'comments', 'HTTP 403 https://example.com/?token=secret Cookie: private', request_id='trace-123')
    notify = AsyncMock(return_value='om-ok')
    asyncio.run(alerts.dispatch(notify))
    card = json.dumps(notify.await_args.args[0], ensure_ascii=False)
    assert 'secret' not in card and 'private' not in card
    assert all(s in card for s in ['微博', '评论', 'trace-123', '追踪 ID', '风控'])


def test_api_5xx_alerts_but_4xx_does_not(tmp_path):
    from fastapi import FastAPI
    from fastapi.responses import Response
    from app.api.reliability import RequestLoggingMiddleware
    app = FastAPI()
    alerts = StatusAlerts(tmp_path, enabled=True)
    app.state.engagement_service = SimpleNamespace(status_alerts=alerts)
    app.add_middleware(RequestLoggingMiddleware)
    @app.get('/fault/{code}')
    async def fault(code: int):
        return Response(status_code=code)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for code in [400, 404, 422]:
                await client.get(f'/fault/{code}')
            assert rows(alerts) == []
            response = await client.get('/fault/503?secret=hidden')
            stored = rows(alerts)
            assert len(stored) == 1
            report = json.loads(stored[0]['report'])
            assert report['request_id'] == response.headers['X-Request-ID']
            assert report['operation'] == '/fault/{code}'
            assert 'hidden' not in stored[0]['report']
    asyncio.run(run())


def test_background_item_timeout_is_not_lost_to_task_cancellation(tmp_path):
    from app.models.engagement import JobItemRequest
    from app.services.job_service import BatchJobManager
    alerts = StatusAlerts(tmp_path, enabled=True)
    async def run():
        manager = BatchJobManager(status_alerts=alerts, item_timeout_seconds=0.01)
        item = JobItemRequest(item_id='1', url='https://www.douyin.com/video/123', media_name='抖音', type='interactions')
        async def slow(_):
            await asyncio.Event().wait()
        try:
            result = await manager._collect_one(item, slow, None)
            assert result.status == 'failed'
            report = json.loads(rows(alerts)[0]['report'])
            assert (report['platform'], report['operation'], report['status']) == ('douyin', 'interactions', 'transport_error')
        finally:
            await manager.aclose()
    asyncio.run(run())


def test_quoted_credentials_are_redacted():
    from app.services.feishu_alert import safe_reason
    text = safe_reason('HTTP 403 headers={"Cookie": "private", "x-s": "signature"}')
    assert 'private' not in text and 'signature' not in text


def test_collection_502_only_alerts_for_explicit_status_errors(tmp_path):
    from app.api.app import create_app
    from app.api.dependencies import get_engagement_service
    alerts = StatusAlerts(tmp_path, enabled=True)
    result = InteractionResult(platform='douyin', canonical_url='https://www.douyin.com/video/123',
                               work_id='123', coverage='failed', reason='作品没有可用互动数据')
    crawler = SimpleNamespace(fetch_interactions=AsyncMock(return_value=result))
    service = EngagementService(crawler, status_alerts=alerts, cache_ttl_seconds=0,
                                failure_cache_ttl_seconds=0)
    app = create_app()
    app.state.engagement_service = service
    app.dependency_overrides[get_engagement_service] = lambda: service
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            params = {'url': result.canonical_url, 'media_name': '抖音'}
            response = await client.get('/api/v1/interactions', params=params)
            assert response.status_code == 502
            assert rows(alerts) == []
            result.reason = 'HTTP 429'
            result.coverage = 'blocked'
            response = await client.get('/api/v1/interactions', params=params)
            assert response.status_code == 502
            stored = rows(alerts)
            assert len(stored) == 1
            assert json.loads(stored[0]['report'])['status'] == 'risk_limited'
    asyncio.run(run())
