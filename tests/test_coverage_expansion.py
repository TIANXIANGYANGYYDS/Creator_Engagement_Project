import asyncio
import json

import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.http_client import PlatformCrawlerError
from app.crawlers.platforms import zhihu
from app.crawlers.platforms.regional_news import ding_headers
from app.crawlers.platforms.web_sources_common import nuxt_literal_state
from app.crawlers.platforms.registry import validate_media_url
from tests.test_publisher_engagement import Client as GetClient, Response


class Client(GetClient):
    async def post(self, url, **kwargs):
        return await self.get(url, **kwargs)


def next_page(state):
    return Response(text='<script id="__NEXT_DATA__" type="application/json">' + json.dumps(state) + '</script>')


def test_jianshu_ignores_recommended_article_and_author_counts():
    state = {'props': {'initialState': {
        'note': {'data': {'slug': 'ba77b670610b', 'views_count': 25, 'likes_count': 0, 'comments_count': 0,
                          'user': {'likes_count': 9999}}},
        'global': {'underRecomment': [{'note': {'views_count': 233041, 'likes_count': 545}}]}}}}
    result = asyncio.run(EngagementCrawler(client=Client(next_page(state))).fetch_interactions(
        'https://www.jianshu.com/p/ba77b670610b', '简书'))
    assert result.coverage == 'partial'
    assert (result.stats.views, result.stats.likes, result.stats.comments) == (25, 0, 0)
    state['props']['initialState']['note']['data']['slug'] = 'other'
    result = asyncio.run(EngagementCrawler(client=Client(next_page(state))).fetch_interactions(
        'https://www.jianshu.com/p/ba77b670610b', '简书'))
    assert result.coverage == 'failed'


@pytest.mark.parametrize('total,coverage', [(0, 'partial'), (None, 'unsupported'), (5, 'unsupported')])
def test_unverified_comment_preview_does_not_report_empty_success(total, coverage):
    state = {'props': {'pageProps': {'post': {'id': '6ae352e138389f5993f6886f', 'likeCount': 1,
                                            'commentCount': total}}}}
    result = asyncio.run(EngagementCrawler(client=Client(next_page(state))).fetch_comments(
        'https://m.okjike.com/originalPosts/6ae352e138389f5993f6886f', '即刻', 1))
    assert result.coverage == coverage


def acfun_page(wid='48918800'):
    return Response(text='window.articleInfo=' + json.dumps({'articleId': wid, 'viewCount': 8774,
        'commentCount': 57, 'likeCount': 12, 'user': {'fanCountValue': 90000}}))


def test_acfun_root_comments_exclude_replies_and_preserve_reply_count():
    payload = {'result': 0, 'sourceType': 3, 'curPage': 1, 'totalPage': 2, 'commentCount': 57,
        'rootComments': [{'commentId': 123, 'sourceId': 48918800, 'userName': '访客',
            'content': '一级正文', 'subCommentCount': 3, 'likeCount': 4, 'timestamp': 1792280357130}],
        'subCommentsMap': {'123': [{'commentId': 124, 'content': '不采集回复正文'}]}}
    result = asyncio.run(EngagementCrawler(client=Client(acfun_page(), Response(payload))).fetch_comments(
        'https://www.acfun.cn/a/ac48918800', 'AcFun', 1))
    assert result.coverage == 'partial' and result.next_cursor == '2'
    assert [(x.comment_id, x.text, x.replies) for x in result.comments] == [('123', '一级正文', 3)]
    assert result.comments[0].created_at.year == 2026
    payload['rootComments'][0]['sourceId'] = 999
    result = asyncio.run(EngagementCrawler(client=Client(acfun_page(), Response(payload))).fetch_comments(
        'https://www.acfun.cn/a/ac48918800', 'AcFun', 1))
    assert result.coverage == 'failed'


def test_zhihu_requires_target_entity_and_does_not_use_author_counts():
    state = {'initialState': {'entities': {'articles': {'123': {'id': '123', 'type': 'article',
        'voteupCount': 2, 'commentCount': 3, 'author': {'voteupCount': 999}}}}}}
    html = '<script id="js-initialData">' + json.dumps(state) + '</script>'
    stats = zhihu.parse_article(html, 'article', '123')
    assert stats.views is None and stats.likes == 2 and stats.comments == 3
    with pytest.raises(PlatformCrawlerError):
        zhihu.parse_article(html, 'article', '999')


@pytest.mark.parametrize('next_url', [
    'https://evil.test/api/v4/comment_v5/articles/123/root_comment?offset=next',
    'https://www.zhihu.com/api/v4/comment_v5/articles/999/root_comment?offset=next',
    'https://www.zhihu.com/api/v4/comment_v5/articles/123/root_comment?offset=old',
])
def test_zhihu_rejects_foreign_or_stalled_comment_cursor(next_url):
    payload = {'counts': {'total_counts': 2}, 'data': [{'id': 1, 'content': 'text'}],
               'paging': {'is_end': False, 'next': next_url}}
    with pytest.raises(PlatformCrawlerError):
        zhihu.parse_comments(payload, '/api/v4/comment_v5/articles/123/root_comment', 'old')


def test_zhihu_filters_child_bodies_and_accepts_explicit_zero():
    payload = {'counts': {'total_counts': 2}, 'paging': {'is_end': True}, 'data': [
        {'id': 1, 'content': '<p>正文</p>', 'child_comment_count': 1, 'author': {'name': '读者'}},
        {'id': 2, 'reply_comment_id': '1', 'content': '回复'}]}
    total, rows, following = zhihu.parse_comments(payload, '/path', '')
    assert total == 2 and len(rows) == 1 and rows[0].replies == 1 and following is None
    payload['data'] = []
    with pytest.raises(PlatformCrawlerError):
        zhihu.parse_comments(payload, '/path', '')
    payload['counts']['total_counts'] = 0
    assert zhihu.parse_comments(payload, '/path', '') == (0, [], None)


def test_ding_sign_uses_sorted_header_fields_and_millisecond_timestamp():
    # Test-only app configuration, never a session or production credential.
    import hashlib
    headers = ding_headers('test-app', 'public-test-config', 1793469879352)
    expected = hashlib.md5(b'app-id=test-appauthorization=timestamp=1793469879352public-test-config').hexdigest()
    assert headers['sign'] == expected and headers['authorization'] == ''


@pytest.mark.parametrize('media,url', [
    ('知乎', 'https://www.zhihu.com.evil.test/p/123'),
    ('AcFun', 'https://user:pass@www.acfun.cn/a/ac123'),
    ('上观', 'https://www.shobserver.cn:8443/staticsg/res/html/web/newsDetail.html?id=123'),
])
def test_added_collectors_reject_foreign_or_credentialed_urls(media, url):
    with pytest.raises(ValueError):
        validate_media_url(url, media)


def test_sina_community_uses_page_declared_thread_and_pages_root_comments():
    target, thread = '123', '456'
    state = {'data': {'base': {'base': {'dataid': 'tiezi:' + target}}, 'weibo': {'info': {
        'interactionInfo': {'likeInfo': {'id': 'tiezi:' + target, 'count': '38'},
            'comment': {'commentId': thread + '_0_qcmnt', 'commentCount': '21'}}}}}}
    payload = {'result': {'status': {'code': 0}, 'count': {'thread_show': 21}, 'cmntlist': [
        {'mid': 'root1', 'newsid': thread, 'channel': 'qcmnt', 'layer': '1', 'parent_mid': '0',
         'nick': '读者', 'content': '公开评论', 'content_ext': {'reply': 2}}]}}
    client = Client(Response(text='window.SM=' + json.dumps(state)), Response(payload))
    crawler = EngagementCrawler(client=client)
    result = asyncio.run(crawler.fetch_comments('https://super.sina.cn/shequn/post/detail_123.html', '新浪新闻', 1))
    assert result.coverage == 'partial' and result.next_cursor == '2'
    assert result.comments[0].replies == 2
    assert result.total_comments == 21
    assert client.calls[1][1]['params']['newsid'] == thread


def test_paper_reply_preview_and_fold_setting_are_not_reply_total():
    page = next_page({'props': {'pageProps': {'detailData': {'contentDetail': {'contId': 123}}}}})
    data = {'pageNum': 1, 'nextPageNum': 2, 'hasNext': True, 'total': 4, 'list': [
        {'commentId': 1, 'objectId': 123, 'parentId': 0, 'content': '一级评论', 'foldNum': 3,
         'originPraiseTimes': 2, 'userInfo': {'sname': '读者'},
         'commentReply': [{'commentId': 2, 'content': '回复正文不采集'}]}]}
    client = Client(page, Response({'code': 200, 'data': {'praiseTimes': 23}}), Response({'code': 200, 'data': data}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://www.thepaper.cn/newsDetail_forward_123', '澎湃新闻', 1))
    assert result.coverage == 'partial' and result.next_cursor == '2'
    assert len(result.comments) == 1 and result.comments[0].replies is None


def test_d1ev_requires_article_id_and_ignores_nested_replies():
    page = Response(text='<body data-newid="123"><div class="likeCount"><span>11</span></div></body>')
    payload = {'article': {'targetId': '123'}, 'total_number': '1', 'pagination': '', 'comment': [
        {'id': '20', 'level': '1', 'nickName': '读者', 'content': '正文', 'likeNum': 2,
         'reply': [{'id': '21', 'content': '回复'}]}]}
    # Upstream prefixes JSON with BOM/newline.
    client = Client(page, Response(text='\ufeff\n' + json.dumps(payload)))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://www.d1ev.com/news/shichang/123', '第一电动网', 1))
    assert result.coverage == 'partial' and len(result.comments) == 1
    assert result.comments[0].text == '正文' and result.comments[0].likes == 2
    payload['article']['targetId'] = '999'
    client = Client(page, Response(text=json.dumps(payload)))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://www.d1ev.com/news/shichang/123', '第一电动网', 1))
    assert result.coverage == 'failed'


def test_ijiwei_initial_request_omits_invalid_empty_cursor():
    page = Response(text='var simpledata=' + json.dumps({'news_id': 123, 'comment_num': 1, 'view_num': 17}))
    payload = {'errno': 0, 'data': {'total_num': 1, 'list': [{'comment_id': 10, 'content': '正文',
        'user_info': {'nickname': '读者'}, 'like_num': 3, 'comment_num': 2}]}}
    client = Client(page, Response(payload))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://ijiwei.com/n/123', '爱集微', 1))
    assert result.coverage == 'partial' and result.comments[0].text == '正文'
    assert result.comments[0].replies == 2 and result.comments[0].author == '读者'
    assert 'after_id' not in client.calls[1][1]['data']


def test_growthhk_uses_article_badge_not_author_comment_count():
    html = '''<link rel="canonical" href="https://www.growthhk.cn/cgo/model/123.html">
        <div class="entry-info">阅读 29339</div>
        <div class="entry-action"><div class="btn-zan" data-id="123"><span class="entry-action-num">(2)</span></div></div>
        <a class="meta-item" href="#comments"><span class="data">0</span></a>
        <div class="author">总计 90000 评论</div>'''
    result = asyncio.run(EngagementCrawler(client=Client(Response(text=html))).fetch_interactions(
        'https://www.growthhk.cn/cgo/model/123.html', '增长黑客'))
    assert (result.stats.views, result.stats.likes, result.stats.comments) == (29339, 2, 0)


def test_discuz_login_placeholder_never_becomes_comment_body():
    html = '''<link rel="canonical" href="https://www.mydigit.cn/thread-123-1-1.html">
        <div id="thread_subject">标题</div><div id="postlist"><table><tr><td><div class="hm">
        <span class="xi1">10</span><span class="xi1">1</span></div></td></tr></table></div>
        <div id="post_456"><a id="postnum456"><em>2</em></a>
        <div id="postmessage_456"><div class="locked">请登录查看</div></div></div>'''
    result = asyncio.run(EngagementCrawler(client=Client(Response(text=html))).fetch_comments(
        'https://www.mydigit.cn/thread-123-1-1.html', '数码之家', 1))
    assert result.coverage == 'blocked' and not result.comments


def test_nuxt_literal_parser_resolves_bindings_without_running_page_code():
    html = '<script>window.__NUXT__=(function(a,b){return {data:[{newsData:{newsId:"123",viewCount:a,commentcount:b}}]}}(17,"0"));</script>'
    assert nuxt_literal_state(html)['data'][0]['newsData'] == {'newsId': '123', 'viewCount': 17, 'commentcount': '0'}
    with pytest.raises(PlatformCrawlerError):
        nuxt_literal_state(html.replace('viewCount:a', 'viewCount:fetch("https://example.test")'))
    with pytest.raises(PlatformCrawlerError):
        nuxt_literal_state(html.replace('viewCount:a', 'viewCount:unknown'))


@pytest.mark.parametrize('media,url,expected', [
    ('上观', 'https://www.jfdaily.com/staticsg/res/html/web/newsDetail.html?id=123', '123'),
    ('爱集微', 'https://www.laoyaoba.com/html/share/news?news_id=123', '123'),
    ('同顺号', 'https://t.10jqka.com.cn/lgt/article_detail/index.html?pid=123', 'post:123'),
    ('同花顺', 'https://stock.10jqka.com.cn/hks/20261010/c123.shtml', 'news:123'),
    ('OFweek维科网', 'https://chuneng.ofweek.com/news/2026-10/ART-180226-8420-123.html', '123'),
    ('42 号车库', 'https://www.42how.com/article/123', '123'),
    ('壹点', 'https://m.ql1d.com/new/general/123', '123'),
])
def test_exported_alias_url_forms(media, url, expected):
    assert validate_media_url(url, media)[1] == expected


def test_chemm_uses_displayed_magnitude_not_internal_hit_counter():
    state = {'newsDetail': {'articleDetailState': {'article': {
        'id': '123', 'post_hits': '1.4w', 'post_hits_real': '165', 'post_like': 260, 'comment_count': 0}}}}
    result = asyncio.run(EngagementCrawler(client=Client(Response(text='window.__INITIAL_STATE__=' + json.dumps(state)))).fetch_interactions(
        'https://www.chemm.com/newsDetail?article_id=123', '玩车教授'))
    assert result.stats.views == 14000 and result.stats.likes == 260


def test_guanhai_does_not_expose_unused_analytics_pv_as_read_count():
    payload = {'state': True, 'data': {'contentid': 123, 'pv': 0, 'digg': 1280, 'share': 0}}
    result = asyncio.run(EngagementCrawler(client=Client(Response(text='article_id: "123"'), Response(payload))).fetch_interactions(
        'https://m.guanhai.com.cn/p/123.html', '观海新闻'))
    assert result.stats.likes == 1280 and result.stats.views is None and result.stats.shares is None
    result = asyncio.run(EngagementCrawler(client=Client(Response(text='article_id: "999"'), Response(payload))).fetch_interactions(
        'https://m.guanhai.com.cn/p/123.html', '观海新闻'))
    assert result.coverage == 'failed'


def test_ichaoqi_uses_article_metadata_not_popular_sidebar():
    html = '''<meta property="og:url" content="https://www.ichaoqi.com/xinchao/2026/1030/123.html">
      <div class="entry-meta contpost-meta">0 评论 /2.8w阅读</div>
      <aside>20000 评论 /999万阅读</aside>'''
    result = asyncio.run(EngagementCrawler(client=Client(Response(text=html))).fetch_interactions(
        'https://www.ichaoqi.com/xinchao/2026/1030/123.html', '潮起网'))
    assert result.stats.views == 28000 and result.stats.comments == 0


def test_yzwb_maps_target_counters_and_ignores_recommendations():
    payload = {'status': 0, 'data': {'news_id': 123, 'review_count': 767, 'praise_count': 0,
        'reply_count': 0, 'thousand_face_list': [{'news_id': 999, 'review_count': 99999}]}}
    result = asyncio.run(EngagementCrawler(client=Client(Response(payload))).fetch_interactions(
        'https://wap.yzwb.net/wap/news/123.html', '紫牛新闻'))
    assert result.coverage == 'partial'
    assert (result.stats.views, result.stats.likes, result.stats.comments) == (767, 0, 0)


def test_ofweek_empty_body_without_explicit_total_is_failure():
    client = Client(Response(text='var DetailID="123";'), Response({'ucount': 0}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://www.ofweek.com/auto/2026-10/ART-70109-8460-123.html', 'OFweek维科网', 1))
    assert result.coverage == 'failed' and result.total_comments is None


def test_zhihu_login_challenge_is_explicitly_blocked():
    from app.crawlers.http_client import PlatformBlockedError
    with pytest.raises(PlatformBlockedError, match='安全验证'):
        zhihu.require_public_page('https://www.zhihu.com/account/unhuman?need_login=true')
