from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from contextlib import nullcontext
from dataclasses import dataclass
from math import ceil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Awaitable, Callable
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
from app.repositories.job_repository import (
    DuplicateIdempotencyKeyError,
    JobRepository,
    MemoryJobRepository,
    SQLiteJobRepository,
    StoredJob,
)


logger = logging.getLogger(__name__)
CollectJobItem = Callable[[JobItemRequest], Awaitable[JobItemResponse]]
BeforeCollectItem = Callable[[JobItemRequest], Awaitable[None]]
CollectionScope = Callable[[JobItemRequest], Any]


class IdempotencyConflictError(ValueError):
    pass


@dataclass
class _ActiveJob:
    stored: StoredJob
    request: CreateJobRequest


class BatchJobManager:
    def __init__(
        self,
        *,
        retention_seconds: int = 86_400,
        max_concurrency: int = 2,
        item_max_concurrency: int = 16,
        item_timeout_seconds: float = 90,
        job_timeout_seconds: float = 1800,
        timeout_batch_size: int = 500,
        max_items: int = 0,
        result_max_bytes: int = 8 * 1024 * 1024,
        webhook_allowed_hosts: set[str] | None = None,
        webhook_timeout_seconds: float = 10,
        webhook_max_attempts: int = 3,
        webhook_client: httpx.AsyncClient | None = None,
        repository: JobRepository | None = None,
        db_path: Path | None = None,
    ) -> None:
        if item_max_concurrency <= 0:
            raise ValueError("item_max_concurrency must be greater than zero")
        if item_timeout_seconds < 0:
            raise ValueError("item_timeout_seconds must not be negative")
        if job_timeout_seconds < 0:
            raise ValueError("job_timeout_seconds must not be negative")
        if max_items < 0:
            raise ValueError("max_items must not be negative")
        if result_max_bytes <= 0:
            raise ValueError("result_max_bytes must be greater than zero")
        if repository is not None and db_path is not None:
            raise ValueError("repository and db_path cannot be used together")
        self.retention_seconds = retention_seconds
        self.item_max_concurrency = item_max_concurrency
        self.item_timeout_seconds = item_timeout_seconds
        self.job_timeout_seconds = job_timeout_seconds
        if timeout_batch_size <= 0:
            raise ValueError("timeout_batch_size must be greater than zero")
        self.timeout_batch_size = timeout_batch_size
        self.max_items = max_items
        self.result_max_bytes = result_max_bytes
        self.webhook_allowed_hosts = {
            host.casefold() for host in (webhook_allowed_hosts or set()) if host
        }
        self.webhook_timeout_seconds = webhook_timeout_seconds
        self.webhook_max_attempts = webhook_max_attempts
        self._active_jobs: dict[str, _ActiveJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._webhook_client = webhook_client or httpx.AsyncClient(
            timeout=webhook_timeout_seconds,
            trust_env=False,
        )
        self._owns_webhook_client = webhook_client is None
        self.db_path = db_path
        self.repository = repository or (
            SQLiteJobRepository(db_path) if db_path is not None else MemoryJobRepository()
        )

    async def initialize(self) -> None:
        await self._ensure_initialized()

    async def submit(
        self,
        request: CreateJobRequest,
        *,
        collector: CollectJobItem,
        proxy_usage_scope: Callable[[], Any],
        before_collect: BeforeCollectItem | None = None,
        idempotency_key: str | None = None,
        collection_scope: CollectionScope | None = None,
    ) -> JobSubmitResponse:
        await self._ensure_initialized()
        self._validate_request(request)
        fingerprint = hashlib.sha256(
            request.model_dump_json(exclude_none=True).encode("utf-8")
        ).hexdigest()
        async with self._lock:
            if idempotency_key:
                existing = await self.repository.find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return self._idempotent_response(existing, fingerprint)

            job_id = f"job_{uuid4().hex}"
            stored = StoredJob(
                job_id=job_id,
                fingerprint=fingerprint,
                idempotency_key=idempotency_key,
                status="queued",
                total=len(request.items),
                completed=0,
                success=0,
                failed=0,
                duration_ms=0,
                cost_yuan=0,
                created_at=datetime.now(timezone.utc),
                webhook_url=(str(request.webhook_url) if request.webhook_url else None),
                webhook_status="pending" if request.webhook_url else None,
            )
            try:
                await self.repository.create_job(stored, request)
            except DuplicateIdempotencyKeyError:
                if not idempotency_key:
                    raise
                existing = await self.repository.find_by_idempotency_key(idempotency_key)
                if existing is None:
                    raise
                return self._idempotent_response(existing, fingerprint)

            active = _ActiveJob(stored=stored, request=request)
            self._active_jobs[job_id] = active
            self._tasks[job_id] = asyncio.create_task(
                self._run_job(
                    active,
                    collector,
                    proxy_usage_scope,
                    before_collect,
                    collection_scope,
                )
            )
            return JobSubmitResponse(job_id=job_id, status="queued")

    async def resume(
        self,
        job_id: str,
        *,
        collector: CollectJobItem,
        proxy_usage_scope: Callable[[], Any],
        collection_scope: CollectionScope | None = None,
    ) -> JobSubmitResponse | None:
        """Continue uncompleted inputs while retaining all existing results."""
        await self._ensure_initialized()
        async with self._lock:
            stored = await self.repository.get_job(job_id)
            if stored is None:
                return None
            if job_id in self._tasks or stored.status == "completed":
                return JobSubmitResponse(job_id=job_id, status=stored.status)
            request = await self.repository.get_request(job_id)
            if request is None:
                raise ValueError("该历史任务没有保存完整请求，请用原始条目重新提交")
            self._validate_request(request)
            completed_items = await self.repository.get_completed_items(job_id)
            completed_ids = {item_id for item_id, _ in completed_items}
            pending = [item for item in request.items if item.item_id not in completed_ids]
            stored.completed = len(completed_items)
            stored.success = sum(status == "success" for _, status in completed_items)
            stored.failed = stored.completed - stored.success
            stored.cancel_requested = False
            stored.error = None
            stored.finished_at = None
            stored.expires_at = None
            stored.webhook_status = "pending" if request.webhook_url else None
            stored.status = "queued"
            await self.repository.update_job(stored)
            active = _ActiveJob(stored=stored, request=request.model_copy(update={"items": pending}))
            self._active_jobs[job_id] = active
            self._tasks[job_id] = asyncio.create_task(self._run_job(
                active, collector, proxy_usage_scope, None, collection_scope,
            ))
            logger.info("job_resumed job_id=%s completed=%s remaining=%s", job_id, stored.completed, len(pending))
            return JobSubmitResponse(job_id=job_id, status="queued")

    async def resume_interrupted(
        self,
        *,
        collector: CollectJobItem,
        proxy_usage_scope: Callable[[], Any],
        collection_scope: CollectionScope | None = None,
    ) -> None:
        await self._ensure_initialized()
        for stored in await self.repository.get_interrupted_jobs():
            try:
                await self.resume(
                    stored.job_id, collector=collector,
                    proxy_usage_scope=proxy_usage_scope, collection_scope=collection_scope,
                )
            except ValueError:
                logger.warning("job_resume_unavailable job_id=%s", stored.job_id)

    async def get_status(self, job_id: str) -> JobStatusResponse | None:
        await self._ensure_initialized()
        stored = await self.repository.get_job(job_id)
        return self._status_response(stored) if stored is not None else None

    async def get_results(
        self,
        job_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> JobResultsResponse | None:
        await self._ensure_initialized()
        offset = _decode_cursor(cursor)
        stored = await self.repository.get_job(job_id)
        if stored is None:
            return None
        page = await self.repository.get_results(job_id, offset, limit)
        next_offset = offset + len(page)
        next_cursor = (
            _encode_cursor(next_offset)
            if next_offset < stored.completed or stored.status in {"queued", "running"}
            else None
        )
        return JobResultsResponse(
            job_id=job_id,
            status=stored.status,
            data=page,
            next_cursor=next_cursor,
            available_count=stored.completed,
            total=stored.total,
            duration_ms=stored.duration_ms,
            cost_yuan=stored.cost_yuan,
        )

    async def cancel(self, job_id: str) -> JobStatusResponse | None:
        await self._ensure_initialized()
        async with self._lock:
            stored = await self.repository.get_job(job_id)
            if stored is None:
                return None
            if stored.status in {"completed", "failed", "cancelled"}:
                return self._status_response(stored)
            active = self._active_jobs.get(job_id)
            if active is None:
                return self._status_response(stored)
            active.stored.cancel_requested = True
            active.stored.status = "cancelled"
            active.stored.error = "任务已由调用方取消"
            active.stored.finished_at = datetime.now(timezone.utc)
            active.stored.expires_at = active.stored.finished_at + timedelta(
                seconds=self.retention_seconds
            )
            await self.repository.update_job(active.stored)
            task = self._tasks.get(job_id)

        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self._status_response(active.stored)

    async def aclose(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._active_jobs.clear()
        if self._owns_webhook_client:
            await self._webhook_client.aclose()
        await self.repository.close()

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self.repository.initialize(self.retention_seconds)
            await self.repository.recover_interrupted(self.retention_seconds)
            self._initialized = True

    async def _run_job(
        self,
        active: _ActiveJob,
        collector: CollectJobItem,
        proxy_usage_scope: Callable[[], Any],
        before_collect: BeforeCollectItem | None,
        collection_scope: CollectionScope | None = None,
    ) -> None:
        stored = active.stored
        timeout = min(86_400, self.job_timeout_seconds * max(1, ceil(stored.total / self.timeout_batch_size)))
        try:
            async with self._semaphore:
                async with asyncio.timeout(timeout or None):
                    await self._execute_job(
                        active,
                        collector,
                        proxy_usage_scope,
                        before_collect,
                        collection_scope,
                    )
            if active.request.webhook_url:
                await self._send_webhook(active)
        except TimeoutError:
            stored.status = "failed"
            stored.error = f"任务超过最大执行时间 {timeout:g} 秒"
            stored.finished_at = datetime.now(timezone.utc)
            stored.expires_at = stored.finished_at + timedelta(
                seconds=self.retention_seconds
            )
            await self.repository.update_job(stored)
            if active.request.webhook_url:
                await self._send_webhook(active)
        except asyncio.CancelledError:
            if stored.status in {"queued", "running"}:
                stored.status = "failed"
                stored.error = "服务关闭，任务未完成"
                stored.finished_at = datetime.now(timezone.utc)
                stored.expires_at = stored.finished_at + timedelta(
                    seconds=self.retention_seconds
                )
            if stored.cancel_requested and active.request.webhook_url:
                await asyncio.shield(self._send_webhook(active))
            elif stored.webhook_status == "pending":
                stored.webhook_status = "failed"
            await self.repository.update_job(stored)
            raise
        finally:
            logger.info(
                "job_finished job_id=%s status=%s total=%s completed=%s success=%s failed=%s remaining=%s duration_ms=%s",
                stored.job_id, stored.status, stored.total, stored.completed,
                stored.success, stored.failed, stored.total - stored.completed, stored.duration_ms,
            )
            self._tasks.pop(stored.job_id, None)
            self._active_jobs.pop(stored.job_id, None)

    async def _execute_job(
        self,
        active: _ActiveJob,
        collector: CollectJobItem,
        proxy_usage_scope: Callable[[], Any],
        before_collect: BeforeCollectItem | None,
        collection_scope: CollectionScope | None = None,
    ) -> None:
        stored = active.stored
        previous_duration = stored.duration_ms
        previous_cost = stored.cost_yuan
        started = monotonic()
        stored.status = "running"
        stored.error = None
        stored.started_at = stored.started_at or datetime.now(timezone.utc)
        await self.repository.update_job(stored)
        tasks: list[asyncio.Task[None]] = []
        proxy_usage: Any = None
        douyin_argus_failures = 0
        douyin_unavailable = asyncio.Event()
        try:
            async with proxy_usage_scope() as active_proxy_usage:
                proxy_usage = active_proxy_usage
                queue: asyncio.Queue[JobItemRequest] = asyncio.Queue()
                for item in active.request.items:
                    queue.put_nowait(item)

                async def worker() -> None:
                    nonlocal douyin_argus_failures
                    while True:
                        if douyin_unavailable.is_set():
                            return
                        try:
                            item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return
                        result = await self._collect_one(
                            item,
                            collector,
                            before_collect,
                            collection_scope,
                        )
                        result = self._bounded_result(result)
                        if result.media_name in {"douyin", "抖音"}:
                            if result.error and "ArgusSecurityPlugin" in result.error:
                                douyin_argus_failures += 1
                                if douyin_argus_failures >= 10:
                                    douyin_unavailable.set()
                            elif result.status == "success":
                                douyin_argus_failures = 0
                        async def persist_result() -> None:
                            async with self._lock:
                                await self.repository.append_result(stored.job_id, stored.completed, result)
                                stored.completed += 1
                                if result.status == "success":
                                    stored.success += 1
                                else:
                                    stored.failed += 1
                                stored.duration_ms = previous_duration + round((monotonic() - started) * 1000)
                                await self.repository.update_job(stored)

                        write_task = asyncio.create_task(persist_result())
                        try:
                            await asyncio.shield(write_task)
                        except asyncio.CancelledError:
                            await write_task
                            raise

                tasks = [
                    asyncio.create_task(worker())
                    for _ in range(
                        min(self.item_max_concurrency, len(active.request.items))
                    )
                ]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            if not stored.cancel_requested:
                if douyin_unavailable.is_set():
                    stored.status = "failed"
                    stored.error = "抖音接口多次返回 ArgusSecurityPlugin 拒绝；已停止任务以保留未处理项"
                else:
                    stored.status = "completed"
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("batch job failed job_id=%s", stored.job_id)
            stored.status = "failed"
        finally:
            if proxy_usage is not None:
                stored.cost_yuan = round(
                    previous_cost + proxy_usage.added_endpoint_count * 0.00084,
                    8,
                )
            stored.duration_ms = previous_duration + round((monotonic() - started) * 1000)
            if stored.finished_at is None:
                stored.finished_at = datetime.now(timezone.utc)
            stored.expires_at = stored.finished_at + timedelta(
                seconds=self.retention_seconds
            )
            await self.repository.update_job(stored)

    async def _collect_one(
        self,
        item: JobItemRequest,
        collector: CollectJobItem,
        before_collect: BeforeCollectItem | None,
        collection_scope: CollectionScope | None = None,
    ) -> JobItemResponse:
        started = monotonic()
        try:
            if before_collect is not None:
                await before_collect(item)
            scope = collection_scope(item) if collection_scope is not None else nullcontext()
            async with scope:
                async with asyncio.timeout(self.item_timeout_seconds or None):
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

    def _bounded_result(self, result: JobItemResponse) -> JobItemResponse:
        size_bytes = len(result.model_dump_json().encode("utf-8"))
        if size_bytes <= self.result_max_bytes:
            return result
        return JobItemResponse(
            item_id=result.item_id,
            url=result.url,
            media_name=result.media_name,
            type=result.type,
            status="failed",
            complete=False,
            result={},
            error=f"单条结果超过 {self.result_max_bytes} 字节存储上限",
            duration_ms=result.duration_ms,
        )

    async def _send_webhook(self, active: _ActiveJob) -> None:
        stored = active.stored
        payload = self._status_response(stored).model_dump(mode="json")
        url = str(active.request.webhook_url)
        for attempt in range(1, self.webhook_max_attempts + 1):
            try:
                response = await self._webhook_client.post(url, json=payload)
                response.raise_for_status()
                stored.webhook_status = "sent"
                await self.repository.update_job(stored)
                return
            except httpx.HTTPError:
                if attempt < self.webhook_max_attempts:
                    await asyncio.sleep(attempt)
        stored.webhook_status = "failed"
        await self.repository.update_job(stored)

    def _validate_request(self, request: CreateJobRequest) -> None:
        if self.max_items and len(request.items) > self.max_items:
            raise ValueError(f"单个异步任务最多允许 {self.max_items} 个采集项")
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

    @staticmethod
    def _idempotent_response(
        stored: StoredJob,
        fingerprint: str,
    ) -> JobSubmitResponse:
        if stored.fingerprint != fingerprint:
            raise IdempotencyConflictError("Idempotency-Key 已用于不同的任务请求")
        return JobSubmitResponse(job_id=stored.job_id, status=stored.status)

    @staticmethod
    def _status_response(stored: StoredJob) -> JobStatusResponse:
        return JobStatusResponse(
            job_id=stored.job_id,
            status=stored.status,
            progress=JobProgress(
                total=stored.total,
                completed=stored.completed,
                success=stored.success,
                failed=stored.failed,
            ),
            duration_ms=stored.duration_ms,
            cost_yuan=stored.cost_yuan,
            created_at=stored.created_at,
            started_at=stored.started_at,
            finished_at=stored.finished_at,
            webhook_status=stored.webhook_status,
            error=stored.error,
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
