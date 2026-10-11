"""Mixed-library Emby regression: same TMDB work, separate Emby SeriesIds.

Only disposable tmp_path .strm files; no real NAS/Emby API.
"""
from pathlib import Path
import copy
import pytest
from app import engine, emby_events


@pytest.fixture
def mixed(tmp_path, monkeypatch):
    local, share = tmp_path / 'local', tmp_path / 'share'
    for p in (local, share):
        p.mkdir()
    monkeypatch.setattr(engine, 'L_ROOT', local)
    monkeypatch.setattr(engine, 'S_ROOT', share)
    monkeypatch.setattr(engine, 'EMBY_PATHS', engine.EmbyPathMap('/local', '/share'))

    metadata = {
        '932232': {'Id': '932232', 'Type': 'Series', 'Name': '国民老公',
                   'ProductionYear': 2018, 'ProviderIds': {'Tmdb': '84842'},
                   'Path': '/local/剧集/国民老公'},
        '656712': {'Id': '656712', 'Type': 'Series', 'Name': '国民老公',
                   'ProductionYear': 2018, 'ProviderIds': {'Tmdb': '84842'},
                   'Path': '/share/剧集/国民老公'},
    }
    rows = []
    for sid, lib, amount in [('932232', 'local', 54), ('656712', 'share', 16)]:
        for number in range(1, amount + 1):
            season, ep = (1, number) if number <= 28 else (2, number - 28)
            relative = '剧集/国民老公/S%02dE%02d.strm' % (season, ep)
            path = (local if lib == 'local' else share) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('https://example.invalid/stream')
            rows.append({'Id': sid + '-' + str(number), 'Type': 'Episode',
                         'SeriesId': sid, 'Path': '/' + lib + '/' + relative,
                         'ParentIndexNumber': season, 'IndexNumber': ep})

    def query(path, params=None, **kwargs):
        assert path == '/Items', 'No full-scan or mutation calls'
        if 'Ids' in params:
            selected = [metadata[id] for id in params['Ids'].split(',') if id in metadata]
        else:
            assert params['ParentId'] in ('932232', '656712')
            selected = rows  # Reproduce the user's Emby: both parent IDs return all 70.
        return {'Items': copy.deepcopy(selected), 'TotalRecordCount': len(selected)}
    monkeypatch.setattr(engine, 'emby_request', query)
    return rows, metadata, query


def test_verified_same_work_cross_library_70_rows_are_split_54_16(mixed):
    local = emby_events._live('932232')
    share = emby_events._live('656712')
    assert len(local) == 54 and len(share) == 16
    assert {x['SeriesId'] for x in local} == {'932232'}
    assert {x['SeriesId'] for x in share} == {'656712'}
    assert len({(x['ParentIndexNumber'], x['IndexNumber']) for x in local + share}) <= 70


@pytest.mark.parametrize('field,value', [
    ('Name', '其他电视剧'),
    ('ProductionYear', 2017),
    ('ProviderIds', {'Tmdb': '999'}),
])
def test_unrelated_title_keeps_old_facts(mixed, monkeypatch, field, value):
    rows, meta, query = mixed
    meta['656712'][field] = value
    with pytest.raises(RuntimeError, match='身份不一致'):
        emby_events._live('932232')


def test_unknown_series_and_missing_strm_fail_closed(mixed, monkeypatch):
    rows, metadata, query = mixed
    rows[0]['SeriesId'] = 'unrelated'
    with pytest.raises(RuntimeError):
        emby_events._live('932232')
    rows[0]['SeriesId'] = '932232'
    (engine.L_ROOT / rows[0]['Path'].lstrip('/').removeprefix('local/')).unlink()
    with pytest.raises(RuntimeError, match='挂载不可见'):
        emby_events._live('932232')
