"""Regressions for verified exported URLs and target-scoped public counters."""
import asyncio
import json

import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platforms.registry import validate_media_url
from app.crawlers.platforms.tonghuashun import encode_news_id
from tests.test_coverage_expansion import Client
from tests.test_publisher_engagement import Response


@pytest.mark.parametrize('seq,encoded', [
    ('681350709', '254d054493557239a04'),
    ('681366668', 'ea25771408781960087'),
    ('681374694', '37a412844629018890b'),
    ('0', 'b4b1000000000000073'),
    ('99999999999', '62dd9785251635395e5'),
])
def test_ths_encoding_matches_official_js_vectors(seq, encoded):
    assert encode_news_id(seq) == encoded


def ths_news(*, seq='681350709', supported=False):
    return Response({'status_code': 0, 'data': {
        'newsInfo': {'seq': seq, 'encodedSeq': encode_news_id(seq),
                     'title': 'Target', 'supportComment': supported},
        'hotKey': 'seq_' + seq, 'token': 'test-public-page-token', 'comment': {'pid': seq}}})


def counter(key, value):
    return Response(text='cb(' + json.dumps({'errorcode': 0, 'result': {key: value}}) + ');')


def test_ths_news_uses_exact_article_counts_without_incrementing():
    client = Client(ths_news(), counter('seq_681350709', 15), counter('seq_681350709', 0))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        'https://news.10jqka.com.cn/m681350709/', '同花顺'))
    assert result.coverage == 'partial'
    assert (result.stats.views, result.stats.likes, result.stats.comments) == (15, 0, None)
    assert all('inc' not in (kwargs.get('params') or {}) for _, kwargs in client.calls)
    assert client.calls[1][1]['params']['token'] == 'test-public-page-token'


def test_ths_news_rejects_other_article_and_disabled_comments_are_not_zero():
    for response, coverage in [(ths_news(seq='681366668'), 'failed'), (ths_news(), 'unsupported')]:
        client = Client(response)
        result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
            'https://news.10jqka.com.cn/m681350709/', '同花顺', 1))
        assert result.coverage == coverage and result.total_comments is None
        assert len(client.calls) == 1


@pytest.mark.parametrize('total,filtered,coverage', [(0, False, 'partial'), (2, False, 'unsupported'),
                                                   (0, True, 'unsupported'), (None, False, 'failed')])
def test_ths_post_empty_comments_need_explicit_unfiltered_total(total, filtered, coverage):
    page = Response({'errorCode': 0, 'result': {'data': {'post': {
        'hotKey': 'sns_post_123', 'title': 'Target'}}}})
    init = Response({'status_code': 0, 'data': {'comment_disabled': False}})
    comments = Response({'status_code': 0, 'data': {'comment_count': total, 'comments': [], 'comment_filter': filtered}})
    client = Client(page, init, comments)
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://t.10jqka.com.cn/pid_123.shtml', '同顺号', 1))
    assert result.coverage == coverage
    if coverage == 'partial':
        assert result.total_comments == 0 and result.comments == []
    assert client.calls[-1][1]['params']['resource_id'] == '123'
    assert client.calls[-1][1]['params']['biz_type'] == 1


@pytest.mark.parametrize('media,path', [('风闻', 'main'), ('风闻社区', 'wap')])
def test_guancha_export_aliases_validate_domain_and_identity(media, path):
    url = f'https://user.guancha.cn/{path}/content?id=123'
    assert validate_media_url(url, media) == ('guancha', '123')
    with pytest.raises(ValueError):
        validate_media_url(url.replace('guancha.cn/', 'guancha.cn.evil.test/'), media)


@pytest.mark.parametrize('total,coverage', [(0, 'partial'), (None, 'failed'), (2, 'unsupported')])
def test_guancha_target_zero_is_distinct_from_unavailable_bodies(total, coverage):
    html = '''<div id="comments-container" data-id="123" data-type="2"></div>
        <a class="tools_praise" data-id="123" data-type="2"><span>点赞</span><span>0</span></a>
        <div class="author">阅读 99999</div>'''
    payload = {'code': 0, 'status': 1, 'items': [], 'count': total}
    # A positive count with an empty first page is a protocol inconsistency.
    if total == 2:
        payload['items'] = [{'id': 1}]
    result = asyncio.run(EngagementCrawler(client=Client(Response(text=html), Response(payload))).fetch_comments(
        'https://user.guancha.cn/wap/content?id=123', '风闻社区', 1))
    assert result.coverage == coverage
    if coverage == 'partial':
        assert result.total_comments == 0 and not result.comments
    client = Client(Response(text=html.replace('data-id="123"', 'data-id="999"')))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        'https://user.guancha.cn/main/content?id=123', '风闻'))
    assert result.coverage == 'failed' and len(client.calls) == 1


def test_how42_does_not_treat_view_count_as_readership():
    html = '<script>window.__NUXT__=(function(a,b){b.id=999;return {data:[{articleInfo:{id:123,likeCount:a,viewCount:8,pv:16000},related:[b]}]}}(0,{}));</script>'
    payload = Response({'code': '00000', 'data': {'count': 0, 'list': []}})
    client = Client(Response(text=html), payload)
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions(
        'https://www.42how.com/article/123', '42号车库'))
    assert result.coverage == 'partial'
    assert (result.stats.views, result.stats.likes, result.stats.comments) == (None, 0, 0)
    assert client.calls[-1][1]['params']['id'] == '123'
    client = Client(Response(text=html), payload)
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://www.42how.com/article/123', '42号车库', 1))
    assert result.coverage == 'partial' and result.total_comments == 0


def xq_page(cid, *, target=123, next_id=-1):
    return Response({'status_reply_count': 3, 'comment_tl_count': 2, 'next_max_id': next_id,
        'type': 2, 'has_filtered': False, 'comments': [{
            'id': cid, 'root_in_reply_to_status_id': target, 'root_reply_to_cid': -1,
            'text': '<p>Root</p>', 'reply_count': 1, 'user': {'screen_name': 'Guest'},
            'child_comments': [{'id': 999, 'text': 'Child body is not collected'}]}]})


def test_xueqiu_v3_keeps_resolved_sort_and_cursor_and_excludes_child_bodies():
    client = Client(Response(text='<html/>'), Response({'id': 123, 'reply_count': 3}),
                    xq_page(50, next_id=50), xq_page(40))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://xueqiu.com/1/123', '雪球', 2))
    assert result.coverage == 'partial' and result.total_comments == 3
    assert [(x.comment_id, x.text, x.replies) for x in result.comments] == [('40', 'Root', 1)]
    assert result.next_cursor is None
    assert client.calls[2][1]['params'] == {'id': '123', 'size': 20, 'type': 4, 'max_id': -1}
    assert client.calls[3][1]['params']['type'] == 2
    assert client.calls[3][1]['params']['max_id'] == 50


def test_xueqiu_v3_resume_does_not_replay_or_switch_target():
    client = Client(Response(text='<html/>'), Response({'id': 123, 'reply_count': 3}), xq_page(40))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://xueqiu.com/1/123', '雪球', 2, cursor='xq:123:2:50'))
    assert result.coverage == 'partial' and len(client.calls) == 3
    assert client.calls[-1][1]['params']['max_id'] == 50
    client = Client(Response(text='<html/>'), Response({'id': 123, 'reply_count': 3}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://xueqiu.com/1/123', '雪球', 2, cursor='xq:999:2:50'))
    assert result.coverage == 'failed' and len(client.calls) == 2


@pytest.mark.parametrize('payload', [
    xq_page(50, target=999),
    Response({'status_reply_count': 3, 'comment_tl_count': 2, 'next_max_id': -1, 'type': 2, 'comments': []}),
    xq_page(50, next_id=50),
])
def test_xueqiu_v3_rejects_wrong_target_empty_positive_page_and_stalled_cursor(payload):
    client = Client(Response(text='<html/>'), Response({'id': 123, 'reply_count': 3}), payload)
    cursor = 'xq:123:2:50' if payload.payload.get('next_max_id') == 50 else None
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://xueqiu.com/1/123', '雪球', 1, cursor=cursor))
    assert result.coverage == 'failed' and not result.comments
