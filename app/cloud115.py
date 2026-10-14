"""First 115 vertical slice: verified identities, owned STRM and delete receipts.

Quality decisions stay in the existing governance domain. This service never
discovers a deletable cloud file by title, directory uniqueness or fuzzy names.
"""
import hashlib
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
from pathlib import Path
if __package__:
    from . import cloud115_store as repo, config, plan_guard
    from .providers.p115_open import Driver, ProviderError, auth_lock, identifier, token, transport, user_id, same_file
else:
    import cloud115_store as repo
    import config, plan_guard
    from providers.p115_open import Driver, ProviderError, auth_lock, identifier, token, transport, user_id, same_file


def _engine():
    if __package__:
        from . import engine
    else:
        import engine
    return engine


def _organize():
    if __package__:
        from . import cloud115_organize
    else:
        import cloud115_organize
    return cloud115_organize


def _transfer():
    if __package__:
        from . import cloud115_transfer
    else:
        import cloud115_transfer
    return cloud115_transfer


def public_status():
    cfg=config.load_config()
    cookie_accounts=set(_transfer().cookie_accounts())
    accounts=[dict(a,has_transfer_cookie=a['id'] in cookie_accounts) for a in repo.accounts()]
    return {'accounts':accounts,'require_binding':cfg.get('cloud115_require_binding','0')=='1',
            'playback_base':cfg.get('cloud115_playback_base',''),
            'bindings':repo.bindings(),'deletions':repo.deletions(),'organize_plans':_organize().public_plans(),'transfer_plans':_transfer().public_plans(),
            'capabilities':{'official_api':True,'strm_generate':True,'identity_delete':True,
                            'share_transfer':True,'official_share_transfer':False,'transfer_auth':'web_cookie',
                            'organize':True,'automatic_organize':False,'qr_login':True},
            'verification':'需使用真实 115 账号验证接口权限与响应；不会根据前端配置宣称已联调'}


def save_account(body):
    access=token(body.get('access_token'))
    refresh=body.get('refresh_token','')
    if refresh:
        refresh=token(refresh)
    name=body.get('name','115')
    if not isinstance(name,str) or not 1<=len(name.strip())<=80:
        raise ProviderError('账号名称应为 1～80 个字符',400)
    with auth_lock():
        uid=user_id(transport('user',access_token=access))
        key=repo.save_account(uid,name.strip(),access,refresh,0)
    return {'id':key,'uid':uid,'name':name.strip()}


def save_policy(body):
    if not isinstance(body.get('require_binding'),bool):
        raise ProviderError('删除模式必须为布尔值',400)
    value=body.get('playback_base','')
    if not isinstance(value,str) or len(value)>1024:
        raise ProviderError('播放服务地址无效',400)
    value=value.strip().rstrip('/')
    if value:
        try:
            parts=urllib.parse.urlsplit(value)
            invalid=(parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or any(c.isspace() or ord(c)<32 or c in '\\<>"' for c in value)
                or any(p in ('.','..') for p in urllib.parse.unquote(parts.path).split('/'))
                or (parts.port is not None and not 1<=parts.port<=65535))
        except ValueError:
            invalid=True
        if invalid:
            raise ProviderError('播放地址只能包含 HTTP/HTTPS 地址与有效路径前缀',400)
    cfg=config.load_config()
    cfg.update(cloud115_require_binding='1' if body['require_binding'] else '0',cloud115_playback_base=value)
    config.save_config(cfg)
    return {'require_binding':body['require_binding'],'playback_base':value}


def target_path(relative):
    root=_engine().L_ROOT
    if not isinstance(relative,str) or not 1<=len(relative)<=2048 or '\\' in relative:
        raise ProviderError('请输入本地库内的 STRM 相对路径',400)
    rel=Path(relative)
    if rel.is_absolute() or any(x in ('.','..') for x in relative.split('/')) or rel.suffix.lower()!='.strm':
        raise ProviderError('必须使用库内相对路径并以 .strm 结尾',400)
    if any(ord(c)<32 for c in relative):
        raise ProviderError('路径包含无效字符',400)
    path=root/rel
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError,OSError):
        raise ProviderError('路径不在当前本地 STRM 库内',400) from None
    cur=path
    while True:
        if cur.is_symlink():
            raise ProviderError('不允许写入软链接路径',400)
        if cur==root:
            break
        cur=cur.parent
    if not root.is_dir():
        raise ProviderError('本地 STRM 根目录不可用',409)
    return root,path


def generate(body):
    account_id=body.get('account_id')
    fid=identifier(body.get('file_id'))
    root,path=target_path(body.get('relative_path'))
    base=config.load_config().get('cloud115_playback_base','')
    if not base:
        raise ProviderError('请先保存 Emby/播放器可访问的播放服务地址',409)
    sidecar_ids=body.get('sidecar_ids',[])
    if not isinstance(sidecar_ids,list) or len(sidecar_ids)>20:
        raise ProviderError('关联文件最多 20 个，必须明确选择文件 ID',400)
    sidecar_ids=[identifier(x) for x in sidecar_ids]
    if len(set(sidecar_ids))!=len(sidecar_ids) or fid in sidecar_ids:
        raise ProviderError('关联文件 ID 重复或包含主视频',400)
    with _engine().mutation_lock():
        driver=Driver(account_id)
        driver.validate_account()
        sidecar_ids=list(dict.fromkeys(sidecar_ids+_transfer().known_sidecars(account_id,fid)))
        if len(sidecar_ids)>20:raise ProviderError('用户选择与已核验转存关联文件总数超过 20',400)
        remote=driver.stat(fid)
        if not remote or remote['is_dir'] or Path(remote['name']).suffix.lower() not in _engine().VIDEO_EXTS:
            raise ProviderError('请选择已存在的视频文件，不能选择目录',400)
        if not remote.get('sha1') and not remote.get('pickcode'):
            raise ProviderError('文件缺少可复核身份，未生成 STRM',409)
        main_origin=_transfer().provenance(account_id,remote)
        if main_origin and main_origin['role']!='video':raise ProviderError('该转存文件的已登记角色不是主视频',409)
        if main_origin:remote=dict(remote,provenance=main_origin)
        sidecars=[]
        for sid in sidecar_ids:
            item=driver.stat(sid)
            if not item or item['is_dir'] or Path(item['name']).suffix.lower() in _engine().VIDEO_EXTS:
                raise ProviderError('关联文件必须是明确选择的字幕或元数据文件',400)
            if Path(item['name']).suffix.lower() not in ('.srt','.ass','.ssa','.sub','.idx','.vtt','.json','.nfo'):
                raise ProviderError('关联文件格式不支持',400)
            if not item.get('sha1') and not item.get('pickcode'):
                raise ProviderError('关联文件缺少可复核身份',409)
            origin=_transfer().provenance(account_id,item)
            if origin and (not main_origin or origin['transfer_id']!=main_origin['transfer_id']):
                raise ProviderError('关联文件已有其他转存来源归属，禁止人工串配到此视频',409)
            if origin:item=dict(item,provenance=origin)
            sidecars.append(item)
        _organize().pending(account_id,[fid]+sidecar_ids)
        _transfer().pending(account_id,[remote]+sidecars,driver)
        prior=repo.binding(path=path)
        if path.exists() or (prior and prior['state']!='deleted'):
            raise ProviderError('目标已有文件或绑定，未覆盖；请使用另一条明确路径',409)
        secret=secrets.token_urlsafe(32)
        digest=hashlib.sha256(secret.encode()).hexdigest()
        try:
            key=repo.reserve(account_id,remote,path,root,digest,sidecars)
        except sqlite3.IntegrityError:
            raise ProviderError('该云端文件或路径已绑定，未创建重复 STRM',409) from None
        try:
            # O_EXCL + O_NOFOLLOW forbids replacing existing files/links.
            # Reserve first: crashes cannot create an owned
            # looking file that is later deleted through the old CD2 heuristics.
            target_path(body['relative_path'])
            content=(base+'/play/115/'+key+'?token='+secret+'\n').encode()
            _write_new(root,path.relative_to(root),content)
            fingerprint=plan_guard.fingerprint(path,[root])
            if not fingerprint:
                raise ProviderError('STRM 写入后身份无法确认，已保留待核实绑定',409)
            repo.set_binding_state(key,'active',fingerprint)
        except Exception:
            # Never delete an existing user file while recovering a failed write.
            repo.set_binding_state(key,'needs_review')
            raise
        _engine()._invalidate_lib_cache()
        return {'status':'success','binding_id':key,'strm_path':str(path),'file_id':fid,
                'message':'STRM 已生成并绑定 115 文件 ID；真实入库由 Emby 确认'}


def _write_new(root,relative,content):
    """Use directory descriptors, not a check-then-follow path through links."""
    directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            try:
                os.mkdir(part,dir_fd=directory)
            except FileExistsError:
                pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            os.close(directory);directory=child
        fd=os.open(relative.name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=directory)
        with os.fdopen(fd,'wb') as stream:
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        os.fsync(directory)
    finally:
        os.close(directory)


def managed_marker(path):
    try:
        with Path(path).open('rb') as stream:
            raw=stream.read(plan_guard.MAX_STRM_BYTES+1)
        if len(raw)>plan_guard.MAX_STRM_BYTES:
            return True
        text=raw.decode('utf-8').strip()
        return '/play/115/' in urllib.parse.urlsplit(text).path
    except (OSError,UnicodeError,ValueError):
        return True  # Unreadable identity is never permission for a path fallback.


def adopt(body):
    """Import only an explicit pickcode in an existing STRM URL.

    Path/filename matches are NOT proof. The official API resolves the code to
    an ID, then a separate ID lookup verifies it under the selected account.
    No STRM rewriting or remote mutation occurs.
    """
    root,path=target_path(body.get('relative_path'))
    with _engine().mutation_lock():
        before=plan_guard.fingerprint(path,[root])
        prior=repo.binding(path=path)
        if not before or (prior and prior['state']!='deleted'):
            raise ProviderError('文件不可读、已绑定或不是普通 STRM，未接管',409)
        try:
            content=path.read_text(encoding='utf-8').strip()
        except (OSError,UnicodeError):
            raise ProviderError('STRM 内容不可读，未接管',409) from None
        parts=None
        try:
            parts=urllib.parse.urlsplit(content)
            values=urllib.parse.parse_qs(parts.query,keep_blank_values=True)
            codes=values.get('pickcode',[])+values.get('pick_code',[])
        except ValueError:
            codes=[]
        if parts is None or parts.scheme not in ('http','https') or len(codes)!=1:
            raise ProviderError('旧 STRM 没有唯一显式 pickcode 参数；不会按名称或目录猜测',409)
        driver=Driver(body.get('account_id'))
        driver.validate_account()
        remote=driver.file_id_for_pickcode(codes[0])
        _organize().pending(body.get('account_id'),[remote['id']])
        _transfer().pending(body.get('account_id'),[remote],driver)
        if Path(remote['name']).suffix.lower() not in _engine().VIDEO_EXTS:
            raise ProviderError('STRM 解析到的源不是视频文件，未接管',409)
        main_origin=_transfer().provenance(body['account_id'],remote)
        sidecars=[]
        if main_origin:
            if main_origin['role']!='video':raise ProviderError('已登记转存角色不是视频，未接管',409)
            remote=dict(remote,provenance=main_origin)
            for sid in _transfer().known_sidecars(body['account_id'],remote['id']):
                item=driver.stat(sid)
                if not item or item['is_dir'] or Path(item['name']).suffix.lower() not in _organize().SIDECAR_EXTS:
                    raise ProviderError('已登记转存关联文件无法复核，未接管',409)
                origin=_transfer().provenance(body['account_id'],item)
                if not origin or origin['transfer_id']!=main_origin['transfer_id']:
                    raise ProviderError('转存关联身份冲突，未接管',409)
                sidecars.append(dict(item,provenance=origin))
            _transfer().pending(body['account_id'],[remote]+sidecars,driver)
        if plan_guard.fingerprint(path,[root])!=before:
            raise ProviderError('核验期间 STRM 已变化，未接管',409)
        try:
            key=repo.reserve(body['account_id'],remote,path,root,'',sidecars)
        except sqlite3.IntegrityError:
            raise ProviderError('该文件已被其他绑定占用，未接管',409) from None
        repo.set_binding_state(key,'active',before)
        return {'status':'success','binding_id':key,'file_id':remote['id'],
                'message':'显式提取码已由官方接口核验；保留原播放地址，只沿用已登记转存关联文件'}


def verify_binding(binding,path,root):
    if binding['root']!=str(root) or binding['strm_path']!=str(path):
        raise ProviderError('STRM 绑定的库路径与当前配置不一致，已保留',409)
    if binding['state'] not in ('active','remote_deleted'):
        raise ProviderError('STRM 绑定状态需要核实，禁止回退路径匹配',409)
    if plan_guard.fingerprint(path,[root])!=binding['fingerprint']:
        raise ProviderError('STRM 已被修改或不可读，原绑定不再允许删除',409)


def delete_source(path,root,dry_run=False):
    """Count newly confirmed facts, including partial failures, only once."""
    binding=repo.binding(path=path)
    if not binding or dry_run:
        return _delete_source(path,root,dry_run)
    targets=[binding['remote']]+binding['sidecars']
    before={x['id'] for x in targets if (repo.receipt(binding['id'],x['id']) or {}).get('state')=='confirmed'}
    def counts():
        newly={x['id'] for x in targets if x['id'] not in before and (repo.receipt(binding['id'],x['id']) or {}).get('state')=='confirmed'}
        return {'cloud_removed':int(binding['file_id'] in newly),
                'cloud_sidecars_removed':len(newly-{binding['file_id']})}
    try:
        result=_delete_source(path,root,dry_run)
    except ProviderError as error:
        try:
            error.confirmed_counts=counts()
        except sqlite3.Error:
            pass
        raise
    return dict(result,**counts())


def _delete_source(path,root,dry_run=False):
    """Return None only for an explicitly legacy, unbound STRM.

    Every binding, including damaged/pending/tombstone records, claims its path;
    failures NEVER fall back to CD2. Callers hold the existing mutation lock.
    """
    binding=repo.binding(path=path)
    if binding is None:
        strict=config.load_config().get('cloud115_require_binding','0')=='1'
        if strict or managed_marker(path):
            raise ProviderError('本地 STRM 没有已核验的云端绑定，已保留；不会按名称猜测删除',409)
        return None
    _organize().pending(binding['account_id'],[x['id'] for x in [binding['remote']]+binding['sidecars']])
    verify_binding(binding,path,root)
    driver=Driver(binding['account_id'])
    driver.validate_account()
    targets=[binding['remote']]+binding['sidecars']
    _transfer().pending(binding['account_id'],targets,driver)
    if dry_run:
        planned=[]
        for expected in targets:
            actual=driver.stat(expected['id'])
            old=repo.receipt(binding['id'],expected['id'])
            if old:
                if old['expected']!=expected or actual is not None:
                    raise ProviderError('删除回执或源可见状态需要复核，未通过预览',409)
                if old['state']!='confirmed':planned.append(expected['id'])
            elif not same_file(expected,actual):
                raise ProviderError('预览复核发现云端文件身份不一致，未删除',409)
            else:planned.append(expected['id'])
        return {'cloud_removed':int(binding['file_id'] in planned),
                'cloud_sidecars_removed':len(set(planned)-{binding['file_id']}),'binding_id':binding['id']}
    # Preflight every associated object before any mutation. Recorded receipts
    # are checked separately because some objects may already be gone.
    for expected in targets:
        old=repo.receipt(binding['id'],expected['id'])
        if old:
            if old['expected']!=expected:
                raise ProviderError('删除记录与绑定不一致，已停止',409)
            continue
        if not same_file(expected,driver.stat(expected['id'])):
            raise ProviderError('115 源文件不存在或身份发生变化，STRM 保留',409)
    for expected in targets:
        sid=expected['id']
        old=repo.receipt(binding['id'],sid)
        if old and old['state']=='confirmed':
            # Reappearing IDs must not be deleted without a new verified binding.
            if driver.stat(sid) is not None:
                raise ProviderError('曾确认删除的文件重新出现，已保留 STRM',409)
            continue
        if old:
            # Existing intent means a write could already have happened. Only
            # reconcile; never blindly submit the same delete a second time.
            if driver.stat(sid) is not None:
                raise ProviderError('115 删除结果待核实，源仍可见；不会自动重复提交',409)
            repo.finish_delete(binding['id'],sid,'confirmed')
            continue
        verify_binding(binding,path,root)
        if not same_file(expected,driver.stat(sid)):
            raise ProviderError('执行前云端身份已变化，STRM 保留',409)
        if not repo.begin_delete(binding['id'],expected):
            raise ProviderError('该文件已有删除操作，请先核实结果',409)
        try:
            driver.delete(sid)
        except ProviderError:
            repo.finish_delete(binding['id'],sid,'unknown','官方删除结果未确认；后续仅复核，不自动重发')
            raise ProviderError('115 删除结果未确认，STRM 保留；操作记录已保存',409) from None
        try:
            gone=driver.stat(sid) is None
        except ProviderError:
            gone=False
        if not gone:
            repo.finish_delete(binding['id'],sid,'unknown','115 尚未确认文件消失，STRM 保留')
            raise ProviderError('115 尚未确认删除完成，STRM 保留；稍后可重新核实',409)
        repo.finish_delete(binding['id'],sid,'confirmed')
    repo.set_binding_state(binding['id'],'remote_deleted')
    return {'cloud_removed':1,'cloud_sidecars_removed':len(binding['sidecars']),'binding_id':binding['id']}


def finish_local(binding_id):
    repo.set_binding_state(binding_id,'deleted')


def reconcile(binding_id):
    """Read-only remote reconciliation: never issue delete or rewrite STRM."""
    with _engine().mutation_lock():
        binding=repo.binding(binding_id=binding_id)
        if not binding:
            raise ProviderError('绑定不存在',404)
        try:
            relative=str(Path(binding['strm_path']).relative_to(_engine().L_ROOT))
        except ValueError:
            raise ProviderError('当前本地库不包含原绑定路径，未复核',409) from None
        root,path=target_path(relative)
        if binding['root']!=str(root):
            raise ProviderError('库路径已变化，未复核',409)
        driver=Driver(binding['account_id'])
        driver.validate_account()
        if binding['state'] in ('generating','needs_review'):
            fp=plan_guard.fingerprint(path,[root])
            if not fp:
                raise ProviderError('生成的 STRM 不存在或不可读，请核对绑定记录',409)
            try:
                parts=urllib.parse.urlsplit(path.read_text().strip())
            except (ValueError,UnicodeError):
                raise ProviderError('STRM 内容无法核实，未激活绑定',409) from None
            params=urllib.parse.parse_qs(parts.query)
            if binding['token_hash']:
                value=params.get('token',[])
                valid=(parts.path.endswith('/play/115/'+binding['id']) and len(value)==1
                       and secrets.compare_digest(hashlib.sha256(value[0].encode()).hexdigest(),binding['token_hash']))
            else:
                values=params.get('pickcode',[])+params.get('pick_code',[])
                valid=len(values)==1 and values[0]==binding['remote']['pickcode']
            if parts.scheme not in ('http','https') or not valid or not all(same_file(x,driver.stat(x['id'])) for x in [binding['remote']]+binding['sidecars']):
                raise ProviderError('STRM 内容与云端身份无法同时核实，未激活绑定',409)
            if plan_guard.fingerprint(path,[root])!=fp:
                raise ProviderError('核验期间 STRM 已变化，未激活绑定',409)
            repo.set_binding_state(binding['id'],'active',fp)
            return {'state':'active','message':'已复核生成结果，身份绑定已激活'}
        try:
            path.lstat()
            local_absent=False
        except FileNotFoundError:
            local_absent=True
        if not (local_absent and binding['state']=='remote_deleted'):
            verify_binding(binding,path,root)
        targets=[binding['remote']]+binding['sidecars']
        confirmed=0
        for expected in targets:
            record=repo.receipt(binding['id'],expected['id'])
            if not record:
                continue
            if record['expected']!=expected:
                raise ProviderError('删除记录与绑定不一致',409)
            actual=driver.stat(expected['id'])
            if actual is None:
                repo.finish_delete(binding['id'],expected['id'],'confirmed')
                confirmed+=1
            elif record['state']=='confirmed' or not same_file(expected,actual):
                raise ProviderError('云端文件重新出现或身份已变化，未处理 STRM',409)
        if confirmed==len(targets):
            if local_absent:
                repo.set_binding_state(binding['id'],'deleted')
                return {'state':'deleted','message':'已复核原 ID 删除回执与本地 STRM 缺失，清理状态已恢复'}
            repo.set_binding_state(binding['id'],'remote_deleted')
            return {'state':'remote_deleted','message':'云端源及登记关联文件均已确认删除；请返回治理完成 STRM 清理'}
        return {'state':binding['state'],'message':'已按原 ID 复核；仍有文件可见或未提交删除，没有重发删除请求'}


def playback(binding_id,secret,user_agent=''):
    binding=repo.binding(binding_id=binding_id)
    if not binding or binding['state']!='active' or not isinstance(secret,str) or len(secret)>200:
        raise ProviderError('播放文件不存在或授权失效',404)
    digest=hashlib.sha256(secret.encode()).hexdigest()
    if not secrets.compare_digest(digest,binding['token_hash']):
        raise ProviderError('播放授权无效',403)
    verify_binding(binding,Path(binding['strm_path']),_engine().L_ROOT)
    driver=Driver(binding['account_id'])
    driver.validate_account()
    item=driver.stat(binding['file_id'])
    if not same_file(binding['remote'],item):
        raise ProviderError('云端文件身份已变化或不存在',409)
    return driver.download(item['pickcode'],user_agent=user_agent,file_id=item['id'])
