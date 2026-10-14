"""Share-to-owned-ID boundary using explicit auxiliary Web authorization.

The official account performs directory creation and target identity reads.
Web Cookie only receives the share. No folder/receive write is auto-retried.
"""
import secrets
import time
from pathlib import Path
if __package__:
    from . import cloud115,cloud115_organize as organize,cloud115_store as repo,state_store,storage
    from .providers import p115_web as web
    from .providers.p115_open import Driver,ProviderError,identifier,auth_lock,same_file
else:
    import cloud115,cloud115_organize as organize,cloud115_store as repo,state_store,storage
    from providers import p115_web as web
    from providers.p115_open import Driver,ProviderError,identifier,auth_lock,same_file


def _path(name):return storage._state_dir()/('cloud115_share_'+name+'.json')


def _read(name):
    value=state_store.read(_path(name),{})
    if not isinstance(value,dict):raise ProviderError('115 转存状态不可用',503)
    return value


def _save(name,rows):state_store.save(_path(name),rows)


def save_cookie(body):
    account_id=body.get('account_id');value,cookie_uid=web.cookie(body.get('cookie'))
    uid=Driver(account_id).validate_account()
    if cookie_uid!=uid or web.user_id(web.transport('user',web_cookie=value))!=uid:
        raise ProviderError('辅助 Cookie 与官方授权不是同一个 115 账号，未保存',409)
    with auth_lock():
        account=repo.account(account_id)
        if not account or account['uid']!=uid or not account['enabled']:raise ProviderError('官方账号状态已变化，未保存辅助授权',409)
        rows=_read('credentials');old=rows.get(account_id,{})
        rows[account_id]={'uid':uid,'cookie':value,'revision':old.get('revision',0)+1,'updated_at':time.time()}
        _save('credentials',rows)
    return {'account_id':account_id,'uid':uid,'has_transfer_cookie':True}


def cookie_accounts():return list(_read('credentials'))


def _authorized(account_id):
    driver=Driver(account_id);uid=driver.validate_account();row=_read('credentials').get(account_id)
    if not row:raise ProviderError('请先为此官方账号保存同账号的分享转存辅助 Cookie',409)
    value,cookie_uid=web.cookie(row['cookie'])
    if row['uid']!=uid or cookie_uid!=uid or web.user_id(web.transport('user',web_cookie=value))!=uid:
        raise ProviderError('分享辅助授权与官方账号身份不一致，未操作',409)
    return driver,uid,row['revision'],value


def _share_page(parameters,value):return web.page(web.transport('share',parameters,web_cookie=value),parameters['offset'],parameters['limit'])


def inspect(body):
    with cloud115._engine().mutation_lock():
        account_id=body.get('account_id');_,uid,revision,value=_authorized(account_id)
        parent=identifier(body.get('parent_id','0'),root=True);offset=body.get('offset',0)
        if type(offset) is not int or not 0<=offset<=10000000:raise ProviderError('分享分页偏移无效',400)
        parameters=dict(web.share_link(body.get('share_url')),cid=parent,offset=offset,limit=100)
        page=_share_page(parameters,value);now=time.time();key=secrets.token_urlsafe(24)
        rows={k:p for k,p in _read('previews').items() if p['expires']>now}
        if len(rows)>=20:raise ProviderError('分享预览较多，请等待旧预览过期',429)
        rows[key]={'account_id':account_id,'uid':uid,'revision':revision,'parameters':parameters,'items':page['items'],'expires':now+600}
        _save('previews',rows)
        return {'handle':key,**page,'message':'分享文件只读预览；目录请逐层打开，转存只支持明确选中的单个视频及关联文件'}


def public_plans():
    return [{'id':p['id'],'account_id':p['account_id'],'state':p['state'],'created_at':p['created_at'],
             'folder_id':p.get('folder_id'),'source_id':p['files'][0]['id'],'target_parent':p['target_parent'],
             'message':p.get('message','')} for p in sorted(_read('plans').values(),key=lambda p:p['created_at'],reverse=True)[:50]]


def detail(plan_id):
    plan=_read('plans').get(plan_id)
    if not plan:raise ProviderError('转存计划不存在',404)
    return {'plan_id':plan_id,'account_id':plan['account_id'],'account_uid':plan['uid'],'share_code':plan['parameters']['share_code'],
            'state':plan['state'],'target_parent':plan['target_parent'],'folder_name':plan['folder_name'],
            'folder_id':plan.get('folder_id'),'files':[{'source_id':x['id'],'name':x['name'],'size':x['size']} for x in plan['files']],
            'targets':[{'source_id':x['source_id'],'file_id':x['file']['id'],'name':x['file']['name']} for x in plan.get('targets',[])],
            'mkdir_state':plan['mkdir_state'],'receive_state':plan['receive_state'],'message':plan.get('message','预览已保存，尚未转存')}


def _same_source(a,b):return a['id']==b['id'] and a['name']==b['name'] and a['size']==b['size'] and a['sha1']==b['sha1'] and not b['is_dir']


def _source_check(plan,value):
    current={x['id']:x for x in _share_page(plan['parameters'],value)['items']}
    if any(x['id'] not in current or not _same_source(x,current[x['id']]) for x in plan['files']):
        raise ProviderError('分享内容在预览后变化或不在原分页，未转存',409)


def _not_duplicate(account_id,parameters,files,driver,exclude=None):
    ids={x['id'] for x in files}
    for key,old in _read('plans').items():
        if key!=exclude and old['account_id']==account_id and old['parameters']['share_code']==parameters['share_code'] and old['state'] in ('running','unknown','awaiting_resume'):
            if ids.intersection(x['id'] for x in old['files']):raise ProviderError('相同分享文件有未确认转存，先复核原记录，不创建另一份重试任务',409)
    for source in files:
        for row in repo.received_source(account_id,parameters['share_code'],source['id']):
            expected=row['remote']
            if (source['sha1'],source['size'])!=(expected['sha1'],expected['size']):continue
            actual=driver.stat(row['file_id'])
            if actual is not None:
                if not same_file(expected,actual):raise ProviderError('已登记的转存目标身份变化，请先核验原 ID',409)
                raise ProviderError('该来源内容已有已核验的目标文件 ID，未重复转存',409)


def known_sidecars(account_id,file_id):
    row=repo.received(account_id,file_id)
    return row['related_ids'] if row and row['role']=='video' else []


def provenance(account_id,item):
    row=repo.received(account_id,item['id'])
    if not row:return None
    if not same_file(row['remote'],item):raise ProviderError('转存目标 ID 的内容证据已变化，未登记 STRM',409)
    return {'kind':'115-share','transfer_id':row['transfer_id'],'source_share_code':row['share_code'],
            'source_file_id':row['source_file_id'],'role':row['role'],'related_file_ids':row['related_ids']}


def preview(body):
    with cloud115._engine().mutation_lock():
        selection=_read('previews').get(body.get('handle'))
        if not selection or selection['expires']<time.time():raise ProviderError('分享文件预览已过期，请重新读取',410)
        account_id=selection['account_id'];driver,uid,revision,value=_authorized(account_id)
        if uid!=selection['uid'] or revision!=selection['revision']:raise ProviderError('辅助授权在预览后变化，请重新读取分享',409)
        ids=[identifier(body.get('file_id'))];sidecars=body.get('sidecar_ids',[])
        if not isinstance(sidecars,list) or len(sidecars)>20:raise ProviderError('关联文件最多 20 个',400)
        ids.extend(identifier(x) for x in sidecars)
        if len(set(ids))!=len(ids):raise ProviderError('分享文件 ID 重复',400)
        available={x['id']:x for x in selection['items']};files=[]
        for i,fid in enumerate(ids):
            item=available.get(fid)
            allowed=cloud115._engine().VIDEO_EXTS if i==0 else organize.SIDECAR_EXTS
            if not item or item['is_dir'] or not item['sha1'] or Path(item['name']).suffix.lower() not in allowed:
                raise ProviderError('请选择本页有 SHA1 的视频及明确关联文件；不递归转存目录',400)
            files.append(item)
        if len({(x['sha1'],x['size']) for x in files})!=len(files):raise ProviderError('所选分享存在相同内容的多个文件，无法唯一核验目标 ID；请分开转存',409)
        _not_duplicate(account_id,selection['parameters'],files,driver)
        parent=identifier(body.get('target_parent'),root=True);target=organize._directory(driver,parent)
        key=secrets.token_urlsafe(24);now=time.time();folder='StrimKeep-'+key
        inventory=organize._inventory(driver,parent)
        if any(x['name'].casefold()==folder.casefold() for x in inventory):raise ProviderError('暂存目录名称冲突，请重新预览',409)
        plan={'id':key,'account_id':account_id,'uid':uid,'revision':revision,'parameters':selection['parameters'],'files':files,
              'target_parent':parent,'target':target,'folder_name':folder,'folder_id':None,'parent_ids':[x['id'] for x in inventory],
              'state':'preview','mkdir_state':'pending','receive_state':'pending','targets':[],'created_at':now,'expires':now+600}
        _source_check(plan,value)
        rows=_read('plans');rows={k:p for k,p in rows.items() if not (p['state']=='preview' and p['expires']<now)}
        done=sorted((p for p in rows.values() if p['state'] in ('done','cancelled')),key=lambda p:p['created_at'])
        while len(rows)>=50 and done:rows.pop(done.pop(0)['id'])
        if len(rows)>=50:raise ProviderError('转存记录较多，请先处理未完成任务',429)
        rows[key]=plan;_save('plans',rows)
        return detail(key)


def _folder(driver,plan):
    item=driver.stat(plan['folder_id'])
    if not item or not item['is_dir'] or item['name']!=plan['folder_name'] or item.get('parent_id')!=plan['target_parent']:
        raise ProviderError('本任务暂存目录的原 ID 与位置无法核实，未继续',409)


def _targets(driver,plan):
    _folder(driver,plan);rows=organize._inventory(driver,plan['folder_id'])
    if len(rows)!=len(plan['files']) or len({x['id'] for x in rows})!=len(rows) or any(x['is_dir'] for x in rows):
        raise ProviderError('转存目标数量不完整或混入其他对象，不会猜测源对应关系',409)
    actual=[]
    for row in rows:
        item=driver.stat(row['id'])
        if not item or item['is_dir'] or item.get('parent_id')!=plan['folder_id']:
            raise ProviderError('目标文件原 ID 与暂存目录不一致',409)
        actual.append(item)
    result=[];used=set()
    for source in plan['files']:
        matches=[x for x in actual if x['sha1']==source['sha1'] and x['size']==source['size'] and Path(x['name']).suffix.lower()==Path(source['name']).suffix.lower()]
        if len(matches)!=1 or matches[0]['id'] in used:raise ProviderError('转存内容证据不能唯一确定目标 ID，未登记绑定',409)
        used.add(matches[0]['id']);result.append({'source_id':source['id'],'file':matches[0]})
    return result


def _finish(rows,plan,driver):
    plan['targets']=_targets(driver,plan)
    repo.save_received(plan)
    plan['receive_state']='confirmed';plan['state']='done'
    plan['message']='转存目标已由官方接口核验为本账号新文件 ID；可按这些 ID 整理并生成 STRM'
    _save('plans',rows)
    return detail(plan['id'])


def execute(plan_id):
    with cloud115._engine().mutation_lock():
        rows=_read('plans');plan=rows.get(plan_id)
        if not plan:raise ProviderError('转存计划不存在',404)
        if plan['state']=='done':return detail(plan_id)
        if plan['state'] not in ('preview','awaiting_resume'):raise ProviderError('转存结果需先复核，禁止自动重发',409)
        if plan['state']=='preview' and plan['expires']<time.time():raise ProviderError('转存预览已过期',410)
        driver,uid,revision,value=_authorized(plan['account_id'])
        if uid!=plan['uid'] or revision!=plan['revision']:raise ProviderError('转存授权在预览后变化，请重新预览',409)
        _not_duplicate(plan['account_id'],plan['parameters'],plan['files'],driver,exclude=plan_id)
        _source_check(plan,value)
        if organize._directory(driver,plan['target_parent'])!=plan['target']:raise ProviderError('目标目录身份在预览后变化',409)
        try:
            plan['state']='running'
            if plan['mkdir_state']=='pending':
                if any(x['name'].casefold()==plan['folder_name'].casefold() for x in organize._inventory(driver,plan['target_parent'])):
                    raise ProviderError('暂存目录被其他对象占用',409)
                plan['mkdir_state']='submitted';_save('plans',rows)
                plan['folder_id']=driver.mkdir(plan['target_parent'],plan['folder_name']);_save('plans',rows)
                _folder(driver,plan)
                if organize._inventory(driver,plan['folder_id']):raise ProviderError('新暂存目录不是空目录',409)
                plan['mkdir_state']='confirmed';_save('plans',rows)
            if plan['mkdir_state']!='confirmed' or plan['receive_state']!='pending':raise ProviderError('已有未确认写请求，不能重复转存',409)
            _folder(driver,plan)
            if organize._inventory(driver,plan['folder_id']):raise ProviderError('暂存目录出现其他文件，未提交转存',409)
            _source_check(plan,value)
            plan['receive_state']='submitted';_save('plans',rows)
            web.transport('receive',{'share_code':plan['parameters']['share_code'],'receive_code':plan['parameters']['receive_code'],
                                    'file_id':','.join(x['id'] for x in plan['files']),'cid':plan['folder_id']},web_cookie=value)
            return _finish(rows,plan,driver)
        except (ProviderError,OSError,ValueError):
            plan['state']='unknown';plan['message']='创建目录或转存结果未确认；保留意图，仅按任务目录原 ID 复核，不重发写请求'
            _save('plans',rows)
            raise ProviderError(plan['message'],409) from None


def reconcile(plan_id):
    with cloud115._engine().mutation_lock():
        rows=_read('plans');plan=rows.get(plan_id)
        if not plan:raise ProviderError('转存记录不存在',404)
        if plan['state']=='done':return detail(plan_id)
        if plan['state'] in ('preview','cancelled'):raise ProviderError('转存尚未执行或已取消',409)
        driver=Driver(plan['account_id'])
        if driver.validate_account()!=plan['uid']:raise ProviderError('转存账号身份不一致',409)
        if plan['mkdir_state']=='pending':
            plan['state']='awaiting_resume';plan['message']='没有已提交的写步骤，可以重新确认执行'
            _save('plans',rows);return detail(plan_id)
        if not plan['folder_id']:
            # The exact random intent marker recovers a directory-creation crash;
            # this is not title/path matching for deleting media source files.
            candidates=[x for x in organize._inventory(driver,plan['target_parent']) if x['is_dir'] and x['name']==plan['folder_name'] and x['id'] not in plan['parent_ids']]
            if len(candidates)!=1:raise ProviderError('无法唯一复核本次目录创建意图，不会再次创建或转存',409)
            plan['folder_id']=candidates[0]['id'];_save('plans',rows)
        _folder(driver,plan);plan['mkdir_state']='confirmed';_save('plans',rows)
        if plan['receive_state'] in ('submitted','confirmed'):return _finish(rows,plan,driver)
        if organize._inventory(driver,plan['folder_id']):raise ProviderError('未提交转存的暂存目录出现了其他对象',409)
        plan['state']='awaiting_resume';plan['message']='已复核目录创建；可确认继续尚未提交的转存请求'
        _save('plans',rows);return detail(plan_id)


def cancel(plan_id):
    with cloud115._engine().mutation_lock():
        rows=_read('plans');plan=rows.get(plan_id)
        if not plan:raise ProviderError('转存计划不存在',404)
        if plan['receive_state']!='pending' or plan['mkdir_state']=='submitted':raise ProviderError('存在未知写请求，请先复核，不能取消意图',409)
        if plan['folder_id']:
            driver=Driver(plan['account_id']);driver.validate_account();_folder(driver,plan)
            if organize._inventory(driver,plan['folder_id']):raise ProviderError('暂存目录非空，不能取消',409)
        plan['state']='cancelled';plan['message']='未提交转存的计划已取消；已创建的空目录保留，不自动递归删除'
        _save('plans',rows);return detail(plan_id)


def pending(account_id,files,driver):
    rows=[p for p in _read('plans').values() if p['account_id']==account_id and p['state'] in ('running','unknown','awaiting_resume')]
    if not rows:return
    if any(not p['folder_id'] for p in rows):raise ProviderError('本账号有未确认目录创建意图，请先复核转存记录',409)
    folders={p['folder_id'] for p in rows}
    for item in files:
        actual=driver.stat(item['id'])
        if actual and (actual.get('parent_id') is None or actual['parent_id'] in folders):
            raise ProviderError('该源所在转存任务结果未确认，禁止登记、整理或交叉删除',409)
