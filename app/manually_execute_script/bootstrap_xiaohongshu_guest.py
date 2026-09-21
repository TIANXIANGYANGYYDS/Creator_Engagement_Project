"""Create and verify a fresh XHS visitor without an account or a browser.

Cookie values are never printed. Optional output is a private storage-state file.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile

from app.core.config import get_settings
from app.crawlers.http_client import CurlAsyncHttpClient
from app.crawlers.proxy_provider import AsyncDailiProxyProvider
from app.crawlers.xiaohongshu_guest import open_guest_session


async def run(output: Path | None):
    settings = get_settings()
    provider = AsyncDailiProxyProvider(3, api_url=settings.proxy_51_api_url)
    client = CurlAsyncHttpClient(timeout_seconds=15, connect_timeout_seconds=5,
                                headers={}, proxy_provider=provider, proxy_mode="required")
    try:
        for attempt in range(3):
            try:
                async with asyncio.timeout(55), open_guest_session(client) as guest:
                    if output is not None:
                        cookies = []
                        for part in guest.cookie.split(";"):
                            name, value = part.strip().split("=", 1)
                            cookies.append(dict(name=name, value=value, domain=".xiaohongshu.com",
                                                path="/", expires=-1, httpOnly=False,
                                                secure=True, sameSite="Lax"))
                        output.parent.mkdir(parents=True, exist_ok=True)
                        fd, temporary = tempfile.mkstemp(dir=output.parent, prefix=".xhs-guest-")
                        try:
                            with os.fdopen(fd, "w") as stream:
                                json.dump({"cookies": cookies, "origins": []}, stream)
                            os.replace(temporary, output)
                        finally:
                            if os.path.exists(temporary):
                                os.unlink(temporary)
                    print(json.dumps({"ok": True, "guest": True, "account_used": False,
                                      "content_access_verified": False,
                                      "state_path": str(output) if output else None}))
                    return 0
            except Exception as exc:
                print(json.dumps({"ok": False, "attempt": attempt + 1,
                                  "error_type": type(exc).__name__}))
        return 1
    finally:
        try:
            await client.aclose()
        finally:
            await provider.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.output)))


if __name__ == "__main__":
    main()
