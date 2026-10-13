"""Drain status-error cards independently of API requests and XHS probes."""
import asyncio
import fcntl
import json
import os

import httpx

from app.core.config import get_settings
from app.services.feishu_alert import FeishuAlert
from app.services.status_alerts import ROOT, StatusAlerts


async def run(settings):
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        sent = await StatusAlerts.from_settings(settings).dispatch(FeishuAlert(settings, client).send)
        print(json.dumps({"sent": sent}))


def main():
    settings = get_settings()
    if not settings.feishu_status_alerts_enabled:
        return
    os.umask(0o077)
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / "dispatch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        asyncio.run(run(settings))


if __name__ == "__main__":
    main()
