from __future__ import annotations

from fastapi import HTTPException, Request

from app.services.engagement_service import EngagementService
from app.services.job_service import BatchJobManager


def get_engagement_service(request: Request) -> EngagementService:
    service = getattr(request.app.state, "engagement_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="互动采集服务不可用")
    return service


def get_job_manager(request: Request) -> BatchJobManager:
    manager = getattr(request.app.state, "batch_job_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="异步任务服务不可用")
    return manager
