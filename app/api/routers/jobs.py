from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.api.dependencies import get_engagement_service, get_job_manager
from app.api.routers.engagement import _collect_item
from app.models.engagement import (
    CollectItemRequest,
    CreateJobRequest,
    JobItemRequest,
    JobItemResponse,
    JobResultsResponse,
    JobStatusResponse,
    JobSubmitResponse,
)
from app.services.engagement_service import EngagementService
from app.services.job_service import BatchJobManager, IdempotencyConflictError


router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.post(
    "",
    response_model=JobSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_job(
    request: CreateJobRequest,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        min_length=1,
        max_length=128,
    ),
    service: EngagementService = Depends(get_engagement_service),
    manager: BatchJobManager = Depends(get_job_manager),
) -> JobSubmitResponse:
    async def collect_item(item: JobItemRequest) -> JobItemResponse:
        collect_request = CollectItemRequest.model_validate(
            item.model_dump(exclude={"item_id"})
        )
        result = await _collect_item(service, collect_request)
        return JobItemResponse(
            item_id=item.item_id,
            **result.model_dump(),
        )

    try:
        return await manager.submit(
            request,
            collector=collect_item,
            proxy_usage_scope=service.proxy_usage_scope,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job_id: str,
    manager: BatchJobManager = Depends(get_job_manager),
) -> JobStatusResponse:
    result = await manager.get_status(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在或结果已过期")
    return result


@router.post("/{job_id}/cancel", response_model=JobStatusResponse)
async def cancel_job(
    job_id: str,
    manager: BatchJobManager = Depends(get_job_manager),
) -> JobStatusResponse:
    result = await manager.cancel(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在或结果已过期")
    return result


@router.get("/{job_id}/results", response_model=JobResultsResponse)
async def get_job_results(
    job_id: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    manager: BatchJobManager = Depends(get_job_manager),
) -> JobResultsResponse:
    try:
        result = await manager.get_results(job_id, cursor=cursor, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="任务不存在或结果已过期")
    return result
