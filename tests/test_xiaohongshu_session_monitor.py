import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.manually_execute_script import monitor_xiaohongshu_session as monitor
from app.services.feishu_alert import FeishuAlert, FeishuAlertError, error_card, safe_reason


def setup_monitor(tmp_path, monkeypatch, owner="ready"):
    clock = SimpleNamespace(now=1800000000.0)
    monkeypatch.setattr(monitor.time, "time", lambda: clock.now)
    status = AsyncMock(return_value={"status": owner, "reason": "test reason"})
    collect = AsyncMock(return_value={"status": "healthy", "reason": "真实样例可用"})
    monkeypatch.setattr(monitor, "owner_status", status)
    monkeypatch.setattr(monitor, "collect_sample", collect)
    notify = AsyncMock(return_value="om-test")
    settings = Settings(_env_file=None)
    return monitor.SessionMonitor(settings, tmp_path, notify), clock, status, collect, notify


def test_probe_every_30_minutes_survives_restarts(tmp_path, monkeypatch):
    runner, clock, _, collect, notify = setup_monitor(tmp_path, monkeypatch)
    async def run():
        await runner.tick()
        clock.now += 60
        restarted = monitor.SessionMonitor(runner.settings, tmp_path, notify)
        await restarted.tick()
        assert collect.await_count == 1
        clock.now += 1740
        await restarted.tick()
        assert collect.await_count == 2
        notify.assert_not_awaited()
    asyncio.run(run())
    assert len((tmp_path / "probes.jsonl").read_text().splitlines()) == 2
    assert (tmp_path / "status.json").stat().st_mode & 0o777 == 0o600


def test_expired_account_alerts_once_and_never_probes_until_recovered(tmp_path, monkeypatch):
    runner, clock, owner, collect, notify = setup_monitor(tmp_path, monkeypatch, "reauthorization_required")
    async def run():
        await runner.tick()
        first_id = runner.state["incident"]["id"]
        restarted = monitor.SessionMonitor(runner.settings, tmp_path, notify)
        clock.now += 1800
        await restarted.tick()
        assert notify.await_count == 1
        collect.assert_not_awaited()
        owner.return_value = {"status": "ready"}
        clock.now += 60
        await restarted.tick()
        assert restarted.state["incident"] is None
        owner.return_value = {"status": "risk_limited"}
        clock.now += 60
        await restarted.tick()
        assert notify.await_count == 2
        assert restarted.state["incident"]["id"] != first_id
    asyncio.run(run())


def test_alert_failure_retries_same_uuid_without_upstream_requests(tmp_path, monkeypatch):
    runner, clock, _, collect, notify = setup_monitor(tmp_path, monkeypatch, "owner_unavailable")
    notify.side_effect = [FeishuAlertError("飞书 HTTP 503"), "om-delivered"]
    async def run():
        await runner.tick()
        assert not runner.state["incident"]["sent"]
        incident_id = runner.state["incident"]["id"]
        clock.now += 60
        await runner.tick()
        assert notify.await_count == 1
        clock.now += 240
        restarted = monitor.SessionMonitor(runner.settings, tmp_path, notify)
        await restarted.tick()
        assert restarted.state["incident"]["sent"]
        assert notify.await_args.args[1] == incident_id
        collect.assert_not_awaited()
    asyncio.run(run())


def test_crashed_probe_does_not_retry_on_next_minute(tmp_path, monkeypatch):
    runner, clock, _, collect, notify = setup_monitor(tmp_path, monkeypatch)
    collect.side_effect = RuntimeError("test crash")
    async def run():
        await runner.tick()
        assert runner.state["status"] == "monitor_error"
        clock.now += 60
        await monitor.SessionMonitor(runner.settings, tmp_path, notify).tick()
        assert collect.await_count == notify.await_count == 1
    asyncio.run(run())


def test_owner_ready_without_successful_probe_does_not_clear_incident(tmp_path, monkeypatch):
    runner, clock, owner, collect, notify = setup_monitor(tmp_path, monkeypatch)
    async def run():
        await runner.tick()
        clock.now += 60
        owner.return_value = {"status": "reauthorization_required"}
        await runner.tick()
        owner.return_value = {"status": "ready"}
        clock.now += 60
        await runner.tick()
        assert runner.state["incident"]["sent"]
        assert notify.await_count == collect.await_count == 1
    asyncio.run(run())


def test_real_sample_accepts_partial_stats_and_valid_empty_comment_page(monkeypatch):
    settings = Settings(_env_file=None)
    stats = SimpleNamespace(model_dump=lambda **kwargs: {"likes": 2, "shares": None})
    service = SimpleNamespace(
        crawler=SimpleNamespace(xiaohongshu_url_resolver=SimpleNamespace(max_pages=20)),
        fetch_interactions=AsyncMock(return_value=SimpleNamespace(coverage="partial", stats=stats, reason="SSR")),
        fetch_comments=AsyncMock(return_value=SimpleNamespace(coverage="partial", comments=[],
            comments_complete=True, next_page=None, reason="有效空末页")), aclose=AsyncMock(),
    )
    configured = []
    def factory(s):
        configured.append(s)
        return service
    monkeypatch.setattr(monitor.EngagementService, "from_settings", factory)
    monkeypatch.setattr(monitor, "owner_status", AsyncMock(return_value={"status": "ready"}))
    result = asyncio.run(monitor.collect_sample(settings))
    assert result["status"] == "healthy"
    assert result["operations"]["comments"]["count"] == 0
    assert configured[0].engagement_cache_ttl_seconds == 0
    assert configured[0].protocol_max_attempts == 1
    service.aclose.assert_awaited_once()


def test_credentials_and_token_not_exposed_on_feishu_failure():
    settings = Settings(_env_file=None, feishu_app_id="app-test",
        feishu_app_secret=SecretStr("private-secret"), feishu_error_re_chat_id="oc-test")
    calls = []
    def handle(request):
        calls.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("/internal"):
            assert body["app_secret"] == "private-secret"
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "private-token"})
        assert request.url.params["receive_id_type"] == "chat_id"
        assert request.headers["authorization"] == "Bearer private-token"
        assert body["uuid"] == "stable-incident" and body["receive_id"] == "oc-test"
        assert body["msg_type"] == "interactive"
        return httpx.Response(200, json={"code": 230002, "msg": "do not leak private-token"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(FeishuAlertError) as exc:
                await FeishuAlert(settings, client).send({"schema": "2.0"}, "stable-incident")
            assert "230002" in str(exc.value) and "private-token" not in str(exc.value)
    asyncio.run(run())
    assert len(calls) == 2


def test_error_card_redacts_urls_headers_and_keeps_banyue_fields():
    reason = "GET https://www.xiaohongshu.com/?xsec_token=secret Cookie: web_session=private"
    card = error_card({"status": "owner_unavailable", "checked_at": "2026-10-13T17:30:00+08:00",
                       "sample_url": "https://www.xiaohongshu.com/explore/note", "reason": reason}, "incident-id")
    body = json.dumps(card, ensure_ascii=False)
    assert card["schema"] == "2.0"
    assert all(key in body for key in ("时间", "等级", "模块", "文件", "追踪 ID"))
    assert "secret" not in body and "private" not in body
    assert "无法确认登录状态" in body
    assert "access_token" not in safe_reason("access_token=private")


def test_missing_sample_data_never_sends_card(tmp_path, monkeypatch):
    runner, clock, _, collect, notify = setup_monitor(tmp_path, monkeypatch)
    collect.return_value = {'status': 'sample_failed', 'reason': '缺字段、空评论或作品不可用'}
    async def run():
        await runner.tick()
        clock.now += 1800
        await runner.tick()
        notify.assert_not_awaited()
        assert runner.state['status'] == 'sample_failed'
        assert not runner.state.get('incident')
    asyncio.run(run())
