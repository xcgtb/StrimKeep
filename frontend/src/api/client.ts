export class ApiError extends Error {
  constructor(
    message: string,
    public status = 0,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

type Options = Omit<RequestInit, 'body'> & { body?: unknown; timeoutMs?: number };

export async function request<T>(path: string, options: Options = {}): Promise<T> {
  const { body, timeoutMs = 30000, ...init } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {
      credentials: 'same-origin',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      ...init,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = payload as { detail?: string; message?: string };
      throw new ApiError(
        detail.detail || detail.message || `请求失败（${response.status}）`,
        response.status,
      );
    }
    return payload as T;
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError')
      throw new ApiError('请求超时，请稍后重试');
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export const get = <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal });
export const post = <T>(path: string, body?: unknown, signal?: AbortSignal) =>
  request<T>(path, { method: 'POST', body, signal });
export const del = <T>(path: string, signal?: AbortSignal) =>
  request<T>(path, { method: 'DELETE', signal });
export { query } from './domain';
