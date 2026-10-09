# -*- coding: utf-8 -*-
"""1.6.4 统一媒体身份/治理快照回归测试。"""
from app import state_store as _state
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'app'))
import engine  # noqa: E402


class TestUnifiedLibrarySnapshot:
    def _with(self, **attrs):
        old = {}
        for k, v in attrs.items():
            old[k] = getattr(engine, k)
            setattr(engine, k, v)
        return old

    def _restore(self, old):
        for k, v in old.items():
            setattr(engine, k, v)

    def test_same_tmdb_tv_unions_local_and_share(self):
        series = [
            {'Id': 'local-series', 'Name': '使徒行者', 'ProviderIds': {'Tmdb': '100'},
             'Path': '/media/local/剧集/使徒行者', 'ProductionYear': 2014,
             'ImageTags': {'Primary': 'x'}},
            {'Id': 'share-series', 'Name': '使徒行者', 'ProviderIds': {'Tmdb': '100'},
             'Path': '/media/share/剧集/使徒行者', 'ProductionYear': 2014,
             'ImageTags': {}},
        ]
        episodes = []
        for n in range(1, 31):
            episodes.append({'SeriesId': 'local-series', 'ParentIndexNumber': 1, 'IndexNumber': n,
                             'Path': f'/media/local/剧集/使徒行者/S01/E{n:02d}.strm'})
        for n in range(31, 61):
            episodes.append({'SeriesId': 'share-series', 'ParentIndexNumber': 1, 'IndexNumber': n,
                             'Path': f'/media/share/剧集/使徒行者/S01/E{n:02d}.strm'})
        movies = []

        def fake_emby(path, params, **kwargs):
            typ = params.get('IncludeItemTypes')
            if typ == 'Series': return {'Items': series}
            if typ == 'Movie': return {'Items': movies}
            return {'Items': []}

        old = self._with(
            emby_request=fake_emby,
            _fetch_all_episodes=lambda: episodes,
            _alive_dir_map=lambda paths: {str(p).rsplit('/', 1)[0]: True for p in paths if p},
            EMBY_PATHS=SimpleNamespace(lib_of=lambda p: 'local' if '/media/local/' in p else 'share' if '/media/share/' in p else None),
        )
        try:
            out = engine._build_emby_library_overview()
        finally:
            self._restore(old)

        assert len(out['series']) == 1
        s = out['series'][0]
        assert s['tmdb_id'] == '100'
        assert s['in_local'] is True and s['in_share'] is True
        assert s['local_eps'] == 30
        assert s['share_eps'] == 30
        assert s['have_eps'] == 60
        assert s['total_episodes'] == 60

    def test_ended_series_uses_final_aired_total(self):
        info = {
            'status': 'Ended',
            'seasons': [{'season_number': 1, 'episode_count': 60}],
            'last_episode_to_air': {'season_number': 1, 'episode_number': 60},
        }
        got = engine.classify_series_by_tmdb(
            [{'season': 1, 'episodes': 30}], info)
        assert got['tmdb_total'] == 60
        assert got['local_total'] == 30
        assert got['diff'] == -30
        assert got['match_status'] == 'missing'

    def test_governance_snapshot_updates_even_when_plan_is_empty(self):
        old_file = engine.GOV_LATEST_FILE
        tmp = Path(tempfile.mkdtemp(prefix='strimkeep_gov_')) / 'gov_latest.json'
        engine.GOV_LATEST_FILE = tmp
        try:
            engine.save_latest_scan(None, {
                'status': 'success', 'total_clean_cnt': 0,
                'del_local_cnt': 0, 'del_share_cnt': 0,
                'del_local_files': 0, 'del_share_files': 0,
                'protected_items': [], 'exempted_count': 0,
                'reason_counts': {}, 'quiet_skipped': 0,
            })
            data = engine.load_latest_scan()
            assert data and data['plan_id'] is None
            assert data['result']['total_clean_cnt'] == 0
            assert data['result']['scan_ts'] == data['ts']
            assert len(data.get('rule_sig', '')) == 16
        finally:
            engine.GOV_LATEST_FILE = old_file


    def test_health_snapshot_applies_manual_done(self, monkeypatch, tmp_path):
        monkeypatch.setattr(engine, 'LIBRARY_SNAPSHOT_FILE', tmp_path / 'library_snapshot.json')
        monkeypatch.setattr(engine, 'EMBY_LIB_CACHE_FILE', tmp_path / 'emby_library_with_tmdb.json')
        monkeypatch.setattr(engine, 'MANUAL_DONE_FILE', tmp_path / 'manual_done.json')
        _state.save(tmp_path / 'emby_library_with_tmdb.json', {
            'ts': time.time(), 'series': [{
                'id': 's1', 'series_ids': ['s1'], 'name': '使徒行者', 'have_eps': 31,
                'total_episodes': 31, 'tmdb_id': '100',
                'tmdb_info': {'match_status': 'missing', 'tmdb_total': 60, 'diff': -29}
            }], 'movies': []
        })
        _state.save(tmp_path / 'manual_done.json', {'s1': {'name': '使徒行者', 'ts': time.time()}})
        snap = engine.unified_health()
        assert snap['stats']['aligned'] == 1
        assert snap['stats']['missing'] == 0
        assert snap['stats']['manual_done'] == 1

    def test_ingest_identity_union_for_same_series_name(self, monkeypatch):
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.0000000Z')
        items = {
            'Movie': [
                {'Name': 'A', 'Path': '/media/local/A.strm', 'DateCreated': now, 'ProviderIds': {'Tmdb': '10'}, 'ProductionYear': 2020, 'Genres': []},
                {'Name': 'A', 'Path': '/media/share/A.strm', 'DateCreated': now, 'ProviderIds': {'Tmdb': '10'}, 'ProductionYear': 2020, 'Genres': []},
            ],
            'Episode': [
                {'Name': 'E1', 'SeriesName': '使徒行者', 'Path': '/media/local/1.strm', 'DateCreated': now, 'SeriesId': 'local', 'ParentIndexNumber': 1, 'IndexNumber': 1, 'Genres': []},
                {'Name': 'E1', 'SeriesName': '使徒行者', 'Path': '/media/share/1.strm', 'DateCreated': now, 'SeriesId': 'share', 'ParentIndexNumber': 1, 'IndexNumber': 1, 'Genres': []},
                {'Name': 'E2', 'SeriesName': '使徒行者', 'Path': '/media/share/2.strm', 'DateCreated': now, 'SeriesId': 'share', 'ParentIndexNumber': 1, 'IndexNumber': 2, 'Genres': []},
            ],
        }
        monkeypatch.setattr(engine, 'emby_request', lambda path, params=None, **kw: {'Items': items.get(params.get('IncludeItemTypes'), []), 'TotalRecordCount': len(items.get(params.get('IncludeItemTypes'), []))})
        out = engine._fetch_ingest(24)
        assert out['stats']['movies'] == 1
        assert out['stats']['series'] == 1
        assert out['stats']['episodes'] == 2


def test_daily_consistency_snapshot_exposes_shared_rule_signature(monkeypatch, tmp_path):
    import app.engine as engine
    monkeypatch.setattr(engine, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(engine, 'INGEST_CACHE_FILE', tmp_path / 'ingest_cache.json')
    monkeypatch.setattr(engine, 'GOV_LATEST_FILE', tmp_path / 'gov_latest.json')
    monkeypatch.setattr(engine, 'load_library_snapshot', lambda max_age=None, background_refresh=True: {
        'ts': 100.0, 'stats': {'series': 2, 'movies': 1}, 'episode_total': 12,
        'top_missing': [{'name': 'A', 'missing': 3}], 'series': [], 'movies': []
    })
    monkeypatch.setattr(engine._cfg, 'load_config', lambda: {
        'strategy_decision': 'quality_first', 'strategy_multi_season_protect': 'compare',
        'strategy_tie_keep_local': '0', 'strategy_exempt_keywords': '收藏',
        'strategy_special_action': 'compare', 'ingest_quiet_minutes': '15',
        'ingest_interval_min': '5', 'ingest_enabled': '1'
    })
    monkeypatch.setattr(engine._cfg, 'get_strategy', lambda: {'decision': 'quality_first'})
    snap = engine.daily_consistency_snapshot(False)
    assert len(snap['rule_sig']) == 16
    assert snap['library']['episode_total'] == 12
    assert snap['ingest']['stats'] == {}


def test_ingest_refill_auto_refreshes_mapping_cache_without_full_tmdb_scan(monkeypatch, tmp_path):
    """补齐新集后只重算受影响剧集，不要求重新执行整库 TMDB 对照。"""
    monkeypatch.setattr(engine, 'EMBY_LIB_CACHE_FILE', tmp_path / 'emby_library_with_tmdb.json')
    monkeypatch.setattr(engine, 'LIBRARY_SNAPSHOT_FILE', tmp_path / 'library_snapshot.json')

    cached = {
        'ts': 100.0,
        'series': [{
            'id': 'series-1',
            'series_ids': ['series-1'],
            'name': '大汉天子',
            'have_eps': 9,
            'total_episodes': 9,
            'tmdb_id': '123',
            'tmdb_info': {'match_status': 'missing', 'tmdb_total': 10, 'diff': -1},
        }],
        'movies': [],
        'stats': {'total_series': 1, 'missing': 1, 'aligned': 0,
                  'extra': 0, 'ongoing': 0, 'unmatched': 0, 'no_tmdb': 0},
    }
    _state.save(engine.EMBY_LIB_CACHE_FILE, cached)

    calls = []
    import morning
    monkeypatch.setattr(morning, '_live_series_episodes',
                        lambda sid: calls.append(sid) or [
                            {'Id': 'e1', 'Path': '/x/S01E01.strm', 'ParentIndexNumber': 1, 'IndexNumber': 1},
                            {'Id': 'e2', 'Path': '/x/S01E02.strm', 'ParentIndexNumber': 1, 'IndexNumber': 2},
                        ])
    monkeypatch.setattr(morning, '_resync_series_entry',
                        lambda entry, live: entry.update(
                            have_eps=10, total_episodes=10,
                            tmdb_info={'match_status': 'aligned', 'tmdb_total': 10, 'diff': 0},
                            complete=True))
    monkeypatch.setattr(morning, '_patch_all_caches', lambda patch: None)
    monkeypatch.setattr(morning, 'save_library_snapshot', lambda snap: None)

    out = engine.refresh_mapping_cache_after_ingest({
        'ok': True,
        'episodes_raw': [{'series_id': 'series-1', 'episode': 10}],
    })

    saved = _state.read(engine.EMBY_LIB_CACHE_FILE)
    assert out['status'] == 'updated'
    assert out['affected'] == 1 and out['updated'] == 1
    assert saved['series'][0]['tmdb_info']['match_status'] == 'aligned'
    assert saved['series'][0]['have_eps'] == 10
    assert saved['stats']['missing'] == 0 and saved['stats']['aligned'] == 1
    assert calls == ['series-1']
