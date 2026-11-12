"""Verify authentication on the wire, especially without QR's in-memory jar."""
import asyncio
from http.cookiejar import CookieJar

import httpx
import pytest

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.store import Store


COOKIE = 'UID=123_A1; CID=test-cid; SEID=test-seid; KID=test-kid'


async def connection(store, handler):
    cloud = Cloud115(store)
    await cloud.http.aclose()
    cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    return cloud


def test_persisted_cookie_authenticates_fresh_connection_and_recreated_store(tmp_path):
    async def scenario():
        root = tmp_path / 'data'
        store = Store(root, 'admin', 'test-password-1234')
        store.set_many({'cookie': COOKIE})
        for _ in range(2):
            store.close()
            store = Store(root, 'admin', 'test-password-1234')
            def serve(request):
                assert request.headers['cookie'] == COOKIE
                if request.url.host == 'my.115.com':
                    return httpx.Response(200, json={'state': True})
                return httpx.Response(200, json={'state': True, 'count': 0, 'data': []})
            cloud = await connection(store, serve)
            assert not cloud.http.cookies
            assert await cloud.status() == {'connected': True}
            cloud.next_request = 0
            assert (await cloud.list_files(0, 0, 100))['total'] == 0
            assert not cloud.http.cookies
            await cloud.close()
        store.close()
        assert b'test-seid' not in (root / 'data/state.db').read_bytes()
    asyncio.run(scenario())


def test_changed_account_does_not_use_stale_session_cookies(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': COOKIE})
        seen = []
        def serve(request):
            seen.append(request.headers.get('cookie', ''))
            return httpx.Response(200, json={'state': True}, headers={
                'set-cookie': 'SEID=memory-only; Domain=.115.com; Path=/'})
        cloud = await connection(store, serve)
        cloud.http.cookies.set('UID', '999_A1', domain='.115.com')
        cloud.http.cookies.set('SEID', 'stale', domain='.115.com')
        await cloud.status()
        store.set_many({'cookie': 'UID=456_A1; CID=new; SEID=new'})
        cloud.next_request = 0
        await cloud.status()
        # A class-level QR request has no authenticated SDK client jar.
        cloud.next_request = 0
        await cloud.call(cloud.client().login_qrcode_token, app='android')
        assert seen == [COOKIE, 'UID=456_A1; CID=new; SEID=new', '']
        await cloud.close(); store.close()
    asyncio.run(scenario())


def test_sdk_transport_retains_domain_path_and_explicit_headers(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        seen = []
        def serve(request):
            seen.append(request)
            return httpx.Response(200, json={'state': True})
        cloud = await connection(store, serve)
        jar = CookieJar()
        for name, value, domain, path in [
            ('UID', '123_A1', '.115.com', '/'),
            ('PRIVATE', 'not-for-root', '.115.com', '/files'),
            ('OTHER', 'not-for-115', 'other.example', '/'),
        ]:
            cookies = httpx.Cookies()
            cookies.set(name, value, domain=domain, path=path)
            for cookie in cookies.jar:
                jar.set_cookie(cookie)
        for url in ['https://my.115.com/', 'https://webapi.115.com/files/file',
                    'https://unrelated.example/']:
            await cloud.sdk_request(url, cookies=jar, async_=True, session=cloud.http, parse=True)
        await cloud.sdk_request('https://webapi.115.com/files/file', cookies=jar,
                                headers={'Cookie': 'explicit=value', 'User-Agent': 'Senplay/Test'},
                                method='POST', data={'file_id': '123'},
                                async_=True, session=cloud.http, parse=True)
        assert seen[0].headers['cookie'] == 'UID=123_A1'
        assert set(seen[1].headers['cookie'].split('; ')) == {'UID=123_A1', 'PRIVATE=not-for-root'}
        assert seen[2].headers['cookie'] == ''
        assert seen[3].headers['cookie'] == 'explicit=value'
        assert seen[3].headers['user-agent'] == 'Senplay/Test'
        assert seen[3].content == b'file_id=123'
        assert int(seen[3].headers['content-length']) == len(seen[3].content)
        await cloud.close(); store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['invalid', 'network'])
def test_real_expiry_and_network_failure_are_not_hidden(tmp_path, failure):
    async def scenario():
        store = Store(tmp_path, 'admin', 'test-password-1234')
        store.set_many({'cookie': COOKIE})
        def serve(request):
            assert request.headers['cookie'] == COOKIE
            if failure == 'network':
                raise httpx.ConnectError('sensitive upstream details', request=request)
            return httpx.Response(200, json={'state': False})
        cloud = await connection(store, serve)
        with pytest.raises(CloudError) as exc:
            await cloud.status()
        assert exc.value.code == ('LOGIN_INVALID' if failure == 'invalid' else 'CLOUD_UNAVAILABLE')
        assert 'sensitive' not in str(exc.value)
        assert store.get('cookie') == COOKIE
        await cloud.close(); store.close()
    asyncio.run(scenario())
