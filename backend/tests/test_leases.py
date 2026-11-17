import asyncio
import time

from strimkeep.leases import reap_stale_jobs


class FakeJobs:
    def __init__(self):
        self.updated = []

    def update(self, job_id, **values):
        self.updated.append((job_id, values))
        return {}


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params):
        class Cursor:
            def fetchall(self):
                return [{'id': row} for row in self_rows]
        self_rows = self.rows
        return Cursor()


class FakeStore:
    def __init__(self, rows):
        self.db = FakeDB(rows)
        self.jobs = FakeJobs()
        self.events = []

    def event(self, kind, message):
        self.events.append((kind, message))


def test_reap_stale_jobs_moves_running_to_needs_review():
    store = FakeStore([7, 9])
    changed = reap_stale_jobs(store, now=1000, timeout=100)
    assert changed == 2
    assert [job_id for job_id, _ in store.jobs.updated] == [7, 9]
    for _, values in store.jobs.updated:
        assert values['state'] == 'needs_review'
        assert '租约超时' in values['stage']
    assert store.events and '2 个任务' in store.events[0][1]


def test_reap_stale_jobs_noop_when_fresh():
    store = FakeStore([])
    assert reap_stale_jobs(store, now=1000, timeout=100) == 0
    assert not store.jobs.updated
