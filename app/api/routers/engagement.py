from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_engagement_service
from app.models.engagement import (
    CommentDataResponse,
    CommentPageResult,
    EngagementComment,
    InteractionDataResponse,
    InteractionResult,
)
from app.services.engagement_service import EngagementService


router = APIRouter(prefix="/api/v1", tags=["engagement"])


@router.get("/interactions", response_model=InteractionDataResponse)
async def get_interactions(
    url: str = Query(min_length=1),
    media_name: str = Query(min_length=1),
    service: EngagementService = Depends(get_engagement_service),
) -> InteractionDataResponse:
    try:
        result = await service.fetch_interactions(url, media_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _require_usable_result(result)
    if not any(value is not None for value in result.stats.model_dump().values()):
        raise HTTPException(status_code=502, detail=result.reason or "互动数据不可用")
    return InteractionDataResponse(data=result.stats)


@router.get("/comments", response_model=CommentDataResponse)
async def get_comments(
    url: str = Query(min_length=1),
    media_name: str = Query(min_length=1),
    page: int | None = Query(default=None, ge=1),
    service: EngagementService = Depends(get_engagement_service),
) -> CommentDataResponse:
    try:
        if page is not None:
            result = await service.fetch_comments(url, media_name, page)
            _require_usable_result(result)
            return CommentDataResponse(data=result.comments)
        return CommentDataResponse(
            data=await _fetch_all_comments(service, url, media_name)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _require_usable_result(result: InteractionResult | CommentPageResult) -> None:
    if result.coverage not in {"complete", "partial"}:
        raise HTTPException(
            status_code=502,
            detail=result.reason or f"{result.platform} 数据不可用",
        )


async def _fetch_all_comments(
    service: EngagementService,
    url: str,
    media_name: str,
) -> list[EngagementComment]:
    comments: list[EngagementComment] = []
    seen_comment_ids: set[str] = set()
    requested_pages: set[int] = set()
    page = 1
    cursor: str | None = None

    while True:
        if page in requested_pages or len(requested_pages) >= 10_000:
            raise HTTPException(status_code=502, detail="评论分页游标异常，已停止采集")
        requested_pages.add(page)

        if cursor is None:
            result = await service.fetch_comments(url, media_name, page)
        else:
            result = await service.fetch_comments(
                url,
                media_name,
                page,
                cursor=cursor,
            )
        if result.coverage not in {"complete", "partial"}:
            if comments:
                return comments
            _require_usable_result(result)
        for comment in result.comments:
            if comment.comment_id not in seen_comment_ids:
                seen_comment_ids.add(comment.comment_id)
                comments.append(comment)

        if result.next_page is None:
            return comments
        if result.next_page <= page:
            raise HTTPException(status_code=502, detail="评论分页游标没有向后推进")
        page = result.next_page
        cursor = result.next_cursor if result.platform != "weibo" else None
