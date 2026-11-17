"""Run web and worker together; fail the container if either exits."""
import os
import signal
import subprocess
import sys
import time

web = subprocess.Popen([
    sys.executable, '-m', 'uvicorn', 'strimkeep.main:create_app', '--factory',
    '--host', '0.0.0.0', '--port', '9000', '--workers', '1', '--no-proxy-headers',
])
worker = subprocess.Popen([sys.executable, '-m', 'strimkeep.worker'])
stopping = False


def stop(signum, _frame):
    global stopping
    stopping = True
    for child in (worker, web):
        if child.poll() is None:
            child.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

try:
    while True:
        if worker.poll() is not None and not stopping:
            print('worker exited; stopping web', flush=True)
            stop(signal.SIGTERM, None)
            break
        if web.poll() is not None and not stopping:
            print('web exited; stopping worker', flush=True)
            stop(signal.SIGTERM, None)
            break
        time.sleep(1)
finally:
    for child in (worker, web):
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
sys.exit(worker.returncode or web.returncode or 0)
