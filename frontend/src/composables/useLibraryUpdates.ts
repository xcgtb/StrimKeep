import { onMounted, onBeforeUnmount, ref } from 'vue';
import { readData, type Data } from '../api/domain';
import { useVisiblePoll } from './useVisiblePoll';

// SSE carries revision hints only. Read the authenticated change journal before
// refreshing visible facts; neither endpoint starts an Emby/TMDB scan.
export function useLibraryUpdates(refresh: (change: Data) => Promise<boolean | void>) {
  const connected = ref(false),
    error = ref('');
  let version: number | undefined, feed: EventSource | undefined;
  let busy = false,
    stopped = false,
    retryAt = 0;
  const controller = new AbortController();
  function disconnect() {
    feed?.close();
    feed = undefined;
    connected.value = false;
  }
  function connect() {
    if (feed || stopped || document.hidden || Date.now() < retryAt) return;
    feed = new EventSource('/api/library/events');
    feed.onopen = () => {
      connected.value = true;
    };
    feed.addEventListener('library', () => {
      void poll();
    });
    feed.onerror = () => {
      disconnect();
      retryAt = Date.now() + 60000;
    };
  }
  async function poll() {
    if (stopped || busy || document.hidden) return;
    connect();
    busy = true;
    try {
      const change = await readData(
        `/api/library/changes?since=${version ?? 0}`,
        controller.signal,
      );
      if (stopped) return;
      const next = change.version;
      if (typeof next !== 'number' || !Number.isSafeInteger(next) || next < 0) return;
      if (next !== version && !(version === undefined && next === 0)) {
        if ((await refresh(change)) === false) return;
      }
      if (!stopped) {
        version = next;
        error.value = '';
      }
    } catch (e) {
      if (!stopped) error.value = e instanceof Error ? e.message : '片库变化读取失败';
    } finally {
      busy = false;
    }
  }
  function visibility() {
    if (document.hidden) disconnect();
    else {
      retryAt = 0;
      void poll();
    }
  }
  onMounted(() => {
    document.addEventListener('visibilitychange', visibility);
    void poll();
  });
  onBeforeUnmount(() => {
    stopped = true;
    disconnect();
    controller.abort();
    document.removeEventListener('visibilitychange', visibility);
  });
  useVisiblePoll(poll, 15000);
  return { connected, error, poll };
}
