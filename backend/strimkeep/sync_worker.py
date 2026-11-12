"""独立子进程执行一次 STRM 同步（对齐「115 子脚本」）。

父进程只在外面看门：子进程崩溃不会拖垮调度与网页；异常退出时任务转待核查，
本地输出一个都不动。
"""
import asyncio
import json
import os
import sys
from pathlib import Path


def main(argv):
    if len(argv) < 2:
        print('用法: python -m strimkeep.sync_worker <job_id>', file=sys.stderr)
        return 2
    try:
        job_id = int(argv[1])
    except ValueError:
        print('job_id 必须是整数', file=sys.stderr)
        return 2
    from .store import Store
    from .cloud115 import Cloud115
    from .network import ServiceHTTP
    from .organizer import Organizer

    data_dir = Path(os.environ.get('STRIMKEEP_DATA_DIR', '/data'))
    strm_dir = Path(os.environ.get('STRIMKEEP_STRM_DIR', '/strm'))
    store = Store(data_dir, os.environ.get('ADMIN_USERNAME', 'admin'),
                  os.environ.get('ADMIN_PASSWORD', ''))
    http = ServiceHTTP(store)
    try:
        row = store.db.execute('SELECT config FROM organize_jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            print(f'任务 {job_id} 不存在', file=sys.stderr)
            return 3
        config = json.loads(row['config'])
        engine = Organizer(store, Cloud115(store), http, strm_dir)
        asyncio.run(engine.sync._run(job_id, config))
        return 0
    finally:
        try:
            asyncio.run(http.aclose())
        except Exception:
            pass
        store.close()


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
