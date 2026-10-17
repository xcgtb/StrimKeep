import { post, request, query } from './client';
export type Data = Record<string, unknown>;
export const object = (value: unknown): Data =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Data) : {};
export const records = (value: unknown): Data[] => (Array.isArray(value) ? value.map(object) : []);
export const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.map(String) : [];
export const text = (value: unknown): string =>
  value === null || value === undefined ? '—' : String(value);
export const readData = (path: string, signal?: AbortSignal, allowStatus: string[] = []) =>
  request<Data>(path, { signal, allowStatus });
export const writeData = (path: string, body?: unknown) => post<Data>(path, body);
export const removeData = (path: string) => request<Data>(path, { method: 'DELETE' });
export const accountPath = (id: string, domain: string) =>
  `/api/cloud/accounts/${encodeURIComponent(id)}/${domain}`;
// crypto.randomUUID 只在安全上下文（HTTPS / localhost）存在；
// 局域网用 http://<NAS-IP>:8321 访问时为 undefined，直接调用会抛异常。
// 删除接口要求 32 位十六进制操作号，这里回退到 getRandomValues 生成同格式。
export const uuid = () => {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === 'function')
    return cryptoApi.randomUUID().replaceAll('-', '');
  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === 'function') cryptoApi.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
};
export { query };
