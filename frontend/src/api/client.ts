export class ApiError extends Error {
  constructor(
    message: string,
    public status = 0,
    public uncertain = false,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
let unauthorized = () => {};
let accepted: (value: {
  id: string;
  kind: 'cloud' | 'task' | 'delete';
  path: string;
}) => void = () => {};
export function onAccepted(handler: typeof accepted) {
  accepted = handler;
}
export function onUnauthorized(handler: () => void) {
  unauthorized = handler;
}
type RequestOptions = Omit<RequestInit, 'body'> & {
  body?: unknown;
  timeoutMs?: number;
  allowStatus?: string[];
};
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { body, timeoutMs = 20000, signal, allowStatus = [], ...init } = options;
  const controller = new AbortController();
  const relay = () => controller.abort(signal?.reason);
  signal?.addEventListener('abort', relay, { once: true });
  if (signal?.aborted) relay();
  const timer = setTimeout(
    () => controller.abort(new DOMException('请求超时', 'TimeoutError')),
    timeoutMs,
  );
  const mutation = !['GET', 'HEAD'].includes((init.method || 'GET').toUpperCase());
  try {
    const response = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        ...init.headers,
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const raw = await response.text();
    let data: Record<string, unknown>;
    try {
      data = raw ? JSON.parse(raw) : {};
    } catch {
      throw new ApiError('服务返回了无法识别的响应', response.status, mutation);
    }
    if (response.status === 401) unauthorized();
    if (!response.ok) {
      const detail =
        typeof data.detail === 'string'
          ? data.detail
          : typeof data.message === 'string'
            ? data.message
            : `请求失败（${response.status}）`;
      throw new ApiError(
        response.status === 409 ? `${detail}，请刷新后核对再保存` : detail,
        response.status,
        mutation && response.status >= 500,
      );
    }
    if (
      ['error', 'busy', 'failed'].includes(String(data.status)) &&
      !allowStatus.includes(String(data.status))
    ) {
      throw new ApiError(
        String(data.message || data.error || data.detail || '操作未完成'),
        response.status,
      );
    }
    if (mutation && !path.includes('/preview') && !path.includes('/qr')) {
      if (typeof data.task_id === 'string') accepted({ id: data.task_id, kind: 'task', path });
      else if (typeof data.job_id === 'string') accepted({ id: data.job_id, kind: 'cloud', path });
      else if (typeof data.operation_id === 'string')
        accepted({ id: data.operation_id, kind: 'delete', path });
    }
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (signal?.aborted) throw new DOMException('请求已取消', 'AbortError');
    throw new ApiError(
      mutation
        ? '请求中断，结果待核实。请查看后台任务或刷新数据后核对，避免重复提交。'
        : '连接中断或请求超时，请重试。',
      0,
      mutation,
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', relay);
  }
}
export function query(path: string, values: Record<string, string | number | undefined>) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== '') params.set(key, String(value));
  });
  return `${path}?${params}`;
}
export const get = <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal });
export const post = <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body });
