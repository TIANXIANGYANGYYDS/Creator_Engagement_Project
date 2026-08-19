from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.engagement_service import EngagementService


def get_engagement_service(request: Request) -> EngagementService:
    service = getattr(request.app.state, "engagement_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="互动采集服务不可用")
    return service
