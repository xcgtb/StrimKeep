# -*- coding: utf-8 -*-
"""
SQLite docs 表（第一阶段：文件为主、库镜像 + 损坏自愈）的测试。

覆盖：
  - 文档读写 / schema 版本 / 老库（无 docs 表）打开自动补表
  - db_migrate 把既有 JSON 文件镜像进库，且幂等
  - 4 个状态文件的写入点都会镜像进库
  - 文件损坏 → 从库自愈；文件缺失 → 维持原语义（不复活旧状态）
"""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='strimkeep_docs_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''

sys.path.insert(0, str(Path(__file__).parents[2] / 'app'))
import engine  # noqa: E402
import storage  # noqa: E402
import wash  # noqa: E402


_ATTRS = ('STATE_DIR', 'WASH_RESIDUAL_FILE', 'GOV_LATEST_FILE', 'MANUAL_DONE_FILE', 'DATA_DIR')


def _fresh_state():
    """换一个全新的 state 目录（库按路径缓存，换目录即换库）。
    storage._state_dir 默认取 app.engine（与顶层 engine 是两个模块实例），这里显式钉住。"""
    d = Path(tempfile.mkdtemp(prefix='state_', dir=_TMP))
    engine.STATE_DIR = d
    engine.WASH_RESIDUAL_FILE = d / 'wash_residuals.json'
    engine.GOV_LATEST_FILE = d / 'gov_latest.json'
    engine.MANUAL_DONE_FILE = d / 'manual_done.json'
    storage._state_dir = lambda: d
    return d


def _isolated(fn):
    """每个测试结束必须还原全局：本 runner 不调用 teardown，而 storage/engine 是进程内共享的，
    不还原会污染后面的测试模块。"""
    def wrapper(*a, **kw):
        saved = {k: getattr(engine, k) for k in _ATTRS}
        saved_sd = storage._state_dir
        saved_ga = None
        try:
            import scheduler
            saved_ga = scheduler.GOV_AUTO_FILE
        except Exception:
            scheduler = None
        try:
            return fn(*a, **kw)
        finally:
            for k, v in saved.items():
                setattr(engine, k, v)
            storage._state_dir = saved_sd
            if scheduler is not None and saved_ga is not None:
                scheduler.GOV_AUTO_FILE = saved_ga
    wrapper.__name__ = fn.__name__
    return wrapper


class TestDocsTable:
    def test_roundtrip_and_default(self):
        _fresh_state()
        assert storage.db_doc_get('nope', 'dflt') == 'dflt'
        assert storage.db_doc_set('a', {'x': [1, 2], '中': '文'}) is True
        assert storage.db_doc_get('a') == {'x': [1, 2], '中': '文'}
        storage.db_doc_set('a', {'x': 3})          # 覆盖
        assert storage.db_doc_get('a') == {'x': 3}

    def test_schema_version_is_set(self):
        d = _fresh_state()
        storage.db_doc_set('a', 1)
        con = sqlite3.connect(str(d / storage.DB_NAME))
        assert con.execute('PRAGMA user_version').fetchone()[0] == storage.SCHEMA_VERSION
        con.close()

    def test_old_v1_db_without_docs_table_is_upgraded_in_place(self):
        d = _fresh_state()
        # 造一个「老库」：只有 kv 表，user_version=0，且带数据
        con = sqlite3.connect(str(d / storage.DB_NAME))
        con.execute('CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT, ts REAL)')
        con.execute("INSERT INTO kv VALUES ('keep', 'me', 0)")
        con.commit()
        con.close()
        assert storage.db_kv_get('keep') == 'me'     # 旧数据不丢
        assert storage.db_doc_set('a', 1) is True    # 新表自动补上
        assert storage.db_doc_get('a') == 1

    def test_mirror_file_is_idempotent_and_ignores_bad_files(self):
        d = _fresh_state()
        f = d / 'x.json'
        f.write_text(json.dumps({'k': 1}), encoding='utf-8')
        assert storage.db_doc_mirror_file('x', f) is True
        assert storage.db_doc_mirror_file('x', f) is False      # 库不旧于文件 → 不重复写
        assert storage.db_doc_mirror_file('missing', d / 'none.json') is False
        bad = d / 'bad.json'
        bad.write_text('{broken', encoding='utf-8')
        assert storage.db_doc_mirror_file('bad', bad) is False
        assert storage.db_doc_get('bad') is None


class TestMigrateMirrorsFiles:
    def test_migrate_imports_existing_files_once(self):
        d = _fresh_state()
        (d / 'wash_residuals.json').write_text(json.dumps({'version': 1, 'items': [{'a': 1}]}), encoding='utf-8')
        (d / 'manual_done.json').write_text(json.dumps({'101': {'name': 'x', 'ts': 1}}), encoding='utf-8')
        first = storage.db_migrate()
        assert first['documents'] == 2
        assert storage.db_doc_get('wash_residuals')['items'] == [{'a': 1}]
        assert storage.db_doc_get('manual_done') == {'101': {'name': 'x', 'ts': 1}}
        second = storage.db_migrate()
        assert second['documents'] == 0          # 幂等：重复启动不重复导入


class TestMirrorOnWrite:
    def test_wash_residuals_are_mirrored(self):
        _fresh_state()
        wash._save_wash_residuals([{'p': 'a'}])
        assert storage.db_doc_get('wash_residuals') == {'version': 1, 'items': [{'p': 'a'}]}
        assert wash._load_wash_residuals() == [{'p': 'a'}]

    def test_wash_residuals_corrupt_file_heals_from_db(self):
        _fresh_state()
        wash._save_wash_residuals([{'p': 'a'}])
        engine.WASH_RESIDUAL_FILE.write_text('{oops', encoding='utf-8')
        assert wash._load_wash_residuals() == [{'p': 'a'}]

    def test_wash_residuals_missing_file_is_not_resurrected(self):
        _fresh_state()
        wash._save_wash_residuals([{'p': 'a'}])
        engine.WASH_RESIDUAL_FILE.unlink(missing_ok=True)
        assert wash._load_wash_residuals() == [{'p': 'a'}]

    def test_latest_scan_is_mirrored_and_heals(self):
        _fresh_state()
        engine.save_latest_scan('pid1', {'n': 3})
        doc = storage.db_doc_get('gov_latest')
        assert doc['plan_id'] == 'pid1' and doc['result']['n'] == 3
        engine.GOV_LATEST_FILE.write_text('not json', encoding='utf-8')
        assert engine.load_latest_scan()['plan_id'] == 'pid1'
        engine.GOV_LATEST_FILE.unlink()
        assert engine.load_latest_scan()['plan_id'] == 'pid1'

    def test_manual_done_corrupt_file_heals(self):
        d = _fresh_state()
        storage.db_doc_set('manual_done', {'7': {'name': 'n', 'ts': 1}})
        engine.MANUAL_DONE_FILE.write_text('{', encoding='utf-8')
        assert engine.read_manual_done() == {'7': {'name': 'n', 'ts': 1}}
        engine.MANUAL_DONE_FILE.unlink()
        assert engine.read_manual_done() == {'7': {'name': 'n', 'ts': 1}}

    def test_gov_auto_mirror_and_heal(self):
        import scheduler
        eng = scheduler.engine          # scheduler 持有的 engine 可能与顶层 engine 是不同模块实例
        d = _fresh_state()
        saved = (eng.STATE_DIR, eng.DATA_DIR)
        try:
            eng.STATE_DIR = eng.DATA_DIR = d
            scheduler.GOV_AUTO_FILE = d / 'gov_auto.json'
            scheduler.gov_auto_save({'enabled': True, 'interval_hours': 12})
            assert eng.db_doc_get('gov_auto')['interval_hours'] == 12
            scheduler.GOV_AUTO_FILE.write_text('x', encoding='utf-8')
            assert scheduler.gov_auto_load()['interval_hours'] == 12      # 损坏 → 库自愈
            scheduler.GOV_AUTO_FILE.unlink()
            assert scheduler.gov_auto_load()['interval_hours'] == 12       # 缺失 → 默认值
        finally:
            eng.STATE_DIR, eng.DATA_DIR = saved


for _cls in (TestDocsTable, TestMigrateMirrorsFiles, TestMirrorOnWrite):
    for _n in [n for n in dir(_cls) if n.startswith('test_')]:
        setattr(_cls, _n, _isolated(getattr(_cls, _n)))
