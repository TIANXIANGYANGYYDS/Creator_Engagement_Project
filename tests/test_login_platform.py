from __future__ import annotations

import pytest

from app.manually_execute_script.login_platform import LOGIN_URLS, resolve_login_url


def test_login_command_supports_all_collection_platforms() -> None:
    assert set(LOGIN_URLS) == {
        "douyin",
        "toutiao",
        "wechat",
        "xiaohongshu",
        "haokan",
        "kuaishou",
        "bilibili",
        "weibo",
    }


def test_login_command_accepts_matching_content_url() -> None:
    target = "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac"

    assert resolve_login_url("xiaohongshu", target) == target


def test_login_command_rejects_mismatched_content_url() -> None:
    with pytest.raises(ValueError, match="does not match"):
        resolve_login_url(
            "weibo",
            "https://www.xiaohongshu.com/explore/6a5585c000000000080326ac",
        )
