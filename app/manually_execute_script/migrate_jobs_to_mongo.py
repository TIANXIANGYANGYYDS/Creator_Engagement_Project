from __future__ import annotations

import argparse
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pymongo import MongoClient, ReplaceOne

from app.core.config import get_settings
from app.models.engagement import CreateJobRequest, JobItemResponse
from app.repositories.mongo_job_repository import MongoJobRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将未过期的异步任务从 SQLite 迁移到项目 MongoDB",
    )
    parser.add_argument("--apply", action="store_true", help="实际写入；默认只预览")
    parser.add_argument("--sqlite-path", type=Path, help="覆盖 JOB_DB_PATH")
    args = parser.parse_args()

    settings = get_settings()
    sqlite_path = args.sqlite_path or Path(settings.job_db_path)
    if not sqlite_path.exists():
        raise SystemExit(f"SQLite 文件不存在: {sqlite_path}")

    jobs, results = _read_active_rows(
        sqlite_path,
        settings.job_result_ttl_seconds,
    )
    print(f"待迁移任务: {len(jobs)}，结果: {len(results)}")
    if not args.apply:
        print("预览完成；确认后添加 --apply 执行迁移")
        return

    repository = MongoJobRepository(settings.mongo_uri, settings.mongo_db_name)
    try:
        asyncio.run(repository.initialize(settings.job_result_ttl_seconds))
        _write_rows(repository.client, settings.mongo_db_name, jobs, results)
    finally:
        repository.client.close()
    print("MongoDB 迁移完成")


def _read_active_rows(
    sqlite_path: Path,
    retention_seconds: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=retention_seconds)
    jobs: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(batch_jobs)")
        }
        if not columns:
            return jobs, results
        for row in connection.execute("SELECT * FROM batch_jobs ORDER BY created_at"):
            finished_at = _parse_datetime(row["finished_at"])
            if finished_at is not None and finished_at < cutoff:
                continue
            request = CreateJobRequest.model_validate_json(row["request_json"])
            expires_at = (
                finished_at + timedelta(seconds=retention_seconds)
                if finished_at is not None
                else None
            )
            job_id = row["job_id"]
            jobs.append(
                {
                    "_id": job_id,
                    "schema_version": 1,
                    "request_fingerprint": row["fingerprint"],
                    "idempotency_key": row["idempotency_key"],
                    "status": row["status"],
                    "progress": {
                        "total": len(request.items),
                        "completed": row["completed"],
                        "success": row["success"],
                        "failed": row["failed"],
                    },
                    "duration_ms": row["duration_ms"],
                    "cost_yuan": row["cost_yuan"],
                    "webhook_url": (
                        str(request.webhook_url) if request.webhook_url else None
                    ),
                    "webhook_status": row["webhook_status"],
                    "error": row["error"] if "error" in columns else None,
                    "cancel_requested": False,
                    "created_at": datetime.fromisoformat(row["created_at"]),
                    "started_at": _parse_datetime(row["started_at"]),
                    "finished_at": finished_at,
                    "expires_at": expires_at,
                }
            )
            for result_row in connection.execute(
                "SELECT sequence, result_json FROM batch_job_results "
                "WHERE job_id = ? ORDER BY sequence",
                (job_id,),
            ):
                result = JobItemResponse.model_validate_json(result_row["result_json"])
                sequence = result_row["sequence"]
                results.append(
                    {
                        "_id": f"{job_id}:{sequence}",
                        "schema_version": 1,
                        "job_id": job_id,
                        "sequence": sequence,
                        **result.model_dump(mode="python"),
                        "created_at": datetime.now(timezone.utc),
                        "expires_at": expires_at,
                    }
                )
    return jobs, results


def _write_rows(
    client: MongoClient[dict[str, Any]],
    database_name: str,
    jobs: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    database = client[database_name]
    for collection_name, documents in (("jobs", jobs), ("job_results", results)):
        collection = database[collection_name]
        for start in range(0, len(documents), 1_000):
            batch = documents[start : start + 1_000]
            if batch:
                collection.bulk_write(
                    [
                        ReplaceOne({"_id": document["_id"]}, document, upsert=True)
                        for document in batch
                    ],
                    ordered=False,
                )


def _parse_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


if __name__ == "__main__":
    main()
