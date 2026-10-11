"""Bounded, authenticated-router-owned progress for manual mapping deletes.

One worker, no queue, no additional filesystem scans. The existing delete
handler still owns matching, mutation_lock, cache updates and audit writes.
Records are ephemeral: a process restart must never trigger a replay.
"""
import copy
import re
import threading
import time

_lock = threading.Lock()
_records = {}
_LIMIT = 32
_TTL = 900
_TOKEN = re.compile(r'^[a-f0-9]{32}$')


def read(operation_id):
    with _lock:
        rec = _records.get(operation_id)
        return copy.deepcopy(rec) if rec else None


def submit(operation_id, identity, execute):
    if not isinstance(operation_id, str) or not _TOKEN.fullmatch(operation_id):
        return {'status': 'error', 'message': '无效删除请求编号'}
    with _lock:
        now = time.monotonic()
        for key in list(_records):
            if _records[key]['state'] != 'running' and now - _records[key]['updated'] > _TTL:
                del _records[key]
        if operation_id in _records:
            if _records[operation_id]['identity'] != identity:
                return {'status': 'error', 'message': '删除请求编号与作品不一致'}
            return {'status': 'accepted', 'operation_id': operation_id}
        if any(rec['state'] == 'running' for rec in _records.values()):
            return {'status': 'busy', 'message': '已有片库删除正在执行，请等待完成'}
        while len(_records) >= _LIMIT:
            oldest = min(_records, key=lambda key: _records[key]['updated'])
            del _records[oldest]
        _records[operation_id] = {
            'identity': identity, 'state': 'running', 'phase': 'preparing',
            'total': 0, 'processed': 0, 'strm_removed': 0, 'cloud_removed': 0,
            'retained': 0, 'error_count': 0, 'updated': now,
        }

    def progress(snapshot):
        with _lock:
            _records[operation_id].update(snapshot, updated=time.monotonic())

    def run():
        try:
            result = execute(progress)
        except Exception as error:
            result = {'status': 'error', 'message': str(error)}
        with _lock:
            rec = _records[operation_id]
            rec.update(state='done', phase='done', result=result, updated=time.monotonic())

    try:
        threading.Thread(target=run, name='mapping-delete', daemon=True).start()
    except Exception as error:
        with _lock:
            _records[operation_id].update(state='done', phase='done',
                result={'status': 'error', 'message': str(error)})
    return {'status': 'accepted', 'operation_id': operation_id}
