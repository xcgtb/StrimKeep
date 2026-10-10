#!/usr/bin/env python3
"""Real loopback HTTP/SSE smoke test in an isolated temporary database.

No NAS media mounts, Emby credentials, bot token or production state required.
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = Path(__file__).resolve().parents[2]
AUTH = 'Basic ' + base64.b64encode(b'p1-smoke:temp-test-only').decode()


def request(port, path, *, authenticated=True, timeout=4):
    headers = {'Authorization': AUTH} if authenticated else {}
    return urllib.request.urlopen(urllib.request.Request(
        f'http://127.0.0.1:{port}{path}', headers=headers), timeout=timeout)


def main():
    with tempfile.TemporaryDirectory(prefix='p1-sse-check-') as directory, tempfile.TemporaryFile(mode='w+') as log:
        data = Path(directory)
        env = os.environ.copy()
        env.update(AGENT_DATA=str(data), WEB_USER='p1-smoke', WEB_PASSWORD='temp-test-only',
                   ALLOW_NO_AUTH='0', EMBY_KEY='', EMBY_HOST='http://127.0.0.1:9',
                   TMDB_KEY='', TG_BOT_TOKEN='', TG_CHAT_ID='', ENABLE_CD2_WATCHDOG='0',
                   PYTHONDONTWRITEBYTECODE='1')
        for key in ('L_ROOT', 'S_ROOT', 'CLOUD_L_ROOT'):
            path = data / key
            path.mkdir()
            env[key] = str(path)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                                    '--host', '127.0.0.1', '--port', str(port)],
                                   cwd=REPO, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    with request(port, '/api/health', authenticated=False, timeout=1) as resp:
                        if resp.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    time.sleep(.15)
            else:
                raise AssertionError('P1 loopback server did not start')
            for path in ['/api/library/events', '/api/library/changes?since=0']:
                try:
                    request(port, path, authenticated=False)
                except urllib.error.HTTPError as error:
                    assert error.code == 401, (path, error.code)
                else:
                    raise AssertionError(f'{path} missing authentication')
            with request(port, '/api/library/changes?since=0') as resp:
                initial = json.load(resp)
                assert initial['status'] == 'success' and initial['version'] == 0
            # Safari's EventSource sends signed login cookies, not custom Basic
            # headers. Verify the actual login/SSE cookie path independently.
            login = urllib.request.Request(f'http://127.0.0.1:{port}/api/login',
                data=json.dumps({'username':'p1-smoke','password':'temp-test-only'}).encode(),
                headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(login, timeout=3) as resp:
                cookie = resp.headers.get('Set-Cookie', '').split(';')[0]
                assert resp.status == 200 and cookie.startswith('strimkeep_session=')
            cookie_request = urllib.request.Request(f'http://127.0.0.1:{port}/api/library/events',
                                                    headers={'Cookie': cookie})
            with urllib.request.urlopen(cookie_request, timeout=3) as resp:
                assert 'text/event-stream' in resp.headers['Content-Type']
                assert resp.readline().decode().strip() == 'retry: 5000'
            with request(port, '/api/library/events', timeout=12) as stream:
                assert stream.status == 200
                assert 'text/event-stream' in stream.headers['Content-Type']
                assert 'no-cache' in stream.headers.get('Cache-Control', '')
                first = stream.readline().decode().strip()
                assert first == 'retry: 5000', first
                subprocess.run([sys.executable, '-c',
                                "from app import library_updates; library_updates.publish(['verified-series'])"],
                               cwd=REPO, env=env, check=True, timeout=10)
                lines = []
                deadline = time.monotonic() + 11
                while time.monotonic() < deadline:
                    line = stream.readline().decode().strip()
                    lines.append(line)
                    if line.startswith('data: '):
                        break
                assert 'event: library' in lines, lines
                assert 'data: 1' in lines, lines
                assert not any('/media/' in line or 'temp-test-only' in line for line in lines)
            with request(port, '/api/library/changes?since=0') as resp:
                delta = json.load(resp)
                assert delta['version'] == 1
                # Mapping cache was not initialized: must conservatively request full refresh.
                assert delta['full'] and not delta['series'], delta
            with request(port, '/api/library/changes?since=1') as resp:
                same = json.load(resp)
                assert same['version'] == 1 and not same['full']
            print('PASS: actual HTTP startup, SSE streaming, auth, revision event, no secrets, conservative full reconciliation')
        except Exception:
            log.seek(0)
            print(log.read()[-4500:], file=sys.stderr)
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
