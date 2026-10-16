import { defineStore } from 'pinia';
import { ref } from 'vue';
import { mediaApi } from '../api/media';
import { ApiError } from '../api/client';
import type { MediaCard, Subscription, SubscriptionsResponse } from '../types/api';
export const useSubscriptions = defineStore('subscriptions', () => {
  const items = ref<Subscription[]>([]),
    busy = ref(false),
    uncertain = ref(false),
    settings = ref<SubscriptionsResponse>();
  async function load(signal?: AbortSignal) {
    const value = await mediaApi.subscriptions(signal);
    items.value = value.subscriptions;
    settings.value = value;
    uncertain.value = false;
    return value;
  }
  const has = (id?: string) => items.value.some((s) => String(s.tmdb_id) === String(id));
  async function mutate(update: (items: Subscription[]) => Subscription[]) {
    if (busy.value || uncertain.value) return;
    busy.value = true;
    try {
      const latest = await mediaApi.subscriptions();
      await mediaApi.saveSubscriptions(update(latest.subscriptions));
      await load();
    } catch (e) {
      uncertain.value = e instanceof ApiError && e.uncertain;
      throw e;
    } finally {
      busy.value = false;
    }
  }
  async function toggle(card: MediaCard) {
    if (card.type !== 'tv' || !card.tmdb_id) return;
    await mutate((rows) =>
      rows.some((s) => String(s.tmdb_id) === String(card.tmdb_id))
        ? rows.filter((s) => String(s.tmdb_id) !== String(card.tmdb_id))
        : [
            ...rows,
            {
              id: String(card.tmdb_id),
              tmdb_id: String(card.tmdb_id),
              name: card.title || card.name || '',
              poster: card.poster,
              enabled: true,
            },
          ],
    );
  }
  return { items, busy, settings, uncertain, has, load, mutate, toggle };
});
