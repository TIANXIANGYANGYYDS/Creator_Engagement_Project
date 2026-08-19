"""Create a local authenticated browser profile for a platform.

The command deliberately does not accept passwords or cookies. The user logs
in or scans a QR code in the visible browser, then presses Enter to persist the
browser storage state under `.local/platform-sessions/`.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.core.config import get_settings
from app.crawlers.platform_session import PlatformSessionStore


LOGIN_URLS = {
    "kuaishou": "https://www.kuaishou.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "wechat": "https://mp.weixin.qq.com/",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在本机建立平台登录态，不接收账号密码")
    parser.add_argument("platform", choices=tuple(LOGIN_URLS))
    parser.add_argument(
        "--headless",
        action="store_true",
        help="使用无头模式；不适合需要扫码或人工验证的首次登录",
    )
    return parser


async def run(platform: str, *, headless: bool = False) -> Path:
    settings = get_settings()
    profile_dir = Path(settings.browser_profile_dir) / platform
    store = PlatformSessionStore(Path(settings.platform_session_dir))
    profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        from camoufox.async_api import AsyncCamoufox
        from camoufox.addons import DefaultAddons
    except ImportError as exc:
        raise RuntimeError("登录命令需要安装 browser optional dependencies") from exc

    async with AsyncCamoufox(
        headless=headless,
        os="windows",
        locale="zh-CN",
        humanize=True,
        block_webrtc=True,
        persistent_context=True,
        user_data_dir=str(profile_dir),
        exclude_addons=[DefaultAddons.UBO],
        enable_cache=True,
    ) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(LOGIN_URLS[platform], wait_until="domcontentloaded", timeout=60_000)
        print(f"已打开 {LOGIN_URLS[platform]}")
        print("请在浏览器中完成登录、扫码或安全验证；完成后回到终端按 Enter 保存会话。")
        await asyncio.to_thread(input)
        path = await store.save_context(platform, context)
        print(f"会话已保存: {path}")
        return path


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args.platform, headless=args.headless))


if __name__ == "__main__":
    main()
