import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from strimkeep import subscriptions
from strimkeep.core import UncertainState
from strimkeep.main import create_app
from strimkeep.resource_inbox import ResourceInbox
from strimkeep.store import Store
from strimkeep.transfer import verify_subscription_video
from test_app import FakeCloud


DETAILS = {
    'id': 78, 'name': '海岸线', 'original_name': 'Coastline', 'first_air_date': '2025-04-01',
    'seasons': [{'season_number': 1, 'name': '第一季', 'air_date': '2025-04-01'}],
}
MOVIE = {'id': 78, 'title': '远山来信', 'original_title': 'Letters from the Hills', 'release_date': '2025-04-01'}
URL = '/api/v1/explore/subscriptions'


def item(code, title, kind='share', link=None):
    return {'key': f'{kind}:{code}', 'title': title, 'link_type': kind,
            'source_names': ['盘搜'], 'link': link or f'https://115.com/s/{code}'}


def source(items):
    async def discover(*_):
        return {'items': items, 'sources': [{'name': '盘搜', 'found': len(items)}]}
    return discover


@pytest.fixture
def follow(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    root = tmp_path / 'strm'
    root.mkdir()
    store.set_many({'organize_config': {'library_dir_name': '网盘库'},
                    'transfer_config': {'explore_cid': '34567'}})
    metadata = {'tv/78': DETAILS, 'tv/78/season/1': {
        'season_number': 1, 'episodes': [
            {'episode_number': n, 'air_date': f'2025-04-0{n}'} for n in range(1, 5)
        ],
    }, 'search/tv': {'results': [DETAILS], 'total_pages': 1},
        'movie/78': MOVIE, 'search/movie': {'results': [MOVIE], 'total_pages': 1}}

    async def tmdb(path, params=None, **_):
        return metadata[path]

    async def share_items(secret, cid):
        names = {
            'episode': ['海岸线.2025.S01E04.1080p.mkv'],
            'pack': ['海岸线.2025.S01E04.1080p.mkv', '海岸线.2025.S01E03.1080p.mkv'],
            'pilot': ['海岸线.2025.S01E01.1080p.mkv'],
            'movieexample': ['远山来信.2025.1080p.WEB-DL.mkv'],
            'badmovie': ['远山来信.2025.1080p.CAM.mkv'],
        }
        assert cid == '0'
        return [{'id': str(index), 'name': name, 'size': 1000, 'is_dir': False}
                for index, name in enumerate(names[secret['share_code']], 1)]

    engine = SimpleNamespace(store=store, root=root, tmdb=tmdb, http=None, telegram=None,
                             cloud=SimpleNamespace(share_items=share_items))
    engine.resources = ResourceInbox(engine)
    service = subscriptions.MediaSubscriptions(engine)
    yield service, metadata
    store.close()


def insert(service, action='transfer', backfill=False):
    with service.store.transaction():
        service.store.db.execute(
            """INSERT INTO media_subscriptions(tmdb_id,season,title,share_action,backfill)
               VALUES(78,1,'海岸线',?,?)""", (action, backfill))


def current(service):
    return service.public(service.store.db.execute('SELECT * FROM media_subscriptions').fetchone())


def due(service):
    with service.store.transaction():
        service.store.db.execute('UPDATE media_subscriptions SET next_check=0')


def media(service, episode, side='网盘库'):
    path = service.engine.root / side / '剧集' / '海岸线 (2025) {tmdb-78}' / 'Season 1'
    path.mkdir(parents=True, exist_ok=True)
    (path / f'海岸线.S01E{episode:02d}.strm').write_text('https://example.org/file')


def test_worker_uses_both_libraries_and_holds_packs_then_confirms_inventory(follow, monkeypatch):
    service, _ = follow
    insert(service)
    media(service, 1)
    media(service, 3, '分享影视库')
    monkeypatch.setattr(subscriptions, 'discover_resources', source([
        item('range', '海岸线.2025.S01E04-E05.1080p'),
        item('wrong', '海岸线.2025.S01E04.{tmdb-999}.1080p'),
        item('pack', '海岸线.2025.S01E04.1080p'),
        item('episode', '海岸线.2025.S01E04.1080p'),
    ]))
    asyncio.run(service.tick())
    row = current(service)
    assert row['details']['present'] == [1, 3]
    assert row['details']['missing'] == [4]
    assert row['details']['submitted'] == [4]
    assert [entry['key'] for entry in row['details']['candidates']] == ['share:range', 'share:wrong', 'share:pack']
    inbox = service.store.db.execute('SELECT * FROM resource_inbox').fetchall()
    assert len(inbox) == 1 and inbox[0]['target_cid'] == '34567' and inbox[0]['source'] == 'subscription'

    handed_off = []

    async def transfer(text, target, auto, origin, **kwargs):
        handed_off.append((text, target, auto, kwargs['expected_video']))
        return [102]

    service.engine.transfer = SimpleNamespace(enqueue=transfer)
    asyncio.run(service.engine.resources.process_pending())
    [(link, target, auto, expected)] = handed_off
    assert (link, target, auto) == ('https://115.com/s/episode', '34567', True)
    assert expected['path'] == ['海岸线.2025.S01E04.1080p.mkv']
    with pytest.raises(UncertainState):
        verify_subscription_video([(expected['path'], 1000), (['another-episode.mkv'], 1000)], expected)
    # Retrying a scheduler check, or restarting with the same data, must not queue it again.
    due(service)
    asyncio.run(subscriptions.MediaSubscriptions(service.engine).tick())
    assert service.store.db.execute('SELECT count(*) FROM resource_inbox').fetchone()[0] == 1
    media(service, 4)
    due(service)
    asyncio.run(service.tick())
    assert current(service)['state'] == 'caught_up'
    assert current(service)['details']['present'] == [1, 3, 4]


def test_premiere_waits_then_enqueues_share_job_and_can_pause(follow, monkeypatch):
    service, metadata = follow
    insert(service, 'share')
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    series = {**DETAILS, 'seasons': [{'season_number': 1, 'air_date': str(today + timedelta(days=4))}]}
    metadata['tv/78'] = series
    calls = []

    async def discover(*_):
        calls.append(True)
        return {'sources': [{'name': '盘搜'}], 'items': [item('pilot', '海岸线.2025.S01E01.1080p')]}

    monkeypatch.setattr(subscriptions, 'discover_resources', discover)
    asyncio.run(service.tick())
    assert current(service)['state'] == 'waiting'
    assert not calls
    theory = str(today + timedelta(days=1))
    series['seasons'][0]['air_date'] = theory
    metadata['tv/78/season/1']['episodes'] = [{'episode_number': 1, 'air_date': theory}]
    due(service)
    asyncio.run(service.tick())
    episode, = current(service)['attempts']
    config = json.loads(service.store.db.execute(
        'SELECT config FROM organize_jobs WHERE id=?', (episode['job_id'],)).fetchone()[0])
    assert config['origin'] == 'subscription'
    assert config['expected_subscription_video']['path'] == ['海岸线.2025.S01E01.1080p.mkv']
    assert service.store.get(f"share-secret:{episode['job_id']}")['share_code'] == 'pilot'
    with service.store.transaction():
        service.store.db.execute('UPDATE media_subscriptions SET enabled=0,next_check=0')
    asyncio.run(service.tick())
    assert len(calls) == 1


def test_same_title_ambiguity_and_offline_wrong_filename_require_review(follow, monkeypatch):
    service, metadata = follow
    insert(service, backfill=True)
    service.store.set_many({'offline_config': {'magnet_cid': '123'}})
    metadata['search/tv']['results'] = [DETAILS, {**DETAILS, 'id': 79}]
    magnet = 'magnet:?xt=urn:btih:' + 'b' * 40 + '&dn=Coastline.S01E04.mkv'
    monkeypatch.setattr(subscriptions, 'discover_resources', source([
        item('pilot', '海岸线.2025.S01E01.1080p'),
        item('b' * 40, '海岸线.2025.S01E01.{tmdb-78}.1080p', 'magnet', magnet),
    ]))
    asyncio.run(service.tick())
    assert current(service)['state'] == 'review'
    assert len(current(service)['details']['candidates']) == 2
    assert not current(service)['attempts']


def test_verified_magnet_enters_offline_inbox_once(follow, monkeypatch):
    service, _ = follow
    insert(service)
    service.store.set_many({'offline_config': {'magnet_cid': '5678'}})
    link = 'magnet:?xt=urn:btih:' + 'a' * 40 + '&dn=Coastline.S01E01.mkv'
    monkeypatch.setattr(subscriptions, 'discover_resources', source([
        item('a' * 40, '海岸线.2025.S01E01.1080p', 'magnet', link),
    ]))
    asyncio.run(service.tick())
    [tracked] = current(service)['attempts']
    inbox = service.store.db.execute('SELECT * FROM resource_inbox WHERE id=?', (tracked['inbox_id'],)).fetchone()
    assert (inbox['target_cid'], inbox['text'], inbox['auto_organize']) == ('5678', link, 1)
    due(service)
    asyncio.run(service.tick())
    assert len(current(service)['attempts']) == 1
    assert service.store.db.execute('SELECT count(*) FROM resource_inbox').fetchone()[0] == 1


def test_pause_during_search_stops_the_pending_submission(follow, monkeypatch):
    service, _ = follow
    insert(service, 'share')

    async def discover(*_):
        with service.store.transaction():
            service.store.db.execute('UPDATE media_subscriptions SET enabled=0')
        return {'items': [item('pilot', '海岸线.2025.S01E01.1080p')], 'sources': [{'name': '盘搜'}]}

    monkeypatch.setattr(subscriptions, 'discover_resources', discover)
    asyncio.run(service.tick())
    assert current(service)['enabled'] == 0
    assert not current(service)['attempts']


def test_movie_waits_for_release_then_completes_only_after_inventory(follow, monkeypatch):
    service, metadata = follow
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    metadata['movie/78'] = {**MOVIE, 'release_date': str(today + timedelta(days=4))}
    with service.store.transaction():
        service.store.db.execute("""INSERT INTO media_subscriptions(media_type,tmdb_id,season,title,share_action)
                                    VALUES('movie',78,0,'远山来信','share')""")
    calls = []

    async def discover(*args):
        calls.append(args[-1])
        return {'sources': [{'name': '盘搜'}], 'items': [
            item('cam', '远山来信.2025.1080p.CAM'),
            item('remake', '远山来信.2012.1080p.WEB-DL'),
            item('badmovie', '远山来信.2025.1080p.WEB-DL'),
            item('movieexample', '远山来信.2025.1080p.WEB-DL'),
        ]}

    monkeypatch.setattr(subscriptions, 'discover_resources', discover)
    asyncio.run(service.tick())
    assert current(service)['state'] == 'waiting'
    assert not calls
    metadata['movie/78'] = MOVIE
    due(service)
    asyncio.run(service.tick())
    row = current(service)
    assert row['state'] == 'submitted' and row['enabled']
    attempt, = row['attempts']
    secret = service.store.get(f"share-secret:{attempt['job_id']}")
    assert secret['share_code'] == 'movieexample'
    due(service)
    asyncio.run(service.tick())
    assert len(calls) == 1
    assert current(service)['state'] == 'submitted'

    film = service.engine.root / '分享影视库' / '电影' / '远山来信 (2025) {tmdb-78}'
    film.mkdir(parents=True)
    (film / '远山来信 (2025).strm').write_text('https://example.org/file')
    due(service)
    asyncio.run(service.tick())
    assert current(service)['state'] == 'completed'
    assert not current(service)['enabled']
    assert current(service)['next_check'] == 0
    asyncio.run(service.tick())
    assert calls == ['远山来信']


def test_subscription_http_routes_save_options_without_running_worker(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', FakeCloud)
    with TestClient(app) as client:
        assert client.get(URL).status_code == 401
        login = client.post('/api/v1/auth/login', json={'username': 'admin', 'password': 'test-password-1234'})
        client.headers['X-CSRF-Token'] = login.json()['csrf']

        async def tmdb(path, **_):
            return {'tv/78': DETAILS, 'movie/78': MOVIE}[path]

        monkeypatch.setattr(app.state.organizer, 'tmdb', tmdb)
        assert client.get(URL + '/seasons/78').json()['seasons'][0]['number'] == 1
        config = {'share_action': 'share', 'backfill': True}
        assert client.put(URL + '/78/2', json=config).status_code == 422
        assert client.put(URL + '/78/1', json=config).status_code == 200
        [saved] = client.get(URL).json()['items']
        assert (saved['tmdb_id'], saved['enabled'], saved['backfill']) == (78, 1, 1)
        assert client.put(URL + '/78/1', json={**config, 'enabled': False}).status_code == 200
        assert client.get(URL).json()['items'][0]['enabled'] == 0
        assert client.put(URL + '/movie/78', json=config).status_code == 200
        rows = {row['media_type']: row for row in client.get(URL).json()['items']}
        assert rows['movie']['season'] == 0 and not rows['movie']['backfill']
        assert rows['tv']['season'] == 1 and rows['tv']['backfill']
        assert client.delete(URL + '/78/1').status_code == 200
        assert len(client.get(URL).json()['items']) == 1
        assert client.delete(URL + '/movie/78').status_code == 200
        assert client.get(URL).json() == {'items': []}
