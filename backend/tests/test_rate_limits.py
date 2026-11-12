"""115 请求节奏：读写分档、限流识别与自动冷却。"""
import asyncio

import httpx
import pytest

from strimkeep.cloud115 import Cloud115, CloudError, classify_rejection, fatal
from strimkeep.organizer import Organizer, dumps
from strimkeep.store import Store
from test_organize import MemoryCloud, handler as tmdb_handler, good_probe

MEDIA = {'width': 3840, 'height': 2160, 'codec': 'hevc', 'fps': 24,
         'dynamic_range': 'SDR', 'bit_depth': 10, 'duration': '3000', 'audio': []}

CONFIG = {'source_cids': ['100'], 'archive_cid': '200', 'pending_cid': '300',
          'directory_labels': {}, 'retries': 0, 'retry_interval': 0, 'strm_delay': 0,
          'notify_emby': False, 'ffprobe_enabled': True, 'probe_prefetch': True,
          'probe_concurrency': 1, 'wash_enabled': False}


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_cloud(tmp_path, throttle=None):
    store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
    store.set_many({'cookie': 'UID=1; CID=t; SEID=t'})
    if throttle:
        store.set_many({'cloud_throttle': throttle})
    cloud = Cloud115(store)
    clock = FakeClock()
    cloud.clock = clock
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    cloud.sleep = fake_sleep
    return store, cloud, clock, sleeps


async def accepted(*args, **kwargs):
    return {'state': True}


def test_reads_and_writes_get_different_minimum_gaps(tmp_path):
    async def run():
        store, cloud, clock, _ = make_cloud(tmp_path)
        await cloud.call(accepted)
        assert cloud.next_request - clock() == pytest.approx(0.35)      # 读：350ms
        await cloud.call(accepted, _kind='write')
        assert cloud.next_request - clock() == pytest.approx(3.0)       # 写：3s
        store.close()
    asyncio.run(run())


def test_legacy_burst_settings_do_not_add_fixed_pause(tmp_path):
    async def run():
        store, cloud, clock, sleeps = make_cloud(tmp_path, {'burst': 15, 'pause_ms': 30000})
        for _ in range(48):
            await cloud.call(accepted, _kind='write')
        assert 30.0 not in sleeps
        assert sum(sleeps) == pytest.approx(47 * 3.0)
        assert cloud.next_request - clock() == pytest.approx(3.0)
        store.close()
    asyncio.run(run())


def test_rate_limit_text_trips_cooldown_and_rejects_immediately(tmp_path):
    async def run():
        store, cloud, clock, _ = make_cloud(tmp_path)
        calls = []
        async def denied(*args, **kwargs):
            calls.append(1)
            return {'state': False, 'errno': 0, 'error_msg': '操作过于频繁，请稍后再试'}

        with pytest.raises(CloudError) as exc:
            await cloud.call(denied)
        assert exc.value.code == 'RATE_LIMITED'
        assert cloud.throttle_scale == pytest.approx(2.0)               # 间隔翻倍
        assert cloud.rate_limit_until - clock() == pytest.approx(60)    # 首次冷却 60 秒

        with pytest.raises(CloudError) as again:
            await cloud.call(denied)
        assert again.value.code == 'RATE_LIMITED'
        assert len(calls) == 1                                          # 冷却期内一个请求都不发
        store.close()
    asyncio.run(run())


def test_interval_recovers_slowly_after_successes(tmp_path):
    async def run():
        store, cloud, clock, _ = make_cloud(tmp_path)
        async def denied(*args, **kwargs):
            return {'state': False, 'error_msg': '请求过多'}

        with pytest.raises(CloudError):
            await cloud.call(denied)
        assert cloud.throttle_scale == pytest.approx(2.0)

        clock.advance(400)                     # 冷却结束
        for _ in range(20):
            await cloud.call(accepted)
        assert cloud.throttle_scale == pytest.approx(1.8)               # 只退 10%
        store.close()
    asyncio.run(run())


def test_classify_rejection_keeps_business_errors_out_of_rate_limit():
    assert classify_rejection(0, '操作过于频繁') == 'RATE_LIMITED'
    assert classify_rejection(0, 'Too Many Requests') == 'RATE_LIMITED'
    assert classify_rejection(0, '错误的链接') == 'CLOUD_REJECTED'
    # 分享重复转存是独立语义：不能被折成限流，也不能折成普通拒绝。
    assert classify_rejection(0, '该文件已转存过', 'share') == 'ALREADY_SAVED'
    assert classify_rejection(0, '文件已保存', 'share') == 'ALREADY_SAVED'
    assert classify_rejection(0, '该文件已转存过', 'file') == 'CLOUD_REJECTED'
    assert classify_rejection(99, '请重新登录') == 'LOGIN_INVALID'


def test_batch_stops_instead_of_marking_files_failed(tmp_path):
    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        store.set_many({'cookie': 'UID=1; CID=t; SEID=t', 'tmdb_key': 'k',
                        'playback_base': 'https://play.example'})

        class LimitedCloud(MemoryCloud):
            async def move(self, file_id, parent_id):
                raise CloudError('操作过于频繁，请稍后再试', 'RATE_LIMITED', 503)

        cloud = LimitedCloud(store)
        http = httpx.AsyncClient(transport=httpx.MockTransport(tmdb_handler))
        engine = Organizer(store, cloud, http, tmp_path / 'strm', good_probe)
        cloud.add_video('1000', '测试电影 (2024).WEB-DL.mkv')
        now = __import__('time').time()
        with store.db:
            jid = store.db.execute("INSERT INTO organize_jobs(state,config,created,updated) VALUES('queued',?,?,?)",
                                   (dumps(CONFIG), now, now)).lastrowid

        await engine.run_job(jid)

        job = engine.job(jid)
        assert job['state'] == 'needs_review', dict(job)
        assert '限流' in job['stage'], job['stage']
        item = store.db.execute('SELECT state FROM organize_items WHERE job_id=?', (jid,)).fetchone()
        assert item['state'] != 'skipped'       # 不能把这个文件当成自己的问题记失败
        await http.aclose()
        store.close()
    asyncio.run(run())


def test_fatal_codes_are_the_ones_that_must_never_be_swallowed():
    assert fatal(CloudError('x', 'RATE_LIMITED', 503))
    assert fatal(CloudError('x', 'LOGIN_INVALID', 401))
    assert fatal(CloudError('x', 'LOGIN_REQUIRED', 409))
    assert not fatal(CloudError('x', 'CLOUD_REJECTED', 422))
    assert not fatal(CloudError('x'))            # 默认 CLOUD_UNAVAILABLE 仍按未知处理

def test_concurrent_jobs_keep_request_ledgers_isolated(tmp_path):
    async def run():
        store, cloud, clock, sleeps = make_cloud(tmp_path)
        async def lane(job, kind, scope, count):
            cloud.use_scope('account', job)
            for _ in range(count):
                await cloud.call(accepted, _kind=kind, _scope=scope)
                await asyncio.sleep(0)
            return cloud.ledger_summary()
        first, second = await asyncio.gather(lane('one', 'read', 'file', 3), lane('two', 'write', 'share', 2))
        assert (first['read'], first['write'], first['scope']) == (3, 0, {'file': 3})
        assert (second['read'], second['write'], second['scope']) == (0, 2, {'share': 2})
        assert '请求分类' in second['text']
        await cloud.close(); store.close()
    asyncio.run(run())
