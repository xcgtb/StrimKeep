"""「尽力而为」分支的失败留痕：必须有痕迹，但不能每轮巡检都灌一条。"""
import json

from test_organize import setup


def _messages(store):
    return [r[0] for r in store.db.execute("SELECT message FROM events WHERE kind='sync' ORDER BY id")]


def test_notice_keeps_first_and_throttles_repeats(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    sync = engine.sync
    sync.notice('sidecar', '附属文件清理失败，本次跳过（目录：/a）。')
    sync.notice('sidecar', '附属文件清理失败，本次跳过（目录：/b）。')
    assert _messages(store) == ['附属文件清理失败，本次跳过（目录：/a）。']
    # 另一类失败各有各的额度，不会被前一类挤掉。
    sync.notice('heal', '转存清单解析失败。')
    assert len(_messages(store)) == 2
    store.close()


def test_notice_emits_again_after_window(tmp_path):
    store, cloud, http, engine, _ = setup(tmp_path)
    sync = engine.sync
    sync.notice('heal', '第一次')
    sync._notices['heal'] = 0.0  # 模拟时间窗已过
    sync.notice('heal', '第二次')
    assert _messages(store) == ['第一次', '第二次']
    store.close()


def test_notice_never_breaks_the_caller(tmp_path):
    """留痕自身出错（比如事件表写不进去）不能反过来打断巡检。"""
    store, cloud, http, engine, _ = setup(tmp_path)
    sync = engine.sync

    def boom(*a, **k):
        raise RuntimeError('events 表不可写')
    store.event = boom
    sync.notice('sidecar', '不会抛出去')
    assert sync._notices['sidecar']  # 仍然记下了时间戳
    store.close()


def test_existing_sidecar_counts_as_kept_not_skipped(tmp_path):
    """本地已有同名附属文件算「已存在保留」，不算跳过。

    全量同步每天都会撞到这条：47 个 .ass 早就下过，以前被写成「跳过 47 个」，
    看起来像同步失败。
    """
    import asyncio
    import json
    from test_organize import setup, new_job, CONFIG
    store, cloud, http, engine, _ = setup(tmp_path)
    jid = new_job(store, {**CONFIG, 'job_kind': 'strm_sync'})
    store.db.execute("UPDATE organize_jobs SET state='running' WHERE id=?", (jid,))
    local = engine.root / '媒体库' / '剧' / 'x.ass'
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text('sub', encoding='utf-8')
    engine.sync.relative = lambda source, config: '媒体库/剧/x.ass'
    config = {'sync_metadata': [{'_root': 'root', 'name': 'x.ass', 'id': '1', 'size': 3}]}
    asyncio.run(engine.sync.download_metadata(jid, config))
    saved = json.loads(store.db.execute('SELECT config FROM organize_jobs WHERE id=?', (jid,)).fetchone()[0])
    stats = saved['sync_metadata_stats']['root']
    assert stats['existing'] == 1 and stats['skipped'] == 0 and stats['downloaded'] == 0
    store.close()


def test_sync_scope_line_explains_the_gap_between_scanned_files_and_videos():
    """1110 个扫描文件、963 个核对视频，差额要写清楚是谁，别让人以为漏了。"""
    from strimkeep.notifications import sync_scope_lines
    assert sync_scope_lines({'sync_filtered_ids': ['x'] * 147, 'sync_filter_count': 1}) == \
        '按生成规则跳过 1 个视频（不生成 STRM）'
    # 只有附属文件、没有视频被规则挡掉：什么都不说。
    assert sync_scope_lines({'sync_filtered_ids': ['x'] * 146, 'sync_filter_count': 0}) == ''
