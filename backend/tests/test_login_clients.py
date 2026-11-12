"""Client selection affects the real SDK exchange, not merely its displayed label."""
import asyncio
import time

import httpx
import pytest
from p115client.const import SSOENT_TO_APP

from strimkeep.cloud115 import Cloud115, CloudError
from strimkeep.login_clients import CLIENT_LABELS, LOGIN_CLIENTS
from strimkeep.store import Store
from test_app import app, client


def test_client_options_are_explicit_sdk_devices():
    assert len(LOGIN_CLIENTS) == len(CLIENT_LABELS)
    assert set(CLIENT_LABELS) <= set(SSOENT_TO_APP.values())
    assert {'os_windows', 'os_mac', 'os_linux', 'android', 'ios', 'web'} <= CLIENT_LABELS.keys()


def test_client_list_and_selection_required(client, app):
    options = client.get('/api/v1/connections/115/clients')
    assert options.status_code == 200
    assert options.json()['selected'] == ''
    for data in [None, {}, {'app': ''}, {'app': 'not-a-client'}]:
        assert client.post('/api/v1/connections/115/qr', json=data).status_code == 422
    calls = []
    async def start(selected):
        calls.append(selected)
        return {'id': 'fixture', 'app': selected, 'client_label': CLIENT_LABELS[selected]}
    app.state.cloud.qr_start = start
    result = client.post('/api/v1/connections/115/qr', json={'app': 'os_windows'})
    assert result.status_code == 200 and calls == ['os_windows']
    app.state.store.set_many({'115_qr_client': 'os_mac'})
    assert client.get('/api/v1/connections/115/clients').json()['selected'] == 'os_mac'


def test_cancel_endpoint_requires_csrf(client, app):
    calls = []
    app.state.cloud.qr_cancel = lambda key: calls.append(key) or {'status': 'cancelled'}
    assert client.delete('/api/v1/connections/115/qr/fixture', headers={'X-CSRF-Token': ''}).status_code == 403
    assert not calls
    assert client.delete('/api/v1/connections/115/qr/fixture').status_code == 200
    assert calls == ['fixture']


@pytest.mark.parametrize('selected', CLIENT_LABELS)
def test_real_sdk_uses_selected_client_at_both_steps(tmp_path, selected):
    async def scenario():
        store = Store(tmp_path, 'admin', 'original-password-123')
        cloud = Cloud115(store)
        await cloud.http.aclose()
        requests = []
        def handler(req):
            requests.append(req)
            path = req.url.path
            if path.endswith('/token/'):
                assert path == f'/api/1.0/{selected}/1.0/token/'
                value = {'state': True, 'data': {'uid': 'fixture-qr', 'time': 1, 'sign': 'fixture-sign', 'qrcode': 'https://115.com/scan/test'}}
            elif path == '/get/status/':
                value = {'state': True, 'data': {'status': 2}}
            elif path.endswith('/login/qrcode/'):
                exchange_app = {'ipad': 'ios', 'qipad': 'ios', 'qios': 'ios'}.get(selected, selected)
                assert path == f'/app/1.0/{exchange_app}/1.0/login/qrcode/'
                value = {'state': True, 'data': {'cookie': {'UID': '2_A1', 'CID': 'fixture', 'SEID': 'fixture'}}}
            else:
                raise AssertionError(path)
            return httpx.Response(200, json=value)
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
        try:
            qr = await cloud.qr_start(selected)
            assert qr['app'] == selected and qr['client_label'] == CLIENT_LABELS[selected]
            assert qr['image'].startswith('data:image/png;base64,')
            result = await cloud.qr_poll(qr['id'])
            assert result['status'] == 'connected' and result['app'] == selected
            assert store.get('115_qr_client') == selected and 'fixture' in store.get('cookie')
            assert len(requests) == 3
        finally:
            await cloud.close(); store.close()
    asyncio.run(scenario())


def test_cancelled_confirmation_cannot_save_cookie(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'original-password-123')
        store.set_many({'cookie': 'UID=original; CID=original; SEID=original'})
        cloud = Cloud115(store)
        started, release = asyncio.Event(), asyncio.Event()
        async def call(method, *args, **kwargs):
            if method.__name__ == 'login_qrcode_token':
                return {'data': {'uid': 'uid', 'time': 1, 'sign': 'sign', 'qrcode': 'https://115.com/scan/test'}}
            if method.__name__ == 'login_qrcode_scan_status':
                return {'data': {'status': 2}}
            started.set(); await release.wait()
            return {'data': {'cookie': {'UID': 'new', 'CID': 'new', 'SEID': 'new'}}}
        cloud.call = call
        try:
            qr = await cloud.qr_start('os_windows')
            task = asyncio.create_task(cloud.qr_poll(qr['id']))
            await started.wait(); cloud.qr_cancel(qr['id']); release.set()
            assert (await task)['status'] == 'cancelled'
            assert 'original' in store.get('cookie') and not store.get('115_qr_client')
        finally:
            await cloud.close(); store.close()
    asyncio.run(scenario())


def test_new_client_invalidates_old_qr(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'original-password-123'); cloud = Cloud115(store)
        async def call(method, *args, **kwargs):
            return {'data': {'uid': kwargs.get('app'), 'time': 1, 'sign': 'sign', 'qrcode': 'https://115.com/scan/test'}}
        cloud.call = call
        try:
            old = await cloud.qr_start('android'); current = await cloud.qr_start('os_mac')
            assert (await cloud.qr_poll(old['id']))['status'] == 'expired'
            assert cloud.qr[current['id']]['app'] == 'os_mac' and len(cloud.qr) == 1
        finally:
            await cloud.close(); store.close()
    asyncio.run(scenario())


def test_expired_and_invalid_clients_do_not_write_cookie(tmp_path):
    async def scenario():
        store = Store(tmp_path, 'admin', 'original-password-123'); cloud = Cloud115(store)
        try:
            with pytest.raises(CloudError): await cloud.qr_start('invalid')
            cloud.qr['expired'] = {'expires': time.monotonic() - 1}
            assert (await cloud.qr_poll('expired'))['status'] == 'expired'
            assert not store.get('cookie')
        finally:
            await cloud.close(); store.close()
    asyncio.run(scenario())
