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
from app.crawlers.platforms.registry import validate_media_url
from app.crawlers.platform_session import PlatformSessionStore
from app.models.engagement import EngagementPlatform


LOGIN_URLS = {
    "douyin": "https://www.douyin.com/",
    "toutiao": "https://www.toutiao.com/",
    "wechat": "https://mp.weixin.qq.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
    "haokan": "https://haokan.baidu.com/",
    "kuaishou": "https://www.kuaishou.com/",
    "bilibili": "https://www.bilibili.com/",
    "weibo": "https://weibo.com/",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在本机建立平台登录态，不接收账号密码")
    parser.add_argument("platform", choices=tuple(LOGIN_URLS))
    parser.add_argument(
        "--url",
        dest="target_url",
        help="可选的同平台内容 URL；用于直接在目标页面完成登录或安全验证",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="使用无头模式；不适合需要扫码或人工验证的首次登录",
    )
    return parser


def resolve_login_url(platform: EngagementPlatform, target_url: str | None) -> str:
    if not target_url:
        return LOGIN_URLS[platform]
    validate_media_url(target_url, platform)
    return target_url


async def run(
    platform: EngagementPlatform,
    *,
    target_url: str | None = None,
    headless: bool = False,
) -> Path:
    settings = get_settings()
    profile_dir = Path(settings.browser_profile_dir) / platform
    store = PlatformSessionStore(Path(settings.platform_session_dir))
    profile_dir.mkdir(parents=True, exist_ok=True)
    login_url = resolve_login_url(platform, target_url)

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
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
        print(f"已打开 {login_url}")
        print("请在浏览器中完成登录、扫码或安全验证；完成后回到终端按 Enter 保存会话。")
        await asyncio.to_thread(input)
        path = await store.save_context(platform, context)
        print(f"会话已保存: {path}")
        return path


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        resolve_login_url(args.platform, args.target_url)
    except ValueError as exc:
        parser.error(str(exc))
    asyncio.run(
        run(
            args.platform,
            target_url=args.target_url,
            headless=args.headless,
        )
    )


if __name__ == "__main__":
    main()
