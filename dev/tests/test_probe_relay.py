"""Real loopback HTTP checks without the Linux-only application engine."""
import http.client
import json
import socket
from types import SimpleNamespace
from urllib.parse import urlsplit
import httpx
import pytest
from app.cloud import media_probe, events
from app.cloud.errors import CloudError


@pytest.mark.parametrize('bounded', [False, True])
def test_relay_streams_continuously_across_internal_chunk_boundary(monkeypatch, bounded):
    size = media_probe.CHUNK_BYTES + 123
    requested = []
    budget = media_probe.CHUNK_BYTES + 50
    if bounded:
        monkeypatch.setattr(media_probe, 'MAX_BYTES', budget)
    monkeypatch.setattr(events, 'emit', lambda *a, **kw: None)
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
            if bounded:
                with pytest.raises(http.client.IncompleteRead) as error:
                    response.read()
                assert len(error.value.partial) == budget
            else:
                assert len(response.read()) == size
        finally:
            connection.close()
        stdout.write(json.dumps({'streams': [{'codec_type': 'video', 'codec_name': 'h264', 'width': 3840, 'height': 2160}]}).encode())
        return SimpleNamespace(returncode=1 if bounded else 0)
    monkeypatch.setattr(media_probe.subprocess, 'run', run)
    if bounded:
        with pytest.raises(CloudError) as caught:
            media_probe.probe_url({'url': 'https://download.115cdn.com/test'}, size)
        assert caught.value.code == 'probe_budget'
    else:
        assert media_probe.probe_url({'url': 'https://download.115cdn.com/test'}, size)['width'] == '3840'
    assert requested == [(0, media_probe.CHUNK_BYTES-1), (media_probe.CHUNK_BYTES, (budget if bounded else size)-1)]


