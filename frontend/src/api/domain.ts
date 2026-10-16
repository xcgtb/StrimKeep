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
export const uuid = () => crypto.randomUUID().replaceAll('-', '');
export { query };
