"""多语言识别：搜索语言对齐、换语言回退、评分诊断、历史批次闭环。

背景（0.8.3）：文件名是英文发布名、TMDB 搜索固定用 zh-CN 时，返回的候选标题全是
中文译名 + 原文名，标题相似度是 0，被 threshold 判掉，最终报「未找到唯一匹配」。
"""
import asyncio
import sqlite3

import pytest

from strimkeep.media import NeedsReview
from strimkeep.organizer import Organizer


class FakeTMDB:
    """按 (路径, language) 返回不同结果，模拟 TMDB 的多语言标题。"""

    def __init__(self, search=None, details=None):
        self.search = search or {}
        self.details = details or {}
        self.calls = []

    async def __call__(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path.startswith('search/'):
            kind = path.split('/')[1]
            language = params.get('language') or 'zh-CN'
            return {'results': (self.search.get(kind) or {}).get(language, [])}
        if path.endswith('/alternative_titles'):
            return {'results': []}
        ident = int(path.split('/')[1])
        return self.details.get(ident, {'id': ident})

    def languages_used(self):
        return [params.get('language') for path, params in self.calls if 'search/' in path]


def engine_with(fake):
    engine = Organizer.__new__(Organizer)
    engine.tmdb = fake
    return engine


def tv_hint(**over):
    hint = {'query': 'Love in Disguise', 'year': 2026, 'media_type': 'tv', 'season': 1,
            'episode': 1, 'end_episode': 1, 'tmdb_id': None, 'identity_source': 'filename'}
    hint.update(over)
    return hint


def test_non_cjk_query_searches_english_titles():
    """英文发布名必须用 en-US 搜，才拿得到同语言的标题来比相似度。"""
    async def run():
        fake = FakeTMDB(
            search={'tv': {
                # zh-CN：只有中文译名，与英文发布名相似度为 0
                'zh-CN': [{'id': 303745, 'name': '我的有罪之人', 'original_name': '나의 유죄인간',
                           'first_air_date': '2026-10-19', 'popularity': 78.1}],
                # en-US：同一条目给出英文标题
                'en-US': [{'id': 303745, 'name': 'Love in Disguise', 'original_name': '나의 유죄인간',
                           'first_air_date': '2026-10-19', 'popularity': 78.1}],
            }},
            details={303745: {'id': 303745, 'name': '我的有罪之人', 'first_air_date': '2026-10-19',
                              'seasons': [{'season_number': 1, 'air_date': '2026-10-19'}]}},
        )
        details = await engine_with(fake).identify(tv_hint())
        assert details['id'] == 303745
        assert fake.languages_used()[0] == 'en-US'
    asyncio.run(run())


def test_cjk_query_keeps_chinese_search():
    """中文文件名仍走 zh-CN，不改变原有行为。"""
    async def run():
        fake = FakeTMDB(
            search={'tv': {'zh-CN': [{'id': 7, 'name': '火凤凰', 'first_air_date': '2026-03-01',
                                      'popularity': 3}]}},
            details={7: {'id': 7, 'name': '火凤凰', 'first_air_date': '2026-03-01', 'seasons': []}},
        )
        await engine_with(fake).identify(tv_hint(query='火凤凰', year=2026))
        assert fake.languages_used()[0] == 'zh-CN'
    asyncio.run(run())


def test_falls_back_to_other_language_when_primary_has_no_title_match():
    """英文发布名在 en-US 候选里匹配不上时，用 zh-CN 再搜一次。"""
    async def run():
        fake = FakeTMDB(
            search={'tv': {
                'en-US': [{'id': 900, 'name': 'Unrelated Work', 'original_name': '무관',
                           'first_air_date': '2026-01-01', 'popularity': 5}],
                'zh-CN': [{'id': 901, 'name': 'The Wartime Post', 'original_name': '战时',
                           'first_air_date': '2026-05-01', 'popularity': 9}],
            }},
            details={901: {'id': 901, 'name': 'The Wartime Post', 'first_air_date': '2026-05-01',
                           'seasons': [{'season_number': 1, 'air_date': '2026-05-01'}]}},
        )
        hint = tv_hint(query='The Wartime Post', year=None)
        details = await engine_with(fake).identify(hint)
        assert details['id'] == 901
        assert 'zh-CN' in fake.languages_used()
    asyncio.run(run())


def test_cache_key_separates_languages_and_keeps_legacy_default():
    """en-US 与 zh-CN 不能共用缓存；不指定 language 时 key 保持原样（旧缓存仍可用）。"""
    base = {'query': 'Love in Disguise'}
    plain = Organizer.tmdb_cache_key('search/tv', base)
    zh = Organizer.tmdb_cache_key('search/tv', {**base, 'language': 'zh-CN'})
    en = Organizer.tmdb_cache_key('search/tv', {**base, 'language': 'en-US'})
    assert zh != en
    assert 'language' not in plain
    assert 'language=en-US' in en


def test_pick_records_scores_when_all_candidates_below_threshold():
    """全灭时必须留下真实分数，否则排查不出是语言错配还是真没这部片。"""
    async def run():
        engine = Organizer.__new__(Organizer)
        attempts = []
        results = [{'id': 1, 'name': '我的有罪之人', 'original_name': '나의 유죄인간',
                    'first_air_date': '2026-10-19'}]
        assert await engine.pick('tv', tv_hint(), results, attempts) is None
        record = [a for a in attempts if a.get('variant') == 'score_below_threshold']
        assert record, attempts
        assert record[0]['threshold'] == pytest.approx(0.5)
        assert '我的有罪之人' in record[0]['top']
    asyncio.run(run())


class FakeStore:
    def __init__(self, conn):
        self.db = conn
        self.events = []

    def event(self, kind, message):
        self.events.append((kind, message))


def make_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        CREATE TABLE organize_jobs(id INTEGER PRIMARY KEY, state TEXT, message TEXT, stage TEXT);
        CREATE TABLE organize_items(id INTEGER PRIMARY KEY, job_id INTEGER, file_id TEXT,
                                    state TEXT, plan TEXT, step TEXT, message TEXT);
    ''')
    return conn


def organizer_on(conn):
    conn.execute("ALTER TABLE organize_jobs ADD COLUMN config TEXT DEFAULT '{\"account_id\":\"account-1\",\"job_kind\":\"organize\"}'")
    engine = Organizer.__new__(Organizer)
    engine.store = FakeStore(conn)

    def job(job_id):
        row = conn.execute('SELECT * FROM organize_jobs WHERE id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def update_job(job_id, **fields):
        keys = [k for k in ('state', 'message', 'stage') if k in fields]
        if not keys:
            return
        conn.execute('UPDATE organize_jobs SET %s WHERE id=?' % ','.join('%s=?' % k for k in keys),
                     (*[fields[k] for k in keys], job_id))
        conn.commit()

    engine.job = job
    engine.update_job = update_job
    return engine


def test_previous_batch_closes_when_all_files_are_done_later():
    """重跑成功后旧批次不能继续显示「部分未完成 / 已跳过 N 个视频」。"""
    conn = make_db()
    conn.executescript('''
        INSERT INTO organize_jobs VALUES (183, 'partial', '已跳过 5 个视频。', '批次结束');
        INSERT INTO organize_jobs VALUES (185, 'completed', '已整理 5 个视频。', '批次结束');
        INSERT INTO organize_items VALUES (1, 183, 'f1', 'skipped', '{}', '未识别转移未完成，保留目录与记录', 'TMDB 未找到唯一匹配');
        INSERT INTO organize_items VALUES (2, 185, 'f1', 'done', '{}', '归档及 STRM 完成', '');
    ''')
    engine = organizer_on(conn)
    engine.close_superseded_batches(185)
    job = dict(conn.execute('SELECT * FROM organize_jobs WHERE id=183').fetchone())
    item = dict(conn.execute('SELECT * FROM organize_items WHERE job_id=183').fetchone())
    assert job['state'] == 'completed'
    assert '已由任务 185 全部补齐' in job['message']
    assert '已跳过 5 个视频' in job['message']          # 原始信息仍可追溯
    assert item['step'] == '已由任务 185 补齐'
    assert 'TMDB 未找到唯一匹配' in item['message']
    assert engine.store.events and '闭环' in engine.store.events[-1][1]


def test_previous_batch_stays_partial_when_other_files_remain():
    """旧批次还有别的没补齐的文件时，只更新条目，不结项。"""
    conn = make_db()
    conn.executescript('''
        INSERT INTO organize_jobs VALUES (183, 'partial', '已跳过 5 个视频。', '批次结束');
        INSERT INTO organize_jobs VALUES (185, 'completed', '已整理 1 个视频。', '批次结束');
        INSERT INTO organize_items VALUES (1, 183, 'f1', 'skipped', '{}', '未识别', 'TMDB 未找到唯一匹配');
        INSERT INTO organize_items VALUES (2, 183, 'f2', 'pending', '{}', '待处理', '');
        INSERT INTO organize_items VALUES (3, 185, 'f1', 'done', '{}', '归档及 STRM 完成', '');
    ''')
    engine = organizer_on(conn)
    engine.close_superseded_batches(185)
    job = dict(conn.execute('SELECT * FROM organize_jobs WHERE id=183').fetchone())
    assert job['state'] == 'partial'
    assert job['message'] == '已跳过 5 个视频。'
    steps = [row[0] for row in conn.execute('SELECT step FROM organize_items WHERE job_id=183 ORDER BY id')]
    assert steps == ['已由任务 185 补齐', '待处理']


def test_completed_batch_is_not_touched_again():
    """已经结项的历史批次不再改写，避免重复追加说明。"""
    conn = make_db()
    conn.executescript('''
        INSERT INTO organize_jobs VALUES (183, 'completed', '已整理 5 个视频。', '批次结束');
        INSERT INTO organize_jobs VALUES (185, 'completed', '已整理 5 个视频。', '批次结束');
        INSERT INTO organize_items VALUES (1, 183, 'f1', 'skipped', '{}', '未识别', '原因');
        INSERT INTO organize_items VALUES (2, 185, 'f1', 'done', '{}', '归档及 STRM 完成', '');
    ''')
    engine = organizer_on(conn)
    engine.close_superseded_batches(185)
    job = dict(conn.execute('SELECT * FROM organize_jobs WHERE id=183').fetchone())
    assert job['message'] == '已整理 5 个视频。'


def test_previous_batch_with_other_skipped_failure_is_not_closed():
    conn=make_db()
    conn.executescript("""
      INSERT INTO organize_jobs VALUES (183,'partial','部分未完成','批次结束');
      INSERT INTO organize_jobs VALUES (185,'completed','已整理','批次结束');
      INSERT INTO organize_items VALUES (1,183,'f1','skipped','{}','未识别','原始原因');
      INSERT INTO organize_items VALUES (2,183,'f2','skipped','{}','未识别','另一项失败');
      INSERT INTO organize_items VALUES (3,185,'f1','done','{}','完成','');
    """)
    engine=organizer_on(conn);engine.close_superseded_batches(185)
    assert engine.job(183)['state']=='partial'
    assert conn.execute('SELECT message FROM organize_items WHERE id=1').fetchone()[0].startswith('原始原因')
    assert conn.execute('SELECT step FROM organize_items WHERE id=2').fetchone()[0]=='未识别'

def test_only_older_same_account_incomplete_batches_are_updated():
    conn=make_db()
    conn.executescript("""
      INSERT INTO organize_jobs VALUES (181,'completed','已完成','批次结束');
      INSERT INTO organize_jobs VALUES (182,'partial','另一账号','批次结束');
      INSERT INTO organize_jobs VALUES (183,'partial','原账号','批次结束');
      INSERT INTO organize_jobs VALUES (185,'completed','新任务','批次结束');
      INSERT INTO organize_jobs VALUES (186,'partial','更晚任务','批次结束');
      INSERT INTO organize_items VALUES (1,181,'f1','skipped','{}','未识别','旧原因');
      INSERT INTO organize_items VALUES (2,182,'f1','skipped','{}','未识别','另一账号');
      INSERT INTO organize_items VALUES (3,183,'f1','skipped','{}','未识别','本账号');
      INSERT INTO organize_items VALUES (4,185,'f1','done','{}','完成','');
      INSERT INTO organize_items VALUES (5,186,'f1','skipped','{}','未识别','更晚');
    """)
    engine=organizer_on(conn)
    conn.execute("UPDATE organize_jobs SET config=? WHERE id=182",('{"account_id":"other","job_kind":"organize"}',))
    engine.close_superseded_batches(185)
    assert conn.execute('SELECT step FROM organize_items WHERE id=3').fetchone()[0]=='已由任务 185 补齐'
    for item in (1,2,5):assert conn.execute('SELECT step FROM organize_items WHERE id=?',(item,)).fetchone()[0]=='未识别'
