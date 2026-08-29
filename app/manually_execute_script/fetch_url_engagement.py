"""Fetch interactions or a public root-comment page for one content URL."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.engagement_service import EngagementService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按内容 URL 获取互动量或公开评论分页")
    parser.add_argument("resource", choices=("interactions", "comments"), help="要获取的数据类型")
    parser.add_argument(
        "url",
        help="抖音、头条、公众号、微信视频号、小红书、好看、快手、B站或微博 URL",
    )
    parser.add_argument("media_name", help="媒体规范名或中文名，例如 bilibili/B站")
    parser.add_argument("--page", type=int, default=1, help="一级评论页码，仅 comments 模式使用")
    return parser


async def _run(args: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging(settings)
    service = EngagementService.from_settings(settings)
    try:
        if args.resource == "comments":
            result = await service.fetch_comments(args.url, args.media_name, args.page)
        else:
            result = await service.fetch_interactions(args.url, args.media_name)
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    finally:
        await service.aclose()


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
