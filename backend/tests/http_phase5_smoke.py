"""Optional local HTTP/worker smoke. Uses temporary data and a controlled cloud fixture.
Run from backend: python tests/http_phase5_smoke.py
"""
import json,os,subprocess,tempfile,time,sys
from pathlib import Path
import httpx
project=Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory() as temporary:
 root=Path(temporary)
 (root/'smoke_app.py').write_text('import os\nfrom pathlib import Path\nfrom fastapi.responses import Response\nfrom strimkeep.main import create_app\nfrom test_organize import MemoryCloud\nclass Cloud(MemoryCloud):\n def __init__(self,store):\n  super().__init__(store);self.add_video(name="Movie.mkv");self.add_video("1001","Movie.chs.srt")["size"]=50\n async def url(self,pc,ua):\n  return \'http://127.0.0.1:19051/fixture-subtitle\',{\'User-Agent\':ua}\napp=create_app(Path(os.environ["SMOKE_DATA"]),Path(os.environ["SMOKE_STRM"]),Cloud)\n@app.get(\'/fixture-subtitle\')\nasync def subtitle():return Response(b\'controlled subtitle\',media_type=\'text/plain\')\napp.router.routes.insert(0,app.router.routes.pop())\n')
 env={**os.environ,'PYTHONPATH':str(root)+':'+str(project/'backend')+':'+str(project/'backend/tests'),'ADMIN_USERNAME':'admin','ADMIN_PASSWORD':'controlled-test-password','SMOKE_DATA':str(root/'data'),'SMOKE_STRM':str(root/'strm'),'STRIMKEEP_STATIC_DIR':str(project/'frontend/dist')}
 with (root/'server.log').open('w') as log:
  proc=subprocess.Popen([sys.executable,'-m','uvicorn','smoke_app:app','--host','127.0.0.1','--port','19051','--no-access-log'],env=env,stdout=log,stderr=log)
  try:
   with httpx.Client(base_url='http://127.0.0.1:19051',timeout=6,trust_env=False) as client:
    for _ in range(50):
     try:
      response=client.get('/health/ready');assert response.json()['stage']==5;break
     except (httpx.HTTPError,ValueError):time.sleep(.1)
    login=client.post('/api/v1/auth/login',json={'username':'admin','password':'controlled-test-password'});assert login.status_code==200
    client.headers['X-CSRF-Token']=login.json()['csrf']
    assert client.put('/api/v1/settings',json={'cookie':'UID=1_A1; CID=mock; SEID=mock','playback_base':'http://play.example'}).status_code==200
    assert client.put('/api/v1/sync/config',json={'source_cids':['100'],'notify_emby':False}).status_code==200
    ids=[client.post('/api/v1/sync/jobs').json()['id'] for _ in range(2)]
    for _ in range(60):
     data=client.get(f'/api/v1/organize/jobs/{ids[-1]}').json()
     if data['job']['state']=='completed':break
     time.sleep(.05)
    assert data['job']['state']=='completed',data
    paths=list((root/'strm').rglob('*.strm'));assert len(paths)==1 and paths[0].with_suffix('.chs.srt').is_file()
    mappings=client.get('/api/v1/playbacks').json()['items'];assert len(mappings)==1
    before=paths[0].read_text();pid=mappings[0]['id']
    assert client.get(before.strip().replace('http://play.example','')).status_code==302
    with client.stream('GET','/api/v1/tasks/events') as stream:
     assert stream.status_code==200 and stream.headers['content-type'].startswith('text/event-stream')
     lines=stream.iter_lines();assert next(lines)=='event: tasks';payload=next(lines)
     assert payload.startswith('data: ') and 'token_cipher' not in payload and 'account_id' not in payload
    assert client.post(f'/api/v1/playbacks/{pid}/revoke').status_code==200
    third=client.post('/api/v1/sync/jobs').json()['id']
    for _ in range(60):
     data=client.get(f'/api/v1/organize/jobs/{third}').json()
     if data['job']['state']=='completed':break
     time.sleep(.05)
    assert data['items'][0]['state']=='skipped' and paths[0].read_text()==before
    assert client.get(before.strip().replace('http://play.example','')).status_code==403
    notices=client.get('/api/v1/notifications').json();assert notices['unread']>=2
    assert client.post('/api/v1/notifications/read').status_code==200
    assert client.get('/api/v1/notifications').json()['unread']==0
    print(json.dumps({'passed':True,'version':client.get('/api/v1/status').json()['version'],'jobs':ids+[third],'strm':1,'subtitles':1,'mapping':1,'sse':'authenticated live stream','revoke':'403; sync did not revive','notifications':len(notices['items'])}))
  finally:
   proc.terminate()
   try:proc.wait(timeout=6)
   except subprocess.TimeoutExpired:proc.kill();proc.wait()
