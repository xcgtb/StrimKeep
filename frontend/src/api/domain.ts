export type Data = Record<string, unknown>;

export function object(value: unknown): Data {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Data) : {};
}
export function records(value: unknown): Data[] {
  return Array.isArray(value) ? (value as Data[]) : [];
}
export function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : [];
}
export function text(value: unknown): string {
  if (value === null || value === undefined) return '';
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}
export function number(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}
export function compact(value: unknown): string {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return '—';
  return parsed.toLocaleString('zh-CN');
}
export function date(ts?: number | string): string {
  const seconds = Number(ts);
  if (!seconds) return '—';
  const ms = seconds > 1e12 ? seconds : seconds * 1000;
  return new Date(ms).toLocaleString('zh-CN', { hour12: false });
}
export function query(path: string, params: Record<string, unknown> = {}) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params))
    if (value !== '' && value !== undefined && value !== null) search.set(key, String(value));
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}
// 局域网 http 访问没有 secure context，crypto.randomUUID 不存在；回退到 getRandomValues
export function uuid(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID().replaceAll('-', '');
  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === 'function') cryptoApi.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}
