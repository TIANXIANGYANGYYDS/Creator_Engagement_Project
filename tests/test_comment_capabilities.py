from app.crawlers.comment_capabilities import comment_capabilities


def test_root_comment_pagination_capabilities_are_truthful() -> None:
    assert {
        platform
        for platform in ("douyin", "toutiao", "haokan", "kuaishou", "bilibili")
        if comment_capabilities(platform).root_comments == "all_public_pages"
    } == {"douyin", "toutiao", "haokan", "kuaishou", "bilibili"}
    assert comment_capabilities("weibo").root_comments == "paged_until_blocked"
    assert all(
        comment_capabilities(platform).root_comments == "unavailable"
        for platform in ("wechat", "wechat_channels", "xiaohongshu")
    )
    assert all(
        comment_capabilities(platform).root_comments != "first_public_page"
        for platform in (
            "douyin",
            "toutiao",
            "wechat",
            "wechat_channels",
            "xiaohongshu",
            "haokan",
            "kuaishou",
            "bilibili",
            "weibo",
        )
    )


def test_xiaohongshu_cookie_mode_reports_authenticated_pagination() -> None:
    capability = comment_capabilities(
        "xiaohongshu",
        xiaohongshu_cookie_enabled=True,
    )

    assert capability.root_comments == "all_public_pages"
    assert capability.anonymous is False
    assert "Cookie" in capability.note
