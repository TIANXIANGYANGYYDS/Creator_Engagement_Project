"""Public source names and exact host allowlists for the 20k source export."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
import re

# Shared publishers remain separate source names in the public API.
EXTENDED_SOURCES = {
    'caifuhao': ('财富号', ('caifuhao.eastmoney.com', 'emcreative.eastmoney.com')),
    'yiche_hao': ('易车号', ('hao.yiche.com', 'news.m.yiche.com', 'news.yiche.com', 'vc.m.yiche.com')),
    'chejiahao': ('车家号', ('chejiahao.autohome.com.cn', 'chejiahao.m.autohome.com.cn')),
    'gelonghui': ('格隆汇', ('www.gelonghui.com',)),
    'dayu': ('大鱼号', ('a.mp.uc.cn', 'iflow.uc.cn', 'm.uczzd.cn')),
    'guba': ('股吧', ('guba.eastmoney.com', 'mguba.eastmoney.com')),
    'yidian': ('一点资讯', ('www.yidianzixun.com',)),
    'sina_auto': ('新浪汽车', ('auto.sina.cn',)),
    'zaker': ('ZAKER', ('app.myzaker.com', 'app3.myzaker.com', 'www.myzaker.com')),
    'qctt': ('汽车头条', ('m.qctt.cn', 'www.qctt.cn')),
    'ifeng_dafeng': ('大风号', ('feng.ifeng.com', 'ishare.ifeng.com')),
    'pcauto': ('太平洋号', ('hj.pcauto.com.cn', 'www.pcauto.com.cn')),
    'sina_news': ('新浪网', ('auto.sina.com.cn', 'finance.sina.com.cn', 'news.sina.com.cn')),
    'dripcar': ('水滴汽车', ('www.dripcar.com',)),
    'cheshi': ('车市号', ('cheshihao.cheshi.com',)),
    'nowcoder': ('牛客网', ('www.nowcoder.com',)),
    'hafoo': ('哈富证券', ('emish5.eastmoney.com', 'hfmgb.eastmoney.com')),
    'fund1234567': ('天天基金', ('h5.1234567.com.cn',)),
    'eastmoney_news': ('东方财富网', ('appcert.eastmoney.com', 'finance.eastmoney.com', 'gubaf10.eastmoney.com')),
    'ximalaya': ('喜马拉雅', ('m.ximalaya.com',)),
    'choice_data': ('Choice数据', ('app.jg.eastmoney.com', 'choicew2z.eastmoney.com')),
    'ifeng': ('凤凰网', ('auto.ifeng.com', 'biz.ifeng.com', 'finance.ifeng.com', 'i.ifeng.com', 'liaon.ifeng.com', 'tech.ifeng.com')),
    'choice_wealth': ('choice', ('choiceweb.eastmoney.com',)),
    'news18': ('网通社·汽车', ('auto.news18a.com', 'ev.news18a.com', 'www.news18a.com')),
    'sina_market': ('新浪财经', ('finance.sina.com.cn', 'guba.sina.cn', 'wap.cj.sina.cn')),
    'netease_video': ('网易视频', ('v.163.com',)),
    'sina_tech': ('新浪科技', ('finance.sina.cn', 'tech.sina.cn')),
    'sohu_news': ('搜狐网', ('www.sohu.com',)),
    'eastmoney': ('东方财富', ('wap.eastmoney.com',)),
}


def source_id(platform: str, url: str) -> str:
    """Validate the source and accept only implemented article URL forms."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("内容 URL 必须为不含用户凭据的 HTTP(S) 地址")
    if parsed.port not in {None, 80, 443} or parsed.hostname not in EXTENDED_SOURCES[platform][1]:
        raise ValueError("media_name 与内容 URL 的平台不匹配")
    query = parse_qs(parsed.query)
    query.update(parse_qs(parsed.fragment.partition("?")[2]))
    if platform.startswith("sina_"):
        match = re.search(r"(?:detail-|doc-)i([a-z0-9]+)\.(?:d\.)?s?html", parsed.path)
        return "comos-" + match[1] if match else ""
    if platform == "netease_video":
        match = re.search(r"/(V[A-Z0-9]{8,10})\.html", parsed.path, re.I)
        return "video:" + match[1].upper() if match else ""
    if platform == "sohu_news":
        match = re.search(r"/a/(\d+)_\d+", parsed.path)
        return match[1] if match else ""
    if platform == "chejiahao":
        match = re.search(r"/(?:share/)?info/(\d+)", parsed.path)
        return match[1] if match else ""
    for key in ("postId", "postid", "artCode", "infocode", "infoCode", "code", "id", "aid", "docid", "pk", "pid", "newsId"):
        value = (query.get(key) or [""])[0]
        if re.fullmatch(r"[a-zA-Z0-9_-]+", value):
            return value
    match = re.search(r"news,[^,]+,(\d+)(?:\.html)?$", parsed.path)
    if not match:
        match = re.search(r"/(\d+)(?:\.html)?/?$", parsed.path)
    if match:
        return match[1]
    match = re.search(r"wm_aid=([0-9]+)", parsed.fragment)
    if match:
        return match[1]
    leaf = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    leaf = re.sub(r"\.(?:s?html?|php)$", "", leaf)
    if platform == "news18":
        leaf = re.sub(r"^storys_", "", leaf)
    return leaf if leaf not in {"", "index", "article", "latest", "detail"} else ""
