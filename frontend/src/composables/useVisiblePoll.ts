import { onMounted, onBeforeUnmount } from 'vue';
export function useVisiblePoll(callback: () => void | Promise<unknown>, interval = 15000) {
  let timer: ReturnType<typeof setTimeout> | undefined,
    stopped = false;
  async function tick() {
    if (stopped || document.hidden) return;
    try {
      await callback();
    } finally {
      if (!stopped && !document.hidden) timer = setTimeout(tick, interval);
    }
  }
  function changed() {
    clearTimeout(timer);
    if (!document.hidden) void tick();
  }
  onMounted(() => {
    timer = setTimeout(tick, interval);
    document.addEventListener('visibilitychange', changed);
  });
  onBeforeUnmount(() => {
    stopped = true;
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', changed);
  });
}
