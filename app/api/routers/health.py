from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings


router = APIRouter(tags=["health"])


@router.get("/api/v1/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "author_content_enabled": True,
        "author_pipeline_version": 7,
        "author_guest_only": settings.author_guest_only,
        "author_use_saved_sessions": settings.author_use_saved_sessions and not settings.author_guest_only,
        "douyin_author_session_mode": "anonymous" if settings.author_guest_only else settings.douyin_author_session_mode,
        "author_request_fields": ["author_id", "media_name"],
        "author_batch_enabled": True,
        "author_platforms": [
            "douyin",
            "kuaishou",
            "weibo",
            "bilibili",
            "xiaohongshu",
            "toutiao",
            "sina_blog",
        ],
        "author_detail_workers": 4,
        "api_sync_timeout_seconds": settings.api_sync_timeout_seconds,
        "api_sync_max_items": settings.api_sync_max_items,
        "proxy_mode": settings.proxy_mode,
        "proxy_configured": bool(settings.proxy_51_api_url.strip()),
        "reliability_mode": settings.reliability_mode,
        "protocol_max_attempts": (
            settings.protocol_max_attempts
            if settings.reliability_mode == "enterprise"
            else 1
        ),
        "proxy_pool_size": settings.proxy_pool_size,
        "proxy_max_concurrency": settings.proxy_max_concurrency,
        "collection_max_concurrency": settings.collection_max_concurrency,
        "douyin_max_concurrency": settings.douyin_max_concurrency,
        "job_item_max_concurrency": settings.job_item_max_concurrency,
        "job_item_timeout_seconds": settings.job_item_timeout_seconds,
        "job_timeout_seconds": settings.job_timeout_seconds,
        "job_timeout_batch_size": settings.job_timeout_batch_size,
        "request_connect_timeout_seconds": settings.request_connect_timeout_seconds,
        "job_store_backend": settings.job_store_backend,
        "job_max_items": settings.job_max_items,
        "job_result_max_bytes": settings.job_result_max_bytes,
        "toutiao_protocol_max_attempts": (
            settings.toutiao_protocol_max_attempts
            if settings.reliability_mode == "enterprise"
            else 1
        ),
        "douyin_protocol_max_attempts": (
            settings.douyin_protocol_max_attempts
            if settings.reliability_mode == "enterprise"
            else 1
        ),
        "browser_max_concurrency": settings.browser_max_concurrency,
        "browser_max_attempts": (
            settings.browser_max_attempts
            if settings.reliability_mode == "enterprise"
            else 1
        ),
        "browser_geoip_enabled": settings.browser_geoip_enabled,
        "circuit_failure_threshold": settings.circuit_failure_threshold,
        "circuit_cooldown_seconds": settings.circuit_cooldown_seconds,
    }
