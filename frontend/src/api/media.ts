import { get, post, query } from './client';
import type {
  ExploreResponse,
  LibraryItem,
  LibraryPage,
  LibraryType,
  MediaCard,
  Overview,
  Runtime,
  SubscriptionsResponse,
  Subscription,
} from '../types/api';
export const mediaApi = {
  runtime: (signal?: AbortSignal) => get<Runtime>('/api/runtime/status', signal),
  overview: (signal?: AbortSignal) => get<Overview>('/api/overview', signal),
  refreshOverview: () => post<{ refresh: Overview['refresh'] }>('/api/overview/refresh'),
  recommendations: (media: string, signal?: AbortSignal) =>
    get<ExploreResponse>(query('/api/overview/recommendations', { media }), signal),
  explore: (filters: Record<string, string | number>, signal?: AbortSignal) =>
    get<ExploreResponse>(query('/api/explore', filters), signal),
  badges: (cards: MediaCard[], signal?: AbortSignal) => postBadges(cards, signal),
  library: (filters: Record<string, string | number>, signal?: AbortSignal) =>
    get<LibraryPage>(query('/api/emby/library/page', filters), signal),
  detail: (media: LibraryType, id: string, signal?: AbortSignal) =>
    get<{ status: string; item?: LibraryItem }>(
      query('/api/emby/library/item', { media, id }),
      signal,
    ),
  subscriptions: (signal?: AbortSignal) => get<SubscriptionsResponse>('/api/subscriptions', signal),
  saveSubscriptions: (subscriptions: Subscription[]) =>
    post('/api/subscriptions', { subscriptions }),
};
import { request } from './client';
function postBadges(cards: MediaCard[], signal?: AbortSignal) {
  return request<ExploreResponse>('/api/explore/library-status', {
    method: 'POST',
    signal,
    body: { cards: cards.slice(0, 200).map((c) => ({ type: c.type, tmdb_id: String(c.tmdb_id) })) },
  });
}
