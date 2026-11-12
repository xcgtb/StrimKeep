import asyncio,json
import pytest
from test_organize import setup,new_job
from test_phase5 import sync_config

# 0.7.7 起定时同步默认走本地巡检；只有「手动」和「勾了旧的全量开关」才是完全核对。
# 这个用例测的是完全核对下的修复与补齐，所以只覆盖真正会全量的触发方式。
# 新契约下全量只有一种开法：显式勾 full_scan；手动不勾就是巡检（另有巡检用例覆盖）。
FULL_TRIGGERS=[(False,True,'manual'),(True,True,'scheduled+full-scan')]

@pytest.mark.parametrize('scheduled,legacy_full_scan,label',FULL_TRIGGERS,ids=[t[2] for t in FULL_TRIGGERS])
def test_full_sync_repairs_strm_and_backfills_sidecars_for_both_triggers(tmp_path,scheduled,legacy_full_scan,label):
 async def run():
  store,cloud,http,engine,_=setup(tmp_path)
  cloud.add_video(name='测试电影 (2024).mkv')['sha1']='A'*40
  identify=engine.identify
  async def identify_with_art(hint):return {**await identify(hint),'poster_path':'/poster.jpg','backdrop_path':'/backdrop.jpg'}
  engine.identify=identify_with_art
  first=new_job(store,{**sync_config(),'notify_emby':False});await engine.run_job(first)
  path=engine.root/engine.outputs.get('1','1000')['relative_path'];original=path.read_bytes();path.unlink()
  cloud.add_video('1005','测试电影 (2024).1080p.mkv')['sha1']='B'*40
  config={**sync_config(),'scheduled':scheduled,'full_scan':legacy_full_scan,'generate_nfo':True,'generate_images':True,'emby_media_info':True}
  jid=new_job(store,config);await engine.run_job(jid)
  row=engine.job(jid);saved=json.loads(row['config'])
  assert row['state']=='completed', (row['message'], [tuple(r) for r in store.db.execute('select state,message from organize_items where job_id=?',(jid,))])
  assert saved['sync_mode']=='full'
  assert engine.outputs.get('1','1005') is not None
  assert path.read_bytes()==original
  assert path.with_suffix('.nfo').is_file()
  assert (path.parent/'poster.jpg').is_file()
  assert (path.parent/'fanart.jpg').is_file()
  plan=json.loads(store.db.execute('select plan from organize_items where job_id=?',(jid,)).fetchone()[0])
  assert plan['output_changed'] and engine.emby.targets(jid)
  assert not cloud.writes
  await http.aclose();store.close()
 asyncio.run(run())


def test_scheduled_sync_without_force_uses_local_sweep(tmp_path):
 """定时 + 没勾全量 = 本地巡检：不改文件、不枚举云端。

 这是 0.7.7 起的既定契约（省 115 配额），和上一个用例的「完全核对」是两条路。
 """
 async def run():
  store,cloud,http,engine,_=setup(tmp_path)
  cloud.add_video(name='测试电影 (2024).mkv')['sha1']='A'*40
  first=new_job(store,{**sync_config(),'notify_emby':False});await engine.run_job(first)
  calls=[]
  async def boom(*args,**kwargs):
   calls.append(args)
   raise AssertionError('定时同步不该枚举云端：%r' % (args,))
  cloud.children=boom;cloud.children_verified=boom
  jid=new_job(store,{**sync_config(),'scheduled':True,'full_scan':False,'notify_emby':False})
  await engine.run_job(jid)
  assert json.loads(engine.job(jid)['config'])['sync_mode']=='sweep'
  assert calls==[]
  await http.aclose();store.close()
 asyncio.run(run())

