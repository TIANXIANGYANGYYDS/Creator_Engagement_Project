from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.dependencies import get_engagement_service
from app.models.engagement import EngagementResult
from app.services.engagement_service import EngagementService


router = APIRouter(prefix="/api/v1", tags=["engagement"])


class BatchEngagementRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)
    comment_limit: int = Field(default=20, ge=1, le=100)
    concurrency: int = Field(default=4, ge=1, le=20)


@router.get("/engagement", response_model=EngagementResult)
async def get_engagement(
    url: str = Query(min_length=1),
    comment_limit: int = Query(default=20, ge=1, le=100),
    service: EngagementService = Depends(get_engagement_service),
) -> EngagementResult:
    try:
        return await service.fetch(url, comment_limit=comment_limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/engagement/batch", response_model=list[EngagementResult])
async def get_engagement_batch(
    request: BatchEngagementRequest,
    service: EngagementService = Depends(get_engagement_service),
) -> list[EngagementResult]:
    try:
        return await service.fetch_many(
            request.urls,
            comment_limit=request.comment_limit,
            concurrency=request.concurrency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
