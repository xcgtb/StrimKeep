"""Explicit, snapshot-bound continuation. Never replay an uncertain provider write."""
import copy
import json
import time
from pathlib import Path
from app.cloud import store, jobs, workflow, settings, events, automation
from app.cloud.errors import CloudError

TERMINAL = ('failed', 'uncertain', 'needs_review', 'stopped')


def _load(job_id):
    with store.db() as con:
        row = con.execute('SELECT kind,state,payload,result,updated FROM cloud_jobs WHERE id=?', (job_id,)).fetchone()
        previous = con.execute('SELECT job_id FROM cloud_recovery_requests WHERE origin_id=?', (job_id,)).fetchone()
    if not row:
        raise CloudError('任务不存在', 404)
    if previous:
        return row, previous[0]
    if row[1] not in TERMINAL:
        raise CloudError('仅可恢复已停止且需要处理的任务', 409)
    return row, None


def preview(job_id):
    row, previous = _load(job_id)
    if previous:
        return {'job_id': previous, 'message':'后续任务已存在，请查看原回执', 'items':[], 'blocked':[]}
    data, result = json.loads(row[2]), json.loads(row[3])
    if row[0] == 'transfer' and not data.get('recovery_plan_id'):
        return {'items':[], 'blocked':['请先使用“只读核实转存回执”，核实成功后继续整理；不会重发转存请求。']}
    plan_id = data.get('recovery_plan_id') or data.get('plan_id')
    if row[0] not in ('organize', 'workflow', 'transfer') or not plan_id:
        return {'items':[], 'blocked':['这类任务需回到所属模块重新预览；STRM 可从来源重新同步，云下载需在115核对完成状态。']}
    with store.db() as con:
        saved = con.execute('SELECT payload FROM cloud_plans WHERE id=?', (plan_id,)).fetchone()
        steps = con.execute('SELECT step_key,state,payload,result FROM cloud_steps WHERE job_id=? ORDER BY updated', (job_id,)).fetchall()
    if not saved:
        raise CloudError('原计划已不可读取，请重新预览', 409)
    original = json.loads(saved[0])
    if original.get('naming_snapshot') != workflow.naming_snapshot(settings.get()):
        raise CloudError('命名或分类规则变化，请在整理页重新预览', 409)
    provider = workflow.verify_account(original)
    blocked, notes, expected, completed = [], [], {}, set()
    for item in [*original['items'], *original.get('unknown_items',[])]:
        for source in [item['source'], *item['followers']]:
            expected[source['id']] = copy.deepcopy(source)
    for key, state, raw, _ in steps:
        intent = json.loads(raw)
        if key.startswith(('rename:', 'move:')):
            fid = intent['file_id']
            if fid not in expected:
                blocked.append('存在无法关联原计划的文件步骤，请人工核对')
                continue
            proposed = {**expected[fid]}
            proposed.update({'name':intent['name']} if key.startswith('rename:') else {'parent_id':intent['target']})
            current = provider.info(fid)
            # Later moves may follow a verified rename; apply that evidence in order.
            if state == 'verified' or workflow._snapshot_equal(proposed,current):
                expected[fid] = proposed
                notes.append({'name':proposed['name'], 'state':'verified', 'message':'当前身份与已记录动作一致'})
            elif state != 'verified':
                blocked.append('有移动或命名结果尚未证实，保留文件：'+str(intent.get('source_name',fid)))
        elif state != 'verified' and key.startswith('mkdir:'):
            matches = [v for v in provider.list(intent['parent']) if v['name']==intent['name'] and v['is_dir']]
            if len(matches)!=1:
                blocked.append('创建目录的结果未唯一确认：'+intent['name'])
        elif key.startswith('publication:'):
            from app.cloud import strm
            with store.db() as con:
                assets = con.execute("SELECT id FROM cloud_assets WHERE account_id=? AND file_id=? AND state='active'",(original['account_id'],intent['source']['id'])).fetchall()
            verified = []
            for (aid,) in assets:
                asset = store.asset(aid)
                path = Path(asset['local_path'])
                if (workflow._snapshot_equal(intent['source'],asset['payload']['source'])
                    and asset['payload']['media']==intent['media']
                    and path.is_file() and strm.digest(path.read_bytes())==asset['payload']['digest']
                    and all(Path(v['path']).is_file() and strm.digest(Path(v['path']).read_bytes())==v['digest'] for v in asset['payload'].get('owned',[]))):
                    verified.append(asset)
            if len(verified)==1:
                completed.add(intent['source']['id'])
                notes.append({'name':intent['source']['name'],'state':'verified','message':'已生成的播放引用和关联文件已核实，恢复时跳过'})
            elif state not in ('queued','failed'):
                blocked.append('发布结果尚未核实，请在 STRM 页面检查后重新同步：'+intent['source']['name'])
        elif state != 'verified' and not key.startswith(('notification','poster:')):
            blocked.append('存在未核实步骤，请人工核对后重新预览：'+key.split(':')[0])
    current_files = {}
    for fid, before in expected.items():
        actual = provider.info(fid)
        if not workflow._snapshot_equal(before, actual):
            blocked.append('文件内容、位置或名称与已记录动作不一致：'+before['name'])
        current_files[fid] = actual
    # Existing delayed jobs own their publication; recovery must not publish again.
    with store.db() as con:
        deferred = con.execute('SELECT file_id FROM cloud_job_due WHERE origin_job=?', (job_id,)).fetchall()
    completed.update(r[0] for r in deferred)
    planned = copy.deepcopy(original)
    items = []
    for group in ('items', 'unknown_items'):
        remaining = []
        for item in planned.get(group, []):
            if item['source']['id'] in completed:
                continue
            item['source'] = current_files[item['source']['id']]
            item['followers'] = [current_files[v['id']] for v in item['followers']]
            remaining.append(item)
            items.append({'name':item['source']['name'], 'target':item.get('directory') or item.get('media',{}).get('directory',''), 'followers':len(item['followers'])})
        planned[group] = remaining
    # Empty directory cleanup and old-version removal are never recovery side effects.
    if any(item.get('upgrades') for item in planned.get('items', [])):
        blocked.append('涉及旧版本清理，请在洗版记录核实后使用专用继续入口')
    planned['cleanup_candidates'] = []
    if blocked or not items:
        return {'items':items, 'blocked':blocked or ['没有待继续项目；请查看已生成资源和入库回执'], 'verified':notes,
                'message':'原始文件未重新提交'}
    token = store.ident()
    envelope = {'origin_id':job_id, 'origin_updated':row[4], 'plan':planned,
                'generate':bool(data.get('recovery_generate', data.get('generate',True))),
                'output_snapshot':automation.output_snapshot()}
    with store.db() as con:
        con.execute('INSERT INTO cloud_plans VALUES(?,?,?,?,?,?)', (token,original['account_id'],original['account_revision'],'recovery_pending',store.encode(envelope),time.time()+300))
    return {'preview_id':token, 'items':items, 'blocked':[], 'verified':notes,
            'expires_in':300, 'message':'只继续以上文件组；已完成的发布和消息不会重发，空目录与旧版本不在恢复范围'}


def apply(job_id, preview_id, confirmed=False):
    if confirmed is not True:
        raise CloudError('请确认恢复预览', 400)
    row, previous = _load(job_id)
    if previous:
        return {'job_id':previous, 'message':'继续任务已存在'}
    with store.db() as con:
        saved = con.execute('SELECT payload,state,expires FROM cloud_plans WHERE id=?', (preview_id,)).fetchone()
    if not saved or saved[1]!='recovery_pending' or saved[2]<time.time():
        raise CloudError('恢复预览已过期，请重新核对',409)
    envelope = json.loads(saved[0])
    if envelope['origin_id']!=job_id or envelope['origin_updated']!=row[4]:
        raise CloudError('原任务状态变化，请重新核对',409)
    plan = envelope['plan']
    if plan['naming_snapshot']!=workflow.naming_snapshot(settings.get()):
        raise CloudError('规则变化，请重新预览',409)
    from app.cloud.automation import output_snapshot
    if envelope['output_snapshot'] != output_snapshot():
        raise CloudError('输出配置变化，请重新核对恢复范围',409)
    provider = workflow.verify_account(plan)
    for item in [*plan['items'], *plan.get('unknown_items',[])]:
        for source in [item['source'], *item['followers']]:
            if not workflow._snapshot_equal(source,provider.info(source['id'])):
                raise CloudError('确认前文件变化，请重新核对',409)
    with store.db() as con:
        old = con.execute('SELECT job_id FROM cloud_recovery_requests WHERE origin_id=?',(job_id,)).fetchone()
        if old:
            return {'job_id':old[0], 'message':'继续任务已存在'}
        current = con.execute('SELECT state,updated FROM cloud_jobs WHERE id=?',(job_id,)).fetchone()
        account = con.execute('SELECT revision FROM cloud_accounts WHERE id=?',(plan['account_id'],)).fetchone()
        if current!=(row[1],row[4]) or not account or account[0]!=plan['account_revision']:
            raise CloudError('任务或账号变化，请重新核对',409)
        changed = con.execute("UPDATE cloud_plans SET payload=?,state='queued' WHERE id=? AND state='recovery_pending' AND expires>?",(store.encode(plan),preview_id,time.time())).rowcount
        if not changed:
            raise CloudError('预览已被使用或过期',409)
        jid = jobs.enqueue('organize', {'plan_id':preview_id, 'generate':envelope['generate'], 'recovery_of':job_id, 'recovery_output_snapshot':envelope['output_snapshot']}, con, parent_id=job_id)
        con.execute('INSERT INTO cloud_recovery_requests VALUES(?,?,?)',(job_id,jid,time.time()))
        events.emit('queue','核实后的剩余项目已单独入队；原始回执保留',job_id,con=con)
    return {'job_id':jid, 'message':'已创建后续任务，请查看同一流程的处理结果'}
