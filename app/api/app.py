from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routers import authors, engagement, health, jobs
from app.api.dependencies import get_engagement_service, get_job_manager
from app.api.reliability import RequestLoggingMiddleware, validation_error_handler
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.repositories.job_repository import SQLiteJobRepository
from app.repositories.mongo_job_repository import MongoJobRepository
from app.services.engagement_service import EngagementService
from app.services.job_service import BatchJobManager
from app.repositories.author_repository import AuthorRepository
from app.services.author_service import AuthorService
from app.services.author_job_service import AuthorJobManager


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
        status_alerts=service.status_alerts,
        retention_seconds=settings.job_result_ttl_seconds,
        max_concurrency=settings.job_max_concurrency,
        item_max_concurrency=settings.job_item_max_concurrency,
        item_timeout_seconds=settings.job_item_timeout_seconds,
        job_timeout_seconds=settings.job_timeout_seconds,
        timeout_batch_size=settings.job_timeout_batch_size,
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
    author_repository = AuthorRepository(settings.mongo_uri, settings.mongo_db_name)
    author_manager = AuthorJobManager(
        author_repository,
        AuthorService(service, identity_repository=author_repository),
        retention_seconds=settings.job_result_ttl_seconds,
    )
    app.state.author_job_manager = author_manager
    try:
        if not ({get_engagement_service, get_job_manager} & app.dependency_overrides.keys()):
            await job_manager.resume_interrupted(
                collector=jobs.job_collector(service),
                proxy_usage_scope=service.proxy_usage_scope,
                collection_scope=lambda item: service.collection_slot(item.media_name),
            )
        if not app.dependency_overrides:
            await author_manager.start()
        yield
    finally:
        await author_manager.aclose()
        await job_manager.aclose()
        await service.aclose()


async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("API request failed request_id=%s method=%s", request_id, request.method)
    return JSONResponse(
        status_code=500, content={"detail": "服务器内部错误"},
        headers={"X-Request-ID": request_id},
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="Creator Engagement API",
        version="0.2.0",
        description="按公开内容 URL 获取互动统计和公开评论，支持同步与异步批量采集。",
        lifespan=lifespan,
    )
    app.add_exception_handler(Exception, _unexpected_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(health.router)
    app.include_router(engagement.router)
    app.include_router(jobs.router)
    app.include_router(authors.router)
    return app
