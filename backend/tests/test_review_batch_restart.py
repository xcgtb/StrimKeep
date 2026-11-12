import asyncio,json
from test_organize import setup,new_job,CONFIG

def test_batch_restart(tmp_path):
 async def go():
  store,cloud,http,engine,_=setup(tmp_path)
  for i in (1,2): cloud.add_video(str(1000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')
  async def move_many(ids,dest):
   for fid in ids: cloud.nodes[fid]['parent_id']=dest
  cloud.move_many=move_many
  entered=asyncio.Event();old=engine.export
  async def blocked(*args): entered.set();await asyncio.Event().wait()
  engine.export=blocked
  jid=new_job(store,{**CONFIG,'notify_emby':False})
  t=asyncio.create_task(engine.run_job(jid));await asyncio.wait_for(entered.wait(),10);t.cancel()
  try: await t
  except asyncio.CancelledError: pass
  print('BEFORE',[(r['state'],json.loads(r['plan']).get('queued')) for r in store.db.execute('select state,plan from organize_items where job_id=?',(jid,))])
  engine.export=old;engine.update_job(jid,state='running');await engine.run_job(jid)
  states=[r['state'] for r in store.db.execute('select state from organize_items where job_id=?',(jid,))]
  print('AFTER',engine.job(jid)['state'],states,'strms',len(list(engine.root.rglob('*.strm'))))
  await http.aclose();store.close()
  assert states==['done','done']
 asyncio.run(go())
