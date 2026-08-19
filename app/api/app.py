from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routers import engagement, health
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.engagement_service import EngagementService


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings)
    service = EngagementService.from_settings(settings)
    app.state.engagement_service = service
    try:
        yield
    finally:
        await service.aclose()


async def _unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("API request failed method=%s path=%s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


def create_app() -> FastAPI:
    app = FastAPI(
        title="Creator Engagement API",
        version="0.1.0",
        description="按公开内容 URL 获取互动统计和公开评论。",
        lifespan=lifespan,
    )
    app.add_exception_handler(Exception, _unexpected_error_handler)
    app.include_router(health.router)
    app.include_router(engagement.router)
    return app
