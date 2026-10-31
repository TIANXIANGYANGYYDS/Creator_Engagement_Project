import asyncio
import json

import pytest

from app.crawlers.engagement import EngagementCrawler
from tests.test_engagement_crawler import FakeClient, FakeResponse

ARTICLE = "2309405359760613965966"
MID = "5359760616458068"
URL = f"https://weibo.com/ttarticle/p/show?id={ARTICLE}"


def page(article=ARTICLE, mid=MID, views="294"):
    return (
        '<script type="application/ld+json">' + json.dumps({
            "@type": "Article", "headline": "target", "url":
            f"https://weibo.com/ttarticle/p/show?id={article}",
        }) + '</script><div class="WB_artical"><div node-type="articleTitle">target</div>'
        f'<div class="authorinfo"><span class="num">阅读数：{views}</span></div></div>'
        '<script>window.createApp("#commonts_container", {config: {uid: "0", '
        f'mid: "{mid}"' + '}});</script>'
    )


@pytest.mark.parametrize("views,expected", [("294", 294), ("0", 0), ("1.2万", 12000)])
def test_article_reads_are_bound_to_article_not_guessed_status(views, expected):
    client = FakeClient(FakeResponse(text=page(views=views)))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(URL, "微博"))
    assert result.coverage == "partial" and result.work_id == MID
    assert result.stats.model_dump(exclude_none=True) == {"views": expected}
    assert len(client.calls) == 1 and client.calls[0][0] == URL


@pytest.mark.parametrize("html", [page(article="2309400000000000"), page(mid=""),
                                  page(views=""), '<span class="num">阅读数：999</span>'])
def test_wrong_article_missing_mid_or_blank_counts_cannot_be_success(html):
    result = asyncio.run(EngagementCrawler(client=FakeClient(FakeResponse(text=html)))
                         .fetch_interactions(URL, "微博"))
    assert result.coverage == "failed"
    assert result.stats.model_dump(exclude_none=True) == {}


def test_article_comments_use_official_mid_and_existing_pagination():
    client = FakeClient(
        FakeResponse(text=page()),
        FakeResponse(text='var request_id = "abcdef1234567890";'),
        FakeResponse(text='callback({"retcode":20000000,"data":{"sub":"guest"}});'),
        FakeResponse(payload={"ok": 1, "data": {"max_id": 99, "data": [
            {"id": 1, "text": "root", "user": {"screen_name": "user"}},
        ]}}),
    )
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(URL, "微博", 1))
    assert result.coverage == "partial" and result.work_id == MID
    assert result.next_cursor == "99" and result.comments[0].text == "root"
    assert client.calls[-1][1]["params"]["id"] == MID
    assert client.calls[-1][1]["params"]["mid"] == MID


def test_empty_mobile_detail_has_explicit_error_instead_of_success_reason():
    client = FakeClient(FakeResponse(payload={"ok": 0, "msg": "not available"}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        "https://m.weibo.cn/detail/5359760616458068", "微博"))
    assert result.coverage == "failed" and "未返回有效互动数值" in result.reason


def test_article_template_without_jsonld_uses_exact_follow_binding():
    html = page().split('</script>', 1)[1].replace('</div></div>',
        f'<a action-type="follow" action-data="refer_flag=2309400002_{ARTICLE}"></a></div></div>')
    result = asyncio.run(EngagementCrawler(client=FakeClient(FakeResponse(text=html)))
                         .fetch_interactions(URL, "微博"))
    assert result.stats.views == 294 and result.work_id == MID


def test_conflicting_article_bindings_are_rejected():
    html = page().replace('</div></div>', '<a action-type="follow" '
        'action-data="refer_flag=2309400002_2309400000000000"></a></div></div>')
    result = asyncio.run(EngagementCrawler(client=FakeClient(FakeResponse(text=html)))
                         .fetch_interactions(URL, "微博"))
    assert result.coverage == "failed" and result.stats.views is None


def test_error_codes_quoted_in_article_content_do_not_mark_article_deleted():
    client = FakeClient(FakeResponse(text=page() + '<p>错误码:100008，错误码:120003</p>'))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(URL, "微博"))
    assert result.coverage == "partial" and result.stats.views == 294


@pytest.mark.parametrize('code', ['120003', '110133', '100008'])
def test_article_without_viewing_permission_is_not_retried(code):
    client = FakeClient(FakeResponse(text='抱歉，您没有这篇文章的查看权限 错误码:' + code))
    result = asyncio.run(EngagementCrawler(client=client, max_protocol_attempts=3)
                         .fetch_interactions(URL, "微博"))
    assert result.coverage == "unsupported"
    assert len(client.calls) == 1
