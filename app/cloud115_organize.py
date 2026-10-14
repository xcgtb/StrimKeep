"""Explicit ID organize plans; each remote write has a durable intent.

This is manual, previewed organization, not TMDB identification or name-based
source matching. Moves and renames preserve owned STRM URLs and content IDs.
"""
import secrets
import time
from pathlib import Path
if __package__:
    from . import cloud115,cloud115_store as repo,state_store,storage
    from .providers.p115_open import Driver,ProviderError,identifier,same_file
else:
    import cloud115,cloud115_store as repo,state_store,storage
    from providers.p115_open import Driver,ProviderError,identifier,same_file

BLOCKING={'running','unknown','awaiting_resume'}
SIDECAR_EXTS={'.srt','.ass','.ssa','.sub','.idx','.vtt','.json','.nfo'}


def _path():return storage._state_dir()/'cloud115_operations.json'


def _plans():
    value=state_store.read(_path(),{})
    if not isinstance(value,dict):raise ProviderError('115 整理记录不可用',503)
    return value


def _save(rows):state_store.save(_path(),rows)


def pending(account_id,file_ids,exclude=None):
    ids=set(file_ids)
    for key,plan in _plans().items():
        if key!=exclude and plan['account_id']==account_id and plan['state'] in BLOCKING:
            if ids.intersection(x['id'] for x in plan['files']):
                raise ProviderError('该文件有未完成的 ID 整理操作，请先复核整理记录；禁止交叉删除或整理',409)


def public_plans():
    return [{'id':p['id'],'account_id':p['account_id'],'state':p['state'],'created_at':p['created_at'],
             'file_id':p['files'][0]['id'],'target_parent':p['target_parent'],'message':p.get('message','')}
            for p in sorted(_plans().values(),key=lambda p:p['created_at'],reverse=True)[:50]]


def detail(plan_id):
    plan=_plans().get(plan_id)
    if not plan:raise ProviderError('整理计划不存在',404)
    return {'plan_id':plan_id,'state':plan['state'],'expires_at':plan['expires'],
            'files':[{'file_id':x['id'],'old_name':x['name'],'new_name':n,'old_parent':x['parent_id'],
                      'new_parent':plan['target_parent']} for x,n in zip(plan['files'],plan['names'])],
            'steps':[{'action':s['action'],'file_id':s['before']['id'],'state':s['state']} for s in plan['steps']],
            'message':plan.get('message','预览已保存，尚未执行')}


def filename(value,old):
    if not isinstance(value,str) or not 1<=len(value)<=255 or value.strip()!=value or value.endswith(('.', ' ')):
        raise ProviderError('整理名称须为 1～255 字符，不含首尾空白',400)
    if value in ('.','..') or any(ord(c)<32 or c in '/\\<>:"|?*' for c in value):
        raise ProviderError('整理名称包含路径或无效字符',400)
    if Path(value).suffix.lower()!=Path(old).suffix.lower():
        raise ProviderError('整理不能改变视频或关联文件扩展名',400)
    return value


def _directory(driver,parent):
    if parent=='0':return {'id':'0','name':'/','parent_id':'0','is_dir':True}
    item=driver.stat(parent)
    if not item or not item['is_dir']:raise ProviderError('目标 ID 不是可用目录',409)
    return {'id':item['id'],'name':item['name'],'parent_id':item.get('parent_id'),'is_dir':True}


def _inventory(driver,parent):
    rows=[];offset=0
    for _ in range(10):
        page=driver.files(parent,offset,100)
        rows.extend(page['items'])
        nxt=page['next_offset']
        if nxt is None:return rows
        if not isinstance(nxt,int) or nxt<=offset:raise ProviderError('115 目录分页不受支持',409)
        offset=nxt
    raise ProviderError('目标目录超过本次核验上限，请选择较小目录；不会假定后续页没有重名',409)


def _conflicts(driver,plan):
    items=_inventory(driver,plan['target_parent'])
    wanted={name.casefold() for name in plan['names']}
    # Moving with the original name is also a step; never allow it to overwrite
    # another file just because the eventual renamed name would be unique.
    wanted.update(x['name'].casefold() for x in plan['files'])
    selected={x['id'] for x in plan['files']}
    if any(x['id'] not in selected and x['name'].casefold() in wanted for x in items):
        raise ProviderError('目标目录存在同名文件，未提交移动或改名',409)


def _binding(driver,account_id,files):
    owner=repo.file_owner(account_id,files[0]['id'])
    for item in files:
        other=repo.file_owner(account_id,item['id'])
        if other and other!=owner:raise ProviderError('所选关联文件属于其他 STRM 绑定，未整理',409)
    if owner:
        row=repo.binding(binding_id=owner)
        cloud115.verify_binding(row,Path(row['strm_path']),cloud115._engine().L_ROOT)
        if row['state']!='active':raise ProviderError('已进入删除链的绑定不能整理',409)
        for item in [row['remote']]+row['sidecars']:
            if repo.receipt(owner,item['id']):raise ProviderError('该绑定已有删除意图，禁止整理',409)
            if not same_file(item,driver.stat(item['id'])):raise ProviderError('已有绑定的源身份无法核实',409)
    return owner


def preview(body):
    with cloud115._engine().mutation_lock():
        account_id=body.get('account_id');fid=identifier(body.get('file_id'))
        parent=identifier(body.get('target_parent'),root=True)
        sidecars=body.get('sidecars',[])
        if not isinstance(sidecars,list) or len(sidecars)>20 or any(not isinstance(x,dict) for x in sidecars):
            raise ProviderError('最多选择 20 个明确关联文件',400)
        sidecars=list(sidecars)
        explicit={identifier(x.get('file_id')) for x in sidecars}
        sidecars.extend({'file_id':sid} for sid in cloud115._transfer().known_sidecars(account_id,fid) if sid not in explicit)
        if len(sidecars)>20:raise ProviderError('用户选择与已登记转存关联文件超过 20 个',400)
        ids=[fid]+[identifier(x.get('file_id')) for x in sidecars]
        if len(set(ids))!=len(ids):raise ProviderError('整理文件 ID 重复',400)
        pending(account_id,ids)
        driver=Driver(account_id);uid=driver.validate_account();target=_directory(driver,parent)
        files=[];names=[]
        main_origin=None
        for i,sid in enumerate(ids):
            item=driver.stat(sid)
            if not item or item['is_dir'] or item.get('parent_id') is None or not (item['sha1'] or item['pickcode']):
                raise ProviderError('文件缺少可复核的身份或父目录 ID，未生成整理计划',409)
            origin=cloud115._transfer().provenance(account_id,item)
            if i==0:
                main_origin=origin
                if origin and origin['role']!='video':raise ProviderError('已登记角色不是视频，不能作为主视频整理',409)
            elif origin and (not main_origin or origin['transfer_id']!=main_origin['transfer_id']):
                raise ProviderError('关联文件已有其他转存来源归属，禁止串配整理',409)
            allowed=cloud115._engine().VIDEO_EXTS if i==0 else SIDECAR_EXTS
            if Path(item['name']).suffix.lower() not in allowed:raise ProviderError('主文件须是视频，关联文件须是字幕或元数据',400)
            name=body.get('new_name') if i==0 else sidecars[i-1].get('new_name')
            names.append(filename(item['name'] if name is None or name=='' else name,item['name']))
            files.append(item)
        if len({n.casefold() for n in names})!=len(names):raise ProviderError('整理后的关联文件名称冲突',409)
        owner=_binding(driver,account_id,files)
        cloud115._transfer().pending(account_id,files,driver)
        key=secrets.token_urlsafe(24);now=time.time()
        plan={'id':key,'account_id':account_id,'uid':uid,'files':files,'names':names,'target_parent':parent,
              'target':target,'binding_id':owner,'state':'preview','created_at':now,'expires':now+600,'steps':[]}
        for item,name in zip(files,names):
            before=dict(item)
            if item['parent_id']!=parent:
                after=dict(before,parent_id=parent)
                plan['steps'].append({'action':'move','before':before,'after':after,'state':'pending'})
                before=after
            if name!=before['name']:
                plan['steps'].append({'action':'rename','before':before,'after':dict(before,name=name),'state':'pending'})
        if not plan['steps']:raise ProviderError('所选名称和目录没有变化，未生成空整理任务',400)
        _conflicts(driver,plan)
        rows=_plans()
        rows={k:p for k,p in rows.items() if not (p['state']=='preview' and p['expires']<now)}
        completed=sorted((p for p in rows.values() if p['state'] in ('done','cancelled')),key=lambda p:p['created_at'])
        while len(rows)>=50 and completed:rows.pop(completed.pop(0)['id'])
        if len(rows)>=50:raise ProviderError('整理记录较多，请先处理未完成任务',429)
        rows[key]=plan;_save(rows)
        return {'plan_id':key,'files':[{'file_id':x['id'],'old_name':x['name'],'new_name':n,
                'old_parent':x['parent_id'],'new_parent':parent} for x,n in zip(files,names)],
                'expires_at':plan['expires'],'step_count':len(plan['steps']),
                'message':'预览已保存；确认后按文件 ID 执行，保持已绑定 STRM 地址不变'}


def _matches(expected,actual):
    return same_file(expected,actual) and actual['name']==expected['name'] and actual.get('parent_id')==expected['parent_id']


def _expected(plan):
    expected={x['id']:x for x in plan['files']}
    for step in plan['steps']:
        if step['state']=='confirmed':expected[step['after']['id']]=step['after']
    return expected


def _finish(rows,plan,driver):
    actual={}
    for item,name in zip(plan['files'],plan['names']):
        value=driver.stat(item['id'])
        if not _matches(dict(item,name=name,parent_id=plan['target_parent']),value):
            raise ProviderError('最终整理结果无法核实，记录保留待复核',409)
        actual[item['id']]=value
    if plan['binding_id']:repo.update_binding_locations(plan['binding_id'],actual)
    plan['state']='done';plan['message']='按原文件 ID 完成移动/改名并复核，已有 STRM 及删除身份保持有效'
    _save(rows)
    return {'state':'done','message':plan['message']}


def execute(plan_id):
    with cloud115._engine().mutation_lock():
        rows=_plans();plan=rows.get(plan_id)
        if not plan:raise ProviderError('整理计划不存在',404)
        if plan['state']=='done':return {'state':'done','message':plan['message']}
        if plan['state'] not in ('preview','awaiting_resume'):raise ProviderError('整理结果待核实，禁止重复提交；请先只读复核',409)
        if plan['state']=='preview' and plan['expires']<time.time():raise ProviderError('整理预览已过期，请重新预览',410)
        pending(plan['account_id'],[x['id'] for x in plan['files']],exclude=plan_id)
        driver=Driver(plan['account_id'])
        if driver.validate_account()!=plan['uid']:raise ProviderError('整理账号已变化',409)
        if _binding(driver,plan['account_id'],plan['files'])!=plan['binding_id']:
            raise ProviderError('文件的 STRM 归属在预览后发生变化，请重新预览',409)
        cloud115._transfer().pending(plan['account_id'],plan['files'],driver)
        if _directory(driver,plan['target_parent'])!=plan['target']:raise ProviderError('目标目录身份或名称已变化，请重新预览',409)
        for expected in _expected(plan).values():
            if not _matches(expected,driver.stat(expected['id'])):raise ProviderError('源身份、名称或父目录已变化，请重新预览',409)
        _conflicts(driver,plan)
        plan['state']='running';_save(rows)
        try:
            for step in plan['steps']:
                if step['state']=='confirmed':continue
                if not _matches(step['before'],driver.stat(step['before']['id'])):
                    raise ProviderError('执行前源位置发生变化，未提交本步骤',409)
                _conflicts(driver,plan)
                step['state']='submitted';_save(rows)
                if step['action']=='move':driver.move(step['before']['id'],plan['target_parent'])
                else:driver.rename(step['before']['id'],step['after']['name'])
                if not _matches(step['after'],driver.stat(step['after']['id'])):
                    raise ProviderError('官方尚未确认本步骤完成',409)
                step['state']='confirmed';_save(rows)
            return _finish(rows,plan,driver)
        except (ProviderError,OSError,ValueError):
            plan['state']='unknown';plan['message']='整理结果未确认；只按原 ID 复核，不自动重复移动或改名'
            _save(rows)
            raise ProviderError(plan['message'],409) from None


def reconcile(plan_id):
    with cloud115._engine().mutation_lock():
        rows=_plans();plan=rows.get(plan_id)
        if not plan:raise ProviderError('整理记录不存在',404)
        if plan['state'] in ('preview','cancelled'):raise ProviderError('此记录尚未执行或已取消',409)
        driver=Driver(plan['account_id'])
        if driver.validate_account()!=plan['uid']:raise ProviderError('整理账号不一致',409)
        for step in plan['steps']:
            if step['state']=='submitted':
                if not _matches(step['after'],driver.stat(step['after']['id'])):
                    raise ProviderError('原 ID 的整理结果仍无法确认，不会重复提交',409)
                step['state']='confirmed';_save(rows)
        for expected in _expected(plan).values():
            if not _matches(expected,driver.stat(expected['id'])):raise ProviderError('整理结果与原 ID 计划不一致',409)
        if all(step['state']=='confirmed' for step in plan['steps']):return _finish(rows,plan,driver)
        plan['state']='awaiting_resume';plan['message']='已复核提交过的步骤；可确认继续尚未提交的步骤'
        _save(rows)
        return {'state':plan['state'],'message':plan['message']}


def cancel(plan_id):
    """Only plans with no persisted write intent may release their claims."""
    with cloud115._engine().mutation_lock():
        rows=_plans();plan=rows.get(plan_id)
        if not plan:raise ProviderError('整理计划不存在',404)
        if any(s['state']!='pending' for s in plan['steps']) or plan['state']=='done':
            raise ProviderError('存在已提交步骤，不能取消未知结果；请先按原 ID 复核',409)
        plan['state']='cancelled';plan['message']='未提交任何远端写请求的整理计划已取消'
        _save(rows)
        return {'state':'cancelled','message':plan['message']}
