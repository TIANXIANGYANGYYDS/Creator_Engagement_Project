from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError

from app.api.dependencies import get_engagement_service
from app.api.reliability import SyncCollectionRoute
from app.crawlers.http_client import PlatformBlockedError, PlatformCrawlerError
from app.models.author import (
    AuthorJobResults,
    AuthorJobStatus,
    AuthorPage,
    AuthorRequest,
    AuthorBatchSubmission,
)
from app.services.author_service import AuthorService


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/authors", tags=["authors"])
page_router = APIRouter(route_class=SyncCollectionRoute)


def get_author_service(request: Request, service=Depends(get_engagement_service)):
    manager = getattr(request.app.state, "author_job_manager", None)
    if manager is not None:
        return manager.service
    return AuthorService(service)


def get_author_manager(request: Request):
    manager = getattr(request.app.state, "author_job_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="作者任务服务不可用")
    return manager


@page_router.get("/works", response_model=AuthorPage)
async def author_works(
    media_name: str = Query(min_length=1),
    author_id: str | None = Query(default=None, min_length=1, max_length=256),
    uid: str | None = Query(
        default=None, min_length=1, max_length=256, deprecated=True
    ),
    cursor: str | None = Query(default=None, max_length=4096),
    service=Depends(get_author_service),
):
    try:
        if author_id is not None and uid is not None and author_id != uid:
            raise ValueError("author_id 与兼容参数 uid 不一致")
        request = AuthorRequest(
            author_id=author_id if author_id is not None else uid, media_name=media_name
        )
        return await service.fetch_page(request, cursor)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc.errors(include_input=False, include_url=False)),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="作者列表本页采集超时，请稍后使用同一 cursor 重试"
        ) from exc
    except PlatformBlockedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PlatformCrawlerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


router.include_router(page_router)


@router.post(
    "/jobs", response_model=AuthorJobStatus | AuthorBatchSubmission, status_code=202
)
async def submit_author_job(
    request: AuthorRequest | list[object],
    http_request: Request,
    manager=Depends(get_author_manager),
):
    if isinstance(request, list):
        return await submit_author_batch(request, http_request, manager)
    result = await manager.submit(request)
    logger.info(
        "author_job_submitted request_id=%s job_id=%s platform=%s",
        getattr(http_request.state, "request_id", "unknown"),
        result.job_id,
        result.media_name,
    )
    return result


@router.post("/jobs/batch", response_model=AuthorBatchSubmission, status_code=202)
async def submit_author_batch(
    request: list[object], http_request: Request, manager=Depends(get_author_manager)
):
    result = await manager.submit_batch(request)
    logger.info(
        "author_batch_submitted request_id=%s total=%d accepted=%d duplicate=%d rejected=%d",
        getattr(http_request.state, "request_id", "unknown"),
        result.total,
        result.accepted_count,
        result.duplicate_count,
        result.rejected_count,
    )
    return result


@router.get("/jobs/{job_id}", response_model=AuthorJobStatus)
async def author_job_status(job_id: str, manager=Depends(get_author_manager)):
    result = await manager.status(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="作者任务不存在或已过期")
    return result


@router.get("/jobs/{job_id}/results", response_model=AuthorJobResults)
async def author_job_results(
    job_id: str,
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=100, ge=1, le=500),
    manager=Depends(get_author_manager),
):
    try:
        result = await manager.results(job_id, cursor, limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="作者任务不存在或已过期")
    return result


@router.post("/jobs/{job_id}/resume", response_model=AuthorJobStatus, status_code=202)
async def resume_author_job(job_id: str, manager=Depends(get_author_manager)):
    try:
        result = await manager.resume(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="作者任务不存在或已过期")
    return result


@router.post("/jobs/{job_id}/cancel", response_model=AuthorJobStatus)
async def cancel_author_job(job_id: str, manager=Depends(get_author_manager)):
    result = await manager.cancel(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="作者任务不存在或已过期")
    return result
