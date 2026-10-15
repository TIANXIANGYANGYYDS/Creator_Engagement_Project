"""Media aliases and public URL identification."""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

from app.models.engagement import EngagementPlatform
from app.crawlers.platforms.source_catalog import EXTENDED_SOURCES, source_id


WEIBO_BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
WECHAT_CHANNELS_MOBILE_PATH = "/mobile/commonFinderJsApi.html"


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
    "wechat_channels": "wechat_channels",
    "wechat-channels": "wechat_channels",
    "channels": "wechat_channels",
    "视频号": "wechat_channels",
    "微信视频号": "wechat_channels",
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
    "哔哩哔哩": "bilibili",
    "weibo": "weibo",
    "微博": "weibo",
    "新浪微博": "weibo",
    "sina": "sina",
    "新浪看点": "sina",
    "baijiahao": "baijiahao",
    "百家号": "baijiahao",
    "yoojia": "yoojia",
    "有驾": "yoojia",
    "sina_finance": "sina_finance",
    "新浪财经头条号": "sina_finance",
    "sohu": "sohu",
    "搜狐号": "sohu",
    "dongchedi": "dongchedi",
    "懂车帝": "dongchedi",
    "weitoutiao": "weitoutiao",
    "今日头条微头条": "weitoutiao",
    "netease": "netease",
    "网易号": "netease",
    "xueqiu": "xueqiu",
    "雪球": "xueqiu",
    "autohome": "autohome",
    "汽车之家": "autohome",
    "ixigua": "ixigua",
    "西瓜视频": "ixigua",
    "qq_stock": "qq_stock",
    "腾讯自选股": "qq_stock",
}

for _key, (_name, _hosts) in EXTENDED_SOURCES.items():
    MEDIA_ALIASES[_key] = _key
    MEDIA_ALIASES[_name.casefold()] = _key


def normalize_media_name(media_name: str) -> EngagementPlatform:
    normalized = re.sub(r"\s+", "", media_name.strip().casefold())
    platform = MEDIA_ALIASES.get(normalized)
    if platform is None:
        supported = ", ".join(dict.fromkeys(MEDIA_ALIASES.values()))
        raise ValueError(f"unsupported media_name; expected one of: {supported}")
    return platform


def validate_media_url(url: str, media_name: str) -> tuple[EngagementPlatform, str]:
    requested_platform = normalize_media_name(media_name)
    if requested_platform in EXTENDED_SOURCES:
        return requested_platform, source_id(requested_platform, url)
    detected_platform, work_id = identify_url(url)
    # Legacy /i links are shared by articles and micro-posts. The caller's
    # media name disambiguates them; /w and /article remain unambiguous.
    if requested_platform == "weitoutiao" and detected_platform == "toutiao" and re.fullmatch(r"/i\d+/?", urlparse(url).path):
        return "weitoutiao", work_id
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


def extract_wechat_channels_mobile_feed_id(url: str) -> str:
    """Extract an encrypted feed ID from a WeChat Channels client jump URL."""

    parsed = urlparse(url)
    if (
        parsed.hostname != "channels.weixin.qq.com"
        or parsed.path != WECHAT_CHANNELS_MOBILE_PATH
    ):
        return ""

    query = parse_qs(parsed.query)
    if query.get("api", [""])[0] != "openFinderView":
        return ""
    raw_ext_info = query.get("extInfo", query.get("extinfo", [""]))[0]
    try:
        ext_info = json.loads(raw_ext_info)
    except (TypeError, ValueError):
        return ""
    if not isinstance(ext_info, dict) or ext_info.get("action") != "openFinderFeed":
        return ""

    feed_id = ext_info.get("feedID")
    if not isinstance(feed_id, str):
        return ""
    feed_id = feed_id.strip()
    if not 8 <= len(feed_id) <= 512:
        return ""
    if re.fullmatch(r"export/[0-9A-Za-z_-]+", feed_id) is None:
        return ""
    return feed_id


def identify_url(url: str) -> tuple[EngagementPlatform, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    path = parsed.path
    query = parse_qs(parsed.query)
    if host in {"k.sina.cn", "k.sina.com.cn"}:
        match = re.search(r"/article_(\d+)_([0-9a-z]+)\.html", path)
        return "sina", f"{match[1]}-{match[2]}" if match else ""
    if host in {"cj.sina.com.cn", "cj.sina.cn", "t.cj.sina.com.cn", "t.cj.sina.cn", "finance.sina.com.cn", "finance.sina.cn"}:
        match = re.search(r"/articles/view/(\d+)/([0-9a-z]+)", path)
        if match:
            return "sina_finance", f"{match[1]}-{match[2]}"
        if path == "/article/norm_detail" and query.get("url"):
            nested_platform, nested_id = identify_url(query["url"][0])
            if nested_platform != "sina_finance":
                raise ValueError("新浪财经分享链接目标域名不匹配")
            return nested_platform, nested_id
        detail = re.search(r"(?:detail-|doc-)i([0-9a-z]+)\.(?:d\.)?s?html", path)
        return "sina_finance", ("comos-" + detail[1]) if detail else query.get("id", [""])[0]
    if host in {"baijiahao.baidu.com", "mbd.baidu.com"}:
        nid = query.get("nid", query.get("id", [""]))[0]
        if not nid and query.get("context"):
            try:
                nid = str(json.loads(query["context"][0]).get("nid") or "")
            except (ValueError, TypeError):
                pass
        return "baijiahao", nid
    if host in {"www.yoojia.com", "yoojia.com", "youjia.baidu.com"}:
        match = re.search(r"/(?:article|video)/(\d+)\.html", path)
        return "yoojia", match[1] if match else query.get("id", query.get("nid", [""]))[0]
    if host in {"www.sohu.com", "m.sohu.com", "3g.k.sohu.com"}:
        match = re.search(r"/a/(\d+)(?:_\d+)?|/t/n(\d+)", path)
        return "sohu", next(v for v in match.groups() if v) if match else ""
    if host in {"www.dongchedi.com", "m.dongchedi.com", "dcd.zjbyte.cn"}:
        match = re.search(r"/(?:article/|video/|i)(\d+)", path)
        return "dongchedi", match[1] if match else ""
    if host in {"www.163.com", "m.163.com", "c.m.163.com", "dy.163.com"}:
        video = re.search(r"/(V[A-Z0-9]{8,10})\.html", path, re.I)
        if video:
            return "netease", "video:" + video[1].upper()
        match = re.search(r"/([A-Z0-9]{16})\.html", path, re.I)
        return "netease", match[1].upper() if match else ""
    if host in {"xueqiu.com", "www.xueqiu.com"}:
        match = re.fullmatch(r"/\d+/(\d+)/?", path)
        return "xueqiu", match[1] if match else ""
    if host == "autohome.com.cn" or host.endswith(".autohome.com.cn"):
        match = re.search(r"/(?:share/)?info/(\d+)|/(\d+)\.html", path)
        if host == "car.autohome.com.cn":
            return "autohome", ""
        return "autohome", next(v for v in match.groups() if v) if match else ""
    if host in {"www.ixigua.com", "m.ixigua.com", "ixigua.com"}:
        match = re.fullmatch(r"/i?(\d+)/?", path)
        return "ixigua", match[1] if match else ""
    if host == "gu.qq.com":
        fragment_query = parse_qs(parsed.fragment.partition("?")[2])
        if path == "/community/comment/index.html":
            nid = (fragment_query.get("nid") or query.get("nid") or [""])[0]
            return "qq_stock", "community:" + nid if re.fullmatch(r"\d{24}", nid) else ""
        return "qq_stock", (fragment_query.get("id") or query.get("id") or query.get("comment_id") or [""])[0]
    if "bilibili.com" in host:
        if host == "live.bilibili.com":
            return "bilibili", ""
        if host == "t.bilibili.com":
            dynamic_match = re.fullmatch(r"/(\d+)/?", path)
            return "bilibili", f"opus:{dynamic_match[1]}" if dynamic_match else ""
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
        fid = query.get("fid", [""])[0]
        video_fid_match = re.fullmatch(r"(?:\d+:)?(\d{8,})", fid)
        if host == "video.weibo.com" and video_fid_match:
            return "weibo", video_fid_match.group(1)
        tv_match = re.search(r"/tv/show/(?:\d+:)?(\d{8,})(?:/|$)", path)
        if tv_match:
            return "weibo", tv_match.group(1)
        numeric_match = re.search(r"/(?:detail|status)/(\d{8,})(?:/|$)", path)
        if numeric_match:
            return "weibo", numeric_match.group(1)
        desktop_match = re.search(r"^/\d+/([0-9A-Za-z]+)(?:/|$)", path)
        if desktop_match:
            return "weibo", weibo_bid_to_mid(desktop_match.group(1))
        return "weibo", query.get("id", [""])[0]
    if "xiaohongshu.com" in host:
        match = re.search(r"/(?:explore|discovery/item)/([0-9a-f]{24})(?:/|$)", path, re.I)
        return "xiaohongshu", match.group(1) if match else ""
    if "haokan.baidu.com" in host:
        return "haokan", query.get("vid", [""])[0]
    if "douyin.com" in host or "iesdouyin.com" in host:
        match = re.search(r"/(?:video|share/video)/(\d+)", path)
        return "douyin", match.group(1) if match else ""
    if "toutiao.com" in host:
        micro = re.fullmatch(r"/w/(\d+)/?", path)
        if micro:
            return "weitoutiao", micro[1]
        match = re.search(r"/(?:article|video)/(\d+)|/i(\d+)(?:/|$)", path)
        return "toutiao", next((value for value in match.groups() if value), "") if match else ""
    if "kuaishou.com" in host:
        match = re.search(r"/(?:short-video|profile|fw/photo)/([^/?]+)", path)
        return "kuaishou", match.group(1) if match else ""
    if host == "weixin.qq.com":
        match = re.search(r"/sph/([0-9A-Za-z_-]+)(?:/|$)", path)
        if match:
            return "wechat_channels", match.group(1)
    if host == "channels.weixin.qq.com":
        mobile_feed_id = extract_wechat_channels_mobile_feed_id(url)
        if mobile_feed_id:
            return "wechat_channels", mobile_feed_id
        short_match = re.search(r"/finder-preview/pages/sph(?:/|$)", path)
        if short_match:
            return "wechat_channels", query.get("id", [""])[0]
        if path in {"/finder-preview/pages/feed", "/web/pages/feed"}:
            return "wechat_channels", (
                query.get("eid", [""])[0]
                or query.get("oid", [""])[0]
            )
    if "mp.weixin.qq.com" in host:
        path_match = re.search(r"/s/([^/?]+)", path)
        return "wechat", (
            query.get("mid", [""])[0]
            or query.get("sn", [""])[0]
            or (path_match.group(1) if path_match else "")
        )
    for platform, (_, hosts) in EXTENDED_SOURCES.items():
        if host in hosts:
            return platform, source_id(platform, url)
    raise ValueError("unsupported content URL host")
