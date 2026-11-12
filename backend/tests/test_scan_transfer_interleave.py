import asyncio

from test_organize import CONFIG, new_job, setup
from test_transfer import mock_share


def test_transfer_can_finish_between_scanned_directories(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        cloud.nodes['101'] = {'id': '101', 'name': '已有目录', 'parent_id': '100', 'is_dir': True}
        cloud.add_video('1000', parent='101')
        calls = mock_share(cloud)
        transfer_id = (await engine.transfer.enqueue('https://115.com/s/abc', '100'))[0]
        scan_id = new_job(store)
        engine.update_job(scan_id, state='running')
        actual_children = cloud.children
        root_entered, finish_root, child_entered, finish_child = (asyncio.Event() for _ in range(4))

        async def fresh(cid):
            if str(cid) == '100':
                root_entered.set()
                await finish_root.wait()
            if str(cid) == '101':
                child_entered.set()
                await finish_child.wait()
            return await actual_children(cid)

        cloud.fresh_children = fresh
        scan = asyncio.create_task(engine.manifest(scan_id, dict(CONFIG)))
        transfer = None
        try:
            await asyncio.wait_for(root_entered.wait(), 2)
            transfer = asyncio.create_task(engine.run_job(transfer_id))
            for _ in range(100):
                if engine.job(transfer_id)['stage'] == '等待目录扫描或其他转存核对': break
                await asyncio.sleep(.01)
            assert engine.job(transfer_id)['stage'] == '等待目录扫描或其他转存核对'
            assert not calls
            finish_root.set()
            await asyncio.wait_for(transfer, 3)
            await asyncio.wait_for(child_entered.wait(), 2)
            assert engine.job(transfer_id)['state'] == 'completed'
            assert len(calls) == 1 and not scan.done()
            finish_child.set()
            await asyncio.wait_for(scan, 3)
            found = [r[0] for r in store.db.execute('SELECT file_id FROM organize_items WHERE job_id=?', (scan_id,))]
            assert found == ['1000']  # 本轮根目录快照形成后转入，新内容留待下一轮。
        finally:
            finish_root.set(); finish_child.set()
            for task in (scan, transfer):
                if task and not task.done(): task.cancel()
            await asyncio.gather(*(t for t in (scan, transfer) if t), return_exceptions=True)
            await http.aclose(); store.close()
    asyncio.run(scenario())


def test_scan_waits_until_transfer_receipt_is_verified(tmp_path):
    async def scenario():
        store, cloud, http, engine, _ = setup(tmp_path)
        mock_share(cloud)
        original_receive = cloud.share_receive
        received, finish_receive, scan_entered = (asyncio.Event() for _ in range(3))
        async def slow_receive(*args):
            await original_receive(*args)
            received.set()
            await finish_receive.wait()
        cloud.share_receive = slow_receive
        transfer_id = (await engine.transfer.enqueue('https://115.com/s/abc', '100'))[0]
        scan_id = new_job(store)
        engine.update_job(scan_id, state='running')
        actual_children = cloud.children
        async def fresh(cid):
            scan_entered.set()
            return await actual_children(cid)
        cloud.fresh_children = fresh
        transfer = asyncio.create_task(engine.run_job(transfer_id))
        scan = None
        try:
            await asyncio.wait_for(received.wait(), 2)
            scan = asyncio.create_task(engine.manifest(scan_id, dict(CONFIG)))
            await asyncio.sleep(.05)
            assert not scan_entered.is_set()
            finish_receive.set()
            await asyncio.wait_for(transfer, 3)
            await asyncio.wait_for(scan, 3)
            assert engine.job(transfer_id)['state'] == 'completed'
            assert store.db.execute('SELECT file_id FROM organize_items WHERE job_id=?', (scan_id,)).fetchone()[0] == '7000'
        finally:
            finish_receive.set()
            for task in (transfer, scan):
                if task and not task.done(): task.cancel()
            await asyncio.gather(*(t for t in (transfer, scan) if t), return_exceptions=True)
            await http.aclose(); store.close()
    asyncio.run(scenario())
