from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Protocol

from app.models.engagement import CreateJobRequest, JobItemResponse


class DuplicateIdempotencyKeyError(RuntimeError):
    pass


@dataclass
class StoredJob:
    job_id: str
    fingerprint: str
    status: str
    total: int
    completed: int
    success: int
    failed: int
    duration_ms: int
    cost_yuan: float
    created_at: datetime
    idempotency_key: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    webhook_url: str | None = None
    webhook_status: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    expires_at: datetime | None = None


class JobRepository(Protocol):
    async def initialize(self, retention_seconds: int) -> None: ...

    async def create_job(
        self,
        job: StoredJob,
        request: CreateJobRequest,
    ) -> None: ...

    async def get_job(self, job_id: str) -> StoredJob | None: ...

    async def find_by_idempotency_key(self, key: str) -> StoredJob | None: ...

    async def update_job(self, job: StoredJob) -> None: ...

    async def append_result(
        self,
        job_id: str,
        sequence: int,
        result: JobItemResponse,
    ) -> None: ...

    async def get_results(
        self,
        job_id: str,
        offset: int,
        limit: int,
    ) -> list[JobItemResponse]: ...

    async def recover_interrupted(self, retention_seconds: int) -> None: ...

    async def close(self) -> None: ...


class MemoryJobRepository:
    def __init__(self) -> None:
        self._jobs: dict[str, StoredJob] = {}
        self._results: dict[str, dict[int, JobItemResponse]] = {}

    async def initialize(self, retention_seconds: int) -> None:
        self._cleanup_expired()

    async def create_job(
        self,
        job: StoredJob,
        request: CreateJobRequest,
    ) -> None:
        self._cleanup_expired()
        if job.idempotency_key and any(
            existing.idempotency_key == job.idempotency_key
            for existing in self._jobs.values()
        ):
            raise DuplicateIdempotencyKeyError(job.idempotency_key)
        self._jobs[job.job_id] = replace(job)
        self._results[job.job_id] = {}

    async def get_job(self, job_id: str) -> StoredJob | None:
        self._cleanup_expired()
        job = self._jobs.get(job_id)
        return replace(job) if job is not None else None

    async def find_by_idempotency_key(self, key: str) -> StoredJob | None:
        self._cleanup_expired()
        for job in self._jobs.values():
            if job.idempotency_key == key:
                return replace(job)
        return None

    async def update_job(self, job: StoredJob) -> None:
        if job.job_id in self._jobs:
            self._jobs[job.job_id] = replace(job)

    async def append_result(
        self,
        job_id: str,
        sequence: int,
        result: JobItemResponse,
    ) -> None:
        self._results.setdefault(job_id, {})[sequence] = result.model_copy(deep=True)

    async def get_results(
        self,
        job_id: str,
        offset: int,
        limit: int,
    ) -> list[JobItemResponse]:
        self._cleanup_expired()
        results = self._results.get(job_id, {})
        return [
            results[sequence].model_copy(deep=True)
            for sequence in sorted(results)
            if sequence >= offset
        ][:limit]

    async def recover_interrupted(self, retention_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        for job_id, job in list(self._jobs.items()):
            if job.status not in {"queued", "running"}:
                continue
            results = self._results.get(job_id, {}).values()
            completed = len(results)
            success = sum(result.status == "success" for result in results)
            self._jobs[job_id] = replace(
                job,
                status="failed",
                completed=completed,
                success=success,
                failed=completed - success,
                finished_at=now,
                webhook_status=(
                    "failed" if job.webhook_status == "pending" else job.webhook_status
                ),
                error="服务重启，任务未完成",
                expires_at=now + timedelta(seconds=retention_seconds),
            )

    async def close(self) -> None:
        return None

    def _cleanup_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.expires_at is not None and job.expires_at <= now
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)
            self._results.pop(job_id, None)


class SQLiteJobRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._retention_seconds = 86_400

    async def initialize(self, retention_seconds: int) -> None:
        self._retention_seconds = retention_seconds
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._initialize_sync)

    async def create_job(
        self,
        job: StoredJob,
        request: CreateJobRequest,
    ) -> None:
        await asyncio.to_thread(self._create_job_sync, job, request)

    async def get_job(self, job_id: str) -> StoredJob | None:
        return await asyncio.to_thread(self._get_job_sync, job_id)

    async def find_by_idempotency_key(self, key: str) -> StoredJob | None:
        return await asyncio.to_thread(self._find_by_idempotency_key_sync, key)

    async def update_job(self, job: StoredJob) -> None:
        await asyncio.to_thread(self._update_job_sync, job)

    async def append_result(
        self,
        job_id: str,
        sequence: int,
        result: JobItemResponse,
    ) -> None:
        await asyncio.to_thread(
            self._append_result_sync,
            job_id,
            sequence,
            result,
        )

    async def get_results(
        self,
        job_id: str,
        offset: int,
        limit: int,
    ) -> list[JobItemResponse]:
        return await asyncio.to_thread(self._get_results_sync, job_id, offset, limit)

    async def recover_interrupted(self, retention_seconds: int) -> None:
        await asyncio.to_thread(self._recover_interrupted_sync, retention_seconds)

    async def close(self) -> None:
        return None

    def _initialize_sync(self) -> None:
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
                    error TEXT,
                    total INTEGER,
                    webhook_url TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    schema_version INTEGER NOT NULL DEFAULT 1
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
            additions = {
                "error": "TEXT",
                "total": "INTEGER",
                "webhook_url": "TEXT",
                "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
                "expires_at": "TEXT",
                "schema_version": "INTEGER NOT NULL DEFAULT 1",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE batch_jobs ADD COLUMN {name} {declaration}"
                    )

    def _create_job_sync(self, job: StoredJob, request: CreateJobRequest) -> None:
        self._cleanup_expired_sync()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO batch_jobs (
                        job_id, request_json, fingerprint, idempotency_key, status,
                        completed, success, failed, duration_ms, cost_yuan,
                        created_at, started_at, finished_at, webhook_status, error,
                        total, webhook_url, cancel_requested, expires_at, schema_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    self._job_values(job, request.model_dump_json(exclude_none=True)),
                )
        except sqlite3.IntegrityError as exc:
            if job.idempotency_key:
                raise DuplicateIdempotencyKeyError(job.idempotency_key) from exc
            raise

    def _get_job_sync(self, job_id: str) -> StoredJob | None:
        self._cleanup_expired_sync()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM batch_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def _find_by_idempotency_key_sync(self, key: str) -> StoredJob | None:
        self._cleanup_expired_sync()
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM batch_jobs WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def _update_job_sync(self, job: StoredJob) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE batch_jobs SET
                    status = ?, completed = ?, success = ?, failed = ?,
                    duration_ms = ?, cost_yuan = ?, started_at = ?, finished_at = ?,
                    webhook_status = ?, error = ?, cancel_requested = ?, expires_at = ?
                WHERE job_id = ?
                """,
                (
                    job.status,
                    job.completed,
                    job.success,
                    job.failed,
                    job.duration_ms,
                    job.cost_yuan,
                    _format_datetime(job.started_at),
                    _format_datetime(job.finished_at),
                    job.webhook_status,
                    job.error,
                    int(job.cancel_requested),
                    _format_datetime(job.expires_at),
                    job.job_id,
                ),
            )

    def _append_result_sync(
        self,
        job_id: str,
        sequence: int,
        result: JobItemResponse,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO batch_job_results (job_id, sequence, result_json) "
                "VALUES (?, ?, ?)",
                (job_id, sequence, result.model_dump_json()),
            )

    def _get_results_sync(
        self,
        job_id: str,
        offset: int,
        limit: int,
    ) -> list[JobItemResponse]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT result_json FROM batch_job_results "
                "WHERE job_id = ? AND sequence >= ? ORDER BY sequence LIMIT ?",
                (job_id, offset, limit),
            ).fetchall()
        return [
            JobItemResponse.model_validate_json(row["result_json"])
            for row in rows
        ]

    def _recover_interrupted_sync(self, retention_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM batch_jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                result_rows = connection.execute(
                    "SELECT result_json FROM batch_job_results "
                    "WHERE job_id = ? ORDER BY sequence",
                    (row["job_id"],),
                ).fetchall()
                results = [
                    JobItemResponse.model_validate_json(result_row["result_json"])
                    for result_row in result_rows
                ]
                success = sum(result.status == "success" for result in results)
                connection.execute(
                    """
                    UPDATE batch_jobs SET
                        status = 'failed', completed = ?, success = ?, failed = ?,
                        finished_at = ?, webhook_status = CASE
                            WHEN webhook_status = 'pending' THEN 'failed'
                            ELSE webhook_status
                        END,
                        error = ?, expires_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        len(results),
                        success,
                        len(results) - success,
                        now.isoformat(),
                        "服务重启，任务未完成",
                        (now + timedelta(seconds=retention_seconds)).isoformat(),
                        row["job_id"],
                    ),
                )
        self._cleanup_expired_sync()

    def _cleanup_expired_sync(self) -> None:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self._retention_seconds)
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM batch_jobs
                WHERE (expires_at IS NOT NULL AND expires_at <= ?)
                   OR (expires_at IS NULL AND finished_at IS NOT NULL AND finished_at < ?)
                """,
                (now.isoformat(), cutoff.isoformat()),
            )

    @staticmethod
    def _job_values(job: StoredJob, request_json: str) -> tuple[object, ...]:
        return (
            job.job_id,
            request_json,
            job.fingerprint,
            job.idempotency_key,
            job.status,
            job.completed,
            job.success,
            job.failed,
            job.duration_ms,
            job.cost_yuan,
            job.created_at.isoformat(),
            _format_datetime(job.started_at),
            _format_datetime(job.finished_at),
            job.webhook_status,
            job.error,
            job.total,
            job.webhook_url,
            int(job.cancel_requested),
            _format_datetime(job.expires_at),
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> StoredJob:
        request = CreateJobRequest.model_validate_json(row["request_json"])
        return StoredJob(
            job_id=row["job_id"],
            fingerprint=row["fingerprint"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            total=row["total"] if row["total"] is not None else len(request.items),
            completed=row["completed"],
            success=row["success"],
            failed=row["failed"],
            duration_ms=row["duration_ms"],
            cost_yuan=row["cost_yuan"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=_parse_datetime(row["started_at"]),
            finished_at=_parse_datetime(row["finished_at"]),
            webhook_url=row["webhook_url"] or (
                str(request.webhook_url) if request.webhook_url else None
            ),
            webhook_status=row["webhook_status"],
            error=row["error"],
            cancel_requested=bool(row["cancel_requested"]),
            expires_at=_parse_datetime(row["expires_at"]),
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def _format_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
