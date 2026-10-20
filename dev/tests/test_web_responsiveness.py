"""Concurrency regressions: storage contention must not freeze the whole Web UI."""
import asyncio
import subprocess
import sys
import threading
from pathlib import Path

from app.routers import library


def test_document_writer_and_nested_transaction_reader_finish(tmp_path):
    # Run in a child so a regression cannot leave deadlocked threads/locks in
    # pytest. This is the same access pattern as ingestion's settings.get()
    # inside store.db(), racing a document/configuration write.
    script = r'''
import os, sys, threading
from contextlib import contextmanager
from pathlib import Path
os.environ['AGENT_DATA'] = sys.argv[1]
from app import storage, state_store
path = Path(sys.argv[1]) / 'state' / 'concurrency.json'
state_store.save(path, {'value': 0})
entered = threading.Event()
attempted = threading.Event()
finished = threading.Event()
errors = []
original = state_store._access
@contextmanager
def observed(path, operation):
    if threading.current_thread().name == 'document-writer':
        entered.set()
    with original(path, operation) as value:
        yield value
state_store._access = observed
def writer():
    attempted.set()
    try:
        state_store.save(path, {'value': 1})
    except Exception as error:
        errors.append(type(error).__name__)
    finally:
        finished.set()
with storage._transaction():
    thread = threading.Thread(target=writer, name='document-writer', daemon=True)
    thread.start()
    assert attempted.wait(1)
    # In the old implementation this means the writer holds the document lock
    # and is waiting for our SQLite lock. With a shared lock it cannot enter.
    entered.wait(.2)
    assert state_store.read(path)['value'] == 0
assert finished.wait(2), 'document write deadlocked'
thread.join(1)
assert not errors, errors
assert state_store.read(path)['value'] == 1
print('both operations completed')
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, timeout=8)
    assert result.returncode == 0, result.stderr
    assert 'both operations completed' in result.stdout


def test_library_stream_does_not_wait_on_storage_in_event_loop(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    reader_threads = []

    def slow_changes(since):
        reader_threads.append(threading.get_ident())
        entered.set()
        release.wait(1)
        return {'version': 0}

    monkeypatch.setattr(library.library_updates, 'changes', slow_changes)

    async def exercise():
        response = await library.api_library_events(None)
        pending = asyncio.create_task(anext(response.body_iterator))
        try:
            # A blocked synchronous read must leave unrelated async requests
            # runnable. The old generator blocks here until release's timeout.
            await asyncio.sleep(.05)
            assert not pending.done(), 'storage wait blocked the event loop'
            assert entered.is_set()
            assert reader_threads[0] != threading.get_ident()
        finally:
            release.set()
            await pending
            await response.body_iterator.aclose()

    asyncio.run(exercise())
