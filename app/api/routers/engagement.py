from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_engagement_service
from app.models.engagement import CommentPageResult, InteractionResult
from app.services.engagement_service import EngagementService


router = APIRouter(prefix="/api/v1", tags=["engagement"])


@router.get("/interactions", response_model=InteractionResult)
async def get_interactions(
    url: str = Query(min_length=1),
    media_name: str = Query(min_length=1),
    service: EngagementService = Depends(get_engagement_service),
) -> InteractionResult:
    try:
        return await service.fetch_interactions(url, media_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/comments", response_model=CommentPageResult)
async def get_comments(
    url: str = Query(min_length=1),
    media_name: str = Query(min_length=1),
    page: int = Query(default=1, ge=1),
    service: EngagementService = Depends(get_engagement_service),
) -> CommentPageResult:
    try:
        return await service.fetch_comments(url, media_name, page)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
