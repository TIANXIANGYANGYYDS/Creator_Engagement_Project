"""Additional source names backed by verified publisher protocol families."""
from __future__ import annotations

from app.crawlers.http_client import PlatformBlockedError
from app.crawlers.platforms import autohome, eastmoney, netease, sina, sohu, public_news
from app.crawlers.platforms import car_news, yiche
from app.crawlers.platforms.common import result_error
from app.models.engagement import EngagementResult


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor,
                include_stats, include_comments, platform):
    kwargs = dict(page=page, comment_cursor=comment_cursor,
                  include_stats=include_stats, include_comments=include_comments)
    if platform.startswith("sina_"):
        return await sina.fetch(crawler, url, work_id, limit, platform=platform, **kwargs)
    handler = {"chejiahao": autohome.fetch, "netease_video": netease.fetch,
               "sohu_news": sohu.fetch}.get(platform)
    if handler is not None:
        result = await handler(crawler, url, work_id, limit, **kwargs)
        return result.model_copy(update={"platform": platform})
    try:
        if platform in {"caifuhao", "guba", "hafoo", "fund1234567", "eastmoney_news", "choice_data", "choice_wealth", "eastmoney"}:
            stats, comments, cursor, source = await eastmoney.collect(
                crawler, url, work_id, limit, page, comment_cursor, include_comments, platform)
        elif platform == "yiche_hao":
            stats, comments, cursor, source = await yiche.collect(
                crawler, url, work_id, limit, page, comment_cursor, include_comments)
        else:
            collector = car_news.COLLECTORS.get(platform) or public_news.COLLECTORS[platform]
            stats, comments, cursor, source = await collector(
                crawler, url, work_id, limit, page, comment_cursor, include_comments)
        return EngagementResult(platform=platform, canonical_url=url, work_id=work_id,
            coverage="partial", stats=stats, comments=comments, next_cursor=cursor,
            source=source, reason="公开互动字段和一级评论；未公开字段保留 null")
    except public_news.PublicDataUnavailable as exc:
        return result_error(platform, url, work_id, "unsupported", exc).model_copy(update={"retryable": False})
    except PlatformBlockedError as exc:
        return result_error(platform, url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error(platform, url, work_id, "failed", exc)
