"""两个小修：离线任务完成判定、沿用旧名只在同年份时生效。"""
import re
from pathlib import Path

from strimkeep.offline_api import OfflinePostProcess
from strimkeep.organize_options import file_title


def test_offline_done_accepts_percent_forms_and_rejects_midway_states():
    done = OfflinePostProcess._done
    assert done({'percent': 100}) is True
    assert done({'percent': '100%'}) is True
    assert done({'percent': 99}) is False
    assert done({'status': '已完成'}) is True
    assert done({'status': '任务完成'}) is True
    assert done({'status': '未完成'}) is False
    assert done({'status': '等待中'}) is False
    assert done({'status': '下载失败'}) is False
    assert done({'status': '下载中 80%'}) is False


def test_offline_completion_displays_verified_result_and_channel_context(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from strimkeep.offline_api import OfflinePostProcess
    from strimkeep.store import Store

    class Cloud:
        async def offline_task_page(self, page=1, size=20):
            # 115 的 file_id 是「保存到哪个目录」，产物文件 ID 在 delete_file_id 里。
            return {'data': [{'info_hash': 'abc123', 'delete_file_id': '1001',
                              'name': 'submitted.mkv', 'percentDone': 100}]}

        async def file(self, file_id):
            return {'id': file_id, 'name': 'landed.mkv', 'parent_id': '123'}

        async def children(self, cid):
            return []

        async def offline_add(self, urls, cid):
            pass

    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        notified = []

        async def publish(**kwargs):
            notified.append(kwargs)

        async def scan_scope_allows(cids):
            return True

        # OfflinePostProcess 的第一个参数就是 engine，store / 通知都从它身上取。
        engine = SimpleNamespace(store=store, cloud=Cloud(), tasks=SimpleNamespace(config=lambda _: {}),
                                 notifications=SimpleNamespace(publish=publish),
                                 scan_scope_allows=scan_scope_allows)
        context = {'channel': '示例频道', 'subject': '影片', 'note': '#DEMO-001', 'resource_kind': '磁力',
                   'post_url': 'https://t.me/example/44'}
        for organize in (False, True):
            post = OfflinePostProcess(engine)
            sid = await post.submit(['magnet:?xt=urn:btih:abc123'], '123', '115 网盘 / 下载',
                                    organize=organize, origin='channel', notice_context=context)
            row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
            await OfflinePostProcess(engine).check(row)
            row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
            assert row['state'] == 'completed', dict(row)
            assert bool(row['organize_job_id']) == organize
            receipt = notified[-1]
            assert receipt['key'] == f'offline_done:{sid}'
            assert '频道离线下载 · 已完成' in receipt['title']
            assert all(value in receipt['message'] for value in (
                '示例频道', context['post_url'], 'landed.mkv', f'离线记录  #{sid}', '已核实落盘  1 项'))
            assert '#DEMO-001' not in receipt['message']
            assert 'submitted.mkv' not in receipt['message']
            assert ('已入队' if organize else '仅落盘') in receipt['message']
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 1
        store.close()

    asyncio.run(run())


def pick_existing(names, target_name):
    """与 archive.refresh_related 相同的挑选口径：只沿用同年份的文件名。"""
    year_match = re.search(r'\(((?:19|20)\d{2})\)', target_name)
    year = year_match.group(1) if year_match else ''
    existing = ''
    for name in sorted(names):
        title = file_title(name)
        if not title:
            continue
        found = re.search(r'\(((?:19|20)\d{2})\)|(?<!\d)((?:19|20)\d{2})(?!\d)', Path(name).stem)
        if year and found and (found.group(1) or found.group(2)) != year:
            continue
        if 2 <= len(title) <= 100:
            existing = title
            break
    return existing


def test_keep_existing_names_skips_a_different_year_in_the_same_folder():
    target = '沙丘 (2021) - 2160p.WEB-DL.H.265.mkv'
    # 同目录混进 1984 版重拍时，不能把它的标题搬到 2021 版上。
    assert pick_existing(['沙丘 (1984) - 1080p.mkv', '沙丘 (2021) - 2160p.mkv'], target) == '沙丘'
    assert pick_existing(['沙丘 (1984) - 1080p.mkv'], target) == ''


def test_keep_existing_names_keeps_a_custom_title_of_the_same_year():
    target = 'Dune (2021) - 2160p.WEB-DL.mkv'
    assert pick_existing(['沙丘 (2021) - 2160p.mkv'], target) == '沙丘'
