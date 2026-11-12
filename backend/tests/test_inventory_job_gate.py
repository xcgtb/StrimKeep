import asyncio
from strimkeep.organizer import Organizer


def test_job_claim_waits_for_inventory_publication_gate():
    async def run():
        engine = object.__new__(Organizer)
        engine.closing = False
        engine.publication_gate = asyncio.Lock()
        called = []
        def next_job(lane):
            called.append(lane)
            engine.closing = True
            return None
        engine.next_lane_job = next_job
        await engine.publication_gate.acquire()
        task = asyncio.create_task(engine.loop('organize'))
        await asyncio.sleep(.05)
        assert called == []
        engine.publication_gate.release()
        await asyncio.wait_for(task, 2)
        assert called == ['organize']
    asyncio.run(run())
