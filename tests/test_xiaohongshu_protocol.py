import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.crawlers.engagement import EngagementCrawler
from app.crawlers.http_client import PlatformBlockedError
from app.crawlers.platforms import xiaohongshu as xhs

NOTE = '6aca09f1000000000202a5c7'
URL = f'https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=test'


def comment(cid, **extra):
    return {'id': cid, 'content': '正文 ' + cid, 'note_id': NOTE, **extra}


def test_replies_keep_inline_then_fetch_remaining_pages_and_deduplicate(monkeypatch):
    root = comment('root', sub_comment_count='3', sub_comments=[comment('a')],
                   sub_comment_has_more=True, sub_comment_cursor='c1')
    get = AsyncMock(side_effect=[
        {'comments': [comment('a'), comment('b')], 'has_more': True, 'cursor': 'c2'},
        {'comments': [comment('c')], 'has_more': False, 'cursor': ''},
    ])
    monkeypatch.setattr(xhs, '_get', get)
    comments, complete, reason = asyncio.run(xhs._expand_replies(None, [root], NOTE, 'token', 'cookie', URL))
    assert complete and not reason
    assert [x.comment_id for x in comments] == ['root', 'a', 'b', 'c']
    assert [get.await_args_list[i].args[2]['cursor'] for i in range(2)] == ['c1', 'c2']
    assert all(x.parent_comment_id == 'root' for x in comments[1:])


@pytest.mark.parametrize('failure', [
    PlatformBlockedError('HTTP 461'),
    {'comments': [], 'has_more': True, 'cursor': 'c2'},
    {'comments': [comment('b')], 'has_more': True, 'cursor': 'c1'},
    {'comments': [comment('b', note_id='wrong')], 'has_more': False},
    {'comments': [comment('b')], 'cursor': ''},
])
def test_reply_failures_preserve_received_content_and_report_incomplete(monkeypatch, failure):
    roots = [comment('root', sub_comments=[comment('a')], sub_comment_count='2',
                     sub_comment_cursor='c1', sub_comment_has_more=True)]
    get = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
    monkeypatch.setattr(xhs, '_get', get)
    rows, complete, reason = asyncio.run(xhs._expand_replies(None, roots, NOTE, 't', 'c', URL))
    assert not complete and '未完成' in reason
    assert {x.comment_id for x in rows} >= {'root', 'a'}
    assert get.await_count == 1


@pytest.mark.parametrize('status,attempts', [(461, 1), (429, 1), (403, 1), (406, 2)])
def test_only_signature_format_rejection_retries(monkeypatch, status, attempts):
    monkeypatch.setattr(xhs, 'signed_headers', lambda *args, **kwargs: {})
    get = AsyncMock(side_effect=[PlatformBlockedError(f'HTTP {status}'), {'success': True, 'data': {'ok': True}}])
    crawler = SimpleNamespace(_get_json=get)
    if status == 406:
        assert asyncio.run(xhs._get(crawler, '/path', {}, 'c', URL)) == {'ok': True}
    else:
        with pytest.raises(PlatformBlockedError, match=str(status)):
            asyncio.run(xhs._get(crawler, '/path', {}, 'c', URL))
    assert get.await_count == attempts


def test_combined_blank_stats_still_collects_comments(monkeypatch):
    text = json.dumps({'noteDetailMap': {NOTE: {'note': {'noteId': NOTE, 'interactInfo': {'likedCount': ''}}}}})
    class Client:
        async def get(self, url, **kwargs):
            assert kwargs['headers']['Cookie'] == 'a1=a; web_session=b'
            return SimpleNamespace(status_code=200, text=text)
    monkeypatch.setattr(xhs, '_get', AsyncMock(return_value={'comments': [comment('root')], 'has_more': False}))
    crawler = EngagementCrawler(client=Client(), platform_cookies={'xiaohongshu': 'a1=a; web_session=b'})
    result = asyncio.run(crawler.fetch(URL))
    assert result.coverage == 'partial' and result.stats.likes is None
    assert [c.comment_id for c in result.comments] == ['root']


def test_root_restriction_not_retried_by_crawler(monkeypatch):
    get = AsyncMock(side_effect=PlatformBlockedError('HTTP 461'))
    monkeypatch.setattr(xhs, '_get', get)
    crawler = EngagementCrawler(client=object(), max_protocol_attempts=3,
                                platform_cookies={'xiaohongshu': 'a1=a; web_session=b'})
    result = asyncio.run(crawler.fetch_comments(URL, '小红书', 1))
    assert result.coverage == 'blocked' and '461' in result.reason
    assert result.protocol_attempts == get.await_count == 1


def mobile_html(note_id=NOTE, *, comments=None, info=None):
    data = {'noteData': {'noteId': note_id, 'interactInfo': info or {}},
            'commentData': comments}
    return '<script>window.__SETUP_SERVER_STATE__=' + json.dumps({'LAUNCHER_SSR_STORE_PAGE_DATA': data}) + '</script>'


def mobile_comment_data(**overrides):
    return {'commentCount': 0, 'commentCountL1': 0, 'comments': [],
            'hasMore': False, 'time': 1791804306546, **overrides}


def test_mobile_explicit_comment_zero_does_not_invent_other_counters():
    html = mobile_html(comments=mobile_comment_data(), info={'likedCount': '', 'commentCount': ''})
    assert xhs.parse_mobile_stats(html, NOTE).model_dump(exclude_none=True) == {'comments': 0}


def test_mobile_comment_total_is_not_the_truncated_preview_length():
    html = mobile_html(comments=mobile_comment_data(
        commentCount=28, commentCountL1=9, hasMore=True, cursor='c2',
        comments=[{'id': 'one', 'noteId': NOTE}],
    ), info={'likedCount': '9', 'collectedCount': '3', 'shareCount': '12'})
    assert xhs.parse_mobile_stats(html, NOTE).model_dump(exclude_none=True) == {
        'likes': 9, 'comments': 28, 'favorites': 3, 'shares': 12,
    }


@pytest.mark.parametrize('data', [
    None, {}, {'commentCount': 0},
    mobile_comment_data(commentCount=''), mobile_comment_data(commentCount=None),
    mobile_comment_data(commentCount=False), mobile_comment_data(commentCount=-1),
    mobile_comment_data(hasMore=True), mobile_comment_data(hasMore=None),
    mobile_comment_data(time=None), mobile_comment_data(commentCountL1=1),
    mobile_comment_data(commentCount=1, commentCountL1=1, comments=[{'noteId': 'other'}]),
    mobile_comment_data(comments=None),
])
def test_mobile_missing_or_invalid_comment_data_remains_unknown(data):
    assert xhs.parse_mobile_stats(mobile_html(comments=data), NOTE).comments is None


def test_mobile_counters_must_belong_to_target_note():
    html = mobile_html('another-note', comments=mobile_comment_data(), info={'likedCount': '999'})
    assert xhs.parse_mobile_stats(html, NOTE).model_dump(exclude_none=True) == {}
    assert xhs.parse_mobile_stats('window.__SETUP_SERVER_STATE__={broken}', NOTE).comments is None


def test_blank_desktop_stats_use_verified_mobile_comment_count():
    desktop = json.dumps({'noteDetailMap': {NOTE: {'note': {'noteId': NOTE, 'interactInfo': {'likedCount': ''}}}}})
    class Client:
        def __init__(self): self.calls = []
        async def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            html = desktop if len(self.calls) == 1 else mobile_html(comments=mobile_comment_data())
            return SimpleNamespace(status_code=200, text=html)
    client = Client()
    crawler = EngagementCrawler(client=client, platform_cookies={'xiaohongshu': 'a1=a; web_session=b'})
    result = asyncio.run(crawler.fetch_interactions(URL, '小红书'))
    assert result.coverage == 'partial'
    assert result.stats.model_dump(exclude_none=True) == {'comments': 0}
    assert len(client.calls) == 2 and '/discovery/item/' in client.calls[1][0]
    assert client.calls[1][1]['headers']['User-Agent'] == xhs.MOBILE_USER_AGENT
    assert client.calls[1][1]['headers']['Cookie'] == 'a1=a; web_session=b'
    assert 'mobile SSR' in result.source


def test_mobile_restriction_stops_without_retrying_or_inventing_zero():
    desktop = json.dumps({'noteDetailMap': {NOTE: {'note': {'noteId': NOTE, 'interactInfo': {'likedCount': ''}}}}})
    class Client:
        def __init__(self): self.calls = 0
        async def get(self, url, **kwargs):
            self.calls += 1
            return SimpleNamespace(status_code=200, text=desktop) if self.calls == 1 else SimpleNamespace(status_code=461, text='')
    client = Client()
    crawler = EngagementCrawler(client=client, max_protocol_attempts=3,
                                platform_cookies={'xiaohongshu': 'a1=a; web_session=b'})
    result = asyncio.run(crawler.fetch_interactions(URL, '小红书'))
    assert result.coverage == 'blocked' and result.stats.comments is None
    assert client.calls == 2 and result.protocol_attempts == 1
