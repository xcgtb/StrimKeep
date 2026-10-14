"""User-owned 115 application PKCE device authorization; no borrowed AppID.

Only a user-confirmed QR exchange can obtain tokens. Exchange intent and
returned tokens are persisted before the next step; uncertain exchanges are
never repeated. Sessions are bounded, expire, and never project secrets.
"""
import base64
import hashlib
import io
import secrets
import time
import urllib.parse
from . import cloud115_store as repo, state_store, storage
from .providers.p115_open import ProviderError, auth_lock, identifier, token, transport, user_id


def _path():
    return storage._state_dir()/'cloud115_device_sessions.json'


def _sessions():
    rows=state_store.read(_path(),{})
    if not isinstance(rows,dict):
        raise ProviderError('115 授权记录不可用',503)
    return rows


def start(body):
    app_id=identifier(body.get('app_id'))
    name=body.get('name','115')
    if not isinstance(name,str) or not 1<=len(name.strip())<=80:
        raise ProviderError('请输入账号名称',400)
    verifier=secrets.token_urlsafe(64)
    challenge=base64.b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    with auth_lock():
        now=time.time()
        rows={k:v for k,v in _sessions().items() if v.get('expires',0)>now}
        if len(rows)>=10:
            raise ProviderError('授权会话较多，请等待旧二维码过期',429)
        result=transport('device_start',{'client_id':app_id,'code_challenge':challenge,'code_challenge_method':'sha256'})
        data=result.get('data')
        if not isinstance(data,dict) or not all(data.get(k) is not None for k in ('uid','time','sign','qrcode')):
            raise ProviderError('115 授权二维码响应暂不兼容')
        if not all(isinstance(data[k],(str,int)) and not isinstance(data[k],bool) and len(str(data[k]))<4096 for k in ('uid','time','sign','qrcode')):
            raise ProviderError('115 授权二维码字段不受支持')
        try:
            parts=urllib.parse.urlsplit(str(data['qrcode']))
        except ValueError:
            raise ProviderError('115 返回的授权扫码地址无效') from None
        if parts.scheme not in ('http','https') or parts.hostname!='115.com' or parts.username or parts.password:
            raise ProviderError('115 返回的授权扫码地址不受支持')
        key=secrets.token_urlsafe(24)
        rows[key]={'verifier':verifier,'uid':str(data['uid']),'time':data['time'],'sign':str(data['sign']),
                   'qrcode':str(data['qrcode']),'name':name.strip(),'app_id':app_id,'state':'waiting',
                   'expires':now+300,'last_poll':0}
        state_store.save(_path(),rows)
        return {'session_id':key,'expires_at':now+300,'state':'waiting','poll_interval':3}


def _session(rows,key):
    row=rows.get(key)
    if not row or row.get('expires',0)<time.time():
        raise ProviderError('115 授权会话已过期，请重新生成二维码',410)
    return row


def qr_svg(key):
    import qrcode
    from qrcode.image.svg import SvgPathImage
    with auth_lock():
        row=_session(_sessions(),key)
        if row['state'] not in ('waiting','scanned'):
            raise ProviderError('该二维码已结束使用',410)
        output=io.BytesIO()
        qrcode.make(row['qrcode'],image_factory=SvgPathImage,box_size=8,border=4).save(output)
        return output.getvalue()


def poll(key):
    with auth_lock():
        rows=_sessions();row=_session(rows,key)
        state=row['state']
        if state=='done':
            return {'state':'done','account':row['account']}
        if state in ('unknown','exchanging'):
            return {'state':'unknown','message':'令牌换取结果未确认，不会重复换取；请重新扫码授权'}
        if state in ('cancelled','expired'):
            return {'state':state}
        if state!='verifying':
            if time.time()-row['last_poll']<3:
                return {'state':state}
            row['last_poll']=time.time()
            state_store.save(_path(),rows)
            result=transport('device_status',{'uid':row['uid'],'time':row['time'],'sign':row['sign']})
            data=result.get('data')
            code=data.get('status') if isinstance(data,dict) else None
            if type(code) is not int or code not in (0,1,2,-1,-2):
                raise ProviderError('115 扫码状态暂不兼容')
            if code!=2:
                row['state']={0:'waiting',1:'scanned',-1:'expired',-2:'cancelled'}[code]
                state_store.save(_path(),rows)
                return {'state':row['state']}
            row['state']='exchanging'
            state_store.save(_path(),rows)
            try:
                result=transport('device_exchange',{'uid':row['uid'],'code_verifier':row['verifier']})
                data=result.get('data') or {}
                row['access_token']=token(data.get('access_token'))
                row['refresh_token']=token(data.get('refresh_token'))
                row['state']='verifying'
                state_store.save(_path(),rows)
            except (ProviderError,OSError,ValueError):
                row['state']='unknown'
                state_store.save(_path(),rows)
                raise ProviderError('115 令牌换取结果未确认，不会重复换取',409) from None
        uid=user_id(transport('user',access_token=row['access_token']))
        account_id=repo.save_account(uid,row['name'],row['access_token'],row['refresh_token'],0)
        public={'id':account_id,'uid':uid,'name':row['name']}
        rows[key]={'state':'done','expires':row['expires'],'account':public}
        state_store.save(_path(),rows)
        return {'state':'done','account':public}
