"""Cron entry: inspect local status each minute, sample XHS at most every 30m."""
from __future__ import annotations

import asyncio
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from app.core.config import PROJECT_ROOT, get_settings
from app.core.status_errors import status_error
from app.services.engagement_service import EngagementService
from app.services.feishu_alert import FeishuAlert, error_card, safe_reason


ROOT = PROJECT_ROOT / ".local/xhs-monitor"
INTERVAL = 30 * 60
ALERT_STATUSES = {
    "reauthorization_required", "risk_limited", "awaiting_scan", "transport_stopped",
    "owner_unavailable", "monitor_error", "starting", "login_expired", "session_rejected",
    "transport_error", "protocol_error", "program_error", "upstream_error",
}


def timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def save_json(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


async def owner_status(settings) -> dict:
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=settings.xiaohongshu_session_socket),
            base_url="http://xhs-owner", trust_env=False, timeout=10,
        ) as client:
            response = await client.get("/status")
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or "status" not in data:
                raise ValueError("missing owner status")
            return data
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": "owner_unavailable", "reason": "无法连接或读取会话进程：" + type(exc).__name__}


async def collect_sample(settings) -> dict:
    settings = settings.model_copy(update={
        "engagement_cache_ttl_seconds": 0, "engagement_failure_cache_ttl_seconds": 0,
        "protocol_max_attempts": 1, "browser_fallback_enabled": False,
        "feishu_status_alerts_enabled": False,
    })
    service = EngagementService.from_settings(settings)
    # A fixed canary should not traverse an author's whole history every run.
    if service.crawler.xiaohongshu_url_resolver:
        service.crawler.xiaohongshu_url_resolver.max_pages = 3
    report = {"status": "sample_failed", "reason": "尚未完成样例检查", "operations": {}}
    try:
        async with asyncio.timeout(300):
            interactions = await service.fetch_interactions(settings.xiaohongshu_monitor_url, "小红书")
            report["operations"]["interactions"] = {
                "coverage": interactions.coverage, "stats": interactions.stats.model_dump(exclude_none=True),
                "reason": safe_reason(interactions.reason),
            }
            state = await owner_status(settings)
            if state["status"] != "ready":
                report.update(status=state["status"], reason=safe_reason(state.get("reason", "")))
                return report
            comments = await service.fetch_comments(settings.xiaohongshu_monitor_url, "小红书", page=1)
            report["operations"]["comments"] = {
                "coverage": comments.coverage, "count": len(comments.comments),
                "comments_complete": comments.comments_complete,
                "has_next_page": comments.next_page is not None,
                "reason": safe_reason(comments.reason),
            }
            state = await owner_status(settings)
            if state["status"] != "ready":
                report.update(status=state["status"], reason=safe_reason(state.get("reason", "")))
                return report
            good = {"partial", "complete"}
            if (interactions.coverage in good
                and any(value is not None for value in interactions.stats.model_dump().values())
                and comments.coverage in good and comments.comments_complete):
                report.update(status="healthy", reason="真实样例互动与第一页评论均可用")
            else:
                report["reason"] = "样例数据不完整；会话进程仍正常，不能据此判定登录失效"
            for result in (interactions, comments):
                if result.coverage not in {"failed", "blocked", "partial"}:
                    continue
                category = getattr(result, "_status_error", None) or status_error(result.reason)
                if category:
                    report.update(status=category, reason=safe_reason(result.reason))
                    break
    except Exception as exc:
        state = await owner_status(settings)
        report.update(status=state["status"] if state["status"] != "ready" else (status_error(exc) or "sample_failed"),
                      reason=safe_reason(state.get("reason") or ("样例检查异常：" + type(exc).__name__)))
    finally:
        await service.aclose()
    return report


class SessionMonitor:
    def __init__(self, settings, root: Path, notify):
        self.settings, self.root, self.notify = settings, root, notify
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "status.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        save_json(self.path, self.state)

    async def tick(self):
        now = time.time()
        checked_at = timestamp(now)
        owner = await owner_status(self.settings)
        self.state.update(last_local_check_at=checked_at, owner_status=owner["status"],
                          sample_url=self.settings.xiaohongshu_monitor_url, interval_seconds=INTERVAL)
        probed = False
        if owner["status"] != "ready":
            report = {"status": owner["status"], "reason": safe_reason(owner.get("reason", ""))}
        elif now < self.state.get("next_probe_at", 0):
            # Keep pending alert delivery alive without touching XHS again.
            report = self.state.get("last_report", {"status": "healthy", "reason": "等待下一次样例检查"})
        else:
            # Persist the deadline before network I/O: a crash cannot trigger a
            # rapid retry on the next cron tick. flock prevents overlapping jobs.
            self.state.update(next_probe_at=now + INTERVAL, last_probe_started_at=checked_at)
            self.save()
            try:
                report = await collect_sample(self.settings)
            except Exception as exc:
                report = {"status": "monitor_error", "reason": "监测程序异常：" + type(exc).__name__}
            probed = True
        report = {**report, "checked_at": checked_at, "sample_url": self.settings.xiaohongshu_monitor_url,
                  "last_good_at": self.state.get("last_good_at")}
        if probed or owner["status"] != "ready":
            self.state["last_report"] = report
        if report["status"] == "healthy":
            if probed:
                self.state.update(last_good_at=timestamp(time.time()), incident=None)
        elif report["status"] in ALERT_STATUSES:
            incident = self.state.get("incident")
            if not incident:
                incident = {"id": str(uuid4()), "first_detected_at": checked_at,
                            "report": report, "sent": False, "attempts": 0}
                self.state["incident"] = incident
            if not incident["sent"] and now >= incident.get("next_notify_at", 0):
                # Same incident UUID survives process restarts and uncertain
                # delivery, so Feishu can deduplicate ambiguous POST retries.
                incident.update(attempts=incident["attempts"] + 1, next_notify_at=now + 300)
                self.save()
                try:
                    message_id = await self.notify(error_card(incident["report"], incident["id"]), incident["id"])
                except Exception as exc:
                    incident["delivery_error"] = safe_reason(str(exc))
                else:
                    incident.update(sent=True, sent_at=timestamp(time.time()), message_id=message_id)
                    incident.pop("delivery_error", None)
        self.state["status"] = report["status"]
        self.save()
        if probed:
            with (self.root / "probes.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(report, ensure_ascii=False) + "\n")
        return self.state


async def run(settings):
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        notifier = FeishuAlert(settings, client)
        monitor = SessionMonitor(settings, ROOT, notifier.send)
        state = await monitor.tick()
        print(json.dumps({"checked_at": state["last_local_check_at"], "status": state["status"],
                          "last_good_at": state.get("last_good_at"),
                          "next_probe_at": timestamp(state["next_probe_at"]) if state.get("next_probe_at") else None,
                          "alert_sent": bool((state.get("incident") or {}).get("sent"))}, ensure_ascii=False))


def main():
    settings = get_settings()
    if settings.xiaohongshu_session_mode != "stored" or not settings.xiaohongshu_session_socket:
        raise SystemExit("监测必须使用已保存的小红书常驻浏览器；禁止独立会话")
    url = urlsplit(settings.xiaohongshu_monitor_url)
    if (url.scheme != "https" or url.hostname != "www.xiaohongshu.com" or url.query
        or not url.path.startswith(("/discovery/item/", "/explore/"))):
        raise SystemExit("监测样例应为不含凭据参数的小红书原始笔记 URL")
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    with (ROOT / "monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
