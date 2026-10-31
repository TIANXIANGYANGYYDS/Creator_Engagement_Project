"""Additional source names backed by verified publisher protocol families."""
from __future__ import annotations

from app.crawlers.http_client import PlatformBlockedError
from app.crawlers.platforms import autohome, eastmoney, netease, sina, sohu, public_news
from app.crawlers.platforms import car_news, more_car_news, yiche, qq_news, zol, ithome, smzdm, xcar, kr36, csdn, kuai
from app.crawlers.platforms import article_communities, regional_news, zhihu, tonghuashun
from app.crawlers.platforms import sina_community, publisher_pages
from app.crawlers.platforms.common import result_error
from app.models.engagement import EngagementResult


async def fetch(crawler, url, work_id, limit, *, page, comment_cursor,
                include_stats, include_comments, platform):
    kwargs = dict(page=page, comment_cursor=comment_cursor,
                  include_stats=include_stats, include_comments=include_comments)
    if platform.startswith("sina_") and platform != "sina_community":
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
        elif platform in {"qq_news", "zol", "ithome", "smzdm", "xcar", "kr36", "csdn", "kuai", "zhihu", "tonghuashun", "sina_community"}:
            module = {"qq_news": qq_news, "zol": zol, "ithome": ithome, "smzdm": smzdm,
                      "xcar": xcar, "kr36": kr36, "csdn": csdn, "kuai": kuai, "zhihu": zhihu,
                      "tonghuashun": tonghuashun, "sina_community": sina_community}[platform]
            stats, comments, cursor, source = await module.collect(
                crawler, url, work_id, limit, page, comment_cursor, include_comments)
        else:
            collector = (article_communities.COLLECTORS.get(platform) or regional_news.COLLECTORS.get(platform)
                         or publisher_pages.COLLECTORS.get(platform)
                         or car_news.COLLECTORS.get(platform) or more_car_news.COLLECTORS.get(platform)
                         or public_news.COLLECTORS[platform])
            stats, comments, cursor, source = await collector(
                crawler, url, work_id, limit, page, comment_cursor, include_comments)
        return EngagementResult(platform=platform, canonical_url=url, work_id=work_id,
            coverage="partial", stats=stats, comments=comments, next_cursor=cursor,
            source=source, reason="已取得目标内容公开数据；未公开字段保留 null")
    except public_news.PublicDataUnavailable as exc:
        return result_error(platform, url, work_id, "unsupported", exc).model_copy(update={"retryable": False})
    except PlatformBlockedError as exc:
        return result_error(platform, url, work_id, "blocked", exc)
    except Exception as exc:
        return result_error(platform, url, work_id, "failed", exc)
