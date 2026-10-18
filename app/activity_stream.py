"""Authenticated revision hints backed by durable logs and task metadata."""
import asyncio
import json
from app import storage


def revision():
    # No provider calls or media scans. A single indexed log cursor survives
    # worker threads and transactions; task timestamps also cover cancellation.
    with storage._transaction() as con:
        cursor = con.execute('SELECT COALESCE(MAX(id),0) FROM runtime_logs').fetchone()[0]
        job = con.execute('SELECT COALESCE(MAX(updated),0),COUNT(*) FROM cloud_jobs').fetchone()
    from app import task_history
    return [cursor, job[0], job[1], task_history._REVISION]


async def updates(request, interval=1):
    previous = None
    ticks = 0
    yield 'retry: 5000\n\n'
    while not await request.is_disconnected():
        try:
            current = await asyncio.to_thread(revision)
        except Exception:
            yield 'event: degraded\ndata: {}\n\n'
            return  # Let the client fall back to authenticated JSON reads.
        if current != previous:
            previous = current
            yield 'event: activity\ndata: ' + json.dumps(current) + '\n\n'
        elif ticks % 15 == 0:
            yield ': alive\n\n'
        ticks += 1
        await asyncio.sleep(interval)
