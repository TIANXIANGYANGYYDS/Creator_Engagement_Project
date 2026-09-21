from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.models.author import (
    AuthorJobResults,
    AuthorJobStatus,
    AuthorRequest,
    AuthorWork,
    AuthorBatchItem,
    AuthorBatchSubmission,
)
from app.crawlers.authors.common import AuthorSessionError, decode_cursor
from app.crawlers.authors.identity import AuthorIdentityError
from app.crawlers.http_client import PlatformCrawlerError


logger = logging.getLogger(__name__)


class AuthorJobManager:
    def __init__(
        self,
        repository,
        service,
        *,
        retention_seconds=86400,
        timeout_seconds=86400,
        detail_workers=4,
        retry_base_seconds=5,
        retry_max_seconds=60,
    ):
        if detail_workers < 1:
            raise ValueError("detail_workers must be positive")
        self.detail_workers = detail_workers
        self.repository = repository
        self.service = service
        self.retention_seconds = retention_seconds
        self.timeout_seconds = timeout_seconds
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self._tasks = {}
        self._gate = asyncio.Semaphore(2)
        self._lock = asyncio.Lock()

    async def start(self):
        await self.repository.initialize()
        for job in await self.repository.pending():
            self._launch(job["_id"])

    def _launch(self, job_id):
        self._tasks[job_id] = asyncio.create_task(self._run(job_id))

    @staticmethod
    def _new_job(request):
        now = datetime.now(timezone.utc)
        return {
            "_id": "author_" + uuid4().hex,
            **request.model_dump(),
            "status": "queued",
            "saved_count": 0,
            "detail_failed_count": 0,
            "pages_completed": 0,
            "complete": False,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "expires_at": None,
            "cursor": None,
            "seen_cursors": [],
            "seen_pages": [],
            "retry_count": 0,
            "next_retry_at": None,
            "deadline_at": None,
            "coverage_complete": True,
            "error": None,
        }

    async def submit(self, request):
        job = self._new_job(request)
        await self.repository.create(job)
        self._launch(job["_id"])
        return self._status(job)

    async def submit_batch(self, items):
        from pydantic import ValidationError

        jobs, results, seen = [], [], {}
        for index, value in enumerate(items):
            try:
                request = AuthorRequest.model_validate(value)
            except ValidationError as exc:
                result = AuthorBatchItem(
                    index=index,
                    status="rejected",
                    error="; ".join(
                        e["msg"]
                        for e in exc.errors(include_input=False, include_url=False)
                    ),
                )
                if isinstance(value, dict):
                    result.author_id = (
                        str(value.get("author_id", value.get("uid")))
                        if value.get("author_id", value.get("uid")) is not None
                        else None
                    )
                    result.media_name = str(
                        value.get("media_name", value.get("platform", ""))
                    )
                results.append(result)
                continue
            key = (request.media_name, request.uid)
            duplicate = key in seen
            if not duplicate:
                job = self._new_job(request)
                seen[key] = job["_id"]
                jobs.append(job)
            results.append(
                AuthorBatchItem(
                    index=index,
                    author_id=request.uid,
                    media_name=request.media_name,
                    job_id=seen[key],
                    status="duplicate" if duplicate else "accepted",
                )
            )
        # Persist in bounded chunks; task activation follows each durable write.
        # No limit on submitted author count or proxy purchases.
        for offset in range(0, len(jobs), 500):
            chunk = jobs[offset : offset + 500]

            async def save_and_launch(chunk=chunk):
                await self.repository.create_many(chunk)
                for job in chunk:
                    self._launch(job["_id"])

            await self._checkpoint(save_and_launch())
        rejected = sum(r.status == "rejected" for r in results)
        return AuthorBatchSubmission(
            total=len(items),
            accepted_count=len(jobs),
            rejected_count=rejected,
            duplicate_count=len(items) - len(jobs) - rejected,
            data=results,
        )

    async def status(self, job_id):
        job = await self.repository.get(job_id)
        if job:
            job.update(await self.repository.counts(job_id))
        return self._status(job) if job else None

    @staticmethod
    def _status(job):
        return AuthorJobStatus(
            job_id=job["_id"], **{k: v for k, v in job.items() if k != "_id"}
        )

    async def results(self, job_id, cursor, limit):
        job = await self.repository.get(job_id)
        if job is None:
            return None
        job.update(await self.repository.counts(job_id))
        after = None
        if cursor:
            try:
                value = json.loads(
                    base64.b64decode(cursor, altchars=b"-_", validate=True)
                )
                if value["job_id"] != job_id or not isinstance(value["after"], str):
                    raise ValueError()
                after = value["after"]
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ValueError("结果 cursor 无效或属于其他任务") from exc
        rows = await self.repository.results(
            job_id,
            after,
            limit + 1,
            wait_for_details=job["status"] in {"queued", "running"},
        )
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if more or job["status"] in {"queued", "running"}:
            next_cursor = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "job_id": job_id,
                        "after": rows[-1]["sequence"] if rows else after or "",
                    }
                ).encode()
            ).decode()
        return AuthorJobResults(
            job=self._status(job),
            data=[AuthorWork.model_validate(row["work"]) for row in rows],
            next_cursor=next_cursor,
        )

    async def resume(self, job_id):
        async with self._lock:
            job = await self.repository.get(job_id)
            if not job:
                return None
            if job_id in self._tasks or job["status"] == "completed":
                return await self.status(job_id)
            counts = await self.repository.counts(job_id)
            if job.get("discovery_done") and not (
                counts["detail_failed_count"] or counts["detail_pending_count"]
            ):
                raise ValueError(
                    "平台公开列表已遍历结束且无待重试详情；重新发现作品请新建任务"
                )
            await self.repository.prepare_details(job_id, retry_failed=True)
            await self.repository.reset_expiry(job_id)
            await self.repository.update(
                job_id,
                status="queued",
                phase="queued",
                finished_at=None,
                expires_at=None,
                error=None,
                next_retry_at=None,
                deadline_at=None,
            )
            self._launch(job_id)
            return await self.status(job_id)

    async def cancel(self, job_id):
        async with self._lock:
            job = await self.repository.get(job_id)
            if not job:
                return None
            task = self._tasks.get(job_id)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self._tasks.pop(job_id, None)
            job = await self.repository.get(job_id)
            if job["status"] in {"queued", "running"}:
                await self.repository.finish(
                    job_id,
                    retention_seconds=self.retention_seconds,
                    status="cancelled",
                    phase="finished",
                    complete=False,
                    next_retry_at=None,
                    error="调用方取消任务",
                )
            return await self.status(job_id)

    async def _run(self, job_id):
        try:
            while True:
                job = await self.repository.get(job_id)
                if job is None:
                    return
                retry_at = job.get("next_retry_at")
                if retry_at:
                    # No job, collection, browser or proxy slot is held in backoff.
                    await asyncio.sleep(max(0, (retry_at - datetime.now(timezone.utc)).total_seconds()))
                try:
                    async with self._gate:
                        await self._run_attempt(job_id, job)
                    return
                except (PlatformCrawlerError, TimeoutError) as exc:
                    job = await self.repository.get(job_id)
                    now = datetime.now(timezone.utc)
                    deadline = job.get("deadline_at") if job else None
                    if isinstance(exc, (AuthorIdentityError, AuthorSessionError)):
                        raise
                    if not deadline or now >= deadline:
                        raise TimeoutError() from exc
                    retry_count = job.get("retry_count", 0) + 1
                    delay = min(self.retry_max_seconds, self.retry_base_seconds * 2 ** min(retry_count - 1, 10))
                    await self.repository.update(
                        job_id, status="running", phase="retry_wait",
                        retry_count=retry_count,
                        next_retry_at=min(deadline, now + timedelta(seconds=delay)),
                        error=("本页超时，将从已保存游标自动重试" if isinstance(exc, TimeoutError) else str(exc))[:500],
                    )
                    logger.warning("author_job_retry job_id=%s retry=%s error_type=%s", job_id, retry_count, type(exc).__name__)
        except asyncio.CancelledError:
            # Shutdown keeps the checkpoint active so start() resumes it.
            raise
        except Exception as exc:
            error = (
                "作者任务超过执行时限，可续跑"
                if isinstance(exc, TimeoutError)
                else str(exc)
            )
            logger.warning(
                "author_job_failed job_id=%s error_type=%s", job_id, type(exc).__name__
            )
            try:
                job = await self.repository.get(job_id)
                await self.repository.finish(
                    job_id,
                    retention_seconds=self.retention_seconds,
                    status="partial"
                    if job and (await self.repository.counts(job_id))["saved_count"]
                    else "failed",
                    phase="finished",
                    complete=False,
                    next_retry_at=None,
                    error=error[:500],
                )
            except Exception:
                logger.exception("author_job_persistence_failed job_id=%s", job_id)
        finally:
            self._tasks.pop(job_id, None)

    async def _run_attempt(self, job_id, job):
        request = AuthorRequest(uid=job["uid"], media_name=job["media_name"])
        now = datetime.now(timezone.utc)
        deadline = job.get("deadline_at") or now + timedelta(seconds=self.timeout_seconds)
        await self.repository.reset_expiry(job_id)
        await self.repository.update(
            job_id, status="running",
            phase="details" if job.get("discovery_done") else "discovery",
            error=None, next_retry_at=None, deadline_at=deadline,
        )
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise TimeoutError()
        await self.repository.prepare_details(job_id)
        async with asyncio.timeout(remaining):
            discovery_finished = asyncio.Event()
            changed = asyncio.Event()
            discovery_error = []

            async def discover():
                try:
                    if not job.get("discovery_done"):
                        async with self._discovery_session(request) as source:
                            await self._discover(job_id, job, request, source, changed)
                except Exception as exc:
                    discovery_error.append(exc)
                finally:
                    discovery_finished.set()
                    changed.set()

            # Siblings are created before the discovery ContextVars: details
            # must never inherit the list proxy or visitor cookie jar.
            async with asyncio.TaskGroup() as group:
                group.create_task(discover())
                for _ in range(self.detail_workers):
                    group.create_task(self._detail_worker(job_id, request, discovery_finished, changed))
            if discovery_error:
                raise discovery_error[0]
            await self._finish(job_id, await self.repository.get(job_id))

    @asynccontextmanager
    async def _discovery_session(self, request):
        factory = getattr(self.service, "discovery_session", None)
        if factory is None:
            yield self.service
        else:
            async with factory(request) as source:
                yield source

    @staticmethod
    async def _checkpoint(operation):
        write = asyncio.create_task(operation)
        try:
            return await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise

    async def _discover(self, job_id, job, request, source, changed):
        while True:
            page = await source.fetch_page(request, job.get("cursor"))
            digest = (
                hashlib.sha256(page.next_cursor.encode()).hexdigest()
                if page.next_cursor
                else None
            )
            if digest and digest in job["seen_cursors"]:
                raise RuntimeError("作者分页游标重复，已停止；保留已保存结果")
            values = await self._checkpoint(self._save_page(job_id, job, page, digest))
            job.update(values)
            changed.set()
            logger.info(
                "author_page_saved job_id=%s pages=%s saved=%s",
                job_id,
                job["pages_completed"],
                job["saved_count"],
            )
            if page.next_cursor is None:
                await self.repository.update(job_id, phase="details")
                return

    async def _detail_worker(self, job_id, request, discovery_finished, changed):
        while True:
            changed.clear()
            row = await self.repository.claim_detail(job_id)
            if row is None:
                if discovery_finished.is_set():
                    return
                try:
                    await asyncio.wait_for(changed.wait(), 0.5)
                except TimeoutError:
                    pass
                continue
            work = AuthorWork.model_validate(
                {**row["work"], "detail_context": row.get("detail_context", {})}
            )
            try:
                work = await self.service.fetch_detail(request, work)
            except Exception as exc:
                work.content_complete = False
                work.detail_status = "failed"
                work.error = (
                    "详情超时，可续跑"
                    if isinstance(exc, TimeoutError)
                    else str(exc)[:500]
                )
            await self._checkpoint(self.repository.save_detail(job_id, work))

    async def _save_page(self, job_id, job, page, digest):
        # Different Bilibili sources legitimately contain the same works.
        # Detect a repeated page within a source, not a page with zero inserts.
        source = ""
        if job["media_name"] == "bilibili":
            request = AuthorRequest(uid=job["uid"], media_name=job["media_name"])
            state = decode_cursor(request, job.get("cursor"))
            source = state.get("source", "dynamic" if "offset" in state else "video")
        fingerprint = hashlib.sha256(json.dumps(
            [source, sorted({work.work_id for work in page.data})], separators=(",", ":")
        ).encode()).hexdigest() if page.data else None
        if page.next_cursor and fingerprint and fingerprint in job.get("seen_pages", []):
            raise RuntimeError("作者分页反复返回相同作品，已停止；不能证明历史完整")
        count, failed = await self.repository.save_page(
            job_id, job["pages_completed"], page.data
        )
        values = {
            "cursor": page.next_cursor,
            "pages_completed": job["pages_completed"] + 1,
            "saved_count": count,
            "detail_failed_count": failed,
            "coverage_complete": job["coverage_complete"]
            and page.coverage == "complete",
            "seen_cursors": job["seen_cursors"] + ([digest] if digest else []),
            "seen_pages": job.get("seen_pages", []) + ([fingerprint] if fingerprint else []),
            "coverage_reason": page.reason,
            "discovery_done": page.next_cursor is None,
        }
        await self.repository.update(job_id, **values)
        return values

    async def _finish(self, job_id, job):
        counts = await self.repository.counts(job_id)
        complete = (
            bool(job.get("discovery_done"))
            and job["coverage_complete"]
            and counts["detail_failed_count"] == 0
            and counts["detail_pending_count"] == 0
        )
        await self.repository.finish(
            job_id,
            retention_seconds=self.retention_seconds,
            status="completed" if complete else "partial",
            phase="finished",
            next_retry_at=None,
            **counts,
            complete=complete,
            error=None
            if complete
            else (job.get("coverage_reason") or "部分作品详情失败"),
        )

    async def aclose(self):
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        await self.repository.close()
