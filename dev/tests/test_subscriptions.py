# -*- coding: utf-8 -*-
"""
追更订阅（check_subscriptions）状态机测试。

覆盖两类历史缺陷：
  旧口径（集号比较）：
  - 多季剧只拿「最新一集的集号」去比 TMDB 全部季总集数 → 每次都报缺几十集
  - 连载季 TMDB episode_count 含未播集 → 在更的剧永远「缺集」
  - 按 DateCreated 取最新集 → 洗版 / 重新入库的旧集让 latest_ep 倒退，下次又当新集播报
  - 同一集在本地 + 分享两库各一份 → 不能重复计数
  新口径（集合差集 + 发送成功才记账）：
  - 只看最大集号 → 中间新入库的集（E16/E17/E18）完全不通知
  - 跳集入库（17、18 进、16 缺）→ 不能只说「更新到 E18」，要报缺集 E16
  - 补齐缺集 → 要发「已补齐」通知，且不重复播报已通知过的集
  - 发送失败却已落盘 → 吞通知；发送成功前落盘 → 重复通知

Emby / TMDB 都用假实现替换，不联网；运行方式同 dev/tests/test_engine.py。
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_subs_test_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import engine  # noqa: E402
from app import state_store as _state

TMDB_ID = '9999'


def _ep(season, episode, created='2026-01-01T00:00:00Z', **extra):
    d = {'ParentIndexNumber': season, 'IndexNumber': episode, 'DateCreated': created}
    d.update(extra)
    return d


def _full(seasons: dict):
    """{季: 集数} → 每季 1..N 的 Emby 集列表"""
    return [_ep(s, e) for s, n in seasons.items() for e in range(1, n + 1)]


def _tmdb_info(seasons: dict, last=None, status='Returning Series'):
    """构造 /tv/{id} 响应；last=(季, 集) 对应 last_episode_to_air"""
    info = {
        'name': '测试剧', 'status': status,
        'seasons': [{'season_number': 0, 'episode_count': 5}] +
                   [{'season_number': s, 'episode_count': n, 'air_date': '2020-01-01'}
                    for s, n in seasons.items()],
    }
    if last:
        info['last_episode_to_air'] = {'season_number': last[0], 'episode_number': last[1]}
    return info


class _Env:
    """替换 emby_request / Tmdb / notify_telegram / 订阅配置 / 状态文件，结束时全部还原"""

    def __init__(self):
        self.episodes = []
        self.series_items = [{'Id': 'S1', 'Name': '测试剧', 'ProviderIds': {'Tmdb': TMDB_ID}}]
        self.tmdb = None
        self.ep_calls = []
        # notify_telegram 的假实现：记录发出的消息，ok 控制返回成功/失败
        self.sent = []
        self.notify_ok = True

    def _emby(self, path, params=None, method='GET', timeout=15):
        params = params or {}
        if params.get('IncludeItemTypes') == 'Series':
            return {'Items': list(self.series_items)}
        if params.get('IncludeItemTypes') == 'Episode':
            self.ep_calls.append(dict(params))
            return {'Items': list(self.episodes)}
        return {}

    def _notify(self, text, chat_id=None):
        self.sent.append(text)
        return self.notify_ok

    def __enter__(self):
        env = self

        class FakeTmdb:
            def get(self, path, ttl=0, **kw):
                return env.tmdb

            def save(self):
                pass

        self._orig = (engine.emby_request, engine.Tmdb, engine.notify_telegram,
                      engine.SUB_STATE_FILE, engine._cfg.get_subscriptions())
        self._orig_cfg = {k: engine._cfg.load_config().get(k)
                          for k in ('subscribe_enabled', 'subscribe_check_tmdb')}
        engine.emby_request = self._emby
        engine.Tmdb = FakeTmdb
        engine.notify_telegram = self._notify
        engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
        engine.SUB_STATE_FILE = engine.STATE_DIR / 'subscriptions_state_test.json'
        _state.save(engine.SUB_STATE_FILE, {})
        cfg = engine._cfg.load_config()
        cfg['subscribe_enabled'] = '1'
        cfg['subscribe_check_tmdb'] = '1'
        engine._cfg.save_config(cfg)
        engine._cfg.set_subscriptions([{'id': 'sub1', 'tmdb_id': TMDB_ID, 'name': '测试剧', 'enabled': True}])
        return self

    def __exit__(self, *exc):
        _state.save(engine.SUB_STATE_FILE, {})
        (engine.emby_request, engine.Tmdb, engine.notify_telegram,
         engine.SUB_STATE_FILE, subs) = self._orig
        cfg = engine._cfg.load_config()
        for k, v in self._orig_cfg.items():
            if v is None: cfg.pop(k, None)
            else: cfg[k] = v
        engine._cfg.save_config(cfg)
        engine._cfg.set_subscriptions(subs)
        return False

    def run(self, send_notify=False):
        """跑一轮检查，返回第一个 update（无变化时返回 None）"""
        r = engine.check_subscriptions(send_notify=send_notify)
        return r['updates'][0] if r['updates'] else None

    def updates(self, send_notify=False):
        return engine.check_subscriptions(send_notify=send_notify)['updates']

    def state(self):
        return engine._load_sub_state().get('sub1') or {}

    def seed_state(self, d):
        """直接写入订阅状态（用于构造旧结构 / 前置基线）"""
        engine._save_sub_state({'sub1': dict(d)})


class TestMissingEpisodes:
    def test_multi_season_complete_no_missing(self):
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 10, 3: 10})
            env.tmdb = _tmdb_info({1: 10, 2: 10, 3: 10}, last=(3, 10), status='Ended')
            assert env.run() is None
            st = env.state()
            assert st['latest_ep'] == 'S03E10'
            assert st['tmdb_total'] == 30

    def test_ongoing_season_unaired_not_missing(self):
        # S03 共 10 集但只播到第 5 集，Emby 也有到 S03E05 → 不缺
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 10, 3: 5})
            env.tmdb = _tmdb_info({1: 10, 2: 10, 3: 10}, last=(3, 5))
            assert env.run() is None
            assert env.state()['tmdb_total'] == 25

    def test_future_season_ignored(self):
        # TMDB 已公布 S04 但尚未开播
        with _Env() as env:
            env.episodes = _full({1: 8})
            env.tmdb = _tmdb_info({1: 8, 2: 8}, last=(1, 8))
            assert env.run() is None

    def test_gap_in_earlier_season_counted(self):
        with _Env() as env:
            eps = _full({1: 10, 2: 10, 3: 5})
            eps = [e for e in eps if (e['ParentIndexNumber'], e['IndexNumber']) not in ((1, 3), (2, 7))]
            env.episodes = eps
            env.tmdb = _tmdb_info({1: 10, 2: 10, 3: 10}, last=(3, 5))
            u = env.run()
            assert u is not None and u['new_eps'] == []
            assert u['missing_eps'] == ['S01E03', 'S02E07']
            assert u['newly_missing'] == ['S01E03', 'S02E07']

    def test_existing_gap_not_reported_again(self):
        # 缺集只报一次，下一轮不再重复播报同一缺口
        with _Env() as env:
            env.episodes = [e for e in _full({1: 10}) if e['IndexNumber'] != 4]
            env.tmdb = _tmdb_info({1: 10}, last=(1, 10))
            assert env.run()['newly_missing'] == ['S01E04']
            assert env.run() is None

    def test_extras_do_not_offset_gap(self):
        # Emby 多出一集未播集（TMDB 未计入）不能抵扣真实缺口；S00 特别篇也不计
        with _Env() as env:
            eps = [e for e in _full({1: 10}) if e['IndexNumber'] != 4]
            eps += [_ep(1, 11), _ep(0, 1), _ep(0, 2)]
            env.episodes = eps
            env.tmdb = _tmdb_info({1: 12}, last=(1, 10))
            u = env.run()
            assert u['missing_eps'] == ['S01E04']

    def test_duplicates_across_libraries_not_double_counted(self):
        # 本地 + 分享各一份 S01E01-E05，缺 E06
        with _Env() as env:
            env.episodes = _full({1: 5}) + _full({1: 5})
            env.tmdb = _tmdb_info({1: 6}, last=(1, 6))
            u = env.run()
            assert u['missing_eps'] == ['S01E06']

    def test_multi_episode_file_covers_range(self):
        with _Env() as env:
            env.episodes = [_ep(1, 1, IndexNumberEnd=2), _ep(1, 3)]
            env.tmdb = _tmdb_info({1: 3}, last=(1, 3))
            assert env.run() is None

    def test_no_last_episode_to_air_fallback(self):
        # 没有 last_episode_to_air → 各季 episode_count 全部计入（旧口径）
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 3})
            env.tmdb = _tmdb_info({1: 10, 2: 10})
            u = env.run()
            assert u['missing_eps'] == [f'S02E{e:02d}' for e in range(4, 11)]

    def test_episode_query_fetches_all(self):
        with _Env() as env:
            env.episodes = _full({1: 3})
            env.tmdb = _tmdb_info({1: 3}, last=(1, 3))
            env.run()
            p = env.ep_calls[0]
            assert p['ParentId'] == 'S1' and int(p['Limit']) >= 5000
            assert 'SortBy' not in p

    def test_local_and_share_series_are_unioned(self):
        with _Env() as env:
            env.series_items = [
                {'Id': 'LOCAL', 'Name': '测试剧', 'ProviderIds': {'Tmdb': TMDB_ID}, 'Path': '/local/测试剧'},
                {'Id': 'SHARE', 'Name': '测试剧', 'ProviderIds': {'Tmdb': TMDB_ID}, 'Path': '/share/测试剧'},
            ]

            def fake_emby(path, params=None, method='GET', timeout=15):
                params = params or {}
                if params.get('IncludeItemTypes') == 'Series':
                    return {'Items': list(env.series_items)}
                if params.get('IncludeItemTypes') == 'Episode':
                    sid = params.get('ParentId')
                    return {'Items': _full({1: 5}) if sid == 'LOCAL' else _full({1: 6})}
                return {}
            old = engine.emby_request
            engine.emby_request = fake_emby
            try:
                env.tmdb = _tmdb_info({1: 6}, last=(1, 6), status='Ended')
                u = env.run()
                assert u is None
                assert env.state()['latest_ep'] == 'S01E06'
            finally:
                engine.emby_request = old


class TestNewEpisode:
    def test_readded_old_episode_not_announced(self):
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 5})
            env.tmdb = _tmdb_info({1: 10, 2: 10}, last=(2, 5))
            env.run()
            assert env.state()['latest_ep'] == 'S02E05'
            # 洗版：S01E03 重新入库，DateCreated 最新
            env.episodes = [e for e in env.episodes if (e['ParentIndexNumber'], e['IndexNumber']) != (1, 3)]
            env.episodes.append(_ep(1, 3, created='2026-09-29T00:00:00Z'))
            assert env.run() is None
            assert env.state()['latest_ep'] == 'S02E05'

    def test_latest_never_moves_backwards(self):
        # 最新一集被删 / 下架：latest_ep 保持不退（notified_episodes 记着它，
        # 不需要靠现网重新出现来维持），也不会误报缺集
        with _Env() as env:
            env.episodes = _full({1: 6})
            env.tmdb = _tmdb_info({1: 6}, last=(1, 6))
            env.run()
            assert env.state()['latest_ep'] == 'S01E06'
            env.episodes = _full({1: 5})
            assert env.run() is None
            assert env.state()['latest_ep'] == 'S01E06'   # 不退
            env.episodes = _full({1: 6})
            assert env.run() is None                      # 重新入库不重复播报

    def test_new_episode_announced(self):
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 5})
            env.tmdb = _tmdb_info({1: 10, 2: 10}, last=(2, 5))
            assert env.run() is None
            env.episodes = _full({1: 10, 2: 6})
            env.tmdb = _tmdb_info({1: 10, 2: 10}, last=(2, 6))
            u = env.run()
            assert u['new_eps'] == ['S02E06']
            assert u['newly_missing'] == [] and u['refilled'] == []
            assert env.state()['latest_ep'] == 'S02E06'

    def test_check_records_per_show_rows_in_audit(self):
        """追更检查把每部订阅的结果（新增/缺集/补齐集号）写进执行记录 extra，供前端点开查看。"""
        from app import logger as _lg
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 5})
            env.tmdb = _tmdb_info({1: 10, 2: 10}, last=(2, 5))
            env.run()
            env.episodes = _full({1: 10, 2: 7})
            env.tmdb = _tmdb_info({1: 10, 2: 10}, last=(2, 7))
            env.run()
            # Per-show facts live in the current subscription status, not archive cards.
            from app import runtime_logs
            status = engine.load_subscription_check_status()
            assert status['checked'] == 1 and status['updates'] == 1
            row = status['rows'][0]
            assert row['changed'] is True and row['new'] == 'S02E06–E07'
            assert row['latest_ep'] == 'S02E07'
            assert any('订阅检查' in r['message'] for r in runtime_logs.read(limit=50)['logs'])
            assert all(r['category'] != '追更' for r in _lg.read_archives(100))

    def test_new_season_announced(self):
        with _Env() as env:
            env.episodes = _full({1: 10})
            env.tmdb = _tmdb_info({1: 10, 2: 8}, last=(1, 10))
            env.run()
            env.episodes = _full({1: 10}) + [_ep(2, 1)]
            env.tmdb = _tmdb_info({1: 10, 2: 8}, last=(2, 1))
            u = env.run()
            assert u['new_eps'] == ['S02E01']
            assert env.state()['latest_ep'] == 'S02E01'


class TestBatchGapRefillStateMachine:
    """集合差集状态机：批量入库 / 跳集报警 / 补齐通知 / 发送成功才记账"""

    def _baseline_15(self, env, total=20):
        """建立「库中已有 S01E01–E15，且已通知到位」的基线"""
        env.episodes = _full({1: 15})
        env.tmdb = _tmdb_info({1: total}, last=(1, 15))
        assert env.run() is None
        assert env.state()['latest_ep'] == 'S01E15'

    def test_batch_new_episodes_notified(self):
        # 库里原本 E15，连入 E16/E17/E18 → 一次通知三集（区间 S01E16–E18）
        with _Env() as env:
            self._baseline_15(env)
            env.episodes = _full({1: 18})
            env.tmdb = _tmdb_info({1: 20}, last=(1, 18))
            u = env.run()
            assert u['new_eps'] == ['S01E16', 'S01E17', 'S01E18']
            assert engine._fmt_ep_ranges(engine._keys_to_eps(u['new_eps'])) == 'S01E16–E18'
            assert u['newly_missing'] == [] and u['refilled'] == []
            assert env.state()['latest_ep'] == 'S01E18'

    def test_skip_gap_notified(self):
        # 入 E17/E18、缺中间的 E16 → 必须同时报「新增」和「缺集 E16」
        with _Env() as env:
            self._baseline_15(env)
            env.episodes = _full({1: 15}) + [_ep(1, 17), _ep(1, 18)]
            env.tmdb = _tmdb_info({1: 20}, last=(1, 18))
            u = env.run()
            assert u['new_eps'] == ['S01E17', 'S01E18']
            assert u['newly_missing'] == ['S01E16']
            assert engine._fmt_ep_ranges(engine._keys_to_eps(u['newly_missing'])) == 'S01E16'

    def test_missing_not_repeated(self):
        # 缺集只报一次，下一轮不重复
        with _Env() as env:
            self._baseline_15(env)
            env.episodes = _full({1: 15}) + [_ep(1, 17), _ep(1, 18)]
            env.tmdb = _tmdb_info({1: 20}, last=(1, 18))
            assert env.run()['newly_missing'] == ['S01E16']
            assert env.run() is None

    def test_refill_notified(self):
        # 补齐 E16 → 同时报「已补齐 E16」与「新增 E16」；且不重复播报已通知过的 E17/E18
        with _Env() as env:
            self._baseline_15(env)
            env.episodes = _full({1: 15}) + [_ep(1, 17), _ep(1, 18)]
            env.tmdb = _tmdb_info({1: 20}, last=(1, 18))
            env.run()
            env.episodes = _full({1: 18})
            env.tmdb = _tmdb_info({1: 20}, last=(1, 18))
            u = env.run()
            assert u['refilled'] == ['S01E16']
            assert u['new_eps'] == ['S01E16']      # 只有新到的 E16，E17/E18 不重复
            assert u['newly_missing'] == []
            assert env.state()['latest_ep'] == 'S01E18'
            assert env.run() is None

    def test_send_failure_keeps_state_and_retries(self):
        # 发送失败 → 状态不推进；下一次发送成功仍能通知同一批集（不吞通知）
        with _Env() as env:
            self._baseline_15(env)
            before = env.state()['notified_episodes'][:]
            env.episodes = _full({1: 18})
            env.tmdb = _tmdb_info({1: 20}, last=(1, 18))
            env.notify_ok = False
            us = env.updates(send_notify=True)
            assert us and us[0]['new_eps'] == ['S01E16', 'S01E17', 'S01E18']
            assert env.state()['notified_episodes'] == before    # 未记账
            assert env.state()['latest_ep'] == 'S01E15'
            # 重试成功 → 同一批集再次通知，然后才记账
            env.notify_ok = True
            us = env.updates(send_notify=True)
            assert us and us[0]['new_eps'] == ['S01E16', 'S01E17', 'S01E18']
            assert env.state()['latest_ep'] == 'S01E18'
            # 再检查无变化，不重复通知
            assert env.run(send_notify=True) is None

    def test_notify_message_contains_ranges(self):
        # Telegram 文案用区间表示，缺集提示精确到集号
        with _Env() as env:
            self._baseline_15(env)
            env.episodes = _full({1: 15}) + [_ep(1, 17), _ep(1, 18)]
            env.tmdb = _tmdb_info({1: 20}, last=(1, 18))
            env.updates(send_notify=True)
            msg = env.sent[-1]
            assert '新增入库' in msg and 'S01E17–E18' in msg
            assert '缺集' in msg and 'S01E16' in msg

    def test_send_success_then_no_repeat(self):
        with _Env() as env:
            self._baseline_15(env)
            env.episodes = _full({1: 16})
            env.tmdb = _tmdb_info({1: 20}, last=(1, 16))
            assert env.updates(send_notify=True)[0]['new_eps'] == ['S01E16']
            assert env.updates(send_notify=True) == []

    def test_legacy_latest_ep_seeding_no_spam(self):
        # 免迁移：老结构只有 latest_ep=S01E15，库中已有 E01–E18
        # → E01–E15 视为已通知，只播报 E16–E18
        with _Env() as env:
            env.seed_state({'tmdb_id': TMDB_ID, 'name': '测试剧', 'latest_ep': 'S01E15',
                            'tmdb_total': 20, 'tmdb_status': 'Returning Series',
                            'updated_at': '2026-09-01 00:00:00'})
            env.episodes = _full({1: 18})
            env.tmdb = _tmdb_info({1: 20}, last=(1, 18))
            u = env.run()
            assert u is not None
            assert u['new_eps'] == ['S01E16', 'S01E17', 'S01E18']
            assert env.state()['latest_ep'] == 'S01E18'

    def test_new_subscription_first_check_is_quiet(self):
        # 全新订阅（无任何状态）且库中已有 10 集 → 首次检查不刷屏
        with _Env() as env:
            env.episodes = _full({1: 10})
            env.tmdb = _tmdb_info({1: 20}, last=(1, 10))
            assert env.run() is None
            assert len(env.state()['notified_episodes']) == 10

    def test_latest_ep_synced_even_without_change(self):
        # 无变化时也要把 latest_ep / tmdb_total 写回，供前端与 bot 展示
        with _Env() as env:
            env.episodes = _full({1: 10, 2: 10, 3: 10})
            env.tmdb = _tmdb_info({1: 10, 2: 10, 3: 10}, last=(3, 10), status='Ended')
            assert env.run() is None
            st = env.state()
            assert st['latest_ep'] == 'S03E10'
            assert st['tmdb_total'] == 30
            assert st['notified_episodes'][0] == 'S01E01'

    def test_no_tmdb_still_detects_new_eps(self):
        # 关闭 TMDB 对照时仍能做集合差集，只是不判缺集
        with _Env() as env:
            cfg = engine._cfg.load_config()
            cfg['subscribe_check_tmdb'] = '0'
            engine._cfg.save_config(cfg)
            self._baseline_15(env)
            env.episodes = _full({1: 18})
            env.tmdb = None
            u = env.run()
            assert u['new_eps'] == ['S01E16', 'S01E17', 'S01E18']
            assert u['newly_missing'] == []


class TestEpRangeFormatting:
    def test_single_episode(self):
        assert engine._fmt_ep_ranges([(1, 16)]) == 'S01E16'

    def test_contiguous_range(self):
        assert engine._fmt_ep_ranges([(1, 16), (1, 17), (1, 18)]) == 'S01E16–E18'

    def test_cross_season_not_merged(self):
        # S01E10 后面不是 S02E01 的延续，必须断开
        assert engine._fmt_ep_ranges([(1, 10), (2, 1), (2, 2)]) == 'S01E10, S02E01–E02'

    def test_gap_not_merged(self):
        assert engine._fmt_ep_ranges([(1, 16), (1, 18)]) == 'S01E16, S01E18'

    def test_unsorted_input(self):
        assert engine._fmt_ep_ranges([(1, 18), (1, 16), (1, 17)]) == 'S01E16–E18'

    def test_empty(self):
        assert engine._fmt_ep_ranges([]) == ''

    def test_key_roundtrip(self):
        assert engine._parse_ep_key(engine._ep_key(2, 7)) == (2, 7)
        assert engine._parse_ep_key('garbage') is None
        assert engine._eps_to_keys([(1, 1), (1, 2)]) == {'S01E01', 'S01E02'}


class TestExploreLiveEps:
    """探索页实时集数：绕过 30 分钟统一快照，直接问 Emby 这部剧现在有哪些集"""

    def test_reflects_newly_ingested_episodes_immediately(self):
        state = {'eps': list(range(1, 16))}

        def fake(path, params=None, method='GET', timeout=15):
            p = params or {}
            if p.get('IncludeItemTypes') == 'Series':
                return {'Items': [{'Id': 'SR1', 'Name': '我不是大师'}]}
            if p.get('IncludeItemTypes') == 'Episode':
                return {'Items': [
                    {'ParentIndexNumber': 1, 'IndexNumber': e,
                     'Path': f'/media/local/剧集/我不是大师/S01/x.S01E{e:02d}.strm'}
                    for e in state['eps']]}

            return {}

        old = engine.emby_request
        engine.emby_request = fake
        engine.EMBY_PATHS = engine.emby_path_map('/media/local', '/media/share')
        try:
            r = engine._emby_series_live_eps('12345', 'SR1', use_cache=False)
            assert (r['have_eps'], r['local_eps']) == (15, 15)
            # Emby 立刻入库 E16/E17/E18 → 下一次实时查询马上反映，无需等快照过期
            state['eps'] = list(range(1, 19))
            r = engine._emby_series_live_eps('12345', 'SR1', use_cache=False)
            assert (r['have_eps'], r['local_eps']) == (18, 18)
        finally:
            engine.emby_request = old

    def test_splits_local_and_share_by_path(self):
        def fake(path, params=None, method='GET', timeout=15):
            p = params or {}
            if p.get('IncludeItemTypes') == 'Series':
                return {'Items': [{'Id': 'SR1'}]}
            if p.get('IncludeItemTypes') == 'Episode':
                items = []
                for e in range(1, 19):
                    root = '/media/local' if e <= 15 else '/media/share'
                    items.append({'ParentIndexNumber': 1, 'IndexNumber': e,
                                  'Path': f'{root}/剧集/剧/S01/x.S01E{e:02d}.strm'})
                return {'Items': items}
            return {}

        old = engine.emby_request
        engine.emby_request = fake
        engine.EMBY_PATHS = engine.emby_path_map('/media/local', '/media/share')
        try:
            r = engine._emby_series_live_eps('12345', 'SR1', use_cache=False)
            assert r['have_eps'] == 18
            assert r['local_eps'] == 15
            assert r['share_eps'] == 3
        finally:
            engine.emby_request = old

    def test_ghost_entries_outside_library_roots_are_ignored(self):
        # CD2 已删源、Emby 还没刷掉的幽灵条目不应计入入库集数
        def fake(path, params=None, method='GET', timeout=15):
            p = params or {}
            if p.get('IncludeItemTypes') == 'Series':
                return {'Items': [{'Id': 'SR1'}]}
            if p.get('IncludeItemTypes') == 'Episode':
                return {'Items': [
                    {'ParentIndexNumber': 1, 'IndexNumber': 1,
                     'Path': '/media/local/剧集/剧/S01/x.S01E01.strm'},
                    {'ParentIndexNumber': 1, 'IndexNumber': 2,
                     'Path': '/other/ghost/deleted.S01E02.strm'}]}
            return {}

        old = engine.emby_request
        engine.emby_request = fake
        engine.EMBY_PATHS = engine.emby_path_map('/media/local', '/media/share')
        try:
            r = engine._emby_series_live_eps('12345', None, use_cache=False)
            assert r['have_eps'] == 1
        finally:
            engine.emby_request = old

    def test_micro_cache_absorbs_rapid_refresh(self):
        calls = {'n': 0}

        def fake(path, params=None, method='GET', timeout=15):
            p = params or {}
            if p.get('IncludeItemTypes') == 'Series':
                calls['n'] += 1
                return {'Items': [{'Id': 'CACHE_HIT_SR'}]}
            if p.get('IncludeItemTypes') == 'Episode':
                return {'Items': [{'ParentIndexNumber': 1, 'IndexNumber': 1,
                                   'Path': '/media/local/剧集/剧/S01/x.S01E01.strm'}]}
            return {}

        old = engine.emby_request
        engine.emby_request = fake
        engine.EMBY_PATHS = engine.emby_path_map('/media/local', '/media/share')
        try:
            engine._emby_series_live_eps('CACHE_HIT_TMDB', 'CACHE_HIT_SR')
            first = calls['n']
            engine._emby_series_live_eps('CACHE_HIT_TMDB', 'CACHE_HIT_SR')
            assert calls['n'] == first, '15 秒内应命中微缓存，不重复打 Emby'
        finally:
            engine.emby_request = old
            with engine._live_eps_lock:
                engine._live_eps_cache.pop('CACHE_HIT_TMDB', None)

    def test_emby_error_returns_none(self):
        def boom(path, params=None, method='GET', timeout=15):
            raise RuntimeError('Emby 挂了')

        old = engine.emby_request
        engine.emby_request = boom
        engine.EMBY_PATHS = engine.emby_path_map('/media/local', '/media/share')
        try:
            # 异常不外抛；系列查不到时返回 None，由调用方回退快照
            assert engine._emby_series_live_eps('ERR_TMDB', None, use_cache=False) is None
        finally:
            engine.emby_request = old
