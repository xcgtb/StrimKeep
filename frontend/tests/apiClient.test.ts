import {test} from 'node:test'
import assert from 'node:assert/strict'
import {ApiError, createApi} from '../src/api/client.ts'

const runtime = globalThis as any
runtime.window = runtime

function reply(status: number, body: any, ok = status >= 200 && status < 300) {
  return {ok, status, json: async () => body} as any
}

test('GET requests with the same path share one in-flight request', async () => {
  let calls = 0
  let release: (value: any) => void = () => {}
  const api = createApi({
    getCsrf: () => '',
    fetchImpl: (() => {
      calls += 1
      return new Promise((resolve) => { release = resolve })
    }) as any,
  })
  const first = api('/tasks')
  const second = api('/tasks')
  assert.equal(calls, 1)
  release(reply(200, {items: [1]}))
  assert.deepEqual(await first, {items: [1]})
  assert.deepEqual(await second, {items: [1]})
})

test('POST sends CSRF token and JSON body', async () => {
  let seen: any = null
  const api = createApi({
    getCsrf: () => 'csrf-token',
    fetchImpl: (async (_url: string, init: any) => {
      seen = init
      return reply(200, {ok: true})
    }) as any,
  })
  await api('/settings', 'POST', {theme: 'dark'})
  assert.equal(seen.method, 'POST')
  assert.equal(seen.headers['X-CSRF-Token'], 'csrf-token')
  assert.equal(seen.body, '{"theme":"dark"}')
  assert.equal(seen.credentials, 'same-origin')
})

test('401 triggers onUnauthorized and keeps the server error code', async () => {
  let unauthorized = 0
  const api = createApi({
    getCsrf: () => '',
    onUnauthorized: () => { unauthorized += 1 },
    fetchImpl: (async () => reply(401, {error: {message: '登录已失效', code: 'AUTH'} }, false)) as any,
  })
  await assert.rejects(
    () => api('/tasks'),
    (error: any) => error instanceof ApiError && error.status === 401 && error.code === 'AUTH',
  )
  assert.equal(unauthorized, 1)
})

test('timeout aborts the request and reports TIMEOUT', async () => {
  const api = createApi({
    getCsrf: () => '',
    fetchImpl: ((_url: string, init: any) => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => {
        const error = new Error('aborted')
        error.name = 'AbortError'
        reject(error)
      })
    })) as any,
  })
  await assert.rejects(
    () => api('/slow', 'GET', undefined, {timeoutMs: 10}),
    (error: any) => error instanceof ApiError && error.code === 'TIMEOUT',
  )
})

test('non-JSON responses become INVALID_RESPONSE', async () => {
  const api = createApi({
    getCsrf: () => '',
    fetchImpl: (async () => ({ok: true, status: 200, json: async () => { throw new Error('bad') }})) as any,
  })
  await assert.rejects(
    () => api('/broken'),
    (error: any) => error instanceof ApiError && error.code === 'INVALID_RESPONSE',
  )
})
