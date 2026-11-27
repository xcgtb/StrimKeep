from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from strimkeep.offline_api import install_offline
from strimkeep.transfer import shares


def test_intake_separates_share_aliases_and_routes_each_offline_link():
    transfers, submissions = [], []

    async def enqueue(text, target, *_):
        transfers.append((target, shares(text)))
        return [41, 42]

    async def submit(urls, target, label, organize):
        submissions.append((target, urls))
        return 50 + len(submissions)

    settings = {
        'transfer_config': {'target_cid': '101'},
        'offline_config': {
            'directories_v2': True,
            'target_cid': '202',
            'magnet_cid': '203',
            'magnet_routes': [{'keywords': '纪录片', 'target_cid': '204', 'target_label': '纪录片'}],
        },
    }
    app = FastAPI()
    app.state.organizer = SimpleNamespace(transfer=SimpleNamespace(enqueue=enqueue),
                                          offline=SimpleNamespace(submit=submit))
    install_offline(app, settings, lambda: True)
    doc = 'magnet:?xt=urn:btih:' + 'a' * 40
    other = 'magnet:?xt=urn:btih:' + 'b' * 40
    with TestClient(app) as client:
        mixed = client.post('/api/v1/intake', json={'text': '\n'.join((
            'https://115.com/s/First?password=abcd', 'https://115cdn.com/s/Second',
            '提取码 qwer', '纪录片', doc, '其他作品', other,
        ))}).json()
        assert mixed['errors'] == []
        assert mixed['share'] == [41, 42]
        assert mixed['offline'] == [51, 52]
        assert transfers == [('101', [{'share_code': 'First', 'receive_code': 'abcd'},
                                      {'share_code': 'Second', 'receive_code': 'qwer'}])]
        assert submissions == [('204', [doc]), ('203', [other])]
        assert mixed['receipts'][0]['count'] == 1
        assert client.post('/api/v1/intake', json={'text': other}).json()['errors'] == []
