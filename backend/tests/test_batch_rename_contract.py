import asyncio
from urllib.parse import parse_qs
import httpx
import pytest
from strimkeep.cloud115 import CloudError
from test_rate_limits import make_cloud

@pytest.mark.parametrize('count', [2, 50, 51])
def test_batch_rename_requests_and_write_interval(tmp_path, count):
    async def run():
        store, cloud, clock, sleeps = make_cloud(tmp_path)
        store.set_many({'cookie': 'UID=1_A1; CID=fake; SEID=fake'})
        await cloud.http.aclose()
        requests = []
        def handle(request):
            requests.append((request.url.path, parse_qs(request.content.decode())))
            return httpx.Response(200, json={'state': True})
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        pairs = [(str(i), f'影片{i}.mkv') for i in range(1, count + 1)]
        await cloud.rename_many(pairs)
        assert len(requests) == (count + 49) // 50
        assert all(path == '/files/batch_rename' for path, _ in requests)
        submitted = {key: value for _, data in requests for key, value in data.items()}
        assert submitted == {f'files_new_name[{fid}]': [name] for fid, name in pairs}
        assert sum(sleeps) == pytest.approx(3.0 * (len(requests) - 1))
        assert cloud.next_request - clock() == pytest.approx(3.0)
        await cloud.close(); store.close()
    asyncio.run(run())

@pytest.mark.parametrize('mode,code', [('lost', 'CLOUD_UNAVAILABLE'), ('limited', 'RATE_LIMITED')])
def test_batch_rename_does_not_repeat_failed_request(tmp_path, mode, code):
    async def run():
        store, cloud, clock, sleeps = make_cloud(tmp_path)
        store.set_many({'cookie': 'UID=1_A1; CID=fake; SEID=fake'})
        await cloud.http.aclose()
        calls = []
        def handle(request):
            calls.append(request)
            if mode == 'lost':
                raise httpx.ReadTimeout('simulated', request=request)
            return httpx.Response(200, json={'state': False, 'error_msg': '操作过于频繁'})
        cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        with pytest.raises(CloudError) as exc:
            await cloud.rename_many([('1', '电影.mkv'), ('2', '剧集.mkv')])
        assert exc.value.code == code and len(calls) == 1
        if mode == 'limited':
            assert cloud.rate_limit_until > clock()
        await cloud.close(); store.close()
    asyncio.run(run())
