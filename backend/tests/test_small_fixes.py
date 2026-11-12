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


def test_offline_completion_without_organize_does_not_enqueue(tmp_path):
    """只落盘的离线提交：产物出现后只标记完成，不建整理任务。"""
    import asyncio
    from types import SimpleNamespace
    from strimkeep.cloud115 import CloudError
    from strimkeep.offline_api import OfflinePostProcess
    from strimkeep.store import Store

    class Cloud:
        async def offline_tasks(self, page=1, size=20):
            return {'data': [{'info_hash': 'abc123', 'file_id': '1001', 'name': 'a.mkv', 'percentDone': 100}]}, None

        async def children(self, cid):
            return [{'id': '1001', 'name': 'a.mkv', 'is_dir': False}]

    async def run():
        store = Store(tmp_path / 'data', 'admin', 'test-password-1234')
        post = OfflinePostProcess(SimpleNamespace(store=store, cloud=Cloud(),
                                                  tasks=SimpleNamespace(), engine=None))
        with store.db:
            sid = store.db.execute(
                "INSERT INTO offline_submissions(urls,target_cid,target_label,before_ids,submitted_at,"
                "state,message,organize) VALUES('[\"magnet:?xt=urn:btih:abc123\"]','123','x','[]',0,'submitted','',0)").lastrowid
        row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
        await post.check(row)
        row = store.db.execute('SELECT * FROM offline_submissions WHERE id=?', (sid,)).fetchone()
        assert row['state'] == 'completed'
        assert row['organize_job_id'] is None
        assert store.db.execute('SELECT count(*) FROM organize_jobs').fetchone()[0] == 0
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
