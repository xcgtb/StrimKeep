import { defineStore } from 'pinia';
import { ref } from 'vue';
import { readData, records, type Data } from '../api/domain';
export const useCloud = defineStore('cloud', () => {
  const accounts = ref<Data[]>([]),
    devices = ref<Data[]>([]),
    id = ref(''),
    error = ref(''),
    loading = ref(false);
  let promise: Promise<void> | undefined;
  async function load() {
    if (promise) return promise;
    loading.value = true;
    error.value = '';
    promise = (async () => {
      try {
        const value = await readData('/api/cloud/status');
        accounts.value = records(value.accounts);
        devices.value = records(value.devices);
        if (!accounts.value.some((a) => a.id === id.value))
          id.value = String(accounts.value[0]?.id || '');
      } catch (e) {
        error.value = (e as Error).message;
      } finally {
        loading.value = false;
        promise = undefined;
      }
    })();
    return promise;
  }
  return { accounts, devices, id, error, loading, load };
});
