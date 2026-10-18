"""Authenticated, bounded upstream business operations; no generic API proxy."""
from fastapi import APIRouter, Depends, HTTPException, Response
from app import upstream
from app.routers.deps import auth

router = APIRouter(prefix='/api/upstream', dependencies=[Depends(auth)])


def call(fn, *args):
    try:
        return fn(*args)
    except upstream.UpstreamError as error:
        raise HTTPException(error.code, str(error)) from None
    except OSError:
        raise HTTPException(503, '上游接入状态保存失败，请检查数据目录') from None


@router.get('/config')
def get_config(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'status': 'success', 'settings': call(lambda: upstream.public_settings(upstream.settings()))}


@router.post('/config')
def save_config(body: dict):
    return {'status': 'success', 'settings': call(upstream.save_settings, body)}


@router.post('/test')
def test_connection(body: dict):
    cfg = call(upstream.settings, body)
    return {'status': 'success', 'capabilities': call(upstream.capabilities, cfg, True)}


@router.post('/search')
def search(body: dict):
    return {'status': 'success', **call(upstream.search, body)}


@router.get('/providers')
def providers():
    return {'status': 'success', **call(upstream.providers)}


@router.get('/directories')
def directories(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'status': 'success', **call(upstream.transfer_directories)}


@router.get('/resource-subscriptions')
def resource_subscriptions(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    from app import resource_watch
    return {'status': 'success', 'items': call(upstream.resource_subscriptions), 'notifications': resource_watch.status()}


@router.get('/subscription-events')
def subscription_events(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'status': 'success', 'items': call(upstream.subscription_events)}


@router.post('/operations/{action}', status_code=202)
def business_operation(action: str, body: dict):
    return {'status': 'success', 'job': call(upstream.business_operation, action, body)}


@router.get('/jobs/{operation}/link')
def operation_link(operation: str, response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'status': 'success', 'link': call(upstream.operation_link, operation)}


@router.post('/transfer', status_code=202)
def transfer(body: dict):
    return {'status': 'success', 'job': call(upstream.transfer, body)}


@router.get('/jobs')
def jobs(response: Response):
    response.headers['Cache-Control'] = 'no-store'
    return {'status': 'success', 'items': call(upstream.get_jobs)}


@router.post('/jobs/{operation}/acknowledge')
def acknowledge(operation: str, body: dict):
    call(upstream.acknowledge, operation, body.get('confirmed'))
    return {'status': 'success'}


@router.get('/history')
def history(keyword: str, page: int = 1):
    return {'status': 'success', 'items': call(upstream.history, keyword, page)}


@router.get('/history/{record_id}')
def detail(record_id: str):
    return {'status': 'success', 'fields': call(upstream.detail, record_id)}
