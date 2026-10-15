import asyncio
import json
from urllib.parse import parse_qs

import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platforms import PLATFORM_HANDLERS
from app.crawlers.platforms.registry import validate_media_url
from app.crawlers.platforms.source_catalog import EXTENDED_SOURCES
from tests.test_publisher_engagement import Client, Response


class PostClient(Client):
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.parametrize("shape", ["flat", "nested"])
def test_eastmoney_real_zero_and_form_proxy(shape):
    post = {"post_id": 123, "post_click_count": 0, "post_comment_count": 0}
    data = {"post": post} if shape == "nested" else post
    client = PostClient(Response(text="var post_article=" + json.dumps(data)),
                        Response({"rc": 1, "re": [], "count": 0, "reply_total_count": 0}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://guba.eastmoney.com/news,0,123.html", "股吧", 1))
    assert result.coverage == "partial" and result.total_comments == 0
    assert result.comments == [] and result.next_cursor is None
    form = parse_qs(client.calls[1][1]["data"])
    assert parse_qs(form["param"][0])["postid"] == ["123"]
    assert client.calls[1][1]["headers"]["Content-Type"].startswith("application/x-www-form-urlencoded")


@pytest.mark.parametrize("reply", [
    {"rc": 0, "re": None, "count": 0},
    {"rc": 1, "re": None, "count": 0},
    {"rc": 1, "re": [], "count": 1},
    {"rc": 1, "re": [{"reply_id": 1, "source_post_id": 999}], "count": 1},
])
def test_eastmoney_errors_and_other_article_are_not_successful_empty(reply):
    client = PostClient(Response(text='var post_article={"post_id":123}'), Response(reply))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://guba.eastmoney.com/news,0,123.html", "股吧", 1))
    assert result.coverage == "failed" and result.comments == []


def test_eastmoney_news_mapping_must_match_original_news():
    client = PostClient(Response({"rc": 1, "re": [{"post_id": 123, "post_sourceid_id": "999"}]}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        "https://wap.eastmoney.com/a/202610103891912175.html", "东方财富"))
    assert result.coverage == "failed" and result.stats.views is None


def test_extended_url_validation_and_family_identity():
    assert validate_media_url("https://www.sohu.com/a/1085903440_129720", "搜狐网") == ("sohu_news", "1085903440")
    assert validate_media_url("https://v.163.com/static/1/VK7UO1435.html", "网易视频")[1] == "video:VK7UO1435"
    for url in ["https://guba.eastmoney.com.evil.test/news,0,123.html", "https://user@guba.eastmoney.com/news,0,123.html"]:
        with pytest.raises(ValueError):
            validate_media_url(url, "股吧")
    assert set(EXTENDED_SOURCES) <= set(PLATFORM_HANDLERS)


@pytest.mark.parametrize("index,accepted", [("niusxpp2253526", True), ("other", False)])
def test_sina_mobile_syndication_requires_explicit_page_binding(index, accepted):
    newsid = "7880068271-1d5b04caf06802jvyg"
    config = {"channel": "mp", "newsid": newsid, "product": "comos", "index": index}
    client = Client(Response(text="var commentConfig=" + json.dumps(config)), Response({"result": {
        "status": {"code": 0}, "news": {"newsid": newsid, "title": "目标文章"},
        "count": {"show": 0}, "cmntlist": []}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://auto.sina.cn/2026-10-10/detail-iniusxpp2253526.d.html", "新浪汽车", 1))
    assert (result.coverage == "partial") is accepted


def test_yiche_signature_matches_captured_public_web_request():
    from app.crawlers.platforms.yiche import signed_headers
    param, headers = signed_headers({"contentId": "113759730", "contentType": "21"},
                                    1792081872205, "visitor")
    assert param == '{"contentId":"113759730","contentType":"21"}'
    assert headers["x-sign"] == "e5078cad212ebf2735a72b2d1582271b"


@pytest.mark.parametrize("article_id,expected", [("123", "partial"), ("999", "failed")])
def test_cheshi_comments_are_bound_to_target_article(article_id, expected):
    client = PostClient(Response(text="var article_id = '123';"),
        Response({"status": 200, "data": {"comments": "1", "likes": 60, "views": 20050, "focus": 36}}),
        Response({"status": 200, "data": {"count": "1", "data": [{
            "id": "456", "aid": article_id, "content": "实测正文", "likes": "0",
            "user": {"username": "访客"}, "reply_count": 0}]}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://cheshihao.cheshi.com/news/123.html", "车市号", 1))
    assert result.coverage == expected
    if expected == "partial":
        assert result.comments[0].text == "实测正文" and result.comments[0].author == "访客"
        assert result.next_cursor is None


@pytest.mark.parametrize("total,expected", [(0, "partial"), (3, "failed")])
def test_ximalaya_empty_comments_require_actual_zero(total, expected):
    client = Client(Response({"ret": 0, "data": {"id": 123, "trackDetailInfo": {
        "trackInfo": {"id": 123, "title": "目标声音"}, "statCountInfo": {"playCount": 18}},
        "albumDetailInfo": {"statCountInfo": {"playCount": 999999}}}}),
        Response({"ret": 0, "data": {"trackId": 123, "totalCount": total, "comments": []}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://m.ximalaya.com/sound/123", "喜马拉雅", 1))
    assert result.coverage == expected


def test_pcauto_does_not_accept_other_article_comment_thread():
    client = Client(Response({"code": 200, "data": {"articleId": 123, "title": "目标", "pvCount": 18}}),
                    Response({"title": "另一篇", "total": 0, "data": []}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://hj.pcauto.com.cn/article/123.html", "太平洋号", 1))
    assert result.coverage == "failed"


def test_yidian_comment_cursor_advances_to_last_received_id():
    doc = {"doc": {"docid": "abc123", "title": "目标", "comment_count": 21}}
    client = Client(Response(text="window.yidian.docinfo=" + json.dumps(doc)),
        Response({"status": "success", "docid": "abc123", "total": 21, "comments": [
            {"comment_id": f"c{i}", "nickname": "访客", "comment": "正文", "createAt": 1792082927}
            for i in range(1, 21)]}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://www.yidianzixun.com/article/abc123", "一点资讯", 1))
    assert result.coverage == "partial" and result.next_cursor == "c20"
    assert result.comments[0].text == "正文"


def test_qctt_does_not_offer_unimplemented_pagination():
    from app.crawlers.comment_capabilities import COMMENT_CAPABILITIES
    client = Client(Response(text="<script>var newsId='123';</script>"
        '<span class="read_num">1.4万次阅读</span><ul class="pic-list2"><p class="no_commont">暂无评论</p></ul>'))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        "https://m.qctt.cn/news/123", "汽车头条", 2))
    assert result.coverage == "unsupported"
    assert COMMENT_CAPABILITIES["qctt"].root_comments == "first_public_page"
