import type { MediaCard } from '../types/api';
export function posterUrl(card: MediaCard): string {
  const raw = card.poster || card.poster_tmdb || card.tmdb_info?.poster || '';
  if (/^\/api\/(tmdb|emby)\/poster\//.test(raw)) return raw;
  if (/^https:\/\/image\.tmdb\.org\/t\/p\//.test(raw))
    return '/api/tmdb/poster/' + raw.split('/').pop();
  if (/^\/[a-zA-Z0-9_.-]+\.(jpg|png|webp)$/i.test(raw)) return '/api/tmdb/poster' + raw;
  return card.has_image && card.id ? '/api/emby/poster/' + encodeURIComponent(card.id) : '';
}
export const titleOf = (card: MediaCard) => card.title || card.name || '未命名作品';
export function number(value: unknown) {
  if (value === undefined || value === null || value === '') return '—';
  const n = Number(String(value).replaceAll(',', ''));
  return Number.isFinite(n) ? n.toLocaleString('zh-CN') : '—';
}
export function date(value?: number | string) {
  if (!value) return '尚无记录';
  const d = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString('zh-CN', { hour12: false });
}
export function badge(card: MediaCard) {
  const info = card.tmdb_info;
  if (info?.match_status === 'missing') return `缺 ${Math.abs(info.diff || 0)} 集`;
  const labels: Record<string, string> = {
    aligned: '已对齐',
    ongoing: '连载中',
    extra: '存在多集',
    unmatched: '待对照',
    no_tmdb: '未匹配',
    pending: '对照中',
  };
  if (info?.match_status) return labels[info.match_status] || '待核实';
  if (card.library_status && card.library_status !== 'available') return '片库待同步';
  if (card.in_emby === true) return '已入库';
  if (card.in_local || card.in_share) return '已有 STRM';
  return '';
}
export const stateLabel = (value: string) =>
  ({
    queued: '排队中',
    running: '执行中',
    success: '已完成',
    done: '已结束，请核对结果',
    error: '失败',
    partial: '部分完成',
    submitted: '已提交，等待核实',
    confirmed: '已核实',
    failed: '失败',
    uncertain: '结果待核实',
    needs_review: '需人工核实',
    cancelled: '已取消',
    stopped: '已停止',
  })[value] ||
  value ||
  '未知状态';
