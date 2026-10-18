"""Bounded task metadata survives restarts; task results remain in their own stores."""
import threading
if __package__:
    from . import state_store, storage
else:
    import state_store, storage

_LOCK = threading.RLock()
_REVISION = 0


def _path():
    return storage._diagnostic_dir() / 'task_history.json'


def record(task):
    global _REVISION
    row = {'id': task.id, 'kind': task.kind, 'source': task.source,
           'status': task.status, 'ts': task.ts, 'finished_at': task.finished_at}
    with _LOCK:
        saved = state_store.read(_path(), {})
        rows = saved.get('items', [])
        rows = [r for r in rows if r.get('id') != task.id] + [row]
        state_store.save(_path(), {'items': sorted(rows, key=lambda r:r['ts'], reverse=True)[:200]})
        _REVISION += 1


def recent():
    with _LOCK:
        return state_store.read(_path(), {}).get('items', [])


def archived(tid, rows=None):
    row = next((dict(r) for r in (recent() if rows is None else rows) if r['id'] == tid), None)
    if row is not None:
        row.update(archived=True, cancellable=False, logs=[], result=None)
        row['archive_note'] = '历史任务仅保留状态摘要；文件结果请到对应治理或扫描模块核对。'
        if row['status'] == 'running':
            row['status'] = 'needs_review'
            row['error'] = '进程重启前任务尚未结束，请核对实际结果，不会自动重放文件操作。'
    return row
