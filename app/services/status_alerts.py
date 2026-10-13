"""Persistent, bounded status-error outbox shared by API workers and cron."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.core.config import PROJECT_ROOT
from app.core.status_errors import status_error
from app.services.feishu_alert import safe_reason, status_error_card


logger = logging.getLogger(__name__)
ROOT = PROJECT_ROOT / ".local/status-alerts"
COOLDOWN = 1800


class StatusAlerts:
    def __init__(self, root: Path = ROOT, *, enabled: bool = False, xhs_monitor: bool = False):
        self.root, self.enabled, self.xhs_monitor = root, enabled, xhs_monitor

    @classmethod
    def from_settings(cls, settings):
        return cls(enabled=settings.feishu_status_alerts_enabled,
                   xhs_monitor=bool(settings.xiaohongshu_session_socket))

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / "alerts.sqlite3"
        with closing(sqlite3.connect(path, timeout=1)) as db:
            path.chmod(0o600)
            db.row_factory = sqlite3.Row
            db.execute("""CREATE TABLE IF NOT EXISTS alerts (
                bucket TEXT PRIMARY KEY, incident_id TEXT NOT NULL, report TEXT NOT NULL,
                created REAL NOT NULL, last_seen REAL NOT NULL, occurrences INTEGER NOT NULL,
                next_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                message_id TEXT, sent_at REAL, delivery_error TEXT
            )""")
            with db:
                yield db

    @contextmanager
    def observe(self, platform: str, operation: str):
        try:
            yield
        except Exception as exc:
            self.record(platform, operation, exc)
            raise

    def result(self, result, operation: str):
        # A missing field/empty page or an unsupported URL is not an outage.
        if result.coverage in {"failed", "blocked", "partial"}:
            self.record(result.platform, operation, result.reason,
                        category=getattr(result, "_status_error", None))

    def record(self, platform: str, operation: str, error: Exception | str,
               *, category: str | None = None, request_id: str = ""):
        if not self.enabled:
            return
        from app.crawlers.platforms.registry import MEDIA_ALIASES

        platform = MEDIA_ALIASES.get(platform, platform)
        category = category or status_error(error)
        if not category:
            return
        # The existing minute-level owner monitor sends one card per XHS
        # incident and pauses probes. Do not send another from each caller.
        if self.xhs_monitor and platform == "xiaohongshu" and operation in {"engagement", "interactions", "comments"} and (
            category in {"login_expired", "risk_limited"}
            or "常驻浏览器不可用" in str(error)
            or "transport_stopped" in str(error)
        ):
            return
        now = time.time()
        report = {
            "platform": platform, "operation": operation, "status": category,
            "checked_at": datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
            "reason": safe_reason((type(error).__name__ + ": " if isinstance(error, Exception) else "") + str(error)),
            "request_id": request_id,
        }
        bucket = f"{platform}:{operation}:{category}"
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT * FROM alerts WHERE bucket=?", (bucket,)).fetchone()
                if old and (not old["message_id"] or now - old["sent_at"] < COOLDOWN):
                    db.execute("UPDATE alerts SET last_seen=?, occurrences=occurrences+1 WHERE bucket=?", (now, bucket))
                else:
                    db.execute("""INSERT OR REPLACE INTO alerts
                        (bucket,incident_id,report,created,last_seen,occurrences,next_attempt)
                        VALUES (?,?,?,?,?,1,?)""",
                        (bucket, str(uuid4()), json.dumps(report, ensure_ascii=False), now, now, now))
        except (OSError, sqlite3.Error) as exc:
            # Notification storage must never break collection or leak secrets.
            logger.error("status_alert_store_failed error_type=%s", type(exc).__name__)

    async def dispatch(self, notify) -> int:
        if not self.enabled:
            return 0
        with self.connect() as db:
            rows = db.execute("SELECT * FROM alerts WHERE message_id IS NULL AND next_attempt<=? ORDER BY created LIMIT 20",
                              (time.time(),)).fetchall()
        sent = 0
        for row in rows:
            with self.connect() as db:
                db.execute("UPDATE alerts SET next_attempt=?, attempts=attempts+1 WHERE incident_id=?",
                           (time.time() + 300, row["incident_id"]))
            report = json.loads(row["report"])
            report["occurrences"] = row["occurrences"]
            try:
                message_id = await notify(status_error_card(report, row["incident_id"]), row["incident_id"])
            except Exception as exc:
                with self.connect() as db:
                    db.execute("UPDATE alerts SET delivery_error=? WHERE incident_id=?", (safe_reason(str(exc)), row["incident_id"]))
            else:
                with self.connect() as db:
                    db.execute("UPDATE alerts SET message_id=?, sent_at=?, delivery_error=NULL WHERE incident_id=?",
                               (message_id, time.time(), row["incident_id"]))
                sent += 1
        return sent
