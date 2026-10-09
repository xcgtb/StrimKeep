"""Migration, SQL-only writes, live logs and archives with temporary state only."""
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
import pytest
from app import engine, storage, state_store as store, logger, runtime_logs, config, governance, bot


@pytest.fixture
def state(tmp_path, monkeypatch):
    directory = tmp_path / 'state'; directory.mkdir()
    monkeypatch.setattr(engine, 'STATE_DIR', directory)
    monkeypatch.setattr(engine, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(engine, 'SUB_STATE_FILE', directory / 'subscriptions_state.json')
    monkeypatch.setattr(engine, 'GOV_LATEST_FILE', directory / 'gov_latest.json')
    monkeypatch.setattr(engine, 'MANUAL_DONE_FILE', directory / 'manual_done.json')
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'config.json')
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(config, '_CFG_CACHE', {'sig': None, 'data': None})
    monkeypatch.setattr(logger, 'RECORDS_DIR', tmp_path / 'records')
    monkeypatch.setattr(logger, 'LEGACY_LOG', tmp_path / 'legacy.log')
    with runtime_logs._LOCK: runtime_logs._PENDING.clear()
    return directory


def legacy(path, value): path.write_text(json.dumps(value), encoding='utf-8')


def test_all_legacy_documents_import_once_and_old_exports_never_override(state):
    values = {'subscriptions_state': {'a': {'notified_episodes': ['S01E01']}},
              'manual_done': {'a': {'name':'test'}}, 'gov_latest': {'ts':1},
              'wash_residuals': {'items':[]}, 'bot_delete_queue': [[1,2,3]],
              'emby_library_with_tmdb': {'series':[], 'movies':[]},
              'explore_pages': {}, 'tmdb_cache': {'k':{'ts':1,'data':{'v':2}}}}
    for name,value in values.items(): legacy(state / (name+'.json'), value)
    legacy(state.parent / 'config.json', {'subscribe_interval_min':'35'})
    legacy(state.parent / 'gov_auto.json', {'enabled':True})
    assert storage.db_migrate()['documents'] == 10
    for name,value in values.items(): assert store.read(state/(name+'.json')) == value
    assert config.load_config()['subscribe_interval_min'] == '35'
    store.save(state/'manual_done.json', {})
    legacy(state/'manual_done.json', {'obsolete':True})
    store._READY.clear()  # Simulate a fresh process; marker is persisted in SQL.
    assert storage.db_migrate()['documents'] == 0
    assert store.read(state/'manual_done.json') == {}
    assert not list(state.glob('*.tmp'))


def test_import_failure_rolls_back_documents_and_markers_and_can_retry(state):
    legacy(state/'manual_done.json', {'valid':True})
    legacy(state/'subscriptions_state.json', ['invalid baseline'])
    with pytest.raises(OSError): storage.db_migrate()
    con=storage._conn()
    assert con.execute('SELECT 1 FROM docs WHERE name=?',('manual_done',)).fetchone() is None
    assert con.execute("SELECT 1 FROM kv WHERE k LIKE 'sqlite_import:%'").fetchone() is None
    legacy(state/'subscriptions_state.json', {'a':{'notified_episodes':['S01E01']}})
    storage.db_migrate()
    assert store.read(state/'manual_done.json')['valid']


def test_invalid_optional_cache_can_rebuild_without_reimporting_old_file(state):
    path = state / 'tmdb_cache.json'
    path.write_text('{truncated legacy cache', encoding='utf-8')
    storage.db_migrate()
    assert store.read(path, {}) == {}
    store.save(path, {'tv:1': {'ts': time.time(), 'data': {'name': 'rebuilt'}}})
    store._READY.clear()
    assert storage.db_migrate()['documents'] == 0
    assert store.read(path)['tv:1']['data']['name'] == 'rebuilt'
    assert path.read_text() == '{truncated legacy cache'


def test_empty_sub_state_is_authoritative_and_never_reimported(state):
    legacy(engine.SUB_STATE_FILE, {'a': {'latest_ep':'S01E01'}})
    assert store.read(engine.SUB_STATE_FILE)['a']
    store.save(engine.SUB_STATE_FILE, {})
    store._READY.clear()
    assert store.read(engine.SUB_STATE_FILE) == {}
    engine.SUB_STATE_FILE.unlink()
    assert store.read(engine.SUB_STATE_FILE) == {}


def test_new_config_and_all_runtime_writes_create_only_sqlite_files(state):
    config.save_config(dict(config.DEFAULTS, subscribe_interval_min='40'))
    for name in ('manual_done','gov_latest','gov_scan_status','subscription_report','subscription_check_status',
                 'bot_delete_queue','ingest_cache','library_snapshot','emby_library_with_tmdb',
                 'emby_index_cache','emby_overview_cache','strm_count_cache','explore_pages'):
        store.save(state/(name+'.json'), {})
    assert config.load_config()['subscribe_interval_min'] == '40'
    logger.write('目录清理', 'completed', ['20 subtitles'])
    assert not list(state.parent.rglob('*.json')) and not list(state.parent.rglob('*.jsonl'))
    assert (state/storage.DB_NAME).exists()


def test_config_cache_reuses_one_read_and_refreshes_after_sql_write(state):
    config.save_config(dict(config.DEFAULTS, subscribe_interval_min='40'))
    with patch.object(store,'read',wraps=store.read) as read:
        for _ in range(20): assert config.load_config()['subscribe_interval_min']=='40'
        assert read.call_count == 1
        config.save_config(dict(config.DEFAULTS, subscribe_interval_min='45'))
        assert config.load_config()['subscribe_interval_min']=='45'
        assert read.call_count==2


def test_failed_required_scan_publication_is_not_reported_as_saved(state):
    store.save(engine.GOV_LATEST_FILE, {'old':True})
    con=storage._conn()
    con.execute("CREATE TRIGGER fail_latest BEFORE INSERT ON docs WHEN NEW.name='gov_latest' BEGIN SELECT RAISE(ABORT,'fixture'); END")
    con.commit()
    with pytest.raises(OSError): governance.save_latest_scan(None, {'status':'success'})
    assert store.read(engine.GOV_LATEST_FILE)=={'old':True}


def test_logs_incremental_order_and_archive_separation(state):
    logger.write('追更','订阅检查',['no changes'])
    logging.getLogger('strimkeep.bot').info('机器人健康：正常')
    first=runtime_logs.read(limit=20)
    logger.write('目录清理','完成',['20 subtitles'],extra={'scope':'local'})
    next_page=runtime_logs.read(after=first['cursor'])
    assert len(next_page['logs'])==1 and '完成' in next_page['logs'][0]['message']
    assert next_page['cursor'] > first['cursor']
    archives=logger.read_archives()
    assert len(archives)==1 and archives[0]['category']=='目录清理'
    assert archives[0]['extra']['scope']=='local'


def test_runtime_retention_does_not_prune_cleanup_archives(state):
    logger.write('目录清理','permanent')
    with patch.object(runtime_logs,'MAX_LOGS',30):
        for i in range(120): logging.getLogger('strimkeep.bot').info('health %s',i)
    count=storage._conn().execute('SELECT COUNT(*) FROM runtime_logs').fetchone()[0]
    assert 30 <= count < 130
    assert logger.read_archives()[0]['title']=='permanent'


def test_concurrent_logs_and_state_writes_complete_without_nested_commit(state):
    store.save(state/'a.json', {'value':0})
    def run(i):
        store.save(state/('a%d.json'%i), {'value':i})
        logging.getLogger('strimkeep.bot').info('concurrent %s',i)
    with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(run,range(30)))
    assert all(store.read(state/('a%d.json'%i))=={'value':i} for i in range(30))
    assert len(runtime_logs.read(limit=100)['logs'])==30
    assert not storage._conn().in_transaction


def test_legacy_audit_import_deduplicates_existing_rows_and_is_idempotent(state):
    logger.RECORDS_DIR.mkdir()
    rec={'ts':'2026-01-01 00:00:00','category':'目录清理','title':'old','details':['x']}
    assert storage.db_add_audit(rec)
    (logger.RECORDS_DIR/'audit.jsonl').write_text(json.dumps(rec)+'\n'+json.dumps(dict(rec,title='new'))+'\n')
    assert logger.migrate_legacy()==1 and logger.migrate_legacy()==0
    assert len(logger.read_archives())==2


def test_api_logs_archives_download_and_auth(state, monkeypatch):
    monkeypatch.setenv('WEB_PASSWORD', 'temporary-test-password')
    from app.main import app
    from app.routers.deps import auth
    from fastapi.testclient import TestClient
    client=TestClient(app)
    assert client.get('/api/logs').status_code==401
    assert client.get('/api/logs/download').status_code==401
    app.dependency_overrides[auth]=lambda:True
    try:
        logger.write('目录清理','API completion',['done'])
        body=client.get('/api/logs?n=99999').json()
        assert body['logs'] and isinstance(body['cursor'],int)
        assert client.get('/api/logs?after='+str(body['cursor'])).json()['logs']==[]
        assert client.get('/api/plans').json()['archives'][0]['title']=='API completion'
        response=client.get('/api/logs/download')
        assert response.status_code==200 and 'API completion' in response.text
        assert 'attachment' in response.headers['content-disposition']
    finally: app.dependency_overrides.pop(auth,None)


def test_failed_runtime_db_write_stays_visible_then_flushes_once(state):
    storage._conn()
    with patch.object(storage,'_conn',side_effect=sqlite3.OperationalError('private sql')):
        logging.getLogger('strimkeep.bot').info('pending health')
        failed=runtime_logs.read()
        assert failed['degraded'] and failed['logs'][0]['message']=='pending health'
    logging.getLogger('strimkeep.bot').info('recovered')
    healthy=runtime_logs.read()
    assert not healthy['degraded']
    assert sum(r['message']=='pending health' for r in healthy['logs'])==1
    assert not any(r['operation'].startswith('runtime_logs_') for r in storage.storage_health()['issues'])


def test_runtime_redacts_tokens_and_keeps_multiline_messages(state):
    logging.getLogger('strimkeep.bot').warning('https://api.telegram.org/botSECRET/getUpdates?api_key=PRIVATE\nsecond line')
    row=runtime_logs.read()['logs'][-1]
    assert 'SECRET' not in row['message'] and 'PRIVATE' not in row['message'] and '\nsecond line' in row['message']


def test_sqlite_classifications_distinguish_common_db_failures(state):
    from app import storage_status
    for code,expected in [(sqlite3.SQLITE_BUSY,'busy'),(sqlite3.SQLITE_READONLY,'permission'),
                          (sqlite3.SQLITE_CORRUPT,'corrupt'),(sqlite3.SQLITE_FULL,'full'),
                          (sqlite3.SQLITE_CANTOPEN,'open')]:
        error=sqlite3.OperationalError('private query'); error.sqlite_errorcode=code
        assert storage_status._reason(error)[0]==expected


def test_restart_loads_config_and_notification_baseline_from_sql_without_json(state):
    config.save_config(dict(config.DEFAULTS, subscribe_interval_min='41'))
    store.save(engine.SUB_STATE_FILE, {'a':{'notified_episodes':['S01E01']}})
    env=dict(os.environ,AGENT_DATA=str(state.parent),TMDB_KEY='',TG_BOT_TOKEN='',WEB_PASSWORD='temporary')
    source="from app import config,engine; assert config.load_config()['subscribe_interval_min']=='41'; assert engine._load_sub_state()['a']['notified_episodes']==['S01E01']"
    result=subprocess.run([sys.executable,'-c',source],env=env,cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr[-1000:]


def test_package_and_cli_logging_share_one_stream_without_duplicate_rows(state):
    source = """
import logging, sys
from pathlib import Path
from app import engine, logger, runtime_logs
sys.path.insert(0, str(Path.cwd() / 'app'))
import engine as cli_engine
import logger as cli_logger
import runtime_logs as cli_logs
logging.getLogger('strimkeep.bot').info('one shared event')
for stream in (runtime_logs, cli_logs):
    assert sum(r['message'] == 'one shared event' for r in stream.read()['logs']) == 1
cli_engine.STATE_DIR = engine.STATE_DIR.parent / 'cli-state'
cli_engine.STATE_DIR.mkdir()
legacy = cli_engine.STATE_DIR / 'cli_document.json'
legacy.write_text('{"cli": true}')
assert cli_engine.db_doc_mirror_file('cli_document', legacy)
logging.getLogger('strimkeep.bot').info('independent applications')
for stream in (runtime_logs, cli_logs):
    assert sum(r['message'] == 'independent applications' for r in stream.read()['logs']) == 1
"""
    env = dict(os.environ, AGENT_DATA=str(state.parent), TMDB_KEY='', TG_BOT_TOKEN='')
    result = subprocess.run([sys.executable, '-c', source], env=env,
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr[-1000:]
