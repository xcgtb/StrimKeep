"""迭代 1 回归：任务族合并、限流自动重排、离线三态、启动建表、Webhook 令牌。

这些用例对应本轮修复的四个"卡死/丢工作"缺陷，任何一个回退都会让它们失败。
"""
import asyncio
import json
import time
from pathlib import Path

import httpx

from strimkeep.offline_api import OfflinePostProcess
from strimkeep.organizer import Organizer
from strimkeep.store import Store
from test_organize import MemoryCloud


ORGANIZE = {'job_kind': 'organize', 'account_id': '1', 'source_cids': ['100'], 'scheduled': True}
ORGANIZE_OTHER = {**ORGANIZE, 'source_cids': ['200']}
TRANSFER = {'job_kind': 'transfer', 'account_id': '1', 'target_cid': '300'}
SYNC = {'job_kind': 'strm_sync', 'account_id': '1', 'source_cids': ['400']}


def make_engine(tmp_path):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                    'playback_base': 'http://nas:9000'})
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    return store, Organizer(store, MemoryCloud(store), http, tmp_path / 'strm'), http


def insert_job(store, state, config, stage='排队', updated=None):
    now = time.time() if updated is None else updated
    with store.db:
        return store.db.execute(
            "INSERT INTO organize_jobs(state,config,created,updated,stage) VALUES(?,?,?,?,?)",
            (state, json.dumps(config), now, now, stage)).lastrowid


def test_login_ready_keeps_one_job_per_family(tmp_path):
    store, engine, http = make_engine(tmp_path)
    duplicate = insert_job(store, 'waiting_login', ORGANIZE)
    same_family = insert_job(store, 'waiting_login', ORGANIZE)
    other_dir = insert_job(store, 'waiting_login', ORGANIZE_OTHER)
    transfer = insert_job(store, 'waiting_login', TRANSFER)
    sync = insert_job(store, 'waiting_login', SYNC)

    engine.login_ready()

    states = {row['id']: row['state'] for row in
              store.db.execute('SELECT id,state FROM organize_jobs').fetchall()}
    # 同族重复入队只保留最早一条，其余取消；不同种类/不同目录必须各自继续。
    assert states[duplicate] == 'queued'
    assert states[same_family] == 'cancelled'
    assert states[other_dir] == 'queued'
    assert states[transfer] == 'queued'
    assert states[sync] == 'queued'
    cancelled = store.db.execute(
        "SELECT notify_state FROM organize_jobs WHERE state='cancelled'").fetchall()
    assert [row['notify_state'] for row in cancelled] == ['not_needed']
    asyncio.run(http.aclose())
    store.close()


def test_login_ready_ignores_paused_jobs(tmp_path):
    store, engine, http = make_engine(tmp_path)
    paused = insert_job(store, 'paused', ORGANIZE)
    engine.login_ready()
    assert engine.job(paused)['state'] == 'paused'
    asyncio.run(http.aclose())
    store.close()


def test_rate_limited_job_requeues_after_cooldown(tmp_path):
    store, engine, http = make_engine(tmp_path)
    now = time.time()
    jid = insert_job(store, 'needs_review', ORGANIZE,
                     stage='115 限流冷却，稍后自动重排', updated=now - 1000)

    assert engine.auto_retry_stalled(now) == [jid]

    job = engine.job(jid)
    assert job['state'] == 'queued'
    assert '自动重试' in job['stage']
    assert '限流' not in job['stage']          # 换掉关键词，避免同一轮反复命中
    assert json.loads(job['config'])['auto_retry_count'] == 1
    assert engine.auto_retry_stalled(now) == []   # 已入队后不会被再次选中
    asyncio.run(http.aclose())
    store.close()


def test_auto_retry_skips_manual_review_busy_lane_and_cap(tmp_path):
    store, engine, http = make_engine(tmp_path)
    now = time.time()
    manual = insert_job(store, 'needs_review', ORGANIZE, stage='等待核查', updated=now - 1000)
    busy = insert_job(store, 'queued', ORGANIZE)
    waiting = insert_job(store, 'needs_review', ORGANIZE,
                         stage='115 限流冷却，稍后自动重排', updated=now - 1000)
    capped = insert_job(store, 'needs_review', SYNC,
                        stage='115 限流冷却，稍后自动重排', updated=now - 100000)
    with store.db:
        config = json.loads(engine.job(capped)['config'])
        config['auto_retry_count'] = engine.AUTO_RETRY_MAX
        store.db.execute('UPDATE organize_jobs SET config=? WHERE id=?', (json.dumps(config), capped))

    assert engine.auto_retry_stalled(now) == []
    assert engine.job(manual)['state'] == 'needs_review'    # 人工核查不自动重排
    assert engine.job(busy)['state'] == 'queued'
    assert engine.job(waiting)['state'] == 'needs_review'   # 同族已有排队任务
    assert engine.job(capped)['state'] == 'needs_review'    # 重试次数用尽
    asyncio.run(http.aclose())
    store.close()


def test_auto_retry_respects_backoff(tmp_path):
    store, engine, http = make_engine(tmp_path)
    now = time.time()
    jid = insert_job(store, 'needs_review', ORGANIZE,
                     stage='115 限流冷却，稍后自动重排', updated=now - 400)
    with store.db:
        config = json.loads(engine.job(jid)['config'])
        config['auto_retry_count'] = 3          # 第 4 次失败后要等 300*2^3=2400 秒
        store.db.execute('UPDATE organize_jobs SET config=? WHERE id=?', (json.dumps(config), jid))
    assert engine.auto_retry_stalled(now) == []
    assert engine.job(jid)['state'] == 'needs_review'
    asyncio.run(http.aclose())
    store.close()


# --------------------------------------------------------------- 离线三态

def test_offline_state_failure_beats_percent_complete():
    assert OfflinePostProcess._state({'status': '任务失败', 'percent': 100}) == 'failed'
    assert OfflinePostProcess._state({'status': '已完成', 'percent': 100}) == 'done'
    assert OfflinePostProcess._state({'status': '未完成', 'percent': 40}) == 'running'
    assert OfflinePostProcess._state({'status': '', 'percent': '35%'}) == 'running'
    # 失败不得被当成完成，否则 check() 会把它当「下载进行中」一直挂着
    assert OfflinePostProcess._done({'status': '失败', 'percent': 100}) is False


def offline_engine(tmp_path, tasks):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')

    class Cloud:
        async def offline_tasks(self, page, size):
            return {'data': tasks}, None

        async def children(self, cid):
            return []

    class Engine:
        pass

    engine = Engine()
    engine.store, engine.cloud = store, Cloud()
    return store, OfflinePostProcess(engine)


def submit_row(store, urls):
    with store.db:
        return store.db.execute(
            "INSERT INTO offline_submissions(urls,target_cid,target_label,before_ids,"
            "submitted_at,state,message,organize) VALUES(?,?,?,?,?,'submitted','',1)",
            (json.dumps(urls), '1000', '下载', json.dumps([]), time.time())).lastrowid


def test_offline_failed_task_surfaces_as_needs_review(tmp_path):
    store, post = offline_engine(tmp_path, [
        {'id': 'abc123', 'name': '测试资源', 'percent': 0, 'status': '下载失败', 'size': 1, 'url': ''}])
    sid = submit_row(store, ['magnet:?xt=urn:btih:abc123'])
    row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()

    asyncio.run(post.check(row))

    saved = store.db.execute('SELECT state,message FROM offline_submissions WHERE id=?',
                             (sid,)).fetchone()
    assert saved['state'] == 'needs_review', dict(saved)
    assert '失败' in saved['message']
    store.close()


def test_offline_running_task_stays_submitted(tmp_path):
    store, post = offline_engine(tmp_path, [
        {'id': 'abc123', 'name': '测试资源', 'percent': 40, 'status': '下载中', 'size': 1, 'url': ''}])
    sid = submit_row(store, ['magnet:?xt=urn:btih:abc123'])
    row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()

    asyncio.run(post.check(row))

    saved = store.db.execute('SELECT state,message FROM offline_submissions WHERE id=?',
                             (sid,)).fetchone()
    assert saved['state'] == 'submitted', dict(saved)
    assert '下载进行中' in saved['message']
    store.close()


# ------------------------------------------------------- 启动建表与安全默认值

def test_store_retires_derived_tables_at_startup(tmp_path):
    """0.9.54：output_stats / media_info_outputs 是纯派生表，启动时应当被丢掉。

    STRM 只有几百字节，摘要缓存省不下什么；sidecar 归属由文件内容（version+
    playback_id）决定。旧库升级后这两张表必须消失，避免又被当成"必须对账的登记簿"。
    """
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    names = {row[0] for row in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'output_stats' not in names and 'media_info_outputs' not in names
    assert store.meta('schema_pruned_0954'), '退场标记没写进 meta'
    store.close()


def test_store_indexes_the_hot_lookups(tmp_path):
    """0.9.55：organize_items(file_id) 与 organize_jobs(state) 必须有索引。

    生产库实测：`file_id=?` 25.36ms/次 → 0.01ms（2351×）、`state=?` 2.23ms → 0.01ms（200×）。
    分享产线在 per-file 循环里查 file_id，缺一个索引就能把事件循环堵成秒级
    （现场「事件循环被阻塞」中位 1.28 秒、最大 18.2 秒）。
    """
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    names = {row[0] for row in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert 'organize_items_file' in names and 'organize_jobs_state' in names
    # 不只看索引存在，还要确认查询计划真的用上它（唯一键是 (job_id,file_id)，
    # 第二列不会被自动复用，历史上就是这么漏掉的）。
    plan = ' '.join(str(row[-1]) for row in store.db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM organize_items WHERE file_id='x'"))
    assert 'organize_items_file' in plan, plan
    plan = ' '.join(str(row[-1]) for row in store.db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM organize_jobs WHERE state='running'"))
    assert 'organize_jobs_state' in plan, plan
    store.close()


def test_webhook_token_reads_sync_config_and_legacy(tmp_path):
    """写入端在 sync_config，读取端必须一致；同时兼容早期写顶层的安装。"""
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    assert store.webhook_token() == ''
    store.set_many({'sync_config': {'webhook_token': 'from-sync-config'}})
    assert store.webhook_token() == 'from-sync-config'
    store.set_many({'webhook_token': 'legacy-top-level'})
    assert store.webhook_token() == 'from-sync-config'    # 新口径优先
    store.set_many({'sync_config': {}})
    assert store.webhook_token() == 'legacy-top-level'    # 旧安装仍可用
    store.close()


ROOT = Path(__file__).resolve().parents[2]


def test_webhook_token_is_generated_server_side():
    backend = (ROOT / 'backend/strimkeep/phase5_api.py').read_text(encoding='utf-8')
    frontend = (ROOT / 'frontend/src/Sync.vue').read_text(encoding='utf-8')
    assert 'secrets.token_urlsafe(24)' in backend
    assert 'Math.random' not in frontend


def test_compose_has_no_hardcoded_password():
    compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    assert 'ADMIN_PASSWORD=${ADMIN_PASSWORD:' in compose
    assert 'zzx1234567890' not in compose


def test_deploy_config_moves_password_into_env(tmp_path):
    """更新流程不能把解析后的口令写回 compose：应搬进 .env 只留变量引用。"""
    import importlib.util

    path = ROOT / 'scripts/deploy_config.py'
    spec = importlib.util.spec_from_file_location('sk_deploy_config', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    env = tmp_path / '.env'
    env.write_text("ADMIN_USERNAME='admin'\n", encoding='utf-8')
    assert module.env_reference('ADMIN_PASSWORD', 'secret-pass-1234', str(env)) == '${ADMIN_PASSWORD}'
    text = env.read_text(encoding='utf-8')
    assert 'ADMIN_PASSWORD=secret-pass-1234' in text
    assert 'ADMIN_USERNAME' in text                     # 其它键保留
    module.env_reference('ADMIN_PASSWORD', 'another-pass-5678', str(env))
    text = env.read_text(encoding='utf-8')
    assert text.count('ADMIN_PASSWORD=') == 1 and 'another-pass-5678' in text
    # 没有 .env 路径、或口令含单引号时退回字面值，不卡住更新
    assert module.env_reference('ADMIN_PASSWORD', 'plain-value', None) == 'plain-value'
    assert module.env_reference('ADMIN_PASSWORD', "a'b", str(env)) == "a'b"

    model = {'name': 'x', 'services': {'strimkeep': {'environment': {'ADMIN_PASSWORD': 'p@ss-1234'},
                                                     'volumes': []}}}
    rendered = module.convert(model, str(env))
    assert 'ADMIN_PASSWORD=${ADMIN_PASSWORD}' in rendered
    assert 'p@ss-1234' not in rendered

def test_offline_unmatched_old_submission_never_claims_later_directory_items(tmp_path):
    store, post = offline_engine(tmp_path, [])
    async def children(_): return [{'id':'1001','name':'other.mkv','is_dir':False}]
    post.engine.cloud.children = children
    sid = submit_row(store, ['magnet:?xt=urn:btih:abc123'])
    with store.db: store.db.execute('UPDATE offline_submissions SET submitted_at=0 WHERE id=?', (sid,))
    row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
    asyncio.run(post.check(row))
    row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
    assert row['state'] == 'needs_review' and row['organize_job_id'] is None
    assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 0
    store.close()


def test_offline_partial_match_and_missing_file_id_do_not_claim_other_items(tmp_path):
    async def run():
        store, post = offline_engine(tmp_path, [
            {'info_hash':'aaa', 'name':'other.mkv', 'percentDone':100},
        ])
        async def children(_): return [{'id':'1001','name':'other.mkv','is_dir':False}]
        post.engine.cloud.children = children
        for urls in [['magnet:?xt=urn:btih:aaa'],
                     ['magnet:?xt=urn:btih:aaa','magnet:?xt=urn:btih:bbb']]:
            sid = submit_row(store, urls)
            row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
            await post.check(row)
            saved = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
            assert saved['state'] == 'submitted' and saved['organize_job_id'] is None
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 0
        store.close()
    asyncio.run(run())
