"""Truthful anonymous comment coverage for each platform collector."""

from __future__ import annotations

from app.models.engagement import CommentCapabilities, EngagementPlatform
from app.crawlers.platforms.source_catalog import EXTENDED_SOURCES


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
        root_comments="first_public_page",
        anonymous=True,
        note="游客浏览器可读取页面公开的首批一级评论；继续分页通常要求登录。",
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

# These endpoints expose paginated public discussions. Deep-page access may be
# restricted independently of the first page; do not promise a full archive.
for _platform in ("sina", "sina_finance", "baijiahao", "yoojia", "sohu", "dongchedi",
                  "weitoutiao", "netease", "xueqiu", "autohome", "ixigua", "qq_stock"):
    COMMENT_CAPABILITIES[_platform] = CommentCapabilities(
        root_comments="paged_until_blocked", anonymous=True,
        note="公开评论接口分页读取；登录、验证、失效链接或分页异常会返回受限/失败，不能保证全部历史评论。",
    )



for _platform in EXTENDED_SOURCES:
    COMMENT_CAPABILITIES[_platform] = CommentCapabilities(
        root_comments="paged_until_blocked", anonymous=True,
        note="按目标页面的公开评论协议获取；部分来源不公开评论或需要登录，接口会明确返回失败。首屏成功不代表历史全集完整。",
    )

COMMENT_CAPABILITIES["qctt"] = CommentCapabilities(
    root_comments="first_public_page", anonymous=True,
    note="汽车头条仅返回页面公开的首批一级评论；不提供后续分页。",
)

COMMENT_CAPABILITIES["kr36"] = CommentCapabilities(
    root_comments="unavailable", anonymous=True,
    note="仅已验证公开互动计数；签名评论接口尚未通过实网业务验收。",
)
COMMENT_CAPABILITIES["csdn"] = CommentCapabilities(
    root_comments="unavailable", anonymous=True,
    note="已验证目标文章公开互动计数；评论接口访问挑战尚未通过实网验收。",
)

for _platform in ("jike", "chemm", "jianshu", "dingnews", "ofweek", "growthhk", "ql1d", "ichaoqi", "yzwb"):
    COMMENT_CAPABILITIES[_platform] = CommentCapabilities(
        root_comments="unavailable", anonymous=True,
        note="仅在目标文章明确显示评论数为 0 时确认空列表；非空评论正文/分页尚未完成实网验收。",
    )
for _platform in ("tidenews", "tonghuashun", "icswb", "donews", "how42", "guanhai"):
    COMMENT_CAPABILITIES[_platform] = CommentCapabilities(
        root_comments="unavailable", anonymous=True,
        note="已接入目标文章公开互动字段；未取得可验证的评论正文，不能将未知评论当作零条。",
    )
for _platform in ("acfun", "sina_community", "thepaper", "d1ev"):
    COMMENT_CAPABILITIES[_platform] = CommentCapabilities(
        root_comments="paged_until_blocked", anonymous=True,
        note="非空一级评论已实测；按官方页码/结束标记分页，回复正文不返回；失效、登录或分页异常会明确报错。",
    )
COMMENT_CAPABILITIES["mydigit"] = CommentCapabilities(
    root_comments="paged_until_blocked", anonymous=True,
    note="公开帖子计数可用；回帖正文可能仅登录可见，此时返回受限，不把登录提示当成正文。",
)
COMMENT_CAPABILITIES["zhihu"] = CommentCapabilities(
    root_comments="paged_until_blocked", anonymous=True,
    note="专栏/回答按目标实体取值，匿名浏览器通过现有代理兜底；挑战或登录限制会明确报错。",
)
for _platform in ("ijiwei", "geekpark", "shobserver"):
    COMMENT_CAPABILITIES[_platform] = CommentCapabilities(
        root_comments="paged_until_blocked", anonymous=True,
        note="按官网公开接口读取一级评论；空列表已实测，非空及深页仍需更多实网样本，不能承诺历史全集。",
    )


def comment_capabilities(
    platform: EngagementPlatform,
    *,
    xiaohongshu_cookie_enabled: bool = False,
) -> CommentCapabilities:
    if platform == "xiaohongshu" and xiaohongshu_cookie_enabled:
        return CommentCapabilities(
            root_comments="all_public_pages",
            anonymous=False,
            note="使用现有账号 Cookie 会话，独立限速并按 cursor 读取一级评论；回复仅保留数量。访问或分页受限时保留已取得内容并标记未完成。",
        )
    return COMMENT_CAPABILITIES[platform].model_copy(deep=True)
