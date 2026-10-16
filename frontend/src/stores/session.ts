import { defineStore } from 'pinia';
import { ref } from 'vue';
import { ApiError, post } from '../api/client';
import { mediaApi } from '../api/media';
import type { Runtime } from '../types/api';
export const useSession = defineStore('session', () => {
  const authenticated = ref(false),
    checked = ref(false),
    runtime = ref<Runtime>({});
  let probe: Promise<void> | undefined;
  async function check() {
    if (checked.value) return;
    if (!probe)
      probe = (async () => {
        try {
          runtime.value = await mediaApi.runtime();
          authenticated.value = true;
          checked.value = true;
        } catch (e) {
          if (e instanceof ApiError && e.status === 401) {
            authenticated.value = false;
            checked.value = true;
          } else throw e;
        } finally {
          probe = undefined;
        }
      })();
    return probe;
  }
  async function login(username: string, password: string) {
    await post('/api/login', { username, password });
    checked.value = false;
    await check();
  }
  async function logout() {
    await post('/api/logout');
    authenticated.value = false;
    checked.value = true;
    runtime.value = {};
  }
  function expire() {
    authenticated.value = false;
    checked.value = true;
    runtime.value = {};
  }
  return { authenticated, checked, runtime, check, login, logout, expire };
});
