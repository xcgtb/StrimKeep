"""0.7.13 的四类修订：集号口径、平台 ASCII、通知瘦身、登录检查。

这些是用户侧反馈的直接回归，断言的是「看着不乱的输出」而不是某个版本快照。
"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from strimkeep.cloud115 import CloudError
from strimkeep.media import naming, platform_of
from strimkeep.main import create_app
from strimkeep.notifications import Notifications
from strimkeep.store import Store

from test_app import FakeCloud


# --------------------------------------------------------------- 集号口径
def test_tv_episode_slot_is_always_chinese_number():
    """第 4 段固定「第N集」，TMDB 集名（含「第 22 集」这种）一律不进文件名。"""
    hint = {'media_type': 'tv', 'season': 1, 'episode': 22, 'end_episode': 22,
            'query': '嫁金钗', 'year': 2026, 'tmdb_id': 300146}
    details = {'title': '嫁金钗', 'first_air_date': '2026-10-16', 'id': 300146, 'seasons': []}
    options = {'classify_enabled': False}   # 分类与命名无关，这里只验集号段
    for episode_name in ('第 22 集', '凌晨1点13分', '你是谁？', None):
        episodes = [{'name': episode_name}] if episode_name else [{}]
        _, name, _, _ = naming(hint, details, episodes, {}, 'raw.mkv', options=options)
        assert '.S01E22.第22集.' in name, name


def test_merged_episodes_keep_both_chinese_numbers():
    hint = {'media_type': 'tv', 'season': 1, 'episode': 5, 'end_episode': 7,
            'query': '测试剧', 'year': 2026, 'tmdb_id': 1}
    details = {'title': '测试剧', 'first_air_date': '2026-01-01', 'id': 1, 'seasons': []}
    _, name, _, _ = naming(hint, details, [{'name': '合并集'}], {}, 'raw.mkv',
                           options={'classify_enabled': False})
    assert '.S01E05-E07.第5集-第7集.' in name, name


# ------------------------------------------------------------- 平台 ASCII
def test_chinese_platforms_are_ascii():
    assert platform_of('嫁金钗.2026.S01E22.第22集.2160p.优酷.WEB-DL.H.265.mkv') == 'YOUKU'
    assert platform_of('某剧.S01E01.2160p.爱奇艺.WEB-DL.H.265.mkv') == 'IQIYI'
    assert platform_of('某剧.S01E01.1080p.腾讯.WEB-DL.H.264.mkv') == 'TENCENT'
    assert platform_of('某番.S01E01.1080p.哔哩哔哩.WEB-DL.H.264.mkv') == 'BILIBILI'
    # 已经有英文写法的仍然命中
    assert platform_of('某剧.S01E01.2160p.WeTV.WEB-DL.H.265.mkv') == 'TENCENT'


# ------------------------------------------------------- 元数据统计与开关
def _store_with_metadata(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    with store.db:
        store.db.execute("INSERT INTO organize_jobs(id,state,config,created,updated) "
                         "VALUES(5,'completed','{}',0,0)")
        plans = {1: {'relative_path': '剧集/x.strm', 'metadata_files': ['a.nfo', 'poster.jpg']},
                 2: {'relative_path': '剧集/y.strm', 'metadata_files': ['b.nfo']}}
        for item_id, plan in plans.items():
            store.db.execute("INSERT INTO organize_items(id,job_id,file_id,original,plan) "
                             "VALUES(?,5,?, '{}', ?)",
                             (item_id, 'f%d' % item_id, json.dumps(plan)))
        store.db.execute('''CREATE TABLE IF NOT EXISTS native_media_info_queue(
            job_id INTEGER NOT NULL, item_id TEXT NOT NULL, target TEXT NOT NULL,
            connection TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0, next_due REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(job_id,item_id))''')
        store.db.execute("INSERT INTO native_media_info_queue(job_id,item_id,target,connection,state) "
                         "VALUES(5,'emby-id',?, 'y','done')", (json.dumps({'relative':'剧集/x.strm'}),))
    return store


def test_metadata_counts_only_counts_written_outputs(tmp_path):
    store = _store_with_metadata(tmp_path)
    notifications = Notifications(store, httpx.AsyncClient())
    # 海报不统计：剧集一集一张，会把明细刷乱
    assert notifications.metadata_counts(5, [1, 2]) == {'strm': 2, 'nfo': 2, 'nfo_show': 0, 'json': 1}
    asyncio.run(notifications.http.aclose())
    store.close()


def test_metadata_line_follows_category_toggle(tmp_path):
    store = _store_with_metadata(tmp_path)
    notifications = Notifications(store, httpx.AsyncClient())
    line = notifications.metadata_line(5, [1, 2])
    assert line == '🧩 文件：NFO 写入 2 · 媒体信息 1', line
    assert '海报' not in line

    store.set_many({'notification_config': {'telegram_enabled': True, 'categories': {'metadata': False}}})
    assert notifications.metadata_line(5, [1, 2]) == ''
    asyncio.run(notifications.http.aclose())
    store.close()


# ---------------------------------------------------- 取消不再推机器人
def test_cancelled_job_is_not_pushed_to_bot(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    with store.db:
        store.db.execute("INSERT INTO organize_jobs(id,state,config,created,updated,notify_state) "
                         "VALUES(99,'cancelled','{}',0,0,'queued')")
    notifications = Notifications(store, httpx.AsyncClient())
    asyncio.run(notifications._job({'id': 99, 'state': 'cancelled', 'stage': '', 'config': '{}'}))
    assert store.db.execute('SELECT count(*) FROM notifications').fetchone()[0] == 0
    assert store.db.execute('SELECT notify_state FROM organize_jobs WHERE id=99').fetchone()[0] == 'not_needed'
    asyncio.run(notifications.http.aclose())
    store.close()


# ------------------------------------------- 115 掉登录不踢出管理端
class LoggedOutCloud(FakeCloud):
    async def status(self):
        raise CloudError('请先扫码登录或保存 115 Cookie。', 'LOGIN_REQUIRED', 401)


def test_login_check_reports_state_without_logging_admin_out(tmp_path, monkeypatch):
    monkeypatch.setenv('ADMIN_USERNAME', 'admin')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password-1234')
    app = create_app(tmp_path / 'data', tmp_path / 'strm', LoggedOutCloud)
    with TestClient(app) as client:
        login = client.post('/api/v1/auth/login',
                            json={'username': 'admin', 'password': 'test-password-1234'})
        assert login.status_code == 200
        client.headers['X-CSRF-Token'] = login.json()['csrf']

        response = client.post('/api/v1/connections/115/test')
        assert response.status_code == 200, '115 未登录不能让管理端 401'
        assert response.json()['connected'] is False
        # 管理员会话必须还在
        assert client.get('/api/v1/auth/me').status_code == 200


# ------------------------------------- 目录清单混入别的条目时能自愈
def test_directory_listing_retries_when_platform_mixes_entries(tmp_path):
    """转存目录整理完只剩一张图，卡在「115 返回了其他目录的条目」上。
    只读操作重读一次即可，不该整批放弃清理。"""
    from strimkeep.cloud115 import Cloud115

    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1_A1; CID=test; SEID=test'})
    cloud = Cloud115(store)
    calls = {'n': 0}

    async def fake_list(cid, offset, limit):
        calls['n'] += 1
        if calls['n'] == 1:
            raise CloudError('115 返回了其他目录的条目，正在读取的目录暂不可用。',
                             'DIRECTORY_CHANGED', 409)
        return {'items': [{'id': '9', 'name': '字幕截图.png', 'is_dir': False,
                           'size': 4 * 1024 * 1024, 'pickcode': '', 'sha1': ''}],
                'total': 1, 'offset': 0, 'limit': limit, 'next_offset': 1,
                'has_more': False, 'path': [], 'raw_count': 1}

    cloud.list_files = fake_list
    rows, _ = asyncio.run(cloud._children('100'))
    assert calls['n'] == 2, '一次抖动应当自动重读'
    assert rows[0]['name'] == '字幕截图.png'
    asyncio.run(cloud.close())
    store.close()


def test_metadata_line_counts_show_nfo_and_distinguishes_no_write(tmp_path):
    store=_store_with_metadata(tmp_path);notifications=Notifications(store,httpx.AsyncClient())
    row=store.db.execute('SELECT plan FROM organize_items WHERE id=1').fetchone()
    plan=json.loads(row[0]);plan['metadata_files'].append('show/tvshow.nfo')
    with store.db:store.db.execute('UPDATE organize_items SET plan=? WHERE id=1',(json.dumps(plan),))
    assert 'NFO 写入 3（剧集级 1）' in notifications.metadata_line(5,[1,2])
    with store.db:
        for item_id in (1,2):
            row=store.db.execute('SELECT plan FROM organize_items WHERE id=?',(item_id,)).fetchone()
            value=json.loads(row[0]);value['metadata_files']=[]
            store.db.execute('UPDATE organize_items SET plan=? WHERE id=?',(json.dumps(value),item_id))
    assert 'NFO 本批未写入' in notifications.metadata_line(5,[1,2])
    asyncio.run(notifications.http.aclose());store.close()
