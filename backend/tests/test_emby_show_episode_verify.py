"""Emby can list Series via /Items but requires /Shows/<id>/Episodes for episodes."""
import asyncio
import json
import httpx
import pytest
from test_emby_delivery_states import make_engine, target, insert_delivery


def test_tv_verification_uses_paginated_show_episodes_and_identity(tmp_path):
    async def scenario():
        store, engine, original_http, _ = make_engine(tmp_path, {})
        calls = []
        # The real server may return no episodes for /Items?ParentId=<series>.
        # 201 entries ensure the match is found only after a second page.
        episodes = [
            {'Id': str(n), 'Path': '/media/share/E%03d.strm' % n,
             'MediaSources': [{'Path': 'http://nas:9000/play115/share/id%03d/video.mkv' % n}]}
            for n in range(201)
        ]
        # This exact path must not be accepted for a different playback identity.
        episodes[-1]['Path'] = '/media/wanted.strm'
        def handler(request):
            calls.append((request.url.path, dict(request.url.params)))
            if request.url.path == '/Items':
                assert not request.url.params.get('ParentId')
                return httpx.Response(200, json={'Items':[{'Id':'123'}], 'TotalRecordCount':1})
            if request.url.path == '/Shows/123/Episodes':
                start = int(request.url.params.get('StartIndex', 0)); limit = int(request.url.params['Limit'])
                return httpx.Response(200, json={'Items':episodes[start:start+limit], 'TotalRecordCount':len(episodes)})
            return httpx.Response(404, json={})
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        engine.emby.http = http
        wanted = target('share/id200', 'wanted', kind='tv', tmdb=123)
        wrong = target('share/not-id200', 'wanted', kind='tv', tmdb=123)
        jid = insert_delivery(store, [wanted, wrong], [])
        result = await engine.emby.verify(jid)
        assert result['total'] == 2 and result['confirmed'] == 1
        payload = json.loads(engine.emby.row(jid)['payload'])
        assert [t['confirmed'] for t in payload['targets']] == [True, False]
        pages = [params.get('StartIndex') for path, params in calls if path == '/Shows/123/Episodes']
        assert pages == ['0', '200']
        await original_http.aclose(); await http.aclose(); store.close()
    asyncio.run(scenario())


def test_truncated_episode_listing_does_not_claim_success(tmp_path):
    async def scenario():
        store, engine, original_http, _ = make_engine(tmp_path, {})
        def handler(request):
            if request.url.path == '/Items':
                return httpx.Response(200, json={'Items':[{'Id':'123'}], 'TotalRecordCount':1})
            if request.url.path == '/Shows/123/Episodes':
                return httpx.Response(200, json={'Items':[], 'TotalRecordCount':1})
            return httpx.Response(404)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        engine.emby.http = http
        jid = insert_delivery(store, [target('share/x','wanted',kind='tv',tmdb=123)], [])
        with pytest.raises(ValueError, match='截断'):
            await engine.emby.verify(jid)
        payload = json.loads(engine.emby.row(jid)['payload'])
        assert not payload['targets'][0]['confirmed']
        await original_http.aclose(); await http.aclose(); store.close()
    asyncio.run(scenario())
