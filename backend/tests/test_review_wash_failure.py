import asyncio
from test_upgrade import upgraded_setup,policy
from test_organize import new_job,CONFIG

def test_upstream_order_recycles_old_before_failed_publication(tmp_path):
 async def go():
  store,cloud,http,engine,_,_=upgraded_setup(tmp_path)
  for i in (1,2): cloud.add_video(str(1000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size']=1000
  first=new_job(store,{**CONFIG,'notify_emby':False});await engine.run_job(first)
  for i in (1,2): cloud.add_video(str(2000+i),f'测试剧集 (2024) S01E0{i}.WEB-DL.mkv')['size']=2000
  async def rename_many(pairs):
   for fid,name in pairs:cloud.nodes[fid]['name']=name
  async def move_many(ids,dest):
   for fid in ids:cloud.nodes[fid]['parent_id']=dest
  async def recycle_many(ids,parent):
   for fid in ids:await cloud.recycle(fid,parent)
  cloud.rename_many=rename_many;cloud.move_many=move_many;cloud.recycle_many=recycle_many
  async def fail(*args):raise OSError('injected output failure')
  engine.export=fail
  jid=new_job(store,{**CONFIG,'notify_emby':False,'upgrade_policy':policy('size')});await engine.run_job(jid)
  print('old_present',[str(1000+i) in cloud.nodes for i in (1,2)],'recycle',list(cloud.bin),'state',engine.job(jid)['state'])
  assert all(str(1000+i) not in cloud.nodes for i in (1,2))
  assert set(cloud.bin)=={'1001','1002'}
  assert engine.job(jid)['state']=='partial'
  await http.aclose();store.close()
 asyncio.run(go())
