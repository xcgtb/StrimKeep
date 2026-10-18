"""Production app + isolated empty media; no real 115/Emby/TG credentials."""
import http.cookiejar
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory(prefix='strimkeep-vue3-') as folder:
    data = Path(folder)
    env = dict(os.environ, AGENT_DATA=folder, WEB_USER='admin', WEB_PASSWORD='isolated-vue3-test',
               ENABLE_CD2_WATCHDOG='0', APP_VERSION='0.0.0+local', EMBY_KEY='', TMDB_KEY='',
               TG_BOT_TOKEN='', TG_CHAT_ID='', EMBY_HOST='http://127.0.0.1:9')
    for key in ('L_ROOT', 'S_ROOT', 'CLOUD_L_ROOT'):
        path = data / key
        path.mkdir()
        env[key] = str(path)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.ProxyHandler({}))
    def fetch(path, body=None):
        raw = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=raw,
              headers={'Content-Type': 'application/json', 'Origin': base} if raw else {})
        with client.open(req, timeout=5) as response:
            return response.read(), response.headers
    with tempfile.TemporaryFile(mode='w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    if json.loads(fetch('/api/ready')[0])['status'] == 'ok': break
                except OSError:
                    pass
                if process.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    raise AssertionError('App startup failed: ' + log.read()[-3000:])
                time.sleep(.1)
            html = fetch('/')[0].decode()
            assets = re.findall(r'(?:src|href)="(/static/[^\"]+)"', html)
            assert assets and '/static/js/' not in html
            for asset in assets:
                assert fetch(asset)[0]
            try:
                fetch('/api/cloud/output-directories')
                raise AssertionError('Unauthenticated output directory request accepted')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            login, headers = fetch('/api/login', {'username': 'admin', 'password': 'isolated-vue3-test'})
            assert json.loads(login)['status'] == 'success'
            assert 'HttpOnly' in headers['set-cookie'] and 'SameSite=lax' in headers['set-cookie']
            runtime = json.loads(fetch('/api/runtime/status')[0])
            assert runtime['readiness']['ready'] is True
            for endpoint, key in [('/api/overview', 'refresh'), ('/api/subscriptions', 'subscriptions'), ('/api/config', 'config'), ('/api/cloud/status', 'accounts'), ('/api/cloud/jobs', 'items')]:
                result = json.loads(fetch(endpoint)[0])
                assert key in result, (endpoint, result)
            reads = [('/api/strategy','strategy'),('/api/cover-strategy','strategy'),
              ('/api/governance/auto','settings'),('/api/governance/summary','scan'),
              ('/api/plans','plans'),('/api/cloud/settings','settings'),('/api/morning','morning'),
              ('/api/ingest/settings','settings'),('/api/subscriptions/status','check'),
              ('/api/logs','logs'),('/api/bot/status','bot'),('/api/records','records'),
              ('/api/tmdb/progress','progress'),('/api/upstream/config','settings'),
              ('/api/upstream/jobs','items'),('/api/cloud/assets','items'),
              ('/api/cloud/rules','items'),('/api/tasks/recent','items'),('/api/overview/playback','records'),('/api/wash/empty-dirs/browse','roots'),('/api/cloud/output-directories','roots')]
            for endpoint,key in reads:
                result=json.loads(fetch(endpoint)[0]);assert key in result,(endpoint,result)
            catalog=json.loads(fetch('/api/cloud/resources/catalog')[0])
            assert catalog==json.loads((ROOT/'frontend/tests/e2e/catalog.json').read_text()), catalog
            changes=json.loads(fetch('/api/library/changes?since=0')[0])
            assert isinstance(changes['version'],int) and isinstance(changes['full'],bool)
            assert isinstance(changes['series'],list) and isinstance(changes['movies'],list)
            assert 'has_cache' in json.loads(fetch('/api/ingest/status')[0])
            assert {'L_ROOT','S_ROOT','CLOUD_L_ROOT','DATA_DIR'} <= set(json.loads(fetch('/api/health')[0])['paths'])
            # Round-trip the actual settings contracts without media/network mutations.
            cloud=json.loads(fetch('/api/cloud/settings')[0])['settings']
            fields=(ROOT/'frontend/src/config/cloudFields.ts').read_text()
            special=(ROOT/'frontend/src/views/CloudSettingsView.vue').read_text()
            assert all(key in fields+special for key in cloud),set(cloud)-set(re.findall(r"key: '([^']+)'",fields))
            for endpoint,key in [('/api/strategy','strategy'),('/api/governance/auto','settings')]:
                original=json.loads(fetch(endpoint)[0])[key]
                saved=json.loads(fetch(endpoint,original)[0]);assert saved[key]==original,(endpoint,saved)
            cover=json.loads(fetch('/api/cover-strategy')[0])['strategy']
            assert json.loads(fetch('/api/cover-strategy',{'strategy':cover})[0])['strategy']==cover
            saved=json.loads(fetch('/api/cloud/settings',{'operation_delay_seconds':0})[0])['settings']
            assert saved==dict(cloud,operation_delay_seconds=0)
            assert json.loads(fetch('/api/cloud/settings',{'operation_delay_seconds':cloud['operation_delay_seconds']})[0])['settings']==cloud
            naming=json.loads(fetch('/api/cloud/naming/preview',{'settings':{'naming_style':'template'}})[0])
            assert len(naming['items'])==2
            morning=json.loads(fetch('/api/morning')[0])['morning']
            patch={k:morning[k] for k in ('enabled','hour','minute','items','prescan_min')}
            assert json.loads(fetch('/api/morning',patch)[0])['morning']==morning
            assert json.loads(fetch('/api/emby/library/page?media=series&limit=40')[0])['status'] == 'nocache'
            assert json.loads(fetch('/api/logout', {})[0])['status'] == 'success'
            try:
                fetch('/api/config')
                raise AssertionError('Logout did not revoke browser cookie')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            print('PASS real FastAPI: production assets, HTTPOnly login, cookie session, 32 API reads, catalogue parity, library changes, ingest/paths status, configuration round trips, empty-library status and logout')
        finally:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
