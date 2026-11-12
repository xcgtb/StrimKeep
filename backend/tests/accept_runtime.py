"""运行时验收：在真实镜像里对新代码做 HTTP 级检查（不依赖 115/Emby）。

执行：docker exec <容器> python /acc/accept.py
"""
import http.cookiejar
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = 'http://127.0.0.1:9000'
PASSWORD = os.environ.get('SK_ACCEPT_PASSWORD', 'acc-password-1234')

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
results = []


def call(method, path, body=None, csrf=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    if csrf:
        request.add_header('X-CSRF-Token', csrf)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with opener.open(request, timeout=25) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b'{}')
        except ValueError:
            return exc.code, {}


def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))


DEAD = {'Event': 'library.deleted', 'Item': {'Type': 'Movie', 'Path': '/strimkeep/影视媒体库/电影/x/x.strm'}}

status, _ = call('GET', '/health/ready')
check('health/ready 200', status == 200, status)

status, body = call('POST', '/api/v1/auth/login', {'username': 'admin', 'password': PASSWORD})
csrf = body.get('csrf')
check('login + csrf', status == 200 and bool(csrf), status)

status, body = call('GET', '/api/v1/sync/config')
check('sync config readable', status == 200 and 'config' in body, status)

status, _ = call('POST', '/api/v1/emby/webhook?token=wrong-token', DEAD)
check('webhook rejects wrong token', status == 403, status)

# 默认只读：不轮换令牌。轮换会让已经填进 Emby 的 Webhook URL 立刻失效，
# 所以只有显式 SK_ACCEPT_ROTATE=1 时才做完整的轮换验证。
token = os.environ.get('SK_ACCEPT_TOKEN', '') or (body.get('config') or {}).get('webhook_token') or ''
if os.environ.get('SK_ACCEPT_ROTATE') == '1':
    status, body = call('POST', '/api/v1/sync/webhook-token', {}, csrf)
    token = body.get('webhook_token')
    check('webhook token >=32 chars', status == 200 and isinstance(token, str) and len(token) >= 32,
          (status, len(token or '')))

    status, body = call('GET', '/api/v1/sync/config')
    check('token persisted to sync_config', body.get('config', {}).get('webhook_token') == token)

    status, _ = call('POST', '/api/v1/emby/webhook', DEAD, headers={'X-StrimKeep-Token': token, 'X-StrimKeep-Acceptance': '1'})
    check('webhook accepts X-StrimKeep-Token header', status in (200, 409), status)

    status, body = call('POST', '/api/v1/sync/webhook-token', {}, csrf)
    rotated = body.get('webhook_token')
    check('rotation issues a different token', bool(rotated) and rotated != token)

    status, _ = call('POST', '/api/v1/emby/webhook?token=' + str(token), DEAD)
    check('old token rejected after rotation', status == 403, status)
elif token:
    status, _ = call('POST', '/api/v1/emby/webhook', DEAD, headers={'X-StrimKeep-Token': token, 'X-StrimKeep-Acceptance': '1'})
    check('webhook accepts X-StrimKeep-Token header', status in (200, 409), status)
else:
    check('webhook token left untouched (read-only)', True, 'set SK_ACCEPT_TOKEN to verify auth')

status, body = call('GET', '/api/v1/tasks')
check('tasks endpoint alive', status == 200 and 'active' in body, status)

database = sqlite3.connect('/data/data/state.db')
tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")}
check('retired derived tables are gone', not ({'output_stats', 'media_info_outputs'} & tables))
check('emby_outbox table created', 'emby_outbox' in tables)
version = database.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
check('existing schema version intact', version in (None, ('1',), ('2',)), version)
database.close()

for name, ok, detail in results:
    print(('PASS' if ok else 'FAIL'), name, '' if ok else detail)
failed = [row for row in results if not row[1]]
print('SUMMARY passed=%d failed=%d' % (len(results) - len(failed), len(failed)))
sys.exit(1 if failed else 0)
