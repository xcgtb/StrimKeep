"""空目录候选：单个目录读失败只跳过，不再拖垮整轮清理。"""
import asyncio
import json
from types import SimpleNamespace

from strimkeep.cleanup import EmptyDirectoryCleanup
from strimkeep.cloud115 import CloudError


class FakeCloud:
    def __init__(self, bad_parent=None):
        self.bad = bad_parent

    async def directory(self, cid):
        return ['0', '500', cid]

    async def children(self, cid):
        if cid == self.bad:
            raise CloudError('115 返回了其他目录的条目，正在读取的目录暂不可用。', 'DIRECTORY_CHANGED', 409)
        return [{'id': 'X', 'name': '待整理剧集', 'is_dir': True}]


class FakeEngine:
    def __init__(self, cloud):
        self.cloud = cloud
        self.store = SimpleNamespace(db=None, event=lambda *args: None)

    def boundary(self, job_id):
        return None


def run(cloud, immediate=()):
    cleanup = EmptyDirectoryCleanup.__new__(EmptyDirectoryCleanup)
    cleanup.engine = FakeEngine(cloud)
    rows = [{'state': 'done', 'original': json.dumps({'parent_id': 'X'}), 'plan': json.dumps({})}]
    config = {'source_cids': ['500'], 'archive_cid': '200', 'pending_cid': '300',
              'immediate_cleanup_ids': list(immediate)}
    return asyncio.run(cleanup.candidates(1, config, rows))


def test_candidates_returns_directories_normally():
    entries, warning = run(FakeCloud())
    assert [entry['source']['id'] for entry in entries] == ['X']
    assert warning == ''


def test_read_failure_skips_only_that_directory():
    entries, warning = run(FakeCloud(bad_parent='500'))
    assert entries == []
    assert '读取失败' in warning


def test_immediate_cleanup_is_skipped_only_after_confirmed_missing():
    class Missing(FakeCloud):
        async def directory(self, cid):
            raise CloudError('目录不可读取', 'DIRECTORY_UNAVAILABLE', 409)

        async def live_file_exists(self, cid):
            return False

    assert run(Missing(), ['X']) == ([], '')
    assert '读取失败' in run(Missing(), [])[1]


def test_immediate_cleanup_checks_live_state_without_reading_deleted_directory():
    class Missing(FakeCloud):
        async def directory(self, cid):
            raise AssertionError('已提交回收的目录不应再读取路径')

        async def live_file_exists(self, cid):
            return False

    assert run(Missing(), ['X']) == ([], '')


def test_immediate_cleanup_does_not_retry_deleting_restored_directory():
    class Restored(FakeCloud):
        async def directory(self, cid):
            raise AssertionError('已恢复的目录不能被再次加入删除候选')

        async def live_file_exists(self, cid):
            return True

    entries, warning = run(Restored(), ['X'])
    assert entries == []
    assert '未能确认' in warning
    assert '下次清理' not in warning


def test_immediate_cleanup_read_failure_remains_warning_if_not_proved_missing():
    class Uncertain(FakeCloud):
        def __init__(self, result):
            self.result = result

        async def directory(self, cid):
            raise CloudError('目录不可读取', 'DIRECTORY_UNAVAILABLE', 409)

        async def live_file_exists(self, cid):
            if self.result == 'error':
                raise CloudError('暂时无法核查', 'SCHEMA_CHANGED', 409)
            return True

    for result in ('present', 'error'):
        assert '读取失败' in run(Uncertain(result), ['X'])[1]


def test_missing_identity_is_still_fatal():
    class Shrunk(FakeCloud):
        async def children(self, cid):
            return []

    try:
        run(Shrunk())
    except CloudError as exc:
        assert exc.code == 'DIRECTORY_CHANGED'
    else:
        raise AssertionError('身份核实失败时必须保留目录并报错')
