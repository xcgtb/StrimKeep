import { computed, watch } from 'vue';
import { useRoute } from 'vue-router';
import { useCloud } from '../stores/cloud';
export function useInventorySource() {
  const route = useRoute(),
    cloud = useCloud();
  let appliedAccount = '';
  const source = computed(() => {
    const cid = route.query.source_cid;
    return route.query.account === cloud.id && typeof cid === 'string' && /^\d+$/.test(cid)
      ? {
          id: cid,
          name: typeof route.query.source_name === 'string' ? route.query.source_name : cid,
        }
      : undefined;
  });
  watch(
    () => [route.query.account, cloud.accounts] as const,
    () => {
      const account = route.query.account;
      if (
        typeof account === 'string' &&
        account !== appliedAccount &&
        cloud.accounts.some((a) => a.id === account)
      ) {
        appliedAccount = account;
        cloud.id = account;
      }
    },
    { immediate: true },
  );
  return source;
}
