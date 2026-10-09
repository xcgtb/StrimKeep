#!/usr/bin/env python3
"""Start real HTTP servers using runtime dependencies only; isolated data/media."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = Path.cwd() if __file__ == '<stdin>' else Path(__file__).resolve().parents[2]


def request(port, endpoint, auth=True, body=None):
    headers = {}
    if auth:
        headers['Authorization'] = 'Basic ' + base64.b64encode(b'admin:isolated-only').decode()
    if body is not None:
        headers['Content-Type'] = 'application/json'
        body = json.dumps(body).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{endpoint}', data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=3) as result:
        return result.read().decode()


def start_and_check(data, expected_interval, update=False):
    env = dict(os.environ, AGENT_DATA=str(data), WEB_USER='admin', WEB_PASSWORD='isolated-only',
               ALLOW_NO_AUTH='0', ENABLE_CD2_WATCHDOG='0', APP_VERSION='',
               TG_BOT_TOKEN='', TG_CHAT_ID='', TMDB_KEY='', EMBY_KEY='',
               EMBY_HOST='http://127.0.0.1:9', PYTHONDONTWRITEBYTECODE='1')
    for key in ('L_ROOT', 'S_ROOT', 'CLOUD_L_ROOT'):
        folder = data / key
        folder.mkdir(exist_ok=True)
        env[key] = str(folder)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryFile(mode='w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                                    '--host', '127.0.0.1', '--port', str(port)],
                                   cwd=REPO, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 12
            while True:
                if process.poll() is not None:
                    raise AssertionError('server exited before readiness')
                try:
                    assert json.loads(request(port, '/api/health', False))['status'] == 'ok'
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() >= deadline: raise AssertionError('HTTP startup timed out')
                    time.sleep(.1)
            try:
                request(port, '/api/config', False)
                raise AssertionError('private endpoint accepted an unauthenticated request')
            except urllib.error.HTTPError as error:
                assert error.code == 401
            source_version = re.search(r"__version__\s*=\s*'([^']+)'", (REPO/'app/version.py').read_text())[1]
            runtime = json.loads(request(port, '/api/runtime/status'))
            assert runtime['version'] == source_version, runtime['version']
            assert json.loads(request(port, '/api/config'))['config']['subscribe_interval_min'] == expected_interval
            assert json.loads(request(port, '/api/plans'))['status'] == 'success'
            html = request(port, '/', False)
            assert '<title>StrimKeep</title>' in html
            assets = re.findall(r'(?:src|href)="(/static/[^\"]+)"', html)
            assert assets
            for url in assets: assert request(port, url, False)
            if update:
                result = json.loads(request(port, '/api/config', body={'subscribe_interval_min':'42'}))
                assert result['status'] == 'success'
                assert json.loads(request(port, '/api/config'))['config']['subscribe_interval_min'] == '42'
        except Exception:
            log.seek(0)
            print(log.read()[-4000:], file=sys.stderr)
            raise
        finally:
            process.terminate()
            try: process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    with tempfile.TemporaryDirectory(prefix='strimkeep-release-') as temp:
        for historical in (False, True):
            data = Path(temp) / ('historical' if historical else 'fresh')
            (data/'state').mkdir(parents=True)
            fixture = {'subscribe_interval_min':'41', 'ingest_enabled':'0', 'subscribe_enabled':'0',
                       'tmdb_scan_enabled':'0', 'morning_report_enabled':'0'}
            (data/'config.json').write_text(json.dumps(fixture))
            if historical:
                with sqlite3.connect(data/'state/strimkeep.db') as con:
                    con.execute('CREATE TABLE docs(name TEXT PRIMARY KEY,payload TEXT NOT NULL,'
                                'version INTEGER NOT NULL,updated_at REAL NOT NULL)')
                    con.execute("INSERT INTO docs VALUES('retained','{}',7,1)")
            start_and_check(data, '41', update=True)
            start_and_check(data, '42')
            with sqlite3.connect(data/'state/strimkeep.db') as con:
                assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                if historical: assert con.execute("SELECT version FROM docs WHERE name='retained'").fetchone()[0] == 7
            assert json.loads((data/'config.json').read_text())['subscribe_interval_min'] == '41'
            print('PASS %s: live HTTP, authentication, assets, SQLite-only save and restart' %
                  ('historical required-version schema' if historical else 'fresh database'))
    print('PASS 2/2; real local HTTP servers, temporary data/media, no external API or messages')
    print('httpx installed: %s (not required by this verifier)' % (importlib.util.find_spec('httpx') is not None))


if __name__ == '__main__':
    main()
