import { onBeforeUnmount, ref, shallowRef } from 'vue';
export function useRequest<T>() {
  const data = shallowRef<T>(),
    loading = ref(false),
    error = ref('');
  let controller: AbortController | undefined,
    generation = 0;
  async function run(loader: (signal: AbortSignal) => Promise<T>) {
    const current = ++generation;
    controller?.abort();
    controller = new AbortController();
    loading.value = true;
    error.value = '';
    try {
      const value = await loader(controller.signal);
      if (current === generation) data.value = value;
      return current === generation ? value : undefined;
    } catch (e) {
      if (current === generation && !(e instanceof DOMException && e.name === 'AbortError'))
        error.value = e instanceof Error ? e.message : '请求失败';
    } finally {
      if (current === generation) loading.value = false;
    }
  }
  function cancel() {
    generation++;
    data.value = undefined;
    error.value = '';
    controller?.abort();
    loading.value = false;
  }
  onBeforeUnmount(cancel);
  return { data, loading, error, run, cancel };
}
