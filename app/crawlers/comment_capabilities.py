"""Truthful anonymous comment coverage for each platform collector."""

from __future__ import annotations

from app.models.engagement import CommentCapabilities, EngagementPlatform


COMMENT_CAPABILITIES: dict[EngagementPlatform, CommentCapabilities] = {
    "douyin": CommentCapabilities(
        root_comments="all_public_pages",
        anonymous=True,
        note="一级评论可翻到匿名公开末游标。",
    ),
    "toutiao": CommentCapabilities(
        root_comments="all_public_pages",
        anonymous=True,
        note="一级评论可按 offset 翻到公开末页。",
    ),
    "wechat": CommentCapabilities(
        root_comments="unavailable",
        anonymous=False,
        note="零账号模式无法稳定获得任意公众号文章评论正文。",
    ),
    "wechat_channels": CommentCapabilities(
        root_comments="unavailable",
        anonymous=False,
        note="公开视频页只公开评论总数，不公开评论正文。",
    ),
    "xiaohongshu": CommentCapabilities(
        root_comments="unavailable",
        anonymous=False,
        note="当前签名评论协议仍要求账号会话；零账号部署不启用。",
    ),
    "haokan": CommentCapabilities(
        root_comments="all_public_pages",
        anonymous=True,
        note="一级评论可通过公开页码接口翻到公开末页。",
    ),
    "kuaishou": CommentCapabilities(
        root_comments="all_public_pages",
        anonymous=True,
        note="使用程序自动建立的游客验证状态，不需要账号；状态失效时可能触发验证码。",
    ),
    "bilibili": CommentCapabilities(
        root_comments="all_public_pages",
        anonymous=True,
        note="视频、专栏和 Opus 的一级评论均可匿名分页到公开末页。",
    ),
    "weibo": CommentCapabilities(
        root_comments="paged_until_blocked",
        anonymous=True,
        note="一级评论可匿名翻多页，但深页可能触发登录或风控，不能承诺全量。",
    ),
}


def comment_capabilities(platform: EngagementPlatform) -> CommentCapabilities:
    return COMMENT_CAPABILITIES[platform].model_copy(deep=True)
