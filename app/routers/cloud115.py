"""Authenticated management APIs and separately scoped STRM playback."""
import sqlite3
from fastapi import APIRouter, Depends, HTTPException, Query, Response, Request
from fastapi.responses import RedirectResponse
from app import cloud115, cloud115_auth, cloud115_organize, cloud115_transfer
from app.providers.p115_open import Driver, ProviderError
from app.routers.deps import auth, spawn

router=APIRouter(prefix='/api/cloud115',dependencies=[Depends(auth)])
play_router=APIRouter()


def call(fn,*args,**kwargs):
    try:
        return fn(*args,**kwargs)
    except ProviderError as error:
        raise HTTPException(error.code,str(error)) from None
    except (OSError,sqlite3.Error):
        raise HTTPException(503,'115 状态存储或文件系统不可用') from None


@router.get('/status')
def status(response:Response):
    response.headers['Cache-Control']='no-store'
    return {'status':'success',**call(cloud115.public_status)}


@router.post('/accounts')
def account(body:dict,response:Response):
    response.headers['Cache-Control']='no-store'
    return {'status':'success','account':call(cloud115.save_account,body)}


@router.post('/policy')
def policy(body:dict):
    return {'status':'success',**call(cloud115.save_policy,body)}


@router.post('/authorization/start')
def start_authorization(body:dict,response:Response):
    response.headers['Cache-Control']='no-store'
    return {'status':'success',**call(cloud115_auth.start,body)}


@router.post('/authorization/{session_id}')
def authorization(session_id:str,response:Response):
    response.headers['Cache-Control']='no-store'
    return {'status':'success',**call(cloud115_auth.poll,session_id)}


@router.get('/authorization/{session_id}/qr.svg')
def authorization_qr(session_id:str):
    return Response(call(cloud115_auth.qr_svg,session_id),media_type='image/svg+xml',
                    headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})


@router.get('/accounts/{account_id}/files')
def files(account_id:str,response:Response,parent_id:str='0',offset:int=Query(0,ge=0,le=10000000),limit:int=Query(50,ge=1,le=100)):
    response.headers['Cache-Control']='no-store'
    driver=Driver(account_id)
    call(driver.validate_account)
    return {'status':'success',**call(driver.files,parent_id,offset,limit)}


@router.get('/accounts/{account_id}/files/{file_id}')
def file_detail(account_id:str,file_id:str,response:Response):
    response.headers['Cache-Control']='no-store'
    driver=Driver(account_id);call(driver.validate_account);item=call(driver.stat,file_id)
    if item is None:raise HTTPException(404,'115 文件已不存在')
    return {'status':'success','file':item}


@router.post('/generate',status_code=202)
def generate(body:dict):
    task=spawn('cloud115_generate',cloud115.generate,body)
    return {'status':'success','task_id':task.id}


@router.post('/adopt',status_code=202)
def adopt(body:dict):
    task=spawn('cloud115_adopt',cloud115.adopt,body)
    return {'status':'success','task_id':task.id}


@router.post('/bindings/{binding_id}/verify',status_code=202)
def verify_binding(binding_id:str):
    task=spawn('cloud115_verify',cloud115.reconcile,binding_id)
    return {'status':'success','task_id':task.id}


@router.post('/organize/preview',status_code=202)
def organize_preview(body:dict):
    task=spawn('cloud115_organize_preview',cloud115_organize.preview,body)
    return {'status':'success','task_id':task.id}


@router.get('/organize/{plan_id}')
def organize_detail(plan_id:str,response:Response):
    response.headers['Cache-Control']='no-store'
    return {'status':'success',**call(cloud115_organize.detail,plan_id)}


@router.post('/organize/{plan_id}/execute',status_code=202)
def organize_execute(plan_id:str,body:dict):
    if body.get('confirm') is not True:raise HTTPException(400,'请先查看整理预览并明确确认')
    task=spawn('cloud115_organize',cloud115_organize.execute,plan_id)
    return {'status':'success','task_id':task.id}


@router.post('/organize/{plan_id}/verify',status_code=202)
def organize_verify(plan_id:str):
    task=spawn('cloud115_organize_verify',cloud115_organize.reconcile,plan_id)
    return {'status':'success','task_id':task.id}


@router.post('/organize/{plan_id}/cancel')
def organize_cancel(plan_id:str):
    return {'status':'success',**call(cloud115_organize.cancel,plan_id)}


@router.post('/transfer/cookie')
def transfer_cookie(body:dict,response:Response):
    response.headers['Cache-Control']='no-store'
    return {'status':'success',**call(cloud115_transfer.save_cookie,body)}


@router.post('/transfer/inspect',status_code=202)
def transfer_inspect(body:dict):
    task=spawn('cloud115_share_inspect',cloud115_transfer.inspect,body)
    return {'status':'success','task_id':task.id}


@router.post('/transfer/preview',status_code=202)
def transfer_preview(body:dict):
    task=spawn('cloud115_transfer_preview',cloud115_transfer.preview,body)
    return {'status':'success','task_id':task.id}


@router.get('/transfer/{plan_id}')
def transfer_detail(plan_id:str,response:Response):
    response.headers['Cache-Control']='no-store'
    return {'status':'success',**call(cloud115_transfer.detail,plan_id)}


@router.post('/transfer/{plan_id}/execute',status_code=202)
def transfer_execute(plan_id:str,body:dict):
    if body.get('confirm') is not True:raise HTTPException(400,'请先核对转存预览并明确确认')
    task=spawn('cloud115_transfer',cloud115_transfer.execute,plan_id)
    return {'status':'success','task_id':task.id}


@router.post('/transfer/{plan_id}/verify',status_code=202)
def transfer_verify(plan_id:str):
    task=spawn('cloud115_transfer_verify',cloud115_transfer.reconcile,plan_id)
    return {'status':'success','task_id':task.id}


@router.post('/transfer/{plan_id}/cancel')
def transfer_cancel(plan_id:str):
    return {'status':'success',**call(cloud115_transfer.cancel,plan_id)}


@play_router.api_route('/play/115/{binding_id}',methods=['GET','HEAD'])
def play(binding_id:str,request:Request,token:str=Query('',max_length=200)):
    url=call(cloud115.playback,binding_id,token,request.headers.get('user-agent',''))
    return RedirectResponse(url,status_code=302,headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
