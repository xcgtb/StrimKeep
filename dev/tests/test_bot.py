# -*- coding: utf-8 -*-
"""
bot.py 测试：Telegram 白名单鉴权（_is_allowed）。

这是 Telegram 输入直接进来的那一层，必须保证：
  - 没配置白名单时默认拒绝所有人（v4.4 修的那个 bug，防止回归）
  - 配置了白名单后，user_id 或 chat_id 命中其一即可
  - 没命中的 id 要被拒绝
"""
from app import state_store as _state
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_test_bot_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['TG_ALLOWED_USERS'] = ''  # 白名单从 config.json 读，不从这个环境变量的默认值读

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import bot  # noqa: E402
import config  # noqa: E402


class TestIsAllowed:
    def test_default_deny_when_no_allowlist_configured(self):
        config.save_config({'telegram_allowed_users': ''})
        assert bot._is_allowed('111', '222') is False
        assert bot._is_allowed(None, None) is False

    def test_allows_matching_user_id(self):
        config.save_config({'telegram_allowed_users': '111,222'})
        assert bot._is_allowed('111', '999') is True

    def test_allows_matching_chat_id(self):
        config.save_config({'telegram_allowed_users': '111,222'})
        assert bot._is_allowed('999', '222') is True

    def test_rejects_id_not_in_allowlist(self):
        config.save_config({'telegram_allowed_users': '111,222'})
        assert bot._is_allowed('333', '444') is False

    def test_ignores_blank_entries_in_allowlist(self):
        config.save_config({'telegram_allowed_users': '111,, ,222,'})
        assert bot._is_allowed('111', None) is True
        assert bot._is_allowed('222', None) is True
        assert bot._is_allowed('', None) is False


# ═══════════════════ 线程生命周期 / 结果渲染 / 计划选择 ═══════════════════
import json as _json  # noqa: E402
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402


def _alive(name):
    return sum(1 for t in _threading.enumerate() if t.name == name and t.is_alive())


class TestRestartDoesNotLeakPollers:
    def test_restart_keeps_exactly_one_poll_loop(self):
        """以前 restart() 只 sleep(0.5) 就重新 start()，旧线程还卡在长轮询里，
        每在网页保存一次配置就多出一个 getUpdates 循环，同一条命令可能被处理两次"""
        calls = []

        def fake_api(token, method, params=None, timeout=35):
            calls.append(method)
            if method == 'getUpdates':
                _time.sleep(0.3)  # 模拟长轮询
                return {'ok': True, 'result': []}
            if method == 'getMe':
                return {'ok': True, 'result': {'username': 'test_bot'}}
            return {'ok': True, 'result': {}}

        orig_api, orig_cfg = bot._api, bot.load_config
        bot._api = fake_api
        bot.load_config = lambda: {'telegram_bot_token': 'x', 'telegram_allowed_users': ''}
        try:
            bot.start()
            for _ in range(3):
                bot.restart()
            _time.sleep(2.5)
            assert _alive('tg-bot') == 1
            assert _alive('tg-cleanup') == 1
            assert 'getUpdates' in calls
        finally:
            bot.stop()
            _time.sleep(2.5)
            bot._api, bot.load_config = orig_api, orig_cfg
        assert _alive('tg-bot') == 0


class TestCleanResultRendering:
    def test_skipped_count_is_int(self):
        """引擎返回的 skipped 是跳过项数（int），以前按列表 len() 会在清理完成后抛错"""
        sent = []
        orig = bot._edit, bot._send
        bot._edit = lambda token, chat_id, mid, text, kb=None, **k: sent.append(text) or {'ok': True}
        bot._send = lambda token, chat_id, text, *a, **k: sent.append(text)
        try:
            bot._notify_result('tok', 1, 'clean',
                               {'status': 'success', 'loc_cnt': 2, 'sh_cnt': 1,
                                'refreshed': True, 'skipped': 3}, message_id=9)
        finally:
            bot._edit, bot._send = orig
        assert len(sent) == 1
        assert '跳过' in sent[0] and '3' in sent[0]


class TestLatestPendingPlan:
    def test_picks_newest_pending_not_newest_file(self):
        """计划被标记 done/expired 时会重写文件、mtime 变新；/clean 不能因此选中已执行过的计划"""
        engine = bot.engine
        engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
        for data in engine.db_list_plans(10000):
            engine.db_delete_plan(data['id'])
        now = _time.time()

        def write(pid, state, ts):
            _state.save(engine.STATE_DIR / f'plan_{pid}.json', {
                'schema_version': 2, 'id': pid, 'ts': ts, 'state': state,
                'stats': {}, 'actions': []})
        write('aaaa1111', 'pending', now - 100)
        write('cccc3333', 'pending', now - engine.PLAN_TTL - 60)  # 已过期
        write('bbbb2222', 'done', now - 50)                        # 最后写入、mtime 最新
        assert bot._latest_pending_plan_id() == 'aaaa1111'
        for data in engine.db_list_plans(10000):
            engine.db_delete_plan(data['id'])
        assert bot._latest_pending_plan_id() is None


class TestBotUsesSharedTaskBus:
    def _patch(self):
        out = []
        saved = (bot._edit, bot._send, bot.load_config)
        bot._edit = lambda token, chat_id, mid, text, kb=None, **k: out.append(text) or {'ok': True}
        bot._send = lambda token, chat_id, text, *a, **k: out.append(text) or {}
        bot.load_config = lambda: {'telegram_bot_token': 'tok'}
        return out, saved

    def _restore(self, saved):
        bot._edit, bot._send, bot.load_config = saved

    def test_bot_scan_is_rejected_while_web_task_runs(self):
        """以前 Bot 自己起线程，Web 正在扫描/清理时 Bot 照样能再起一个"""
        out, saved = self._patch()
        gate = _threading.Event()
        web = bot.tasks.manager.spawn('inter_clean', gate.wait, source='web')
        try:
            bot._spawn_and_watch('check', lambda a: {'status': 'success'}, 1, message_id=5)
            assert out and '已有任务 [inter_clean]' in out[-1]
        finally:
            gate.set(); web.done.wait(5)
            self._restore(saved)

    def test_bot_scan_registers_as_exclusive_task_and_reports(self):
        out, saved = self._patch()
        gate = _threading.Event()
        done = _threading.Event()
        orig_notify = bot._notify_result

        def notify(token, chat_id, kind, res, message_id=None, user_msg_id=None):
            out.append((kind, res)); done.set()
        bot._notify_result = notify
        try:
            bot._spawn_and_watch('check', lambda a: gate.wait(5) and {'status': 'success'}, 1, message_id=5)
            cur = bot.tasks.manager.running()
            assert cur is not None and (cur.kind, cur.source) == ('inter_check', 'bot')
            gate.set()
            assert done.wait(5)
            assert out[-1] == ('check', {'status': 'success'})
        finally:
            gate.set()
            bot._notify_result = orig_notify
            self._restore(saved)


# ═══════════════════ 卡片生命周期 / 命令删除 / 状态 ═══════════════════
def _write_plan(pid, state='pending', age=0, rule_sig=None):
    engine = bot.engine
    engine.STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state.save(engine.STATE_DIR / f'plan_{pid}.json', {
        'schema_version': 2, 'id': pid, 'ts': _time.time() - age, 'state': state,
        'rule_sig': rule_sig if rule_sig is not None else bot.engine._current_rule_snapshot()['sig'],
        'stats': {}, 'actions': [{'action_id': 'a', 'text': 'x'}]})


class _Rec:
    """记录 Bot 对 Telegram 的所有出站调用"""
    def __init__(self):
        self.edits, self.sends, self.deletes, self.answers = [], [], [], []
        self.saved = (bot._edit, bot._send, bot._try_delete, bot._answer_callback, bot.load_config)
        bot._edit = lambda token, chat_id, mid, text, kb=None, ttl=None: (
            self.edits.append((mid, text, kb, ttl)) or {'ok': True})
        bot._send = lambda token, chat_id, text, kb=None, *a, **k: (
            self.sends.append((text, kb)) or {'ok': True})
        bot._try_delete = lambda token, chat_id, mid: self.deletes.append(mid) or True
        bot._answer_callback = lambda token, cb_id, text='', alert=False: self.answers.append((text, alert))
        bot.load_config = lambda: {'telegram_bot_token': 'tok', 'telegram_allowed_users': '1'}

    def restore(self):
        bot._edit, bot._send, bot._try_delete, bot._answer_callback, bot.load_config = self.saved


def _cb(data, mid=77):
    return {'id': 'cb1', 'data': data, 'from': {'id': 1},
            'message': {'message_id': mid, 'chat': {'id': 1}}}


def _buttons(kb):
    return [b['callback_data'] for row in (kb or {}).get('inline_keyboard', []) for b in row]


class TestPlanCardLifecycle:
    def test_expired_plan_clean_do_shows_rescan_instead_of_hanging(self):
        """核心回归：计划过期后点「确认执行」，卡片必须就地变成「已过期 + 重新扫描」，
        不能停在「正在执行清理...」"""
        _write_plan('eeee0001', age=bot.engine.PLAN_TTL + 60)
        r = _Rec()
        try:
            bot._handle_callback('tok', _cb('clean_do:eeee0001'))
        finally:
            r.restore()
        assert r.edits, '没有编辑卡片'
        mid, text, kb, ttl = r.edits[-1]
        assert mid == 77 and '过期' in text
        assert 'cmd:check' in _buttons(kb) and 'dismiss' in _buttons(kb)
        assert not any('正在执行' in e[1] for e in r.edits)
        # 并且落盘成 expired，不会再被当成可执行计划
        assert bot.engine.load_plan('eeee0001')['state'] == 'expired'

    def test_clean_ask_on_used_plan_is_blocked(self):
        _write_plan('eeee0002', state='done')
        r = _Rec()
        try:
            bot._handle_callback('tok', _cb('clean_ask:eeee0002'))
        finally:
            r.restore()
        assert '执行过' in r.edits[-1][1]
        assert 'clean_do:eeee0002' not in _buttons(r.edits[-1][2])

    def test_missing_plan_view_list_shows_card_not_dead_alert(self):
        r = _Rec()
        try:
            bot._handle_callback('tok', _cb('plan:00000000'))
        finally:
            r.restore()
        assert r.edits and 'cmd:check' in _buttons(r.edits[-1][2])
        assert r.answers == [('', False)]   # 只应答一次（以前第二次 answer 永远发不出去）

    def test_valid_plan_goes_through_to_confirm(self):
        _write_plan('eeee0003')
        r = _Rec()
        try:
            bot._handle_callback('tok', _cb('clean_ask:eeee0003'))
        finally:
            r.restore()
        assert 'clean_do:eeee0003' in _buttons(r.edits[-1][2])

    def test_engine_error_edits_card_not_new_message(self):
        """引擎在执行时才发现计划过期（clean_do 与核验之间越过 TTL）：同样要编辑原卡片"""
        r = _Rec()
        try:
            bot._notify_result('tok', 1, 'clean',
                               {'status': 'error', 'code': 'plan_expired', 'message': '清理计划已过期'},
                               message_id=55)
        finally:
            r.restore()
        assert r.edits and r.edits[-1][0] == 55
        assert 'cmd:check' in _buttons(r.edits[-1][2])
        assert not r.sends

    def test_task_exception_edits_card(self):
        r = _Rec()
        gate = _threading.Event()
        try:
            bot._spawn_and_watch('check', lambda a: (_ for _ in ()).throw(RuntimeError('boom')),
                                 1, message_id=66)
            _time.sleep(0.5)
        finally:
            r.restore()
        assert r.edits and r.edits[-1][0] == 66 and 'boom' in r.edits[-1][1]
        assert 'dismiss' in _buttons(r.edits[-1][2])

    def test_plan_ttl_left_tracks_expiry(self):
        _write_plan('eeee0004', age=100)
        left = bot._plan_ttl_left('eeee0004')
        assert bot.engine.PLAN_TTL - 110 <= left <= bot.engine.PLAN_TTL - 90
        assert bot._plan_ttl_left('ffffffff', default=123) == 123


class TestDismiss:
    def test_dismiss_deletes_card(self):
        r = _Rec()
        try:
            bot._handle_callback('tok', _cb('dismiss', mid=88))
        finally:
            r.restore()
        assert r.deletes == [88]

    def test_dismiss_falls_back_to_edit_when_delete_fails(self):
        r = _Rec()
        bot._try_delete = lambda *a: False
        try:
            bot._handle_callback('tok', _cb('dismiss', mid=89))
        finally:
            r.restore()
        assert r.edits and r.edits[-1][0] == 89 and r.edits[-1][2] == {'inline_keyboard': []}


class TestUserMessageAutoDelete:
    def test_every_command_schedules_user_message_deletion(self):
        """以前只有 /menu 等少数分支会删用户的命令消息；/check /search 词 /gap 都不删"""
        r = _Rec()
        orig_dispatch = bot._dispatch
        bot._dispatch = lambda *a, **k: None
        with bot._DELETE_LOCK:
            bot._delete_queue[:] = []
        try:
            for i, text in enumerate(['/check', '/search 金色', '/gap', '/ingest force', '/whatever', 'hi']):
                bot._handle_command('tok', {'chat': {'id': 1}, 'from': {'id': 1},
                                            'text': text, 'message_id': 1000 + i})
            with bot._DELETE_LOCK:
                scheduled = {m: d for (d, c, m) in bot._delete_queue}
        finally:
            bot._dispatch = orig_dispatch
            r.restore()
        for i in range(6):
            assert 1000 + i in scheduled, f'命令 #{i} 没有登记删除'
            assert scheduled[1000 + i] - _time.time() <= bot.USER_MSG_TTL + 1


class TestDeleteQueue:
    def test_cleanup_does_not_hold_lock_during_network_delete(self):
        """以前 deleteMessage 的网络调用在 _DELETE_LOCK 里；网络一卡，_send / status() 全部被堵死"""
        started, release = _threading.Event(), _threading.Event()
        saved = (bot._try_delete, bot.load_config)

        def slow_delete(token, chat_id, mid):
            started.set(); release.wait(5); return True
        bot._try_delete = slow_delete
        bot.load_config = lambda: {'telegram_bot_token': 'tok'}
        with bot._DELETE_LOCK:
            bot._delete_queue[:] = [(_time.time() - 1, 1, 4242)]
        bot._state['running'] = True
        bot._state['gen'] += 1
        gen = bot._state['gen']
        t = _threading.Thread(target=bot._cleanup_loop, args=(gen,), daemon=True)
        t.start()
        try:
            assert started.wait(5), '清理线程没开始删除'
            t0 = _time.time()
            bot.status()                              # 若锁被占，这里会卡 5 秒
            bot._schedule_delete(1, 999, 100)
            assert _time.time() - t0 < 1.0
        finally:
            release.set()
            bot._state['running'] = False
            t.join(5)
            bot._try_delete, bot.load_config = saved
            with bot._DELETE_LOCK:
                bot._delete_queue[:] = []

    def test_queue_survives_restart(self):
        with bot._DELETE_LOCK:
            bot._delete_queue[:] = []
        bot._schedule_delete(1, 31337, 500)
        with bot._DELETE_LOCK:
            bot._delete_queue[:] = []          # 模拟进程重启：内存清空
        bot._load_queue()
        with bot._DELETE_LOCK:
            assert any(m == 31337 for (_, _, m) in bot._delete_queue)
            bot._delete_queue[:] = []


class TestBotStatus:
    def _set(self, **kw):
        saved = {k: bot._state[k] for k in ('running', 'last_poll', 'fail_count', 'last_error', 'thread')}
        bot._state.update(kw)
        return saved

    def test_error_cleared_after_successful_poll(self):
        """以前 last_error 出现后永不清除，Web 上「The read operation timed out」一直挂着"""
        t = _threading.Thread(target=lambda: _time.sleep(3), daemon=True); t.start()
        saved = self._set(running=True, thread=t, last_poll=_time.time() - 5, fail_count=0, last_error='')
        try:
            bot._note_poll_failure(TimeoutError('The read operation timed out'))
            assert bot.status()['last_error'] and bot.status()['state'] == 'degraded'
            bot._state.update(last_poll=_time.time(), fail_count=0, last_error='')
            st = bot.status()
            assert st['last_error'] == '' and st['fail_count'] == 0
        finally:
            bot._state.update(saved)

    def test_state_levels(self):
        t = _threading.Thread(target=lambda: _time.sleep(3), daemon=True); t.start()
        saved = self._set(running=True, thread=t, last_poll=_time.time() - 5, fail_count=0, last_error='')
        try:
            assert bot.status()['state'] == 'ok'
            bot._state['last_poll'] = _time.time() - 100
            assert bot.status()['state'] == 'degraded'
            bot._state['last_poll'] = _time.time() - 400
            assert bot.status()['state'] == 'down'
            bot._state['running'] = False
            assert bot.status()['state'] == 'stopped'
        finally:
            bot._state.update(saved)

    def test_timeout_detection(self):
        assert bot._is_timeout(TimeoutError('x'))
        assert bot._is_timeout(Exception('The read operation timed out'))
        assert not bot._is_timeout(Exception('HTTP Error 409: Conflict'))
