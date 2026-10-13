from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from uuid import uuid4

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings


logger = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = monotonic()
        status = 500
        complete = False
        # JSON escaping prevents control characters from creating fake log lines.
        path = json.dumps(scope["path"], ensure_ascii=True)
        logger.info(
            "request_started request_id=%s method=%s path=%s",
            request_id,
            scope["method"],
            path,
        )

        async def logged_send(message: Message) -> None:
            nonlocal status, complete
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)
            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                complete = True

        try:
            await self.app(scope, receive, logged_send)
        except asyncio.CancelledError:
            if not complete:
                status = 499
            raise
        finally:
            # Collection routes also use 502 for absent/deleted content. Their
            # services classify actual status errors before returning, so do
            # not turn every data-unavailable result into a second alert here.
            collection_502 = status == 502 and scope["path"] in {
                "/api/v1/interactions", "/api/v1/comments", "/api/v1/authors/works",
            }
            if status >= 500 and not collection_502:
                app = scope.get("app")
                service = getattr(getattr(app, "state", None), "engagement_service", None)
                alerts = getattr(service, "status_alerts", None)
                if alerts is not None:
                    route = scope.get("route")
                    alerts.record("api", getattr(route, "path", "/unknown"),
                                  f"HTTP {status}", category="api_error", request_id=request_id)
            logger.info(
                "request_finished request_id=%s method=%s path=%s status=%s response_complete=%s duration_ms=%s",
                request_id,
                scope["method"],
                path,
                status,
                complete,
                round((monotonic() - started) * 1000),
            )


class SyncCollectionRoute(APIRoute):
    """Bound the whole synchronous operation, including queueing and pagination."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_handler(request: Request):
            seconds = get_settings().api_sync_timeout_seconds
            deadline = asyncio.timeout(seconds)
            try:
                async with deadline:
                    return await handler(request)
            except TimeoutError:
                if not deadline.expired():
                    raise
                request_id = getattr(request.state, "request_id", "unknown")
                logger.warning(
                    "sync_request_timeout request_id=%s timeout_seconds=%s",
                    request_id,
                    seconds,
                )
                return JSONResponse(
                    status_code=504,
                    content={
                        "detail": f"同步采集超过 {seconds:g} 秒，请改用 POST /api/v1/jobs 异步采集",
                        "request_id": request_id,
                    },
                )

        return bounded_handler


async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Never persist the input, body, query string, headers or validator messages.
    all_errors = exc.errors()
    errors = [{"loc": error["loc"], "type": error["type"]} for error in all_errors[:20]]
    logger.warning(
        "request_validation_failed request_id=%s error_count=%s errors=%s",
        getattr(request.state, "request_id", "unknown"),
        len(all_errors),
        json.dumps(errors, ensure_ascii=True),
    )
    return await request_validation_exception_handler(request, exc)
