"""115 OpenAPI-only transport, written against public endpoint contracts.

No Web/App Cookie fallback, default third-party AppID, automatic write retries,
arbitrary URL proxy or copies of MoviePilot/plugin code. Response fixtures are
contract tests, not evidence of live account capabilities.
"""
import contextlib
import fcntl
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
if __package__ == 'app.providers':
    from .. import cloud115_store as repo, storage
else:
    import cloud115_store as repo
    import storage

_AUTH = threading.RLock()
_NETWORK = threading.BoundedSemaphore(3)
MAX_RESPONSE = 4 * 1024 * 1024
ROUTES = {
    'user': ('GET','https://proapi.115.com/open/user/info'),
    'files': ('GET','https://proapi.115.com/open/ufile/files'),
    'stat': ('GET','https://proapi.115.com/open/folder/get_info'),
    'delete': ('POST','https://proapi.115.com/open/ufile/delete'),
    'move': ('POST','https://proapi.115.com/open/ufile/move'),
    'rename': ('POST','https://proapi.115.com/open/ufile/update'),
    'mkdir': ('POST','https://proapi.115.com/open/folder/add'),
    'download': ('POST','https://proapi.115.com/open/ufile/downurl'),
    'refresh': ('POST','https://qrcodeapi.115.com/open/refreshToken'),
    'device_start': ('POST','https://qrcodeapi.115.com/open/authDeviceCode'),
    'device_status': ('GET','https://qrcodeapi.115.com/get/status/'),
    'device_exchange': ('POST','https://qrcodeapi.115.com/open/deviceCodeToToken'),
}


class ProviderError(ValueError):
    def __init__(self, message, code=502, *, missing=False, auth=False):
        super().__init__(message)
        self.code, self.missing, self.auth = code, missing, auth


def identifier(value, root=False):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]{1,30}', str(value)):
        raise ProviderError('115 文件/账号 ID 必须是完整数字字符串',400)
    value = str(value)
    if not root and int(value) == 0:
        raise ProviderError('不能将根目录作为文件操作目标',400)
    return value


def token(value):
    if not isinstance(value,str) or not 1 <= len(value) <= 8192 or any(c.isspace() or ord(c)<32 for c in value):
        raise ProviderError('请填写完整官方授权令牌，不包含 Bearer 前缀',400)
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def transport(action, payload=None, access_token='', user_agent=None):
    method,url = ROUTES[action]
    payload = payload or {}
    encoded = urllib.parse.urlencode(payload).encode()
    if method == 'GET' and payload:
        url += '?' + encoded.decode()
    headers = {'Accept':'application/json','User-Agent':'StrimKeep-115Open',
               'Content-Type':'application/x-www-form-urlencoded'}
    if user_agent is not None:
        if action!='download' or not isinstance(user_agent,str) or len(user_agent)>1024 or any(ord(c)<32 or ord(c)>126 for c in user_agent):
            raise ProviderError('播放请求的 User-Agent 无效',400)
        headers['User-Agent']=user_agent
    if access_token:
        headers['Authorization'] = 'Bearer '+access_token
    if not _NETWORK.acquire(False):
        raise ProviderError('115 请求繁忙，请稍后重试',429)
    try:
        request = urllib.request.Request(url, data=encoded if method=='POST' else None, method=method,headers=headers)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect())
        try:
            with opener.open(request,timeout=20) as response:
                raw = response.read(MAX_RESPONSE+1)
                if len(raw)>MAX_RESPONSE or 'json' not in response.headers.get('Content-Type','').lower():
                    raise ProviderError('115 响应过大或不是 JSON，操作结果未确认')
                result = json.loads(raw)
        except urllib.error.HTTPError as error:
            code=error.code
            error.close()
            raise ProviderError('115 授权失效' if code==401 else '115 HTTP 请求未成功，操作结果未确认',
                                401 if code==401 else 502,auth=code==401) from None
        except (urllib.error.URLError,TimeoutError,OSError,ValueError):
            raise ProviderError('115 连接/响应异常，操作结果未确认') from None
        if not isinstance(result,dict):
            raise ProviderError('115 返回结构不受支持')
        return check(result, action)
    finally:
        _NETWORK.release()


def check(result, action):
    if result.get('state') is True or (type(result.get('state')) is int and result['state']==1):
        return result
    if action in ('device_start','device_status','device_exchange','refresh') and type(result.get('code')) is int and result['code']==0 and result.get('state') is not False:
        return result
    # Only explicit documented not-found codes on a file lookup mean absence.
    errno=str(result.get('errno',result.get('code','')))
    if action=='stat' and errno in ('20004','430004'):
        raise ProviderError('115 文件不存在',404,missing=True)
    if errno in ('40140125','40140126'):
        raise ProviderError('115 授权失效',401,auth=True)
    raise ProviderError('115 拒绝操作或响应未确认；请检查应用权限与授权')


@contextlib.contextmanager
def auth_lock():
    with _AUTH:
        directory=storage._state_dir()
        directory.mkdir(parents=True,exist_ok=True)
        with (directory/'.cloud115-auth.lock').open('a') as fh:
            fcntl.flock(fh,fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh,fcntl.LOCK_UN)


def user_id(response):
    data=response.get('data')
    if not isinstance(data,dict) or 'user_id' not in data:
        raise ProviderError('115 用户身份响应暂不兼容，未保存授权')
    return identifier(data['user_id'])


def normalize_file(data, expected_id=None):
    if not isinstance(data,dict):
        raise ProviderError('115 文件详情暂不兼容')
    category=str(data.get('file_category',data.get('fc','')))
    fid=identifier(data.get('file_id',data.get('fid',data.get('category_id','') if category=='0' else '')))
    if category not in ('0','1') or expected_id is not None and fid!=str(expected_id):
        raise ProviderError('115 返回的文件身份或类型不一致')
    name=data.get('file_name',data.get('fn',data.get('category_name') if category=='0' else None))
    if not isinstance(name,str) or not name or len(name)>1024:
        raise ProviderError('115 文件名缺失')
    size=data.get('size_byte',data.get('fs',data.get('file_size')))
    if category=='1' and (isinstance(size,bool) or not re.fullmatch(r'\d+',str(size))):
        raise ProviderError('115 文件大小缺失，不能建立可删除绑定')
    pickcode=str(data.get('pick_code',data.get('pc','')) or '')
    sha1=str(data.get('sha1',data.get('file_sha1','')) or '').upper()
    if sha1 and not re.fullmatch(r'[A-F0-9]{40}',sha1):
        raise ProviderError('115 文件校验信息无效')
    result={'id':fid,'name':name,'is_dir':category=='0','size':int(size or 0) if category=='1' else 0,
            'pickcode':pickcode,'sha1':sha1}
    parent=data.get('parent_id',data.get('pid',data.get('category_id')))
    if parent is not None:
        result['parent_id']=identifier(parent,root=True)
    return result


def same_file(expected,actual):
    if actual is None or actual['is_dir'] or expected['is_dir']:
        return False
    if expected['id']!=actual['id'] or expected['size']!=actual['size']:
        return False
    if expected.get('sha1'):
        return actual.get('sha1')==expected['sha1']
    return bool(expected.get('pickcode')) and actual.get('pickcode')==expected['pickcode']


class Driver:
    def __init__(self,account_id):
        self.account_id=account_id

    def request(self,action,payload=None,user_agent=None):
        cfg=repo.account(self.account_id)
        if not cfg or not cfg['enabled']:
            raise ProviderError('115 账号未配置或需要重新保存授权',409)
        extra={'user_agent':user_agent} if user_agent is not None else {}
        try:
            return transport(action,payload,cfg['access_token'],**extra)
        except ProviderError as error:
            if not error.auth or not cfg['refresh_token'] or action in ('delete','move','rename','mkdir'):
                raise
        with auth_lock():
            current=repo.account(self.account_id)
            if not current or not current['enabled']:
                raise ProviderError('115 授权刷新尚未确认，请重新保存授权',409)
            if current['revision']!=cfg['revision']:
                return transport(action,payload,current['access_token'],**extra)
            # Rotation consumes the old refresh token. Persist a disabled marker
            # first so a crash/ambiguous response cannot blindly reuse that token.
            repo.disable_account(self.account_id)
            result=transport('refresh',{'refresh_token':cfg['refresh_token']})
            data=result.get('data') or {}
            access,refresh=token(data.get('access_token')),token(data.get('refresh_token'))
            if user_id(transport('user',access_token=access))!=cfg['uid']:
                raise ProviderError('刷新后的 115 账号身份发生变化，已停用授权',409)
            seconds=data.get('expires_in',0)
            expires=time.time()+int(seconds) if str(seconds).isdigit() else 0
            repo.rotate(self.account_id,cfg['revision'],access,refresh,expires)
            return transport(action,payload,access,**extra)

    def validate_account(self):
        uid=user_id(self.request('user'))
        if uid!=repo.account(self.account_id)['uid']:
            raise ProviderError('115 授权账号与文件绑定不一致，已停止操作',409)
        return uid

    def stat(self,file_id):
        file_id=identifier(file_id)
        try:
            result=self.request('stat',{'file_id':file_id})
        except ProviderError as error:
            if error.missing:
                return None
            raise
        return normalize_file(result.get('data'),file_id)

    def files(self,parent_id='0',offset=0,limit=50):
        result=self.request('files',{'cid':identifier(parent_id,root=True),'offset':offset,
                                    'limit':limit,'show_dir':1,'record_open_time':0})
        rows=result.get('data')
        if not isinstance(rows,list):
            raise ProviderError('115 目录响应暂不兼容')
        return {'items':[normalize_file(row) for row in rows],
                'next_offset':offset+len(rows) if len(rows)==limit else None}

    def delete(self,file_id):
        return self.request('delete',{'file_ids':identifier(file_id)})

    def move(self,file_id,parent_id):
        return self.request('move',{'file_ids':identifier(file_id),'to_cid':identifier(parent_id,root=True)})

    def rename(self,file_id,name):
        return self.request('rename',{'file_id':identifier(file_id),'file_name':name})

    def mkdir(self,parent_id,name):
        result=self.request('mkdir',{'pid':identifier(parent_id,root=True),'file_name':name})
        data=result.get('data')
        if not isinstance(data,dict):raise ProviderError('115 创建目录结果未确认')
        return identifier(data.get('file_id',data.get('cid',data.get('category_id'))))

    def file_id_for_pickcode(self,pickcode):
        if not re.fullmatch(r'[A-Za-z0-9]{6,64}',pickcode):
            raise ProviderError('STRM 中的提取码格式不受支持',409)
        result=self.request('download',{'pick_code':pickcode})
        data=result.get('data')
        if not isinstance(data,dict) or len(data)!=1:
            raise ProviderError('115 未返回唯一文件身份，未接管 STRM',409)
        fid=identifier(next(iter(data)))
        actual=self.stat(fid)
        if not actual or actual['pickcode']!=pickcode or actual['is_dir']:
            raise ProviderError('STRM 提取码与 115 文件身份不一致',409)
        return actual

    def download(self,pickcode,user_agent='',file_id=None):
        if not re.fullmatch(r'[A-Za-z0-9]{6,64}',pickcode):
            raise ProviderError('115 文件没有可用提取码',409)
        # Obtain the URL with the player's exact UA. A 302 cannot supply a
        # response Cookie to the player, so Cookie-bound links remain blocked.
        result=self.request('download',{'pick_code':pickcode},user_agent=user_agent)
        data=result.get('data')
        if not isinstance(data,dict) or len(data)!=1:
            raise ProviderError('115 直链响应暂不兼容')
        if file_id is not None and identifier(next(iter(data)))!=identifier(file_id):
            raise ProviderError('115 直链返回的文件 ID 与绑定不一致',409)
        info=next(iter(data.values()))
        if isinstance(info,dict) and info.get('pick_code') and info['pick_code']!=pickcode:
            raise ProviderError('115 直链提取码与绑定不一致',409)
        url=(info.get('url') or {}).get('url') if isinstance(info,dict) else None
        try:
            parts=urllib.parse.urlsplit(url or '')
        except ValueError:
            raise ProviderError('115 返回的播放地址无效') from None
        if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password:
            raise ProviderError('115 没有返回可用播放地址')
        flags=urllib.parse.parse_qs(parts.query).get('f',[''])[0]
        if flags not in ('','0') and not (flags=='1' and user_agent):
            raise ProviderError('该直链需要额外请求头，当前直连模式不可用',409)
        return url
