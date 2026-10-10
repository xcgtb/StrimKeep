# -*- coding: utf-8 -*-
"""
main.py 测试：鉴权（auth）+ 任务系统（TASKS 清理）。

main.py 之前完全没有测试保护——它是外部输入直接进来的那一层
（Web 端点、Basic Auth），所以这里先补最基本的两块：
  - auth()：正确/错误用户名密码、缺失凭证都要挡住
  - 未设置 WEB_PASSWORD 且未显式 ALLOW_NO_AUTH=1 时，进程必须拒绝启动
    （这条用子进程测，因为 WEB_PASSWORD 是 main.py 导入时就读掉的模块级常量，
    没法在同一个进程里换着测两种分支）
  - _prune_tasks()：TASKS 字典不能无限堆积

运行方式同其它测试：`python -m pytest dev/tests` 或 `pytest dev/tests/ -v`。
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_test_main_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['INGEST_QUIET_MINUTES'] = '0'
os.environ['WEB_USER'] = 'admin'
os.environ['WEB_PASSWORD'] = 'test-pass-123'

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
sys.path.insert(0, _REPO_ROOT)

import app.main as main  # noqa: E402
from fastapi.security import HTTPBasicCredentials  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _cred(u, p):
    return HTTPBasicCredentials(username=u, password=p)


class TestAuth:
    def test_correct_credentials_pass(self):
        assert main.auth(_cred('admin', 'test-pass-123')) is True

    def test_wrong_username_rejected(self):
        try:
            main.auth(_cred('nope', 'test-pass-123'))
            assert False, '应该抛出 401'
        except HTTPException as e:
            assert e.status_code == 401

    def test_wrong_password_rejected(self):
        try:
            main.auth(_cred('admin', 'wrong'))
            assert False, '应该抛出 401'
        except HTTPException as e:
            assert e.status_code == 401

    def test_missing_credentials_rejected(self):
        # HTTPBasic(auto_error=False) 在没带 Authorization 头时会传 None 进来
        try:
            main.auth(None)
            assert False, '应该抛出 401'
        except HTTPException as e:
            assert e.status_code == 401


class TestStartupRequiresPassword:
    def _run(self, env_overrides):
        env = dict(os.environ)
        env.update(env_overrides)
        # CLOUD_L_ROOT 指向不存在/空目录时，CD2 看门狗线程会等 60 秒后
        # 判定"未就绪"，强制 os._exit(42) 杀掉整个进程（含本测试），CI 里必现。
        # 这里给它一个"已就绪"的假目录，让看门狗几秒内自行退出。
        if 'CLOUD_L_ROOT' not in env_overrides:
            tmp = tempfile.mkdtemp()
            (Path(tmp) / '电影').mkdir()
            env['CLOUD_L_ROOT'] = tmp
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import app.main\n"
            "print('IMPORTED_OK')\n"
        ) % _REPO_ROOT
        return subprocess.run(
            [sys.executable, '-c', code],
            env=env, capture_output=True, text=True, timeout=90,
        )

    def test_refuses_without_password_and_without_allow_no_auth(self):
        r = self._run({'WEB_PASSWORD': '', 'ALLOW_NO_AUTH': ''})
        assert r.returncode != 0, f'应该拒绝启动，实际 returncode={r.returncode}'
        assert 'IMPORTED_OK' not in r.stdout

    def test_starts_without_password_when_allow_no_auth_set(self):
        r = self._run({'WEB_PASSWORD': '', 'ALLOW_NO_AUTH': '1'})
        assert r.returncode == 0, f'应该正常导入，实际 stderr={r.stderr[-2000:]}'
        assert 'IMPORTED_OK' in r.stdout


class TestImportHasNoSideEffects:
    def test_import_does_not_start_background_threads(self):
        """后台轮询 / Bot / 看门狗只在应用生命周期（lifespan）里启动，导入模块不再起线程"""
        env = dict(os.environ)
        env['TG_BOT_TOKEN'] = 'fake-token'
        code = (
            "import sys, threading; sys.path.insert(0, %r)\n"
            "import app.main\n"
            "print('THREADS=' + ','.join(sorted(t.name for t in threading.enumerate())))\n"
        ) % _REPO_ROOT
        r = subprocess.run([sys.executable, '-c', code], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-2000:]
        line = [ln for ln in r.stdout.splitlines() if ln.startswith('THREADS=')][0]
        assert line == 'THREADS=MainThread', line


# ═══════════════════ 生命周期 + 任务总线（HTTP 层） ═══════════════════
import threading  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import scheduler, tasks  # noqa: E402

_AUTH = ('admin', 'test-pass-123')


class TestLifespan:
    def test_scheduler_runs_only_inside_lifespan(self):
        with TestClient(main.app) as c:
            t = scheduler._thread['t']
            assert t is not None and t.is_alive()
            assert c.get('/api/health').status_code == 200
        t.join(5)
        assert not t.is_alive(), '应用退出时后台轮询线程应该停止'

    def test_scheduler_restarts_after_stop(self):
        scheduler.start()
        scheduler.stop()
        scheduler.start()
        try:
            t = scheduler._thread['t']
            assert t.is_alive() and not scheduler._stop.is_set()
        finally:
            scheduler.stop()
        assert not scheduler._thread['t'].is_alive()


class TestTaskBus:
    def test_second_scan_is_rejected_with_429(self):
        gate = threading.Event()
        orig = main.engine.ACTIONS['inter_check']
        main.engine.ACTIONS['inter_check'] = lambda args: gate.wait(5) and {'status': 'success'}
        try:
            c = TestClient(main.app)
            r1 = c.post('/api/check', auth=_AUTH)
            assert r1.status_code == 200
            tid = r1.json()['task_id']
            r2 = c.post('/api/check', auth=_AUTH)
            assert r2.status_code == 429
            cur = c.get('/api/health').json()['current_task']
            assert cur['id'] == tid and cur['source'] == 'web'
        finally:
            gate.set()
            main.engine.ACTIONS['inter_check'] = orig
        assert tasks.manager.get(tid).done.wait(5)
        assert c.get(f'/api/task/{tid}', auth=_AUTH).json()['status'] == 'success'


class TestMovieDeleteEndpoint:
    def _mk(self, rel):
        p = main.engine.L_ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('x', encoding='utf-8')
        return p

    def test_exact_tmdb_match_and_busy_lock(self, tmp_path, monkeypatch):
        import fcntl
        # This endpoint must use its own library even if another module imported
        # the shared engine before this module configured the environment.
        for name, leaf in [('L_ROOT', 'local'), ('S_ROOT', 'share'),
                           ('CLOUD_L_ROOT', 'cloud'), ('DATA_DIR', 'data')]:
            monkeypatch.setattr(main.engine, name, tmp_path / leaf)
        monkeypatch.setattr(main.engine, 'LOCK_FILE', tmp_path / 'data/agent.lock')
        self._mk('电影/外语电影/片A {tmdb-77}/片A.strm')
        self._mk('电影/外语电影/片B {tmdb-777}/片B.strm')
        c = TestClient(main.app)
        body = {'tmdb_id': '77', 'target': 'share', 'dry_run': True}
        assert c.post('/api/emby/movie/delete_by_tmdb', json=body, auth=_AUTH).json()['count'] == 0
        body['target'] = 'local'
        assert c.post('/api/emby/movie/delete_by_tmdb', json=body, auth=_AUTH).json()['count'] == 1
        main.engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
        holder = open(main.engine.LOCK_FILE, 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            body['dry_run'] = False
            r = c.post('/api/emby/movie/delete_by_tmdb', json=body, auth=_AUTH).json()
            assert r['status'] == 'busy'
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        assert (main.engine.L_ROOT / '电影/外语电影/片A {tmdb-77}/片A.strm').exists()
