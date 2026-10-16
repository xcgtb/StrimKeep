import { ref, shallowRef, watch, onBeforeUnmount } from 'vue';
import { ApiError } from '../api/client';
import type { Data } from '../api/domain';
export function useOperation(context?: () => unknown) {
  const busy = ref(false),
    error = ref(''),
    uncertain = ref(false),
    receipt = shallowRef<Data>();
  let generation = 0;
  function invalidate() {
    generation++;
    receipt.value = undefined;
    if (!uncertain.value) error.value = '';
  }
  if (context) watch(context, invalidate, { flush: 'sync' });
  onBeforeUnmount(invalidate);
  async function run(action: () => Promise<Data>) {
    if (busy.value || uncertain.value) return;
    const current = generation;
    busy.value = true;
    error.value = '';
    receipt.value = undefined;
    try {
      const result = await action();
      if (current === generation) {
        receipt.value = result;
        return result;
      }
    } catch (e) {
      if (current === generation) {
        error.value = e instanceof Error ? e.message : '操作失败';
        uncertain.value = e instanceof ApiError && e.uncertain;
      }
    } finally {
      busy.value = false;
    }
  }
  function reconcile(verified = false) {
    if (!busy.value && (!uncertain.value || verified)) {
      uncertain.value = false;
      error.value = '';
      receipt.value = undefined;
    }
  }
  return { busy, error, uncertain, receipt, run, reconcile, invalidate };
}
