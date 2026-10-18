import { onMounted, onBeforeUnmount, ref } from 'vue';

// One authenticated SSE connection per tab, shared by task cards and workspaces.
const connected = ref(false);
const listeners = new Set<() => void>();
let feed: EventSource | undefined;
let timer: ReturnType<typeof setInterval> | undefined;
let retryAt = 0;
function notify() {
  for (const listener of listeners) listener();
}
function disconnect() {
  feed?.close();
  feed = undefined;
  connected.value = false;
}
function connect() {
  if (feed || !listeners.size || document.hidden || Date.now() < retryAt) return;
  feed = new EventSource('/api/activity/events');
  feed.onopen = () => {
    connected.value = true;
  };
  feed.addEventListener('activity', notify);
  const failed = () => {
    disconnect();
    retryAt = Date.now() + 30000;
  };
  feed.onerror = failed;
  feed.addEventListener('degraded', failed);
}
function visibility() {
  if (document.hidden) disconnect();
  else {
    retryAt = 0;
    connect();
    notify();
  }
}
export function useActivityUpdates(refresh: () => unknown | Promise<unknown>) {
  let busy = false,
    pending = false,
    stopped = false;
  async function update() {
    if (stopped || document.hidden) return;
    if (busy) {
      pending = true;
      return;
    }
    busy = true;
    try {
      do {
        pending = false;
        await refresh();
      } while (pending && !stopped);
    } catch {
      /* Request owners display their own errors. */
    } finally {
      busy = false;
    }
  }
  onMounted(() => {
    listeners.add(update);
    if (listeners.size === 1) {
      document.addEventListener('visibilitychange', visibility);
      timer = setInterval(() => {
        if (!document.hidden) {
          connect();
          notify();
        }
      }, 15000);
    }
    connect();
  });
  onBeforeUnmount(() => {
    stopped = true;
    listeners.delete(update);
    if (!listeners.size) {
      disconnect();
      clearInterval(timer);
      document.removeEventListener('visibilitychange', visibility);
    }
  });
  return { connected };
}
