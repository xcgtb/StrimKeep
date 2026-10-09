#!/usr/bin/env python3
"""Isolated SQLite migration/log verification; no real NAS files or APIs."""
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path.cwd() if __file__ == '<stdin>' else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def main():
    with tempfile.TemporaryDirectory(prefix='strimkeep-sqlite-verify-') as temporary:
        root = Path(temporary)
        for key, leaf in [('AGENT_DATA', 'data'), ('L_ROOT', 'local'),
                          ('S_ROOT', 'share'), ('CLOUD_L_ROOT', 'cloud')]:
            os.environ[key] = str(root / leaf)
        os.environ['TG_BOT_TOKEN'] = os.environ['TMDB_KEY'] = ''
        os.environ['ENABLE_CD2_WATCHDOG'] = '0'
        os.environ['WEB_PASSWORD'] = 'isolated-verification-only'
        from app import engine, storage, state_store as state, config, logger, runtime_logs, subscribe, governance
        def forbidden(*a, **kw): raise AssertionError('real NAS/API operation forbidden')
        engine.emby_request = engine.notify_telegram = engine._get_lib = forbidden
        engine.STATE_DIR.mkdir(parents=True)
        def legacy(path, value): path.write_text(json.dumps(value), encoding='utf-8')
        legacy(config.CONFIG_FILE, {'subscribe_interval_min': '41'})
        legacy(engine.SUB_STATE_FILE, {'a': {'notified_episodes': ['S01E01']}})
        legacy(engine.MANUAL_DONE_FILE, {'a': {'name': 'temporary'}})
        assert storage.db_migrate()['documents'] == 3
        assert config.load_config()['subscribe_interval_min'] == '41'
        assert subscribe._load_sub_state()['a']['notified_episodes'] == ['S01E01']
        print('PASS legacy configuration, subscriptions and manual completion migrate into SQLite')

        state.save(engine.MANUAL_DONE_FILE, {})
        legacy(engine.MANUAL_DONE_FILE, {'obsolete': True})
        state._READY.clear()
        assert storage.db_migrate()['documents'] == 0
        assert state.read(engine.MANUAL_DONE_FILE) == {}
        print('PASS persisted import markers prevent old JSON from overwriting newer SQL state')

        subscribe._save_sub_state({})
        state._READY.clear()
        assert subscribe._load_sub_state() == {}
        assert json.loads(engine.SUB_STATE_FILE.read_text())['a']
        print('PASS explicit empty subscription state stays empty; retired exports are untouched')

        before = set(root.rglob('*.json'))
        config.save_config(dict(config.DEFAULTS, subscribe_interval_min='42'))
        for name in ('library_snapshot', 'gov_latest', 'bot_delete_queue', 'subscription_report', 'explore_pages'):
            state.save(engine.STATE_DIR / (name + '.json'), {})
        assert config.load_config()['subscribe_interval_min'] == '42'
        assert set(root.rglob('*.json')) == before and not list(root.rglob('*.jsonl'))
        print('PASS subsequent config/cache/queue writes create no JSON or JSONL exports')

        state.save(engine.GOV_LATEST_FILE, {'old': True})
        con = storage._conn()
        con.execute("CREATE TRIGGER fail_latest BEFORE INSERT ON docs WHEN NEW.name='gov_latest' BEGIN SELECT RAISE(ABORT,'fixture'); END")
        con.commit()
        try:
            governance.save_latest_scan(None, {'status': 'success'})
        except OSError: pass
        else: raise AssertionError('failed scan publication reported success')
        assert state.read(engine.GOV_LATEST_FILE) == {'old': True}
        con.execute('DROP TRIGGER fail_latest'); con.commit()
        state.save(engine.GOV_LATEST_FILE, {'old': True})
        print('PASS failed required publication rolls back and preserves the previous scan')

        with patch.object(storage, '_conn', side_effect=PermissionError('private')):
            try: subscribe._load_sub_state()
            except RuntimeError: pass
            else: raise AssertionError('unreadable subscriptions became an empty baseline')
        assert subscribe._load_sub_state() == {}
        print('PASS unreadable subscription state stops the round without resetting its baseline')

        logger.write('追更', 'poll complete', ['no changes'])
        logging.getLogger('strimkeep.bot').info('机器人健康：正常')
        first = runtime_logs.read()
        logger.write('目录清理', 'temporary completed', ['20 subtitles'])
        latest = runtime_logs.read(after=first['cursor'])
        assert len(latest['logs']) == 1 and 'temporary completed' in latest['logs'][0]['message']
        assert len(logger.read_archives()) == 1
        print('PASS incremental live logs include health/polls; only cleanup results enter archives')

        with patch.object(storage, '_conn', side_effect=sqlite3.OperationalError('fixture')):
            logging.getLogger('strimkeep.bot').info('pending temporary health')
            assert runtime_logs.read()['degraded']
        logging.getLogger('strimkeep.bot').info('recovered temporary health')
        logs = runtime_logs.read()['logs']
        assert sum(r['message'] == 'pending temporary health' for r in logs) == 1
        assert not runtime_logs.read()['degraded']
        print('PASS database log failures remain visible and flush once after recovery')

        for pid, status in [('abcdef12', 'done'), ('abcdef13', 'pending')]:
            assert storage.db_save_plan({'schema_version': 2, 'id': pid, 'ts': time.time()-30*86400, 'state': status})
        storage.db_purge_plans(time.time()-7*86400)
        assert storage.db_load_plan('abcdef12') and not storage.db_load_plan('abcdef13')
        assert logger.read_archives()[0]['title'] == 'temporary completed'
        print('PASS expired unexecuted plans are purged while executed plans and cleanup archives remain')

        from app.main import app
        import asyncio
        from app.routers.deps import auth
        async def probe(endpoint):
            messages = []
            received = False
            async def receive():
                nonlocal received
                if not received:
                    received = True
                    return {'type':'http.request', 'body':b'', 'more_body':False}
                await asyncio.Event().wait()
            async def send(message):
                messages.append(message)
            scope = {'type':'http', 'asgi':{'version':'3.0','spec_version':'2.3'},
                     'http_version':'1.1','method':'GET','scheme':'http',
                     'path':endpoint,'raw_path':endpoint.encode(),'query_string':b'',
                     'root_path':'','headers':[(b'host',b'testserver')],
                     'client':('127.0.0.1',1),'server':('testserver',80)}
            await app(scope, receive, send)
            code = next(m['status'] for m in messages if m['type']=='http.response.start')
            body = b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body').decode()
            return code, body
        # Direct ASGI requests, no lifespan or optional test packages.
        assert asyncio.run(probe('/api/logs'))[0] == 401
        app.dependency_overrides[auth] = lambda: True
        try:
            code, body = asyncio.run(probe('/api/logs'))
            assert code == 200 and json.loads(body)['logs']
            code, body = asyncio.run(probe('/api/plans'))
            assert code == 200 and json.loads(body)['archives']
            code, body = asyncio.run(probe('/api/logs/download'))
            assert code == 200 and 'temporary completed' in body
        finally: app.dependency_overrides.pop(auth, None)
        print('PASS authenticated logs, downloadable text and plan archives expose the same SQL data')
        for connection in list(storage._DBS.values()): connection.close()
        storage._DBS.clear()
    print('PASS 10/10; temporary state only; no real NAS media, cloud/API calls or messages')


if __name__ == '__main__': main()
