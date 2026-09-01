from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routers import engagement, health, jobs
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.repositories.job_repository import SQLiteJobRepository
from app.repositories.mongo_job_repository import MongoJobRepository
from app.services.engagement_service import EngagementService
from app.services.job_service import BatchJobManager


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    service = EngagementService.from_settings(settings)
    if settings.job_store_backend == "mongodb":
        job_repository = MongoJobRepository(
            settings.mongo_uri,
            settings.mongo_db_name,
        )
    else:
        job_repository = SQLiteJobRepository(Path(settings.job_db_path))
    job_manager = BatchJobManager(
        retention_seconds=settings.job_result_ttl_seconds,
        max_concurrency=settings.job_max_concurrency,
        item_max_concurrency=settings.job_item_max_concurrency,
        item_timeout_seconds=settings.job_item_timeout_seconds,
        job_timeout_seconds=settings.job_timeout_seconds,
        max_items=settings.job_max_items,
        result_max_bytes=settings.job_result_max_bytes,
        repository=job_repository,
        webhook_allowed_hosts={
            host.strip()
            for host in settings.job_webhook_allowed_hosts.split(",")
            if host.strip()
        },
        webhook_timeout_seconds=settings.job_webhook_timeout_seconds,
        webhook_max_attempts=settings.job_webhook_max_attempts,
    )
    app.state.engagement_service = service
    app.state.batch_job_manager = job_manager
    try:
        yield
    finally:
        await job_manager.aclose()
        await service.aclose()


async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("API request failed method=%s path=%s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


def create_app() -> FastAPI:
    app = FastAPI(
        title="Creator Engagement API",
        version="0.2.0",
        description="按公开内容 URL 获取互动统计和公开评论，支持同步与异步批量采集。",
        lifespan=lifespan,
    )
    app.add_exception_handler(Exception, _unexpected_error_handler)
    app.include_router(health.router)
    app.include_router(engagement.router)
    app.include_router(jobs.router)
    return app
