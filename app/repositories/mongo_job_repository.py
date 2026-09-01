from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from app.models.engagement import CreateJobRequest, JobItemResponse
from app.repositories.job_repository import (
    DuplicateIdempotencyKeyError,
    StoredJob,
)


class MongoJobRepository:
    """MongoDB-backed job store with database-side result pagination."""

    def __init__(self, uri: str, database_name: str) -> None:
        if not database_name.strip():
            raise ValueError("MONGO_DB_NAME 不能为空")
        self.client: MongoClient[dict[str, Any]] = MongoClient(
            uri,
            tz_aware=True,
            serverSelectionTimeoutMS=5_000,
        )
        self.database = self.client[database_name]
        self.jobs = self.database["jobs"]
        self.results = self.database["job_results"]

    async def initialize(self, retention_seconds: int) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def create_job(
        self,
        job: StoredJob,
        request: CreateJobRequest,
    ) -> None:
        await asyncio.to_thread(self._create_job_sync, job)

    async def get_job(self, job_id: str) -> StoredJob | None:
        document = await asyncio.to_thread(
            self.jobs.find_one,
            self._active_filter({"_id": job_id}),
        )
        return self._document_to_job(document) if document is not None else None

    async def find_by_idempotency_key(self, key: str) -> StoredJob | None:
        document = await asyncio.to_thread(
            self.jobs.find_one,
            self._active_filter({"idempotency_key": key}),
        )
        return self._document_to_job(document) if document is not None else None

    async def update_job(self, job: StoredJob) -> None:
        await asyncio.to_thread(self._update_job_sync, job)

    async def append_result(
        self,
        job_id: str,
        sequence: int,
        result: JobItemResponse,
    ) -> None:
        document = {
            "_id": f"{job_id}:{sequence}",
            "schema_version": 1,
            "job_id": job_id,
            "sequence": sequence,
            **result.model_dump(mode="python"),
            "created_at": datetime.now(timezone.utc),
            "expires_at": None,
        }
        await asyncio.to_thread(self.results.insert_one, document)

    async def get_results(
        self,
        job_id: str,
        offset: int,
        limit: int,
    ) -> list[JobItemResponse]:
        documents = await asyncio.to_thread(
            self._get_results_sync,
            job_id,
            offset,
            limit,
        )
        return [self._document_to_result(document) for document in documents]

    async def recover_interrupted(self, retention_seconds: int) -> None:
        await asyncio.to_thread(self._recover_interrupted_sync, retention_seconds)

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)

    def _initialize_sync(self) -> None:
        self.client.admin.command("ping")
        self.jobs.create_index(
            [("idempotency_key", ASCENDING)],
            name="uq_jobs_idempotency_key",
            unique=True,
            partialFilterExpression={"idempotency_key": {"$type": "string"}},
        )
        self.jobs.create_index(
            [("status", ASCENDING), ("created_at", ASCENDING)],
            name="ix_jobs_status_created_at",
        )
        self.jobs.create_index(
            [("expires_at", ASCENDING)],
            name="ttl_jobs_expires_at",
            expireAfterSeconds=0,
        )
        self.results.create_index(
            [("job_id", ASCENDING), ("sequence", ASCENDING)],
            name="uq_job_results_job_sequence",
            unique=True,
        )
        self.results.create_index(
            [("expires_at", ASCENDING)],
            name="ttl_job_results_expires_at",
            expireAfterSeconds=0,
        )

    def _create_job_sync(self, job: StoredJob) -> None:
        if job.idempotency_key:
            expired = list(
                self.jobs.find(
                    {
                        "idempotency_key": job.idempotency_key,
                        "expires_at": {"$lte": datetime.now(timezone.utc)},
                    },
                    {"_id": 1},
                )
            )
            if expired:
                expired_ids = [document["_id"] for document in expired]
                self.results.delete_many({"job_id": {"$in": expired_ids}})
                self.jobs.delete_many({"_id": {"$in": expired_ids}})
        try:
            self.jobs.insert_one(self._job_document(job))
        except DuplicateKeyError as exc:
            if job.idempotency_key:
                raise DuplicateIdempotencyKeyError(job.idempotency_key) from exc
            raise

    def _update_job_sync(self, job: StoredJob) -> None:
        document = self._job_document(job)
        document.pop("_id")
        self.jobs.update_one({"_id": job.job_id}, {"$set": document})
        if job.expires_at is not None:
            self.results.update_many(
                {"job_id": job.job_id},
                {"$set": {"expires_at": job.expires_at}},
            )

    def _get_results_sync(
        self,
        job_id: str,
        offset: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        return list(
            self.results.find(
                {"job_id": job_id, "sequence": {"$gte": offset}},
            )
            .sort("sequence", ASCENDING)
            .limit(limit)
        )

    def _recover_interrupted_sync(self, retention_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=retention_seconds)
        for job in self.jobs.find({"status": {"$in": ["queued", "running"]}}):
            job_id = job["_id"]
            completed = self.results.count_documents({"job_id": job_id})
            success = self.results.count_documents(
                {"job_id": job_id, "status": "success"}
            )
            webhook_status = job.get("webhook_status")
            self.jobs.update_one(
                {"_id": job_id},
                {
                    "$set": {
                        "status": "failed",
                        "progress.completed": completed,
                        "progress.success": success,
                        "progress.failed": completed - success,
                        "finished_at": now,
                        "webhook_status": (
                            "failed" if webhook_status == "pending" else webhook_status
                        ),
                        "error": "服务重启，任务未完成",
                        "expires_at": expires_at,
                    }
                },
            )
            self.results.update_many(
                {"job_id": job_id},
                {"$set": {"expires_at": expires_at}},
            )

    @staticmethod
    def _active_filter(filters: dict[str, Any]) -> dict[str, Any]:
        return {
            "$and": [
                filters,
                {
                    "$or": [
                        {"expires_at": None},
                        {"expires_at": {"$exists": False}},
                        {"expires_at": {"$gt": datetime.now(timezone.utc)}},
                    ]
                },
            ]
        }

    @staticmethod
    def _job_document(job: StoredJob) -> dict[str, Any]:
        return {
            "_id": job.job_id,
            "schema_version": 1,
            "request_fingerprint": job.fingerprint,
            "idempotency_key": job.idempotency_key,
            "status": job.status,
            "progress": {
                "total": job.total,
                "completed": job.completed,
                "success": job.success,
                "failed": job.failed,
            },
            "duration_ms": job.duration_ms,
            "cost_yuan": job.cost_yuan,
            "webhook_url": job.webhook_url,
            "webhook_status": job.webhook_status,
            "error": job.error,
            "cancel_requested": job.cancel_requested,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "expires_at": job.expires_at,
        }

    @staticmethod
    def _document_to_job(document: dict[str, Any]) -> StoredJob:
        progress = document["progress"]
        return StoredJob(
            job_id=document["_id"],
            fingerprint=document["request_fingerprint"],
            idempotency_key=document.get("idempotency_key"),
            status=document["status"],
            total=progress["total"],
            completed=progress["completed"],
            success=progress["success"],
            failed=progress["failed"],
            duration_ms=document["duration_ms"],
            cost_yuan=document["cost_yuan"],
            created_at=document["created_at"],
            started_at=document.get("started_at"),
            finished_at=document.get("finished_at"),
            webhook_url=document.get("webhook_url"),
            webhook_status=document.get("webhook_status"),
            error=document.get("error"),
            cancel_requested=document.get("cancel_requested", False),
            expires_at=document.get("expires_at"),
        )

    @staticmethod
    def _document_to_result(document: dict[str, Any]) -> JobItemResponse:
        payload = {
            key: value
            for key, value in document.items()
            if key
            not in {
                "_id",
                "schema_version",
                "job_id",
                "sequence",
                "created_at",
                "expires_at",
            }
        }
        return JobItemResponse.model_validate(payload)
