"""Real jobs/files/receipts; Telegram and Emby transports are controlled."""
import copy
from pathlib import Path
from unittest.mock import Mock
import pytest
from app import tg
from app.cloud import jobs, notifications, workflow, strm, store, settings, recognition, emby_library as emby
from app.cloud.errors import CloudError
from dev.tests.test_cloud_workbench import cloud
from dev.tests.test_cloud_linkage import connection
from dev.tests.test_share_library import shared, preview


def enable(monkeypatch):
    settings.save({'notify_enabled': True, 'notify_strm_details': True})
    calls = []
    monkeypatch.setattr(tg, 'notify_telegram_card', lambda text, image='': calls.append(('card', text)) or True)
    monkeypatch.setattr(tg, 'notify_telegram', lambda text: calls.append(('text', text)) or True)
    return calls


def test_poster_follows_verified_strm_then_precedes_summary_without_emby(cloud, monkeypatch):
    calls = enable(monkeypatch)
    generate = strm.generate
    def publish(*args, **kwargs):
        assert not calls
        assert cloud.remote.info('4')['parent_id'] == cloud.remote.info('5')['parent_id'] != '1'
        return generate(*args, **kwargs)
    monkeypatch.setattr(strm, 'generate', publish)
    plan = workflow.plan(cloud.id, '1', '2')
    jid = workflow.apply(plan['id'], True)
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'success'
    assert [c[0] for c in calls] == ['card', 'text', 'text']
    assert '115 整理完成' in calls[0][1] and 'Emby' not in calls[0][1]
    assert 'STRM 更新：1 个' in calls[1][1] and '文件明细' in calls[2][1]
    jobs.notify(jid, 'organize', 'success', '任务完成')
    assert len(calls) == 3, 're-inspecting the result must not replay any delivery'


def test_episode_group_card_waits_for_all_verified_outputs(cloud, monkeypatch):
    calls = enable(monkeypatch)
    original = workflow.plan(cloud.id, '1', '2')
    with store.db() as con:
        import json
        plan = json.loads(con.execute('SELECT payload FROM cloud_plans WHERE id=?', (original['id'],)).fetchone()[0])
        template = plan['items'][0]
        plan['items'] = []
        for episode in (1, 2, 5):
            row = copy.deepcopy(template)
            fid = str(80 + episode)
            row['source'] = {**row['source'], 'id': fid, 'name': 'Show.S01E'+str(episode)+'.mkv', 'size': 1024**3}
            cloud.remote.files[fid] = copy.deepcopy(row['source'])
            row['followers'] = []
            row['media'].update(kind='tv', title='<Season>', season=1, episode=episode, directory='剧集/欧美剧集/Show/Season 1', name='Show.S01E'+str(episode)+'.mkv')
            plan['items'].append(row)
        con.execute('UPDATE cloud_plans SET payload=? WHERE id=?', (store.encode(plan), original['id']))
    generated = []
    generate = strm.generate
    def publish(*args, **kwargs):
        generated.append(len([c for c in calls if c[0] == 'card']))
        return generate(*args, **kwargs)
    monkeypatch.setattr(strm, 'generate', publish)
    jid = workflow.apply(original['id'], True)
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'success'
    assert generated == [0, 0, 0]
    cards = [c[1] for c in calls if c[0] == 'card']
    assert len(cards) == 1
    assert all(v in cards[0] for v in ('&lt;Season&gt;', 'E01-E02, E05', '文件：3 个', '3.00 GB', 'TMDB 44', '剧集/欧美剧集'))


def test_failed_companion_never_claims_organize_complete(cloud, monkeypatch):
    calls = enable(monkeypatch)
    plan = workflow.plan(cloud.id, '1', '2')
    relocate = workflow.relocate
    def move(provider, jid, row, target, name):
        if row['id'] == '5':
            raise CloudError('附属文件失败', 409)
        return relocate(provider, jid, row, target, name)
    monkeypatch.setattr(workflow, 'relocate', move)
    jid = workflow.apply(plan['id'], True)
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'needs_review'
    assert not [c for c in calls if c[0] == 'card']
    assert not list(cloud.root.rglob('*.strm'))


@pytest.mark.parametrize('failure', ['unknown', 'interrupted'])
def test_claimed_delivery_is_never_replayed(cloud, failure):
    jid = jobs.enqueue('organize', {})
    def ambiguous():
        with store.db() as con:
            assert con.execute('SELECT state FROM cloud_steps WHERE job_id=?', (jid,)).fetchone()[0] == 'dispatched'
        raise TimeoutError('secret must not be logged')
    assert not notifications.deliver(jid, 'poster:test', ambiguous)
    if failure == 'interrupted':
        with store.db() as con:
            con.execute("UPDATE cloud_steps SET state='dispatched' WHERE job_id=?", (jid,))
    send = Mock(return_value=True)
    assert not notifications.deliver(jid, 'poster:test', send)
    send.assert_not_called()


def test_partial_group_stop_only_reports_verified_subset(cloud, monkeypatch):
    calls = enable(monkeypatch)
    media = recognition.recognize('Movie.2024.1080p.mkv')
    jid = jobs.enqueue('organize', {})
    posters = notifications.BatchPosters(jid, [{'media': media}, {'media': media}])
    posters.verified(0, cloud.remote.info('4'), media)
    assert not calls
    posters.flush();posters.flush()
    assert len(calls) == 1 and '仅完成 1/2' in calls[0][1]


@pytest.mark.parametrize('organize,strm_notice,expected', [(True,False,['card']), (False,True,['text','text'])])
def test_organizer_and_strm_switches_are_independent(cloud, monkeypatch, organize, strm_notice, expected):
    calls = enable(monkeypatch)
    settings.save({'notify_organize': organize, 'notify_strm': strm_notice})
    jid = workflow.apply(workflow.plan(cloud.id, '1', '2')['id'], True)
    jobs.execute(jid)
    assert store.job(jid)['result']['generated']
    assert [c[0] for c in calls] == expected


def test_deferred_strm_waits_for_write_before_one_organizer_poster(cloud, monkeypatch):
    calls = enable(monkeypatch)
    settings.save({'strm_delay_seconds': 60})
    jid = workflow.apply(workflow.plan(cloud.id, '1', '2')['id'], True)
    jobs.execute(jid)
    assert not calls and not list(cloud.root.rglob('*.strm'))
    result = store.job(jid)['result']
    assert len(result['deferred']) == 1
    with store.db() as con:
        due = con.execute('SELECT job_id FROM cloud_job_due WHERE origin_job=?', (jid,)).fetchone()[0]
        con.execute('UPDATE cloud_job_due SET due=0 WHERE job_id=?', (due,))
    jobs.execute(due)
    assert store.job(due)['result']['generated']
    assert len([c for c in calls if c[0] == 'card']) == 1


def test_share_card_only_after_write_and_before_summary_without_cloud_mutation(shared, monkeypatch):
    calls = enable(monkeypatch)
    def card(text, image=''):
        assert list(shared.share_root.rglob('*.strm'))
        assert not calls
        calls.append(('card', text))
        return True
    monkeypatch.setattr(tg, 'notify_telegram_card', card)
    plan = preview(shared)
    assert not calls, 'previews never notify'
    from app.cloud import share_library
    jid = share_library.apply(plan['id'], True)
    jobs.execute(jid)
    assert store.job(jid)['state'] == 'success'
    assert '分享 STRM 已生成' in calls[0][1] and '免转存' in calls[0][1]
    assert [c[0] for c in calls] == ['card', 'text', 'text']
    assert not shared.remote.writes


def test_emby_confirmation_is_web_only_even_if_legacy_notify_enabled(cloud, monkeypatch):
    connection(cloud)
    calls = enable(monkeypatch)
    monkeypatch.setattr(emby, 'request', lambda *a, **kw: {})
    monkeypatch.setattr(emby, 'confirmed_item', lambda *a: {'Id': 'confirmed'})
    jid = workflow.apply(workflow.plan(cloud.id, '1', '2')['id'], True)
    jobs.execute(jid)
    count = len(calls)
    emby.poll_batch();emby.poll_batch()
    assert emby.records(cloud.id)[0]['state'] == 'confirmed'
    assert len(calls) == count and sum(c[0] == 'card' for c in calls) == 1
