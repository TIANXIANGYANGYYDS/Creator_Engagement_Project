"""Create a local authenticated browser profile for a platform.

The command deliberately does not accept passwords or cookies. The user logs
in or scans a QR code in the visible browser, then presses Enter to persist the
browser storage state under `.local/platform-sessions/`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
from pathlib import Path

from app.core.config import get_settings
from app.crawlers.platforms.registry import validate_media_url
from app.crawlers.platform_session import PlatformSessionStore
from app.models.engagement import EngagementPlatform


LOGIN_URLS = {
    "douyin": "https://www.douyin.com/",
    "toutiao": "https://www.toutiao.com/",
    "wechat": "https://mp.weixin.qq.com/",
    "wechat_channels": "https://channels.weixin.qq.com/platform/post/list",
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
        help="使用无头模式；小红书会将二维码写到 .local/analysis/xhs-login-qr.png",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="只检查并续存已有的小红书浏览器登录态；失效时立即退出，不等待扫码",
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
    check_only: bool = False,
) -> Path:
    if check_only and (platform != "xiaohongshu" or not headless):
        raise ValueError("--check-only 只支持 xiaohongshu --headless")
    settings = get_settings()
    if platform == "xiaohongshu" and settings.xiaohongshu_session_socket:
        import httpx

        if not check_only:
            raise RuntimeError("小红书已启用单一浏览器会话，请使用 serve_xiaohongshu_session --login 生成二维码")
        try:
            async with httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(uds=settings.xiaohongshu_session_socket),
                base_url="http://xhs-owner", timeout=10, trust_env=False,
            ) as client:
                status = (await client.get("/status")).json()
        except httpx.HTTPError as exc:
            raise RuntimeError("小红书常驻浏览器不可用；不另启浏览器") from exc
        if status.get("status") != "ready":
            raise RuntimeError("小红书会话暂停：" + str(status.get("status")))
        return Path(settings.xiaohongshu_session_dir) / "xiaohongshu.json"
    profile_dir = Path(settings.browser_profile_dir) / platform
    store = PlatformSessionStore(Path(settings.platform_session_dir))
    if platform == "xiaohongshu":
        if settings.xiaohongshu_browser_profile_dir:
            profile_dir = Path(settings.xiaohongshu_browser_profile_dir)
        if settings.xiaohongshu_session_dir:
            store = PlatformSessionStore(Path(settings.xiaohongshu_session_dir))
    profile_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile_dir, 0o700)
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
        firefox_user_prefs={"network.proxy.type": 0} if platform == "xiaohongshu" else {},
        exclude_addons=[DefaultAddons.UBO],
        enable_cache=True,
    ) as context:
        page = context.pages[0] if context.pages else await context.new_page()
        logged_in = asyncio.Event()
        if platform == "xiaohongshu" and headless:
            async def observe_login(response):
                if "/api/sns/web/v2/user/me" not in response.url:
                    return
                try:
                    payload = await response.json()
                except Exception:
                    return
                if (
                    isinstance(payload, dict)
                    and payload.get("success") is True
                    and (payload.get("data") or {}).get("guest") is False
                ):
                    logged_in.set()

            page.on("response", lambda response: asyncio.create_task(observe_login(response)))
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
        print(f"已打开 {login_url}")
        if platform == "xiaohongshu" and headless:
            if check_only:
                try:
                    await asyncio.wait_for(logged_in.wait(), timeout=15)
                except TimeoutError as exc:
                    raise RuntimeError("小红书浏览器登录态已失效，需要重新扫码") from exc
            qr_path = Path(".local/analysis/xhs-login-qr.png")
            qr_path.parent.mkdir(parents=True, exist_ok=True)
            last_source = None
            for _ in range(180):
                if logged_in.is_set():
                    break
                qr = page.locator("img.qrcode-img")
                source = await qr.get_attribute("src") if await qr.count() else None
                if source and source != last_source and source.startswith("data:image/"):
                    qr_path.write_bytes(base64.b64decode(source.partition(",")[2]))
                    try:
                        from PIL import Image

                        with Image.open(qr_path) as qr_image:
                            qr_image.resize((512, 512), Image.Resampling.NEAREST).save(qr_path)
                    except ImportError:
                        pass
                    os.chmod(qr_path, 0o600)
                    last_source = source
                    print(f"请在 VS Code 打开 {qr_path} 扫码确认", flush=True)
                await asyncio.sleep(1)
            else:
                raise TimeoutError("等待小红书扫码登录超时")
            logged_in.clear()
            await page.reload(wait_until="domcontentloaded", timeout=60_000)
            await asyncio.wait_for(logged_in.wait(), timeout=30)
        else:
            print("请在浏览器中完成登录、扫码或安全验证；完成后回到终端按 Enter 保存会话。")
            await asyncio.to_thread(input)
        path = await store.save_context(platform, context)
        if platform == "xiaohongshu" and headless:
            qr_path.unlink(missing_ok=True)
        print(f"会话已保存: {path}")
        return path


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        resolve_login_url(args.platform, args.target_url)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        asyncio.run(
            run(
                args.platform,
                target_url=args.target_url,
                headless=args.headless,
                check_only=args.check_only,
            )
        )
    except RuntimeError as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
