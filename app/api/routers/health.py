from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings


router = APIRouter(tags=["health"])


@router.get("/api/v1/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "proxy_mode": settings.proxy_mode,
        "proxy_configured": bool(settings.proxy_51_api_url.strip()),
    }
