"""Publisher regression contracts using shapes observed in public responses."""
import asyncio
import json
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.dependencies import get_engagement_service
from app.api.routers.engagement import MEDIA_NAMES_ZH
from app.crawlers.comment_capabilities import COMMENT_CAPABILITIES
from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platforms import PLATFORM_HANDLERS
from app.crawlers.platforms.baidu_publishers import page_metadata, parse_comments
from app.crawlers.platforms.bytedance_publishers import parse_stats
from app.crawlers.platforms.registry import validate_media_url
from app.crawlers.http_client import PlatformCrawlerError
from app.services.engagement_service import EngagementService

URLS = [
    ('sina', 'https://k.sina.cn/article_7879923055_1d5ae156f06801mpt6.html', '7879923055-1d5ae156f06801mpt6'),
    ('sina_finance', 'https://t.cj.sina.com.cn/articles/view/2131593523/7f0d893302001pch2', '2131593523-7f0d893302001pch2'),
    ('baijiahao', 'https://mbd.baidu.com/newspage/data/landingsuper?nid=news_9744266576771526728', 'news_9744266576771526728'),
    ('yoojia', 'https://www.yoojia.com/video/9843529770280862640.html', '9843529770280862640'),
    ('sohu', 'https://3g.k.sohu.com/t/n1066170357?serialId=a', '1066170357'),
    ('dongchedi', 'https://dcd.zjbyte.cn/i7695033858807546430', '7695033858807546430'),
    ('weitoutiao', 'https://www.toutiao.com/i1878634969430020/', '1878634969430020'),
    ('netease', 'https://c.m.163.com/news/v/VN80AO19S.html', 'video:VN80AO19S'),
    ('xueqiu', 'https://xueqiu.com/2107441426/411727508', '411727508'),
    ('autohome', 'https://chejiahao.m.autohome.com.cn/share/info/26626635', '26626635'),
    ('ixigua', 'https://www.ixigua.com/i7694645274110689839/', '7694645274110689839'),
    ('qq_stock', 'https://gu.qq.com/resources/shy/news/detail-v2/index.html?#/index?id=nesSN20261010233325b6503adc', 'nesSN20261010233325b6503adc'),
]


@pytest.mark.parametrize('platform,url,work_id', URLS)
def test_publisher_identity_and_public_contract(platform, url, work_id):
    assert validate_media_url(url, MEDIA_NAMES_ZH[platform]) == (platform, work_id)
    assert platform in PLATFORM_HANDLERS
    assert COMMENT_CAPABILITIES[platform].root_comments == 'paged_until_blocked'


def test_legacy_micro_url_does_not_reclassify_normal_toutiao():
    url = 'https://www.toutiao.com/i1234567890/'
    assert validate_media_url(url, '今日头条') == ('toutiao', '1234567890')
    assert validate_media_url(url, '今日头条微头条') == ('weitoutiao', '1234567890')
    with pytest.raises(ValueError):
        validate_media_url('https://www.toutiao.com/article/1234567890/', '今日头条微头条')


def test_sina_nested_target_host_and_article_identity():
    url = 'https://cj.sina.cn/article/norm_detail?url=' + quote('https://finance.sina.com.cn/roll/2026-10-10/doc-iniuufia0591948.shtml', safe='')
    assert validate_media_url(url, '新浪财经头条号')[1] == 'comos-niuufia0591948'
    with pytest.raises(ValueError):
        validate_media_url('https://cj.sina.cn/article/norm_detail?url=https://example.com/a', '新浪财经头条号')
    with pytest.raises(ValueError):
        validate_media_url('https://xueqiu.com.evil.example/123/456', '雪球')


def test_non_content_urls_are_not_assigned_fake_work_ids():
    for url, media in [('https://3g.k.sohu.com/h5apps/t/feed?uid=123', '搜狐号'),
                       ('https://car.autohome.com.cn/price/series-123-0-3.html', '汽车之家')]:
        assert validate_media_url(url, media)[1] == ''


def test_baidu_counts_are_target_owned_and_missing_is_not_zero():
    state = {'bsData': {'nid':'123', 'comment':{'tid':'456'}, 'like':{'count':'0'}, 'commentNum':'2'},
             'recommend': {'like':{'count':999999}}}
    html = 'window.jsonData = ' + json.dumps(state) + ';'
    tid, stats, _ = page_metadata(html, 'baijiahao', 'news_123')
    assert tid == '456' and stats.likes == 0 and stats.comments == 2
    assert stats.views is None
    with pytest.raises(PlatformCrawlerError, match='不是目标'):
        page_metadata(html, 'baijiahao', 'news_999')
    c = parse_comments([{'reply_id':'1133485551121922902','uname':'访客','content':'<b>正文</b>',
                         'like_count':'1','reply_count':'0','create_time':'1791644523'}])[0]
    assert c.comment_id == '1133485551121922902' and c.text == '正文' and c.likes == 1


def test_yoojia_video_uses_video_state_not_empty_article_defaults():
    state = {'route':{'params':{'nid':'123'}}, 'articleDetail':{'article':{}},
             'videoDetail':{'videoData':{'title':'视频','comment_thread_id':'456'},'isDelete':True}}
    tid, stats, deleted = page_metadata('window.__INITIAL_STATE__=' + json.dumps(state), 'yoojia', '123')
    assert tid == '456' and deleted and stats.likes is None


def test_micro_repost_does_not_take_original_post_counters():
    data = {'data':{'groupId':'123', 'likeData':{'count':0}, 'originThread':{'itemCounter':{'readCount':9999}}}}
    html = '<script id="RENDER_DATA">' + quote(json.dumps(data)) + '</script>'
    stats = parse_stats(html, 'weitoutiao', '123')
    assert stats.likes == 0 and stats.views is None
    with pytest.raises(PlatformCrawlerError):
        parse_stats(html, 'weitoutiao', '999')


class Response:
    def __init__(self, payload=None, text=None, status_code=200):
        self.payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.status_code = status_code
    def json(self):
        return self.payload


class Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


@pytest.mark.parametrize('mobile', [True, False])
@pytest.mark.parametrize('target_matches', [True, False])
def test_sina_finance_comment_metadata_is_bound_to_requested_article(mobile, target_matches):
    work_id = 'comos-niutqmk0639425'
    newsid = work_id if target_matches else 'comos-other'
    html = ('var commentConfig=' + json.dumps({'channel': 'cj', 'newsid': newsid}) + ';'
            if mobile else f'<meta name="sudameta" content="comment_channel:cj;comment_id:{newsid}">')
    client = Client(Response(text=html), Response({'result': {
        'status': {'code': 0}, 'news': {'newsid': newsid, 'title': '目标文章'},
        'count': {'show': 1}, 'cmntlist': [{'mid': '123', 'nick': '访客', 'content': '正文'}]}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://finance.sina.cn/stock/jdts/2026-10-10/detail-iniutqmk0639425.d.html',
        '新浪财经头条号', 1))
    if target_matches:
        assert result.coverage == 'partial' and result.total_comments == 1
        assert result.comments[0].text == '正文'
        assert client.calls[-1][1]['params']['newsid'] == work_id
        assert client.calls[-1][1]['params']['channel'] == 'cj'
    else:
        assert result.coverage == 'failed' and '不是目标' in result.reason
        assert len(client.calls) == 1


@pytest.mark.parametrize('status', [200, 404])
def test_yoojia_retries_transient_page_500_then_validates_metadata(status):
    state = {'route': {'params': {'nid': '123'}}, 'articleDetail': {
        'article': {'title': '目标', 'comment_thread_id': '456'}, 'isDelete': status == 404}}
    client = Client(Response(text='server error', status_code=500),
        Response(text='window.__INITIAL_STATE__=' + json.dumps(state), status_code=status),
        Response({'errno': 0, 'ret': {'thread_id': '456', 'comment_count': 1,
            'is_over': True, 'list': [{'reply_id': '789', 'content': '正文'}]}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://www.yoojia.com/article/123.html', '有驾', 1))
    assert result.coverage == 'partial' and result.comments[0].text == '正文'
    assert client.calls[0][0] == client.calls[1][0]
    assert len(client.calls) == 3


@pytest.mark.parametrize('statuses,coverage', [([500, 500], 'failed'), ([403], 'blocked')])
def test_yoojia_page_retry_is_bounded_and_does_not_retry_blocks(statuses, coverage):
    client = Client(*(Response(text='unavailable', status_code=status) for status in statuses))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://www.yoojia.com/article/123.html', '有驾', 1))
    assert result.coverage == coverage and not result.comments
    assert len(client.calls) == len(statuses)


def test_dongchedi_api_uses_opaque_returned_offset_and_exact_large_id():
    gid = '7695033858807546430'
    client = Client(Response({'status':0,'data':{'title':'目标','url':f'https://toutiao.com/group/{gid}/','is_visible':True}}),
        Response({'err_no':0,'data':[{'comment':{'id_str':'7695220828841132855','text':'评论','digg_count':1}}],
                  'total_number':100,'has_more':True,'offset':43,'repost_params':{'fw_id':int(gid)}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(f'https://www.dongchedi.com/article/{gid}', '懂车帝', 2, cursor='23'))
    assert client.calls[-1][1]['params']['offset'] == 23
    assert result.next_cursor == '43' and result.comments[0].comment_id == '7695220828841132855'


def test_dongchedi_stalled_page_cannot_be_successful_empty_comments():
    gid = '123'
    client = Client(Response({'status':0,'data':{'title':'目标','url':f'https://toutiao.com/group/{gid}/'}}),
                    Response({'err_no':0,'data':[],'total_number':12,'has_more':True,'offset':0}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://www.dongchedi.com/article/123', '懂车帝', 1))
    assert result.coverage == 'failed' and '未前进' in result.reason


@pytest.mark.parametrize('change,accepted', [
    ({}, True),
    ({'binding': '7694845024564413000'}, False),
    ({'binding': 7694845024564423000}, False),
    ({'title': '另一篇文章'}, False),
    ({'author': '999'}, False),
    ({'group_author': '999'}, False),
    ({'detail_url': 'sslocal://thread_detail?fid=6564242300&tid=7694845024564412953'}, True),
    ({'detail_url': 'http://toutiao.com/item/7694845024564412953/'}, True),
    ({'detail_url': 'sslocal://thread_detail?tid=17694845024564412953'}, False),
    ({'detail_url': 'sslocal://thread_detail?tid=7694845024564412953&tid=999'}, False),
    ({'detail_url': 'https://toutiao.com/group/17694845024564412953/'}, False),
    ({'detail_url': 'https://example.com/group/7694845024564412953/'}, False),
])
def test_dongchedi_rounded_proxy_id_requires_independent_target_evidence(change, accepted):
    gid = '7694845024564412953'
    client = Client(Response({'status': 0, 'data': {
        'title': '目标文章', 'url': change.get('detail_url', f'https://toutiao.com/group/{gid}/'),
        'media_user': {'id': '51719180939'}}}),
        Response({'err_no': 0, 'data': [{'comment': {'id_str': '7695118148151886654', 'text': '真实评论'}}],
            'total_number': 1, 'has_more': False, 'offset': 1,
            'group': {'user_id': change.get('group_author', '51719180939')},
            'repost_params': {'fw_id': change.get('binding', 7694845024564413000),
                'fw_user_id': change.get('author', '51719180939'), 'title': change.get('title', '目标文章')}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        f'https://www.dongchedi.com/article/{gid}', '懂车帝', 1))
    assert (result.coverage == 'partial') is accepted
    if accepted:
        assert result.comments[0].comment_id == '7695118148151886654'
        assert result.comments[0].text == '真实评论'
    else:
        assert result.comments == []


def test_xueqiu_does_not_extract_reposted_status_counters():
    client = Client(Response(text='<html/>'), Response({'id':123,'like_count':0,'reply_count':2,
                      'retweeted_status':{'like_count':999}}),
                    Response({'count':2,'comments':[{'id':456,'text':'评论','user':{'screen_name':'访客'},'created_at':1791553321000}]}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://xueqiu.com/1/123', '雪球', 1))
    assert result.total_comments == 2 and result.comments[0].author == '访客'
    assert all('www.xueqiu.com' in url for url,_ in client.calls)


def test_autohome_native_comment_schema_and_page_past_end():
    def responses():
        return [Response(text='var ReplyConfig = {appid:21,id:123,'),
            Response({'returncode':0,'result':{'hasmore':False,'next':0,'list':[
                {'ReplyId':456,'RObjId':123,'RMemberName':'访客','RContent':'正文','RUp':3,'FReplyCount':0,'RReplyDate':'/Date(1791699389640+0800)/'}]}}),
            Response(text='collector({\'commentlist\':[{"objid":123,"replycountall":1}]})')]
    client = Client(*responses())
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://chejiahao.m.autohome.com.cn/share/info/123', '汽车之家', 1))
    assert result.comments[0].likes == 3 and result.comments[0].created_at is not None
    assert result.total_comments == 1
    past_end = asyncio.run(EngagementCrawler(client=Client(*responses())).fetch_comments('https://chejiahao.m.autohome.com.cn/share/info/123', '汽车之家', 5))
    assert past_end.comments == [] and past_end.next_page is None


def test_autohome_mobile_failure_uses_verified_desktop_article_namespace():
    state = {'props': {'pageProps': {'article': {
        'idString': '123', 'articleContent': {'title': '目标', 'recycled': False},
        'recommend': {'replyCount': 9999}}}}}
    client = Client(Response(text='access denied', status_code=567),
        Response(text='<script id="__NEXT_DATA__">' + json.dumps(state) + '</script>'),
        Response({'returncode': 0, 'result': {'hasmore': False, 'next': 0,
            'list': [{'ReplyId': 456, 'RContent': '目标评论'}]}}),
        Response(text='collector({"commentlist":[{"objid":123,"replycountall":1}]})'))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://chejiahao.m.autohome.com.cn/share/info/123', '汽车之家', 1))
    assert result.coverage == 'partial' and result.comments[0].text == '目标评论'
    assert client.calls[1][0] == 'https://chejiahao.autohome.com.cn/info/123'
    assert client.calls[2][1]['params']['appid'] == '21'
    assert result.total_comments == 1


def test_autohome_desktop_rejects_wrong_target_and_deleted_article():
    from app.crawlers.platforms.autohome import page_metadata
    for target, message in [({'idString': '999', 'articleContent': {'title': '其他'}}, '不是目标'),
                            ({'idString': '123', 'articleContent': {'recycled': True}}, '已删除')]:
        state = {'props': {'pageProps': {'article': target}}}
        with pytest.raises(PlatformCrawlerError, match=message):
            page_metadata('<script id="__NEXT_DATA__">' + json.dumps(state) + '</script>',
                          '123', chejiahao=True)


def test_autohome_news_and_dealer_do_not_use_car_home_namespace():
    from app.crawlers.platforms.autohome import page_metadata
    state = {'props': {'pageProps': {'idString': '123', 'articleContent': {'clickCount': 20}}}}
    appid, stats = page_metadata('<script id="__NEXT_DATA__">' + json.dumps(state) + '</script>', '123')
    assert appid == '1' and stats.views == 20
    assert page_metadata('<h1>经销商促销</h1>', '123') is None


def test_sohu_client_comment_author_time_and_likes():
    client = Client(Response(text='var CONFIG = {newsId:123};'), Response({'isSuccess':'S','response':{
        'newsId':'123','allCount':1,'totalCount':1,'cursorId':0,'commentList':[
            {'commentId':456,'author':'访客','content':'正文','ctime':1791626006000,'digNum':2,'replyCount':0}]}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://3g.k.sohu.com/t/n123?serialId=a', '搜狐号', 1))
    assert result.comments[0].author == '访客' and result.comments[0].likes == 2
    assert result.comments[0].created_at is not None


def test_public_api_accepts_new_platform_and_surfaces_real_failure():
    client = Client(Response({'result':{'status':{'code':0},'news':{'newsid':'123-abc','title':'目标'},'count':{'show':2},
                'cmntlist':[{'mid':'456','nick':'访客','content':'正文','agree':1}]}}), Response({'result':{}}))
    service = EngagementService(EngagementCrawler(client=client), cache_ttl_seconds=0, failure_cache_ttl_seconds=0)
    app = create_app()
    app.dependency_overrides[get_engagement_service] = lambda:service
    http = TestClient(app)
    params = {'url':'https://k.sina.cn/article_123_abc.html','media_name':'新浪看点','page':1}
    response = http.get('/api/v1/comments',params=params)
    assert response.status_code == 200 and response.json()['media_name'] == '新浪看点'
    assert response.json()['comments'][0]['text'] == '正文'
    assert http.get('/api/v1/comments',params=params).status_code == 502


def test_qq_signature_matches_captured_public_browser_request():
    from app.crawlers.platforms.qq_stock import detail_params, sign_detail_request
    params = detail_params('SN20261010233325b6503adc', timestamp=1791701238)
    params = {k:v for k,v in params.items() if not k.startswith('x-')}
    params.update({'_appName':'ios','_appver':'6.0.0','_osVer':'12.6','_devId':'1','_dev':'iphoneX'})
    assert sign_detail_request(params, timestamp=1791701238)['x-sa-sign'] == '9ba960bcb078bc852b1f4460e231b781'


def test_netease_video_resolves_its_comment_thread():
    client = Client(Response(text='<a href="https://comment.tie.163.com/N80AO19S050835RB.html">跟帖</a>'),
        Response({'docId':'N80AO19S050835RB','title':'目标视频','cmtCount':1}),
        Response({'commentIds':['123,456'],'comments':{'123':{'content':'引用'},'456':{'content':'正文','user':{'nickname':'访客'}}},'newListSize':1}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://c.m.163.com/news/v/VN80AO19S.html', '网易号', 1))
    assert result.coverage == 'partial' and result.comments[0].text == '正文'
    assert 'N80AO19S050835RB/comments/newList' in client.calls[-1][0]


def test_qq_unknown_news_cannot_succeed_with_zero_counters():
    client = Client(Response({'data':{'queryNewsInfo':{'code':0,'news_info':{'news_id':'OTHER','title':'其他新闻'}}}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_interactions('https://gu.qq.com/resources/shy/news/detail-v2/index.html#/index?id=SN20261010233325b6503adc', '腾讯自选股'))
    assert result.coverage == 'failed' and result.stats.likes is None


def test_micro_unknown_post_echoing_id_and_zero_is_not_success():
    client = Client(Response({'err_no':0,'data':[],'has_more':False,'total_number':0,
        'group':{'group_id':123,'user_id':0,'user_name':''}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://www.toutiao.com/w/123/', '今日头条微头条', 1))
    assert result.coverage == 'failed' and '零评论' in result.reason


def test_qq_discussion_joins_body_and_outer_counters_without_losing_zero():
    news_id = 'SN20261010201546987fec55'
    client = Client(Response({'data':{'queryNewsInfo':{'code':0,'news_info':{'news_id':news_id,'title':'目标'}}}}),
        Response({'code':0,'data':{'rss_list':[{'subject_id':'123','like_num':0,'user_name':'访客'}],
            'subject_dict':{'123':{'content':'正文'}},'userinfo_list':[],
            'comment_cnt':1,'more_flag':0,'min_id':'','news_min_id':''}}))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments('https://gu.qq.com/resources/shy/news/detail-v2/index.html#/index?id='+news_id, '腾讯自选股', 1))
    assert result.comments[0].text == '正文' and result.comments[0].likes == 0
