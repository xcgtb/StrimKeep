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
