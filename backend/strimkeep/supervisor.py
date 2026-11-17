"""Role supervisor: keep Web and Worker independently available.

Borrowed from mature supervisors (Gunicorn/Celery): a crashed child is
restarted in place with backoff; the container only dies when the supervisor
itself cannot maintain the roles. Temporary worker unavailability is exposed
by /health/ready, not by taking down a healthy Web process.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path(os.environ.get('STRIMKEEP_DATA_DIR', '/data'))
STATE_DB = DATA / 'data/state.db'
BACKOFF_MIN = 2
BACKOFF_MAX = 60
HEARTBEAT_TIMEOUT = 45
HEALTH_URL = 'http://127.0.0.1:9000/health/live'

ROLES = {
    'web': [sys.executable, '-m', 'uvicorn', 'strimkeep.main:create_app', '--factory',
            '--host', '0.0.0.0', '--port', '9000', '--workers', '1', '--no-proxy-headers'],
    'worker': [sys.executable, '-m', 'strimkeep.worker'],
}

processes = {}
restart_at = {}
backoff = {'web': BACKOFF_MIN, 'worker': BACKOFF_MIN}
stopping = False


def log(message):
    # Keep the supervisor out of SQLite: it must not compete with Web startup
    # migrations or Worker heartbeats. stdout is captured by docker logs.
    print('[supervisor] %s' % message, flush=True)


def spawn(role):
    processes[role] = subprocess.Popen(ROLES[role])
    restart_at[role] = time.monotonic()
    log('%s started (pid=%s)' % (role, processes[role].pid))


def stop(signum, _frame):
    global stopping
    stopping = True
    log('stopping (signal=%s)' % signum)
    for role, process in processes.items():
        if process and process.poll() is None:
            process.terminate()


def reap(role):
    process = processes.get(role)
    if process is None:
        return
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    processes[role] = None


def web_ready():
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def heartbeat_stale():
    try:
        connection = sqlite3.connect('file:%s?mode=ro' % STATE_DB, uri=True, timeout=2)
        row = connection.execute(
            "SELECT value FROM meta WHERE key='worker_heartbeat'").fetchone()
        connection.close()
        if not row:
            return True
        # set_meta JSON-encodes values.
        raw = row[0]
        try:
            value = json.loads(raw) if raw[:1] in ('"', "'", '-', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9') else raw
        except Exception:
            value = raw
        return time.time() - float(value) > HEARTBEAT_TIMEOUT
    except Exception:
        return False


def should_restart(role):
    process = processes.get(role)
    if process is None:
        return True
    code = process.poll()
    if code is not None:
        log('%s exited (code=%s); restarting in %ss' % (role, code, backoff[role]))
        reap(role)
        return True
    if role == 'worker' and time.monotonic() - restart_at.get(role, 0) > HEARTBEAT_TIMEOUT:
        if heartbeat_stale():
            log('worker heartbeat stale; restarting worker in %ss' % backoff[role])
            process.terminate()
            reap(role)
            return True
    return False


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

spawn('web')
# Web startup runs SQLite migrations (share URL repair, schema checks). Starting
# Worker at the same time caused "database is locked" on first boot; wait until
# Web is accepting health checks before letting Worker write heartbeat/events.
for _ in range(60):
    if web_ready() or processes['web'].poll() is not None:
        break
    time.sleep(1)

try:
    while not stopping:
        for role in ROLES:
            if role == 'worker' and processes.get('worker') is None and processes.get('web') is not None:
                if not web_ready():
                    # Web is still (re)starting; retry worker next tick.
                    continue
            if should_restart(role):
                time.sleep(backoff[role])
                if not stopping:
                    spawn(role)
                    backoff[role] = min(BACKOFF_MAX, backoff[role] * 2)
            else:
                backoff[role] = BACKOFF_MIN
        time.sleep(1)
finally:
    for role in ROLES:
        reap(role)
    log('stopped')
