"""Reproduce user incidents with fake transports, real SQLite and real relay HTTP."""
import http.client
import json
import socket
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock
from urllib.parse import urlsplit

import httpx
import pytest
from app.cloud import (media_probe, public_channel_reader as web, channel_network,
                       provider115, workflow, settings, recognition, diagnostics,
                       automatic_runs, jobs, store, events)
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_my_drive_organize import Catalog
from dev.tests.test_my_drive_probe import probe_data


def test_relay_streams_continuously_across_internal_chunk_boundary(monkeypatch):
    size = media_probe.CHUNK_BYTES + 123
    requested = []
    original_client, original_dns = httpx.Client, socket.getaddrinfo
    def send(request):
        start, end = map(int, request.headers['range'][6:].split('-'))
        requested.append((start, end))
        return httpx.Response(206, headers={'content-range': f'bytes {start}-{end}/{size}'},
                              stream=httpx.ByteStream(b'x'*(end-start+1)))
    monkeypatch.setattr(httpx, 'Client', lambda **kw: original_client(transport=httpx.MockTransport(send), **kw))
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, *a, **kw: original_dns(host, *a, **kw) if host == '127.0.0.1' else [(2, 1, 6, '', ('8.8.8.8', 443))])
    monkeypatch.setattr(media_probe.shutil, 'which', lambda _: 'ffprobe-fixture')
    def run(command, stdout, **kwargs):
        address = urlsplit(command[-1])
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=5)
        try:
            connection.request('GET', address.path, headers={'Range': 'bytes=0-'})
            response = connection.getresponse()
            assert response.status == 206
            assert response.getheader('Content-Range') == f'bytes 0-{size-1}/{size}'
            assert len(response.read()) == size
        finally:
            connection.close()
        stdout.write(json.dumps(probe_data()).encode())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(media_probe.subprocess, 'run', run)
    assert media_probe.probe_url({'url': 'https://download.115cdn.com/test'}, size)['width'] == '3840'
    assert requested == [(0, media_probe.CHUNK_BYTES-1), (media_probe.CHUNK_BYTES, size-1)]


@pytest.mark.parametrize('address', ['198.18.0.1', '198.19.255.254', '149.154.167.99'])
def test_public_preview_accepts_fake_ip_but_keeps_fixed_tls_host(monkeypatch, address):
    monkeypatch.setattr(channel_network, 'http_options', lambda: {'proxy': None, 'trust_env': False})
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', (address, 443))])
    client = MagicMock()
    response = Mock(status_code=200, headers={'Content-Type': 'text/html'})
    response.iter_bytes.return_value = [b'<html></html>']
    client.return_value.__enter__.return_value.stream.return_value.__enter__.return_value = response
    monkeypatch.setattr(web.httpx, 'Client', client)
    assert web._http_page('channelname') == b'<html></html>'
    assert client.call_args.kwargs['verify'] is True
    assert client.call_args.kwargs['follow_redirects'] is False
    assert client.return_value.__enter__.return_value.stream.call_args.args == ('GET', 'https://t.me/s/channelname')


@pytest.mark.parametrize('address', ['127.0.0.1', '192.168.1.2', '169.254.169.254', '::1', 'fc00::1', '100.64.0.1'])
def test_public_preview_still_rejects_arbitrary_private_dns(monkeypatch, address):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [(2, 1, 6, '', (address, 443))])
    with pytest.raises(web.PublicPageError, match='不支持的内网'):
        web._public_dns()


def test_fake_ip_without_tun_has_specific_connection_advice(monkeypatch):
    monkeypatch.setattr(channel_network, 'http_options', lambda: {'proxy': None, 'trust_env': False})
    monkeypatch.setattr(web, '_public_dns', lambda: 'fake_ip')
    monkeypatch.setattr(web.httpx, 'Client', Mock(side_effect=httpx.ConnectError('secret proxy URL')))
    with pytest.raises(web.PublicPageError, match='TUN') as caught:
        web._http_page('channelname')
    assert 'secret' not in str(caught.value)


@pytest.mark.parametrize('status,code', [(401, 'provider_http_401'), (429, 'provider_http_429'), (503, 'provider_http_503')])
def test_provider_http_error_keeps_operation_status_without_credentials(cloud, status, code):
    adapter = object.__new__(provider115.Provider115)
    request = httpx.Request('GET', 'https://115.com/private?cookie=never-output')
    error = httpx.HTTPStatusError('Cookie=never-output', request=request, response=httpx.Response(status, request=request))
    adapter.client = SimpleNamespace(fs_file=Mock(side_effect=error))
    with pytest.raises(CloudError) as caught:
        adapter.call('fs_file', '4')
    assert caught.value.code == code and caught.value.stage == 'info'
    assert '读取文件信息' in str(caught.value) and str(status) in str(caught.value)
    with store.db() as con:
        assert 'never-output' not in str(con.execute('SELECT message,details FROM runtime_logs').fetchall())


def test_write_timeout_stays_uncertain_and_is_not_retried(cloud):
    adapter = object.__new__(provider115.Provider115)
    call = Mock(side_effect=httpx.ReadTimeout('Cookie=private'))
    adapter.client = SimpleNamespace(fs_move=call)
    with pytest.raises(CloudError) as caught:
        adapter.call('fs_move', '4', write=True)
    assert caught.value.uncertain and caught.value.code == 'provider_timeout'
    assert call.call_count == 1 and '写入结果待核实' in str(caught.value)


def test_probe_failure_guidance_is_not_source_or_cookie_guidance():
    for message, code in [('ffprobe 退出码 1，保留源文件', 'probe_exit'), ('115 媒体读取返回 HTTP 403，保留源文件', 'probe_http_403')]:
        row = diagnostics.items({'skipped': [{'reason': message, 'code': code, 'stage': 'probe'}]})[0]
        assert row['stage'] == '媒体信息' and row['code'] == code
        assert row['page'] == 'organize' and 'Cookie' not in row['suggestion']


def test_manual_tv_identity_keeps_parent_season_and_probe_failure_receipt(cloud, monkeypatch):
    monkeypatch.setattr(recognition, 'tmdb', lambda: Catalog())
    cloud.remote.files['1']['name'] = 'Season 02'
    cloud.remote.files['4']['name'] = 'E03.mkv'
    settings.save({'ffprobe_enabled': True})
    monkeypatch.setattr(media_probe, 'extract', Mock(side_effect=CloudError('ffprobe 读取失败，保留源文件', 502, code='probe_exit', stage='probe')))
    choice = {'kind': 'tv', 'tmdb_id': '44'}
    preview = workflow.plan(cloud.id, '1', '2', {'4': choice})
    row = preview['skipped'][0]
    assert row['override'] == choice and row['stage'] == 'probe'
    assert row['recognition']['tmdb_id'] == '44'
    assert row['recognition']['season'] == 2 and row['recognition']['episode'] == 3
    assert not cloud.remote.writes


def test_account_wide_failure_stops_batch_keeps_filename_and_remaining(cloud, monkeypatch):
    for i in range(6, 12):
        cloud.remote.files[str(i)] = {**cloud.remote.files['4'], 'id': str(i), 'name': f'Movie.{i}.mkv'}
    failed = Mock(side_effect=CloudError('115 请求超时', 502, code='provider_timeout', category='transport', stage='info'))
    monkeypatch.setattr(workflow, 'recognize_source', failed)
    with pytest.raises(CloudError) as caught:
        workflow.plan(cloud.id, '1', '2')
    assert failed.call_count == 1
    assert caught.value.partial_result['errors'][0]['name']
    assert len(caught.value.partial_result['remaining']) == 6
    assert not cloud.remote.writes


def test_repeated_automatic_failed_tasks_are_coalesced_but_uncertain_is_not(cloud):
    payload = {'account_id': cloud.id, 'source_cid': '1', 'target_cid': '2', 'rule_id': 'auto'}
    failure = {'errors': [{'code': 'provider_timeout', 'stage': 'info', 'reason': '115 请求超时'}]}
    first, second = (store.job(jobs.enqueue('workflow', payload)) for _ in range(2))
    assert automatic_runs.claim_notice(first, 'workflow', 'failed', failure)
    assert not automatic_runs.claim_notice(second, 'workflow', 'failed', failure)
    assert automatic_runs.claim_notice(second, 'workflow', 'uncertain', failure)
