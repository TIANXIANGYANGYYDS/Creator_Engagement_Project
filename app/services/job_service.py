from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable, Iterator
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from app.models.engagement import (
    CreateJobRequest,
    JobItemRequest,
    JobItemResponse,
    JobProgress,
    JobResultsResponse,
    JobStatusResponse,
    JobSubmitResponse,
)


logger = logging.getLogger(__name__)
CollectJobItem = Callable[[JobItemRequest], Awaitable[JobItemResponse]]
BeforeCollectItem = Callable[[JobItemRequest], Awaitable[None]]


class IdempotencyConflictError(ValueError):
    pass


@dataclass
class _JobRecord:
    job_id: str
    request: CreateJobRequest
    fingerprint: str
    idempotency_key: str | None = None
    status: str = "queued"
    results: list[JobItemResponse] = field(default_factory=list)
    completed: int = 0
    success: int = 0
    failed: int = 0
    duration_ms: int = 0
    cost_yuan: float = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    webhook_status: str | None = None
    error: str | None = None
    cancel_requested: bool = False


class BatchJobManager:
    def __init__(
        self,
        *,
        retention_seconds: int = 86_400,
        max_concurrency: int = 2,
        item_max_concurrency: int = 16,
        item_timeout_seconds: float = 90,
        job_timeout_seconds: float = 1800,
        webhook_allowed_hosts: set[str] | None = None,
        webhook_timeout_seconds: float = 10,
        webhook_max_attempts: int = 3,
        webhook_client: httpx.AsyncClient | None = None,
        db_path: Path | None = None,
    ) -> None:
        if item_max_concurrency <= 0:
            raise ValueError("item_max_concurrency must be greater than zero")
        if item_timeout_seconds < 0:
            raise ValueError("item_timeout_seconds must not be negative")
        if job_timeout_seconds < 0:
            raise ValueError("job_timeout_seconds must not be negative")
        self.retention_seconds = retention_seconds
        self.item_max_concurrency = item_max_concurrency
        self.item_timeout_seconds = item_timeout_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self.webhook_allowed_hosts = {
            host.casefold() for host in (webhook_allowed_hosts or set()) if host
        }
        self.webhook_timeout_seconds = webhook_timeout_seconds
        self.webhook_max_attempts = webhook_max_attempts
        self._jobs: dict[str, _JobRecord] = {}
        self._idempotency: dict[str, tuple[str, str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._webhook_client = webhook_client or httpx.AsyncClient(
            timeout=webhook_timeout_seconds,
            trust_env=False,
        )
        self._owns_webhook_client = webhook_client is None
        self.db_path = db_path
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize_store()
            self._load_store()

    async def submit(
        self,
        request: CreateJobRequest,
        *,
        collector: CollectJobItem,
        proxy_usage_scope: Callable[[], Any],
        before_collect: BeforeCollectItem | None = None,
        idempotency_key: str | None = None,
    ) -> JobSubmitResponse:
        self._validate_request(request)
        fingerprint = hashlib.sha256(
            request.model_dump_json(exclude_none=True).encode("utf-8")
        ).hexdigest()
        async with self._lock:
            self._cleanup_expired_locked()
            if idempotency_key:
                existing = self._idempotency.get(idempotency_key)
                if existing is not None:
                    job_id, existing_fingerprint = existing
                    if existing_fingerprint != fingerprint:
                        raise IdempotencyConflictError(
                            "Idempotency-Key 已用于不同的任务请求"
                        )
                    record = self._jobs[job_id]
                    return JobSubmitResponse(job_id=job_id, status=record.status)

            job_id = f"job_{uuid4().hex}"
            record = _JobRecord(
                job_id=job_id,
                request=request,
                fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                webhook_status="pending" if request.webhook_url else None,
            )
            self._jobs[job_id] = record
            if idempotency_key:
                self._idempotency[idempotency_key] = (job_id, fingerprint)
            self._persist_job(record)
            self._tasks[job_id] = asyncio.create_task(
                self._run_job(
                    record,
                    collector,
                    proxy_usage_scope,
                    before_collect,
                )
            )
            return JobSubmitResponse(job_id=job_id, status="queued")

    async def get_status(self, job_id: str) -> JobStatusResponse | None:
        async with self._lock:
            self._cleanup_expired_locked()
            record = self._jobs.get(job_id)
            return self._status_response(record) if record is not None else None

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> JobResultsResponse | None:
        offset = _decode_cursor(cursor)
        async with self._lock:
            self._cleanup_expired_locked()
            record = self._jobs.get(job_id)
            if record is None:
                return None
            page = record.results[offset : offset + limit]
            next_offset = offset + len(page)
            next_cursor = (
                _encode_cursor(next_offset)
                if next_offset < len(record.results)
                or record.status in {"queued", "running"}
                else None
            )
            return JobResultsResponse(
                job_id=job_id,
                status=record.status,
                data=[item.model_copy(deep=True) for item in page],
                next_cursor=next_cursor,
                available_count=len(record.results),
                total=len(record.request.items),
                duration_ms=record.duration_ms,
                cost_yuan=record.cost_yuan,
            )

    async def cancel(self, job_id: str) -> JobStatusResponse | None:
        async with self._lock:
            self._cleanup_expired_locked()
            record = self._jobs.get(job_id)
            if record is None:
                return None
            if record.status in {"completed", "failed", "cancelled"}:
                return self._status_response(record)
            record.cancel_requested = True
            record.status = "cancelled"
            record.error = "任务已由调用方取消"
            record.finished_at = datetime.now(timezone.utc)
            self._persist_job(record)
            task = self._tasks.get(job_id)

        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._tasks.pop(job_id, None)
        return self._status_response(record)

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._owns_webhook_client:
            await self._webhook_client.aclose()

    async def _run_job(
        self,
        record: _JobRecord,
        collector: CollectJobItem,
        proxy_usage_scope: Callable[[], Any],
        before_collect: BeforeCollectItem | None,
    ) -> None:
        try:
            async with self._semaphore:
                async with asyncio.timeout(
                    self.job_timeout_seconds or None
                ):
                    await self._execute_job(
                        record,
                        collector,
                        proxy_usage_scope,
                        before_collect,
                    )
            if record.request.webhook_url:
                await self._send_webhook(record)
        except TimeoutError:
            record.status = "failed"
            record.error = f"任务超过最大执行时间 {self.job_timeout_seconds:g} 秒"
            record.finished_at = datetime.now(timezone.utc)
            self._persist_job(record)
            if record.request.webhook_url:
                await self._send_webhook(record)
        except asyncio.CancelledError:
            if record.status in {"queued", "running"}:
                record.status = "failed"
                record.error = "服务关闭，任务未完成"
                record.finished_at = datetime.now(timezone.utc)
            if record.cancel_requested and record.request.webhook_url:
                await asyncio.shield(self._send_webhook(record))
            elif record.webhook_status == "pending":
                record.webhook_status = "failed"
            self._persist_job(record)
            raise
        finally:
            self._tasks.pop(record.job_id, None)

    async def _execute_job(
        self,
        record: _JobRecord,
        collector: CollectJobItem,
        proxy_usage_scope: Callable[[], Any],
        before_collect: BeforeCollectItem | None,
    ) -> None:
        started = monotonic()
        record.status = "running"
        record.error = None
        record.started_at = datetime.now(timezone.utc)
        self._persist_job(record)
        tasks: list[asyncio.Task[None]] = []
        proxy_usage: Any = None
        try:
            async with proxy_usage_scope() as active_proxy_usage:
                proxy_usage = active_proxy_usage
                queue: asyncio.Queue[JobItemRequest] = asyncio.Queue()
                for item in record.request.items:
                    queue.put_nowait(item)

                async def worker() -> None:
                    while True:
                        try:
                            item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        result = await self._collect_one(
                            item,
                            collector,
                            before_collect,
                        )
                        record.results.append(result)
                        self._persist_result(record, result)
                        record.completed += 1
                        if result.status == "success":
                            record.success += 1
                        else:
                            record.failed += 1
                        record.duration_ms = round((monotonic() - started) * 1000)
                        self._persist_job(record)

                tasks = [
                    asyncio.create_task(worker())
                    for _ in range(
                        min(self.item_max_concurrency, len(record.request.items))
                    )
                ]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            if not record.cancel_requested:
                record.status = "completed"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("batch job failed job_id=%s", record.job_id)
            record.status = "failed"
        finally:
            if proxy_usage is not None:
                record.cost_yuan = round(
                    proxy_usage.added_endpoint_count * 0.00084,
                    8,
                )
            record.duration_ms = round((monotonic() - started) * 1000)
            if record.finished_at is None:
                record.finished_at = datetime.now(timezone.utc)
            self._persist_job(record)

    async def _collect_one(
        self,
        item: JobItemRequest,
        collector: CollectJobItem,
        before_collect: BeforeCollectItem | None,
    ) -> JobItemResponse:
        started = monotonic()
        try:
            if before_collect is not None:
                await before_collect(item)
            async with asyncio.timeout(
                self.item_timeout_seconds or None
            ):
                result = await collector(item)
        except TimeoutError:
            result = JobItemResponse(
                item_id=item.item_id,
                url=item.url,
                media_name=item.media_name,
                type=item.type,
                status="failed",
                complete=False,
                result={},
                error=f"单条采集超过 {self.item_timeout_seconds:g} 秒",
            )
        except Exception:
            logger.exception("batch job item failed item_id=%s", item.item_id)
            result = JobItemResponse(
                item_id=item.item_id,
                url=item.url,
                media_name=item.media_name,
                type=item.type,
                status="failed",
                complete=False,
                result={},
                error="服务器内部错误",
            )
        result.duration_ms = round((monotonic() - started) * 1000)
        return result

    async def _send_webhook(self, record: _JobRecord) -> None:
        payload = self._status_response(record).model_dump(mode="json")
        url = str(record.request.webhook_url)
        for attempt in range(1, self.webhook_max_attempts + 1):
            try:
                response = await self._webhook_client.post(url, json=payload)
                response.raise_for_status()
                record.webhook_status = "sent"
                self._persist_job(record)
                return
            except httpx.HTTPError:
                if attempt < self.webhook_max_attempts:
                    await asyncio.sleep(attempt)
        record.webhook_status = "failed"
        self._persist_job(record)

    def _validate_request(self, request: CreateJobRequest) -> None:
        item_ids = [item.item_id for item in request.items]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("同一异步任务中的 item_id 不能重复")
        if request.webhook_url is None:
            return
        parsed = urlparse(str(request.webhook_url))
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https":
            raise ValueError("webhook_url 必须使用 HTTPS")
        if host not in self.webhook_allowed_hosts:
            raise ValueError("webhook_url 域名未加入服务端白名单")

    def _cleanup_expired_locked(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.retention_seconds)
        expired = [
            job_id
            for job_id, record in self._jobs.items()
            if record.finished_at is not None and record.finished_at < cutoff
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)
        if expired:
            expired_set = set(expired)
            self._idempotency = {
                key: value
                for key, value in self._idempotency.items()
                if value[0] not in expired_set
            }
            self._delete_jobs(expired)

    def _initialize_store(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS batch_jobs (
                    job_id TEXT PRIMARY KEY,
                    request_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL,
                    completed INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    cost_yuan REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    webhook_status TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_job_results (
                    job_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, sequence),
                    FOREIGN KEY (job_id) REFERENCES batch_jobs(job_id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(batch_jobs)")
            }
            if "error" not in columns:
                connection.execute("ALTER TABLE batch_jobs ADD COLUMN error TEXT")

    def _load_store(self) -> None:
        now = datetime.now(timezone.utc)
        recovered_records: list[_JobRecord] = []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM batch_jobs ORDER BY created_at"
            ).fetchall()
            for row in rows:
                record = _JobRecord(
                    job_id=row["job_id"],
                    request=CreateJobRequest.model_validate_json(row["request_json"]),
                    fingerprint=row["fingerprint"],
                    idempotency_key=row["idempotency_key"],
                    status=row["status"],
                    completed=row["completed"],
                    success=row["success"],
                    failed=row["failed"],
                    duration_ms=row["duration_ms"],
                    cost_yuan=row["cost_yuan"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    started_at=_parse_datetime(row["started_at"]),
                    finished_at=_parse_datetime(row["finished_at"]),
                    webhook_status=row["webhook_status"],
                    error=row["error"],
                )
                result_rows = connection.execute(
                    "SELECT result_json FROM batch_job_results "
                    "WHERE job_id = ? ORDER BY sequence",
                    (record.job_id,),
                ).fetchall()
                record.results = [
                    JobItemResponse.model_validate_json(result_row["result_json"])
                    for result_row in result_rows
                ]
                record.completed = len(record.results)
                record.success = sum(
                    result.status == "success" for result in record.results
                )
                record.failed = record.completed - record.success
                if record.status in {"queued", "running"}:
                    record.status = "failed"
                    record.error = "服务重启，任务未完成"
                    record.finished_at = now
                    if record.webhook_status == "pending":
                        record.webhook_status = "failed"
                    recovered_records.append(record)
                self._jobs[record.job_id] = record
                if record.idempotency_key:
                    self._idempotency[record.idempotency_key] = (
                        record.job_id,
                        record.fingerprint,
                    )
        for record in recovered_records:
            self._persist_job(record)
        self._cleanup_expired_locked()

    def _persist_job(self, record: _JobRecord) -> None:
        if self.db_path is None:
            return
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO batch_jobs (
                    job_id, request_json, fingerprint, idempotency_key, status,
                    completed, success, failed, duration_ms, cost_yuan,
                    created_at, started_at, finished_at, webhook_status
                    , error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status,
                    completed=excluded.completed,
                    success=excluded.success,
                    failed=excluded.failed,
                    duration_ms=excluded.duration_ms,
                    cost_yuan=excluded.cost_yuan,
                    started_at=excluded.started_at,
                    finished_at=excluded.finished_at,
                    webhook_status=excluded.webhook_status,
                    error=excluded.error
                """,
                (
                    record.job_id,
                    record.request.model_dump_json(exclude_none=True),
                    record.fingerprint,
                    record.idempotency_key,
                    record.status,
                    record.completed,
                    record.success,
                    record.failed,
                    record.duration_ms,
                    record.cost_yuan,
                    record.created_at.isoformat(),
                    _format_datetime(record.started_at),
                    _format_datetime(record.finished_at),
                    record.webhook_status,
                    record.error,
                ),
            )

    def _persist_result(
        self,
        record: _JobRecord,
        result: JobItemResponse,
    ) -> None:
        if self.db_path is None:
            return
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO batch_job_results (job_id, sequence, result_json) "
                "VALUES (?, ?, ?)",
                (
                    record.job_id,
                    len(record.results) - 1,
                    result.model_dump_json(),
                ),
            )

    def _delete_jobs(self, job_ids: list[str]) -> None:
        if self.db_path is None or not job_ids:
            return
        with self._connection() as connection:
            connection.executemany(
                "DELETE FROM batch_jobs WHERE job_id = ?",
                ((job_id,) for job_id in job_ids),
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            raise RuntimeError("job store is disabled")
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _status_response(record: _JobRecord) -> JobStatusResponse:
        return JobStatusResponse(
            job_id=record.job_id,
            status=record.status,
            progress=JobProgress(
                total=len(record.request.items),
                completed=record.completed,
                success=record.success,
                failed=record.failed,
            ),
            duration_ms=record.duration_ms,
            cost_yuan=record.cost_yuan,
            created_at=record.created_at,
            started_at=record.started_at,
            finished_at=record.finished_at,
            webhook_status=record.webhook_status,
            error=record.error,
        )


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        offset = int(value)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("无效的结果分页 cursor") from exc
    if offset < 0:
        raise ValueError("无效的结果分页 cursor")
    return offset


def _format_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
