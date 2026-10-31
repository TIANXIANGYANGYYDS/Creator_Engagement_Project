import asyncio
import json

import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.platforms.registry import validate_media_url
from tests.test_publisher_engagement import Client, Response

QQ_ID = '20261010A081GT00'
QQ_URL = 'https://news.qq.com/rain/a/' + QQ_ID

def qq_page(wid=QQ_ID):
    return Response(text='window.DATA=' + json.dumps({'article_id':wid, 'title':'target',
        'comment_id':'thread', 'interationCount':{'like':16,'collect':4,'share':4}}))

def qq_comments(*, count=2, rows=None, more=0, cursor='{}'):
    if rows is None:
        rows = [[{'article_id':QQ_ID,'commentid':'thread','reply_id':'root','nick':'读者',
                  'reply_content':'一级评论','agree_count':1,'reply_num':1,
                  'reply_list':[[{'reply_id':'child','reply_content':'不采集回复正文'}]]}]]
    return Response({'ret':0,'bnext':more,'comments':{'count':str(count),'new':rows,'isClose':0},
                     'transparam':cursor})

def test_qq_exact_counters_roots_and_replies_count():
    crawler = EngagementCrawler(client=Client(qq_page(), qq_comments()))
    result = asyncio.run(crawler.fetch_comments(QQ_URL,'腾讯网',1))
    assert result.coverage == 'partial' and result.total_comments == 2
    assert [(x.comment_id,x.text,x.replies) for x in result.comments] == [('root','一级评论',1)]
    result = asyncio.run(EngagementCrawler(client=Client(qq_page(),qq_comments())).fetch_interactions(QQ_URL,'腾讯新闻企鹅号'))
    assert result.stats.views is None
    assert (result.stats.likes,result.stats.favorites,result.stats.shares) == (16,4,4)

@pytest.mark.parametrize('responses', [
    [qq_page('other')],
    [qq_page(),qq_comments(count=2,rows=[])],
    [qq_page(),qq_comments(rows=[[{'article_id':'other','commentid':'thread'}]])],
    [qq_page(),qq_comments(more=1,cursor='{}')],
])
def test_qq_wrong_target_missing_rows_or_stalled_cursor_is_failure(responses):
    result = asyncio.run(EngagementCrawler(client=Client(*responses)).fetch_comments(QQ_URL,'腾讯网',1))
    assert result.coverage == 'failed'

def test_qq_real_zero_and_pagination():
    result = asyncio.run(EngagementCrawler(client=Client(qq_page(),qq_comments(count=0,rows=[]))).fetch_comments(QQ_URL,'腾讯网',1))
    assert result.coverage == 'partial' and result.total_comments == 0
    client = Client(qq_page(), qq_comments(more=1,cursor='{"cursor":"root"}'), qq_comments(count=2,rows=[]))
    result = asyncio.run(EngagementCrawler(client=client).fetch_comments(QQ_URL,'腾讯网',2))
    assert result.coverage == 'partial' and result.comments == []
    assert client.calls[-1][1]['params']['transparam'] == '{"cursor":"root"}'

def zol_page():
    return Response(text='var temp_doc_root_id="12579869";var commentSrc="//comment.zol.com.cn/147/a12579869.html?from=1";')

def zol_comments(wid='12579869',total=1,body=True):
    return Response(text=f'''var tempDocId='{wid}'; var totalNum='{total}';
        <div id="list_box_new">''' + ('''<div class="comment-item"><div class="comment-box"><ul class="comment-list"><li class="commli">
        <div class="comment-user-bar"><span class="user-name">读者</span><span class="published-time">2026-10-10 13:15</span></div>
        <p class="comment-content">一级正文</p><div class="comment-vote" data-id="613995"><a class="ok-btn"><span></span></a></div>
        </li></ul></div></div>''' if body else '') + '</div>')

def test_zol_target_comment_and_absent_likes_remain_unknown():
    result = asyncio.run(EngagementCrawler(client=Client(zol_page(),zol_comments())).fetch_comments('https://auto.zol.com.cn/1257/12579869.html','中关村在线',1))
    assert result.coverage == 'partial' and result.total_comments == 1
    assert result.comments[0].comment_id == '613995'
    assert result.comments[0].likes is None

@pytest.mark.parametrize('reply',[zol_comments(wid='999'),zol_comments(body=False)])
def test_zol_other_article_or_missing_rows_are_not_empty_success(reply):
    result = asyncio.run(EngagementCrawler(client=Client(zol_page(),reply)).fetch_comments('https://auto.zol.com.cn/1257/12579869.html','中关村在线',1))
    assert result.coverage == 'failed'

@pytest.mark.parametrize('media,url',[('腾讯网','https://news.qq.com.evil.test/rain/a/'+QQ_ID),('中关村在线','https://evil.test/12579869.html')])
def test_new_source_host_allowlist(media,url):
    with pytest.raises(ValueError):validate_media_url(url,media)


def ithome_page():
    return Response(text='<div id="post_comm" data-id="page-token" data-nid="1020389"></div>')


def ithome_comments(*,wid=1020389,hint=None,rows=None):
    if rows is None:
        rows=[{'id':101,'parentCommentId':0,'support':3,'postTime':'2026-10-31T12:00:00',
               'userInfo':{'userNick':'读者'},'elements':[{'type':0,'content':'正文'}],
               'children':[{'id':102,'elements':[{'type':0,'content':'不采集回复'}]}],'expandCount':2}]
    return Response({'success':True,'content':{'newsId':wid,'endHint':hint,'comments':rows}})


def test_ithome_roots_reply_count_and_cursor():
    client=Client(ithome_page(),ithome_comments(),Response(text="ccd.innerHTML='4'"))
    result=asyncio.run(EngagementCrawler(client=client).fetch_comments('https://www.ithome.com/1/020/389.htm','IT之家',1))
    assert result.coverage=='partial' and result.total_comments==4
    assert [(r.comment_id,r.text,r.replies) for r in result.comments]==[('101','正文',3)]
    assert result.next_cursor=='101'


@pytest.mark.parametrize('response,coverage',[
    (ithome_comments(hint='文章发布时间太久，评论不再显示',rows=[]),'unsupported'),
    (ithome_comments(wid=999),'failed'),
    (ithome_comments(rows=[{'id':101,'parentCommentId':100}]),'failed'),
])
def test_ithome_archive_restrictions_and_identity(response,coverage):
    result=asyncio.run(EngagementCrawler(client=Client(ithome_page(),response)).fetch_comments('https://www.ithome.com/1/020/389.htm','IT之家',1))
    assert result.coverage==coverage and result.total_comments is None


def smzdm_page():
    return Response(text='<input id="articleID" value="123"><input id="articleHashid" value="abc123"><input id="channelID" value="11"><input id="article_comment_open" value="open">')


def smzdm_comments(*,aid=123,root=0,more=0,next_id=7):
    return Response({'error_code':0,'data':{'article_close_comment_switch':0,'total':2,'total_root':1,
        'rows':[{'article_id':aid,'root_id':root,'comment_id':7,'display_name':'读者','content':'正文','up_num':0,'child_total':1}],
        'next_page':more,'next_comment_id':next_id,'next_sort_v2':-31}})


def test_smzdm_target_roots_and_real_zero_likes():
    result=asyncio.run(EngagementCrawler(client=Client(smzdm_page(),smzdm_comments())).fetch_comments('https://post.smzdm.com/p/abc123/','什么值得买',1))
    assert result.coverage=='partial' and result.total_comments==2
    assert len(result.comments)==1 and result.comments[0].likes==0 and result.comments[0].replies==1


@pytest.mark.parametrize('response',[smzdm_comments(aid=999),smzdm_comments(root=8),smzdm_comments(more=1,next_id=0)])
def test_smzdm_wrong_article_reply_or_stalled_cursor(response):
    result=asyncio.run(EngagementCrawler(client=Client(smzdm_page(),response)).fetch_comments('https://post.smzdm.com/p/abc123/','什么值得买',1))
    assert result.coverage=='failed'


def test_smzdm_cursor_cannot_change_article_or_root_scope():
    client=Client(smzdm_page())
    result=asyncio.run(EngagementCrawler(client=client).fetch_comments(
        'https://post.smzdm.com/p/abc123/','什么值得买',2,
        cursor='{"comment_id":7,"article_id":999,"root_id":123}'))
    assert result.coverage=='failed' and len(client.calls)==1


def test_zol_mobile_resolves_only_matching_allowed_canonical_article():
    mobile=Response(text='<link rel="canonical" href="https://auto.zol.com.cn/1257/12579869.html">')
    result=asyncio.run(EngagementCrawler(client=Client(mobile,zol_page(),zol_comments())).fetch_comments('https://m.zol.com.cn/article/12579869.html','中关村在线',1))
    assert result.coverage=='partial' and result.total_comments==1
    other=Response(text='<link rel="canonical" href="https://evil.test/1257/12579869.html">')
    client=Client(other)
    result=asyncio.run(EngagementCrawler(client=client).fetch_comments('https://m.zol.com.cn/article/12579869.html','中关村在线',1))
    assert result.coverage=='failed' and len(client.calls)==1


def test_dripcar_bare_host_has_same_article_id():
    assert validate_media_url('https://dripcar.com/zixun/1032348.html','水滴汽车')==('dripcar','1032348')


def xcar_page():
    return Response(text='<div class="detail_lf"><span class="detail_title"><h1>目标</h1></span><input name="article_id" value="3815068"><div class="detail_main"><span class="browse_number">612浏览</span></div></div><aside>999万浏览</aside>')


def xcar_comments(*,cid='3815068',count=0,rows=None,root_total=0):
    return Response({'info':{'status':'success'},'news':{'cid':cid,'ctype':'14'},
                     'config':{'count':str(count),'toplevelcount':root_total},'list':rows or []})


def test_xcar_views_belong_to_target_not_recommendations():
    result=asyncio.run(EngagementCrawler(client=Client(xcar_page(),xcar_comments())).fetch_interactions('https://a.xcar.com.cn/aikahao/item/3815068.html','爱卡汽车爱咖号'))
    assert result.coverage=='partial' and result.stats.views==612 and result.stats.comments==0


def test_xcar_comment_thread_and_root_pagination():
    rows=[{'id':str(i),'parent_id':0,'status':0,'conts':'一级','ups':0,'count':1} for i in range(25)]
    result=asyncio.run(EngagementCrawler(client=Client(xcar_page(),xcar_comments(count=26,rows=rows,root_total=26))).fetch_comments('https://aikahao.xcar.com.cn/item/3815068.html','爱咖号',1))
    assert result.coverage=='partial' and len(result.comments)==25 and result.next_cursor=='2'
    bad=asyncio.run(EngagementCrawler(client=Client(xcar_page(),xcar_comments(cid='other'))).fetch_comments('https://aikahao.xcar.com.cn/item/3815068.html','爱咖号',1))
    assert bad.coverage=='failed'


def forum_page(status=0):
    return Response(text="""window['__TOPICINFO__']={currentPageIdx:1,topicId:116566265,topicDelete:0};
    <ul id="js-reply-list-container"><li data-status="%s" data-reply-id="101"><div class="reply-main"><div class="reply-detail"><p>正文</p><a class="reply-detail-oper-btn">控制按钮</a></div></div></li></ul>""" % status)


def forum_counts():
    return Response({'returncode':0,'result':[{'topicid':116566265,'views':20,'allreplys':1}]})


def test_autohome_forum_counters_and_public_roots():
    url='https://club.autohome.com.cn/bbs/thread/ff7eda5c4f2837b8/116566265-1.html'
    result=asyncio.run(EngagementCrawler(client=Client(forum_page(),forum_counts())).fetch_comments(url,'汽车之家',1))
    assert result.coverage=='partial' and result.comments[0].text=='正文'
    result=asyncio.run(EngagementCrawler(client=Client(forum_page(),forum_counts())).fetch_interactions(url,'汽车之家'))
    assert result.stats.views==20 and result.stats.comments==1


@pytest.mark.parametrize('status',[20,22])
def test_autohome_forum_does_not_expose_moderated_or_author_only_html(status):
    url='https://club.autohome.com.cn/bbs/thread/ff7eda5c4f2837b8/116566265-1.html'
    result=asyncio.run(EngagementCrawler(client=Client(forum_page(status),forum_counts())).fetch_comments(url,'汽车之家',1))
    assert result.coverage=='unsupported' and result.comments==[]


def test_chooseauto_bound_counts_roots_and_page():
    page = Response(text='var currID=1040092;var conTID=428421;')
    counts = Response({'code':200,'data':{'id':1040092,'browseCount':56726,'likeCount':165,'commentCount':21,'shareCount':0,'collectCount':0}})
    rows = Response({'code':200,'data':{'pageIndex':2,'totalCount':21,'totalPage':3,'list':[
        {'pid':7483121,'tid':428421,'layer':1,'message':'一级正文','digg':0,'replyList':{'totalCount':2,'list':[{'message':'回复不采'}]}}]}})
    client = Client(page,counts,rows)
    r=asyncio.run(EngagementCrawler(client=client).fetch_comments('https://www.chooseauto.com.cn/news/1040092.shtml','选车网',2))
    assert r.coverage=='partial' and r.next_cursor=='3' and len(r.comments)==1
    assert r.comments[0].text=='一级正文' and r.comments[0].replies==2
    assert client.calls[-1][1]['params']['tid']=='428421'
    r=asyncio.run(EngagementCrawler(client=Client(page,counts)).fetch_interactions('https://www.chooseauto.com.cn/news/1040092.shtml','选车网'))
    assert r.stats.views==56726 and r.stats.likes==165 and r.stats.favorites==0


def qcwp_page(wid='1074989'):
    table=[['Reactive',1],{'data':2},{'ARTICLE_1074989':3},{'code':4,'data':5},200,
        {'id':6,'title':7,'look':8,'commentCount':9},wid,'目标',1971,19]
    return Response(text='<script id="__NUXT_DATA__" type="application/json">'+json.dumps(table)+'</script>')


def test_qcwp_resolves_nuxt_references_not_indices_as_counts():
    r=asyncio.run(EngagementCrawler(client=Client(qcwp_page())).fetch_interactions('https://www.qcwp.com/news/1074989','汽车网评'))
    assert r.coverage=='partial' and r.stats.views==1971 and r.stats.comments==19
    assert r.stats.likes is None and r.stats.favorites is None
    r=asyncio.run(EngagementCrawler(client=Client(qcwp_page('other'))).fetch_interactions('https://www.qcwp.com/news/1074989','汽车网评'))
    assert r.coverage=='failed'


@pytest.mark.parametrize('wid,level,expected',[('1074989','1','partial'),('other','1','failed'),('1074989','2','failed')])
def test_qcwp_comment_identity_and_level(wid,level,expected):
    response=Response({'code':200,'data':{'pageNum':1,'total':15,'pages':2,'list':[
        {'id':'1271589','articleId':wid,'level':level,'content':'正文','replyCount':2}]}})
    r=asyncio.run(EngagementCrawler(client=Client(qcwp_page(),response)).fetch_comments('https://www.qcwp.com/news/1074989','汽车网评',1))
    assert r.coverage==expected
    if expected=='partial':assert r.comments[0].replies==2 and r.next_cursor=='2' and r.total_comments==19


def test_buycar_guid_and_target_scoped_views():
    html='''<section class="news_info"><div id="wecharimg" style="background-image:url(/Service/MakeQRCode.aspx?data=https%3A%2F%2Fwww.buycar.cn%2Fnews%2F2610%2F1291684_1.html)"></div>
    <div class="news_date"><span class="icon-yuedu"></span><i>1595</i></div><a id="linkwjpr" rel="89be7585-0209-46f2-bffe-6003210ab5e9"></a></section>'''
    client=Client(Response(text=html),Response({'ErrMsg':'','TotCount':0,'Items':[]}))
    r=asyncio.run(EngagementCrawler(client=client).fetch_interactions('https://www.buycar.cn/news/2610/1291684_1.html','买车网'))
    assert r.coverage=='partial' and r.stats.views==1595 and r.stats.comments==0
    assert client.calls[-1][1]['params']['PageId']=='89be7585-0209-46f2-bffe-6003210ab5e9'


def test_36kr_target_counts_and_explicit_comment_limit():
    state={'articleDetail':{'articleDetailData':{'code':0,'data':{'itemId':'4019927233435522','widgetTitle':'目标'}},'articleRecommendData':{'statPraise':26,'statComment':0,'statCollect':9}}}
    crawler=EngagementCrawler(client=Client(Response(text='window.initialState='+json.dumps(state))))
    r=asyncio.run(crawler.fetch_interactions('https://www.36kr.com/p/4019927233435522','36氪'))
    assert r.coverage=='partial' and (r.stats.likes,r.stats.comments,r.stats.favorites)==(26,0,9) and r.stats.views is None
    r=asyncio.run(crawler.fetch_comments('https://www.36kr.com/p/4019927233435522','36氪',1))
    assert r.coverage=='unsupported'


def test_sohu_missing_comment_thread_preserves_confirmed_pv():
    page=Response(text='var cfg={news_id:"1085844243",media_id:"121451150"};var __autoArticleConfig={};')
    empty=Response({'code':200,'data':{'source_id':None,'topic_id':0,'total_count':0,'comments':[]}})
    url='https://www.sohu.com/a/1085844243_121451150'
    r=asyncio.run(EngagementCrawler(client=Client(page,empty,Response(text='999'))).fetch_interactions(url,'搜狐号'))
    assert r.coverage=='partial' and r.stats.views==999 and r.stats.comments is None
    r=asyncio.run(EngagementCrawler(client=Client(page,empty)).fetch_comments(url,'搜狐号',1))
    assert r.coverage=='unsupported'


def test_xcar_decodes_declared_gbk_json_before_parsing():
    from curl_cffi.requests.models import Response as CurlResponse
    response=CurlResponse()
    response.status_code=200
    response.headers={'Content-Type':'text/html; charset=GBK'}
    response.content=json.dumps({'config':{'count':'0'},'news':{'cid':'3815068','ctype':'14'},'list':[],
        'info':{'status':'success','message':'无评论'}},ensure_ascii=False).encode('gbk')
    response.encoding='GBK'
    with pytest.raises(ValueError):response.json()
    r=asyncio.run(EngagementCrawler(client=Client(xcar_page(),response)).fetch_comments('https://aikahao.xcar.com.cn/video/3815068.html','爱咖号',1))
    assert r.coverage=='partial' and r.total_comments==0


def test_sina_mobile_news_domain_uses_same_verified_comos_protocol():
    assert validate_media_url('https://news.sina.cn/2026-10-29/detail-iniwxekn5985518.d.html','新浪网')==('sina_news','comos-niwxekn5985518')


def test_csdn_reads_exact_target_not_sidebar_recommendation_counts():
    html='''<link rel="canonical" href="https://blog.csdn.net/test/article/details/150288767"><script>var articleId=150288767;var viewCountFormat=1427;</script><span id="sidebar-likeCount">21</span><div id="sidebar-collect"><span>8</span></div><aside>999万阅读</aside>'''
    r=asyncio.run(EngagementCrawler(client=Client(Response(text=html))).fetch_interactions('https://blog.csdn.net/test/article/details/150288767','CSDN'))
    assert r.coverage=='partial' and (r.stats.views,r.stats.likes,r.stats.favorites)==(1427,21,8)
    assert r.stats.comments is None
    r=asyncio.run(EngagementCrawler(client=Client(Response(text=html.replace('var articleId=150288767','var articleId=9')))).fetch_interactions('https://blog.csdn.net/test/article/details/150288767','CSDN'))
    assert r.coverage=='failed'


def kuai_page(wid='99d02dcf517aa3f1a', rawurl='https://zm.news.so.com/target'):
    import base64
    raw=base64.b64encode(json.dumps({'detail':{'gnid':wid,'title':'目标','rptid':'thread','rawurl':rawurl,'del':0}},ensure_ascii=False).encode()).decode()
    encoded=''.join(chr(ord(c)+i%2) for i,c in enumerate(raw[:1000]))+raw[1000:]
    return Response(text="window.__INITIAL_DATA__='"+encoded+"';")


def test_kuai_target_thread_root_and_pagination():
    data={'errno':0,'data':{'comments':[{'id':'c1','message':'根评论','pid':0,'likes':3,'user_info':json.dumps({'nick_name':'读者'}),'ext':'{"sub_num":2}','sub_comment':[{'message':'回复不采'}]}],'current_page':1,'new_total':11,'next':1,'pages':2}}
    client=Client(kuai_page(),Response(data))
    r=asyncio.run(EngagementCrawler(client=client).fetch_comments('https://www.360kuai.com/99d02dcf517aa3f1a','快资讯',1))
    assert r.coverage=='partial' and r.next_cursor=='2' and len(r.comments)==1
    assert (r.comments[0].text,r.comments[0].author,r.comments[0].replies)==('根评论','读者',2)
    assert client.calls[-1][1]['params']['page_key']=='thread'
    assert client.calls[-1][1]['params']['url']=='https://zm.news.so.com/target'
    bad=asyncio.run(EngagementCrawler(client=Client(kuai_page('other'))).fetch_comments('https://www.360kuai.com/99d02dcf517aa3f1a','快资讯',1))
    assert bad.coverage=='failed'


def test_kuai_legacy_share_must_match_original_article_url():
    from urllib.parse import quote
    original='http://zm.news.so.com/7fecd76b9170e1c9707b7f6b04e1cc07'
    url='https://www.360kuai.com/pc/detail?url='+quote(original, safe='')
    zero=Response({'errno':0,'data':{'current_page':1,'new_total':0,'pages':0,'comments':[]}})
    r=asyncio.run(EngagementCrawler(client=Client(kuai_page(rawurl=original),zero)).fetch_comments(url,'快资讯',1))
    assert r.coverage=='partial' and r.total_comments==0
    r=asyncio.run(EngagementCrawler(client=Client(kuai_page())).fetch_comments(url,'快资讯',1))
    assert r.coverage=='failed'


@pytest.mark.parametrize('wid,likes,expected',[('3608237',294,'partial'),('3608237',0,'unsupported'),('other',294,'unsupported')])
def test_maiche_does_not_read_recommendations_or_default_zero(wid,likes,expected):
    state={'mcw://api/open/content/view.htm?id=3608237&page=all':json.dumps({'success':True,'data':{'id':wid,'title':'target','upCount':likes}}),
           'mcw://api/open/content/side-info2.htm?articleId=3608237':json.dumps({'success':True,'data':{'hitCount':999999}})}
    client=Client(Response(text='Simple.Tools.ajax._nodeData='+json.dumps(state)))
    r=asyncio.run(EngagementCrawler(client=client).fetch_interactions('https://www.maiche.com/news/detail/3608237_all.html','买车网'))
    assert r.coverage==expected and r.stats.views is None
    if expected=='partial':assert r.stats.likes==294 and r.stats.comments is None


@pytest.mark.parametrize('wid,expected',[('YDJ1277UT6UODXZZ','partial'),('other','failed')])
def test_netease_recommendation_keeps_original_url_and_binds_thread(wid,expected):
    url='https://c.m.163.com/news/rec/YDJ1277UT6UODXZZ.html'
    page=Response(text=f'<html data-content-type="rec" data-recommend-id="{wid}" data-title="target"><article><footer><p class="like" data-vote="3"></p></footer></article></html>')
    client=Client(page,Response({'docId':'YDJ1277UT6UODXZZ','title':'target','cmtCount':0}),Response({'commentIds':[],'comments':{},'newListSize':0}))
    r=asyncio.run(EngagementCrawler(client=client).fetch_comments(url,'网易号',1))
    assert r.coverage==expected and client.calls[0][0]==url
    if expected=='partial':
        assert r.total_comments==0 and r.comments==[]
        assert '23bc0ba86b694b7482703f65292cadb3' in client.calls[1][0]
