"""Run the private, persistent XHS browser. --login permits QR reauthorization."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import fcntl
import json
import os
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from app.core.config import get_settings
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platform_session import PlatformSessionStore
from app.crawlers.xiaohongshu_session import XiaohongshuSessionOwner


class ReadRequest(BaseModel):
    method: str
    url: str
    params: dict | None = None
    data: str | dict | None = None
    json_body: dict | None = None
    sign_format: str = "xys"


async def health_loop(owner):
    while True:
        await asyncio.sleep(60)
        if owner.status != "ready" or time.monotonic() - owner.last_end < 3600:
            continue
        try:
            await owner.request("GET", "https://edith.xiaohongshu.com/api/sns/web/v2/user/me")
        except PlatformCrawlerError:
            pass  # Persistent state records the reason; never clear a stop here.


def create_app(settings, *, login=False):
    @asynccontextmanager
    async def lifespan(app):
        from camoufox.async_api import AsyncCamoufox
        from camoufox.addons import DefaultAddons

        root = Path(settings.xiaohongshu_session_socket).resolve().parent
        profile = Path(settings.xiaohongshu_browser_profile_dir)
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with AsyncCamoufox(
            headless=True, os="windows", locale="zh-CN", humanize=True,
            block_webrtc=True, persistent_context=True, user_data_dir=str(profile),
            firefox_user_prefs={"network.proxy.type": 0},
            exclude_addons=[DefaultAddons.UBO], enable_cache=True,
        ) as context:
            page = context.pages[0] if context.pages else await context.new_page()
            for extra in context.pages[1:]:
                await extra.close()
            owner = XiaohongshuSessionOwner(context, page,
                PlatformSessionStore(Path(settings.xiaohongshu_session_dir)), root,
                interval=settings.xiaohongshu_request_interval_seconds)
            app.state.owner = owner
            await context.route("**/*", owner.route)
            page.on("response", lambda response: asyncio.create_task(owner.observe_network(response)))
            page.on("response", lambda response: asyncio.create_task(owner.observe_login(response)))
            previous = root / "status.json"
            state = json.loads(previous.read_text()) if previous.exists() else {}
            owner.set_status("reauthorization_required", state.get("reason") or "等待显式 --login 授权")
            tasks = [asyncio.create_task(health_loop(owner))]
            app.state.login_task = None
            if login:
                app.state.login_task = asyncio.create_task(owner.login())
            try:
                yield
            finally:
                if app.state.login_task:
                    tasks.append(app.state.login_task)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if owner.status == "ready":
                    await owner.store.save_context("xiaohongshu", context)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/status")
    async def status():
        return app.state.owner.snapshot()

    @app.get("/login/status")
    async def login_status():
        owner = app.state.owner
        return {**owner.snapshot(), "events": owner.login_events,
                "qr_visible": await owner.page.locator("img.qrcode-img").count() > 0}

    @app.post("/login/refresh")
    async def refresh_login():
        if app.state.owner.status == "ready":
            raise HTTPException(409, "已登录，不重复登录")
        if app.state.login_task:
            app.state.login_task.cancel()
            await asyncio.gather(app.state.login_task, return_exceptions=True)
        app.state.login_task = asyncio.create_task(app.state.owner.login())
        return {"status": "awaiting_scan"}

    @app.post("/request")
    async def request(body: ReadRequest):
        try:
            return await app.state.owner.request(**body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PlatformCrawlerError as exc:
            raise HTTPException(503, str(exc)) from exc

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--login", action="store_true", help="允许在此常驻浏览器中扫码登录")
    args = parser.parse_args()
    settings = get_settings()
    if not all((settings.xiaohongshu_session_socket, settings.xiaohongshu_session_dir,
                settings.xiaohongshu_browser_profile_dir)):
        parser.error("必须配置 XIAOHONGSHU_SESSION_SOCKET、SESSION_DIR 和 BROWSER_PROFILE_DIR")
    os.umask(0o077)
    socket = Path(settings.xiaohongshu_session_socket).resolve()
    socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket.parent.chmod(0o700)
    # Keep the descriptor alive until the browser and server both exit.
    with (socket.parent / "owner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        socket.unlink(missing_ok=True)
        uvicorn.run(create_app(settings, login=args.login), uds=str(socket), access_log=False)


if __name__ == "__main__":
    main()
