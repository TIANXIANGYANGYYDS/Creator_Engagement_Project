"""Media aliases and public URL identification."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from app.models.engagement import EngagementPlatform


WEIBO_BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


MEDIA_ALIASES: dict[str, EngagementPlatform] = {
    "douyin": "douyin",
    "抖音": "douyin",
    "toutiao": "toutiao",
    "头条": "toutiao",
    "今日头条": "toutiao",
    "wechat": "wechat",
    "weixin": "wechat",
    "微信": "wechat",
    "公众号": "wechat",
    "微信公众号": "wechat",
    "xiaohongshu": "xiaohongshu",
    "xhs": "xiaohongshu",
    "小红书": "xiaohongshu",
    "haokan": "haokan",
    "好看": "haokan",
    "好看视频": "haokan",
    "kuaishou": "kuaishou",
    "快手": "kuaishou",
    "bilibili": "bilibili",
    "b站": "bilibili",
    "weibo": "weibo",
    "微博": "weibo",
}


def normalize_media_name(media_name: str) -> EngagementPlatform:
    normalized = re.sub(r"\s+", "", media_name.strip().casefold())
    platform = MEDIA_ALIASES.get(normalized)
    if platform is None:
        supported = ", ".join((
            "douyin", "toutiao", "wechat", "xiaohongshu",
            "haokan", "kuaishou", "bilibili", "weibo",
        ))
        raise ValueError(f"unsupported media_name; expected one of: {supported}")
    return platform


def validate_media_url(url: str, media_name: str) -> tuple[EngagementPlatform, str]:
    requested_platform = normalize_media_name(media_name)
    detected_platform, work_id = identify_url(url)
    if detected_platform != requested_platform:
        raise ValueError(
            f"media_name '{media_name}' does not match URL platform '{detected_platform}'"
        )
    return detected_platform, work_id


def weibo_bid_to_mid(bid: str) -> str:
    """Convert the base62 ID used in desktop Weibo URLs to a numeric MID."""

    if not bid or any(char not in WEIBO_BASE62_ALPHABET for char in bid):
        raise ValueError("invalid Weibo base62 content id")

    mid = ""
    for end in range(len(bid), 0, -4):
        start = max(0, end - 4)
        value = 0
        for char in bid[start:end]:
            value = value * 62 + WEIBO_BASE62_ALPHABET.index(char)
        chunk = str(value).zfill(7) if start > 0 else str(value)
        mid = chunk + mid
    return mid.lstrip("0") or "0"


def identify_url(url: str) -> tuple[EngagementPlatform, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path
    query = parse_qs(parsed.query)
    if "bilibili.com" in host:
        if host == "live.bilibili.com":
            return "bilibili", ""
        article_match = re.search(r"/read/cv(\d+)(?:/|$)", path, re.I)
        if article_match:
            return "bilibili", f"article:{article_match.group(1)}"
        if path.rstrip("/") == "/read/mobile" and query.get("id", [""])[0].isdigit():
            return "bilibili", f"article:{query['id'][0]}"
        opus_match = re.search(r"/opus/(\d+)(?:/|$)", path)
        if opus_match:
            return "bilibili", f"opus:{opus_match.group(1)}"
        match = re.search(r"/(BV[0-9A-Za-z]+|av\d+)(?:/|$)", path)
        work_id = match.group(1) if match else query.get("bvid", [""])[0]
        return "bilibili", work_id[2:] if work_id.startswith("av") else work_id
    if "weibo.com" in host or host == "m.weibo.cn":
        numeric_match = re.search(r"/(?:detail|status)/(\d{8,})(?:/|$)", path)
        if numeric_match:
            return "weibo", numeric_match.group(1)
        desktop_match = re.search(r"^/\d+/([0-9A-Za-z]+)(?:/|$)", path)
        if desktop_match:
            return "weibo", weibo_bid_to_mid(desktop_match.group(1))
        return "weibo", query.get("id", [""])[0]
    if "xiaohongshu.com" in host:
        match = re.search(r"/explore/([0-9a-f]{24})", path, re.I)
        return "xiaohongshu", match.group(1) if match else ""
    if "haokan.baidu.com" in host:
        return "haokan", query.get("vid", [""])[0]
    if "douyin.com" in host or "iesdouyin.com" in host:
        match = re.search(r"/(?:video|share/video)/(\d+)", path)
        return "douyin", match.group(1) if match else ""
    if "toutiao.com" in host:
        match = re.search(r"/(?:article|video)/(\d+)|/i(\d+)(?:/|$)", path)
        return "toutiao", next((value for value in match.groups() if value), "") if match else ""
    if "kuaishou.com" in host:
        match = re.search(r"/(?:short-video|profile|fw/photo)/([^/?]+)", path)
        return "kuaishou", match.group(1) if match else ""
    if "mp.weixin.qq.com" in host:
        path_match = re.search(r"/s/([^/?]+)", path)
        return "wechat", (
            query.get("mid", [""])[0]
            or query.get("sn", [""])[0]
            or (path_match.group(1) if path_match else "")
        )
    raise ValueError("unsupported content URL host")
