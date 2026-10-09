#!/usr/bin/env python3
"""Historical docs schema startup regression; temporary databases, no APIs/media."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

REPO = Path.cwd() if __file__ == '<stdin>' else Path(__file__).resolve().parents[2]


def check_case(root, historical, mode):
    data = root / ('legacy' if historical else 'v3') / mode
    state = data / 'state'
    state.mkdir(parents=True)
    con = sqlite3.connect(state / 'strimkeep.db')
    version_column = 'version INTEGER NOT NULL,' if historical else ''
    con.execute('CREATE TABLE docs(name TEXT PRIMARY KEY,payload TEXT NOT NULL,'
                + version_column + 'updated_at REAL NOT NULL,owner_tag TEXT DEFAULT "retained")')
    if historical:
        con.execute("INSERT INTO docs(name,payload,version,updated_at) VALUES('existing','{\"old\":true}',7,1)")
    else:
        con.execute("INSERT INTO docs(name,payload,updated_at) VALUES('existing','{\"old\":true}',1)")
    con.execute('PRAGMA user_version = %d' % (2 if historical else 3))
    con.commit()
    con.close()
    (data / 'config.json').write_text(json.dumps({'subscribe_interval_min': '41'}))
    source = '''
import asyncio, json, sys
from pathlib import Path
if sys.argv[1] == 'cli':
    sys.path.insert(0,str(Path.cwd()/'app'))
    import engine,storage,state_store,config
else:
    from app import engine,storage,state_store,config
assert config.load_config()['subscribe_interval_min']=='41'
storage.db_migrate()
assert storage.db_doc_get('existing')=={'old':True}
assert storage.db_doc_set('existing',{'new':True})
row=storage._conn().execute("SELECT version,owner_tag FROM docs WHERE name='existing'").fetchone()
assert row==(8 if sys.argv[2]=='legacy' else 2,'retained'),row
config.save_config(dict(config.DEFAULTS,subscribe_interval_min='42'))
assert config.load_config()['subscribe_interval_min']=='42'
assert json.loads(config.CONFIG_FILE.read_text())['subscribe_interval_min']=='41'
state_store.save(engine.SUB_STATE_FILE,{})
state_store.save(engine.STATE_DIR/'tmdb_cache.json',{})
assert storage._conn().execute('PRAGMA user_version').fetchone()[0]==storage.SCHEMA_VERSION
from app.main import app
from app.routers.deps import auth
app.dependency_overrides[auth]=lambda:True
async def request(endpoint):
    messages=[]
    received=False
    async def receive():
        nonlocal received
        if not received:
            received=True
            return {'type':'http.request','body':b'','more_body':False}
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
    scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.3'},
           'http_version':'1.1','method':'GET','scheme':'http',
           'path':endpoint,'raw_path':endpoint.encode(),'query_string':b'',
           'root_path':'','headers':[(b'host',b'testserver')],
           'client':('127.0.0.1',1),'server':('testserver',80)}
    await app(scope,receive,send)
    status=next(m['status'] for m in messages if m['type']=='http.response.start')
    assert status==200,(endpoint,status)
async def check_endpoints():
    for endpoint in ('/','/api/health','/api/config','/api/runtime/status','/api/plans'):
        await request(endpoint)
asyncio.run(check_endpoints())
'''
    env = dict(os.environ, AGENT_DATA=str(data), WEB_PASSWORD='isolated-only',
               ENABLE_CD2_WATCHDOG='0', TG_BOT_TOKEN='isolated-only', TMDB_KEY='isolated-only',
               EMBY_HOST='http://127.0.0.1:9', EMBY_KEY='isolated-only')
    result = subprocess.run([sys.executable, '-c', source, mode, 'legacy' if historical else 'v3'],
                            cwd=REPO, env=env, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stderr[-2000:]


def main():
    with tempfile.TemporaryDirectory(prefix='strimkeep-docs-compat-') as temp:
        root = Path(temp)
        for historical in (True, False):
            for mode in ('package', 'cli'):
                check_case(root, historical, mode)
                print('PASS %s docs schema: %s startup, migration, writes and HTTP endpoints' %
                      ('required-version' if historical else 'three-column', mode))
    print('PASS 4/4; temporary databases only; no real NAS/cloud media, API calls or messages')


if __name__ == '__main__':
    main()
