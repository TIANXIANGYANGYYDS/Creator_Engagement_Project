"""Verified article counters; the signed comment gateway remains unverified."""
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms.common import to_int
from app.crawlers.platforms.public_news import PublicDataUnavailable
from app.crawlers.platforms.web_sources_common import assigned_json
from app.models.engagement import EngagementStats


async def collect(crawler, url, wid, limit, page, cursor, comments):
    if comments:
        raise PublicDataUnavailable("36氪当前仅支持目标文章的公开互动计数，评论正文接口尚未完成实网验证")
    response = await crawler._get_response(f"https://www.36kr.com/p/{wid}")
    state = assigned_json(response.text, "window.initialState").get("articleDetail") or {}
    payload = state.get("articleDetailData") or {}
    article = payload.get("data") or {}
    if payload.get("code") != 0 or str(article.get("itemId")) != wid or not article.get("widgetTitle"):
        raise PlatformCrawlerError("36氪页面未提供目标文章")
    counts = state.get("articleRecommendData") or {}
    stats = EngagementStats(likes=to_int(counts.get("statPraise")),
        favorites=to_int(counts.get("statCollect")), comments=to_int(counts.get("statComment")))
    if not any(value is not None for value in stats.model_dump().values()):
        raise PlatformCrawlerError("36氪目标文章未公开互动计数")
    return stats, [], None, "36kr.com initialState.articleDetail"
