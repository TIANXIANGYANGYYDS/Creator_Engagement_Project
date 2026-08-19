"""Fetch engagement statistics and public comments for one content URL."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.engagement_service import EngagementService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按内容 URL 获取互动量和公开评论")
    parser.add_argument("url", help="抖音、头条、公众号、小红书、好看、快手、B站或微博 URL")
    parser.add_argument("--comment-limit", type=int, default=20, help="最多返回的一级评论数")
    parser.add_argument("--direct", action="store_true", help="本次强制直连，忽略代理配置")
    return parser


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging(settings)
    service = EngagementService.from_settings(
        settings,
        proxy_mode="direct" if args.direct else None,
    )
    try:
        result = await service.fetch(args.url, comment_limit=args.comment_limit)
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    finally:
        await service.aclose()


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
