/** 统一 API 客户端：路径前缀、CSRF、JSON 错误、401 回跳都只在这里处理。 */
export type ApiMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
export type Api = <T = any>(path: string, method?: ApiMethod, body?: unknown,
                            options?: {timeoutMs?: number; signal?: AbortSignal}) => Promise<T>

export class ApiError extends Error {
  status: number
  code: string

  constructor(message: string, status = 0, code = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

export interface ApiClientOptions {
  getCsrf: () => string
  onUnauthorized?: () => void
  baseUrl?: string
  fetchImpl?: typeof fetch
}

/** 创建应用级 API 函数；返回的签名与旧 App.vue 里的 api(path, method, body) 兼容。 */
export function createApi(options: ApiClientOptions): Api {
  const baseUrl = options.baseUrl ?? '/api/v1'
  const request = options.fetchImpl ?? fetch
  const pendingReads = new Map<string, Promise<any>>()

  const api: Api = async <T = any>(path: string, method: ApiMethod = 'GET', body?: unknown,
                                   requestOptions: {timeoutMs?: number; signal?: AbortSignal} = {}) => {
    if (method === 'GET' && pendingReads.has(path)) return pendingReads.get(path) as Promise<T>

    const controller = requestOptions.signal ? null : new AbortController()
    const timeoutMs = requestOptions.timeoutMs
      ?? (method === 'GET' && /^\/organize\/jobs\/\d+$/.test(path) ? 15000 : 0)
    const timer = controller && timeoutMs
      ? window.setTimeout(() => controller.abort(), timeoutMs)
      : null
    const run = (async () => {
      try {
        const response = await request(baseUrl + path, {
          method,
          credentials: 'same-origin',
          signal: requestOptions.signal ?? controller?.signal,
          headers: {
            'Content-Type': 'application/json',
            ...(options.getCsrf() ? {'X-CSRF-Token': options.getCsrf()} : {}),
          },
          body: body === undefined ? undefined : JSON.stringify(body),
        })
        let data: any
        try {
          data = await response.json()
        } catch {
          throw new ApiError('服务响应暂不可用，请稍后再试。', response.status, 'INVALID_RESPONSE')
        }
        if (!response.ok) {
          if (response.status === 401) options.onUnauthorized?.()
          throw new ApiError(data?.error?.message || '操作未完成', response.status,
                             String(data?.error?.code || ''))
        }
        return data as T
      } catch (error) {
        if (error instanceof ApiError) throw error
        if ((error as Error)?.name === 'AbortError') throw new ApiError('请求超时，请稍后重试。', 0, 'TIMEOUT')
        throw error
      } finally {
        if (timer !== null) window.clearTimeout(timer)
      }
    })()

    if (method === 'GET') pendingReads.set(path, run)
    try {
      return await run
    } finally {
      if (pendingReads.get(path) === run) pendingReads.delete(path)
    }
  }

  return api
}
