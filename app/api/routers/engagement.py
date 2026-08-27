from __future__ import annotations

import asyncio
import logging
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.dependencies import get_engagement_service
from app.crawlers.platforms.registry import normalize_media_name
from app.models.engagement import (
    CommentDataResponse,
    CommentPageResult,
    CollectItemRequest,
    CollectItemResponse,
    CollectRequest,
    CollectResponse,
    CollectResultData,
    EngagementComment,
    InteractionDataResponse,
    InteractionResult,
)
from app.services.engagement_service import EngagementService


router = APIRouter(prefix="/api/v1", tags=["engagement"])
logger = logging.getLogger(__name__)

MEDIA_NAMES_ZH = {
    "douyin": "抖音",
    "toutiao": "今日头条",
    "wechat": "微信",
    "wechat_channels": "微信视频号",
    "xiaohongshu": "小红书",
    "haokan": "好看视频",
    "kuaishou": "快手",
    "bilibili": "哔哩哔哩",
    "weibo": "微博",
}
PROXY_IP_UNIT_COST_YUAN = 0.00084


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


@router.post("/collect", response_model=CollectResponse)
async def collect(
    request: CollectRequest,
    service: EngagementService = Depends(get_engagement_service),
) -> CollectResponse:
    started_at = monotonic()
    async with service.proxy_usage_scope() as proxy_usage:
        results = await asyncio.gather(*(
            _collect_item(service, item) for item in request.items
        ))
    return CollectResponse(
        data=list(results),
        duration_ms=round((monotonic() - started_at) * 1000),
        cost_yuan=round(
            proxy_usage.added_endpoint_count * PROXY_IP_UNIT_COST_YUAN,
            8,
        ),
    )


async def _collect_item(
    service: EngagementService,
    item: CollectItemRequest,
) -> CollectItemResponse:
    media_name = item.media_name
    try:
        platform = normalize_media_name(item.media_name)
        media_name = MEDIA_NAMES_ZH[platform]
        if item.type == "interactions":
            result = await service.fetch_interactions(item.url, item.media_name)
            if result.coverage not in {"complete", "partial"} or not any(
                value is not None for value in result.stats.model_dump().values()
            ):
                return _failed_collect_item(item, media_name, result.reason)
            stats = result.stats.model_dump()
            return CollectItemResponse(
                url=item.url,
                media_name=MEDIA_NAMES_ZH[result.platform],
                type=item.type,
                status="success" if result.coverage == "complete" else "partial",
                result=CollectResultData(
                    views=result.stats.views,
                    likes=result.stats.likes,
                    total_comments=result.stats.comments,
                    shares=result.stats.shares,
                    favorites=result.stats.favorites,
                    coins=result.stats.coins,
                    danmaku=result.stats.danmaku,
                    reposts=result.stats.reposts,
                    recommendations=(
                        result.stats.recommendations
                        if result.stats.recommendations is not None
                        else stats.get("recommend")
                    ),
                ),
            )

        if item.page is None:
            comments, total_comments, is_partial = await _fetch_all_comments_with_status(
                service,
                item.url,
                item.media_name,
            )
            return CollectItemResponse(
                url=item.url,
                media_name=media_name,
                type=item.type,
                status="partial" if is_partial else "success",
                result=CollectResultData(
                    total_comments=total_comments,
                    comment_list=comments,
                ),
            )

        result = await service.fetch_comments(item.url, item.media_name, item.page)
        if result.coverage not in {"complete", "partial"}:
            return _failed_collect_item(item, media_name, result.reason)
        return CollectItemResponse(
            url=item.url,
            media_name=MEDIA_NAMES_ZH[result.platform],
            type=item.type,
            status="success" if result.coverage == "complete" else "partial",
            result=CollectResultData(
                total_comments=result.total_comments,
                comment_list=result.comments,
            ),
        )
    except ValueError as exc:
        return _failed_collect_item(item, media_name, str(exc))
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else "评论数据不可用"
        return _failed_collect_item(item, media_name, detail)
    except Exception:
        logger.exception(
            "batch collection failed media_name=%s type=%s",
            item.media_name,
            item.type,
        )
        return _failed_collect_item(item, media_name, "服务器内部错误")


def _failed_collect_item(
    item: CollectItemRequest,
    media_name: str,
    error: str,
) -> CollectItemResponse:
    return CollectItemResponse(
        url=item.url,
        media_name=media_name,
        type=item.type,
        status="failed",
        result=CollectResultData(),
        error=error or "数据不可用",
    )


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
    comments, _, _ = await _fetch_all_comments_with_status(service, url, media_name)
    return comments


async def _fetch_all_comments_with_status(
    service: EngagementService,
    url: str,
    media_name: str,
) -> tuple[list[EngagementComment], int | None, bool]:
    comments: list[EngagementComment] = []
    seen_comment_ids: set[str] = set()
    requested_pages: set[int] = set()
    page = 1
    cursor: str | None = None
    total_comments: int | None = None
    is_partial = False

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
                return comments, total_comments, True
            _require_usable_result(result)
        if result.coverage == "partial":
            is_partial = True
        if result.total_comments is not None:
            total_comments = result.total_comments
        for comment in result.comments:
            if comment.comment_id not in seen_comment_ids:
                seen_comment_ids.add(comment.comment_id)
                comments.append(comment)

        if result.next_page is None:
            return comments, total_comments, is_partial
        if result.next_page <= page:
            raise HTTPException(status_code=502, detail="评论分页游标没有向后推进")
        page = result.next_page
        cursor = result.next_cursor if result.platform != "weibo" else None
