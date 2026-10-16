import { computed, onMounted, ref } from 'vue';
import { readData, writeData, object, type Data } from '../api/domain';
import { useRequest } from './useRequest';
import { useOperation } from './useOperation';
import { namingKeys } from '../config/cloudFields';
export function useCloudSettingsDraft() {
  const request = useRequest<Data>(),
    op = useOperation(),
    draft = ref<Data>({}),
    confirmation = ref(false),
    preview = ref<Data>();
  const changed = computed(() =>
    Object.keys(draft.value).filter(
      (k) =>
        JSON.stringify(draft.value[k]) !== JSON.stringify(object(request.data.value?.settings)[k]),
    ),
  );
  const changes = computed(() => Object.fromEntries(changed.value.map((k) => [k, draft.value[k]])));
  async function load() {
    const result = await request.run((signal) => readData('/api/cloud/settings', signal));
    if (result) {
      draft.value = structuredClone(object(result.settings));
      op.reconcile();
    }
  }
  async function save() {
    const result = await op.run(() => writeData('/api/cloud/settings', changes.value));
    if (result) {
      request.data.value = result;
      draft.value = structuredClone(object(result.settings));
      confirmation.value = false;
    }
  }
  async function previewNaming(details = false) {
    const result = await op.run(() =>
      writeData(details ? '/api/cloud/naming/details' : '/api/cloud/naming/preview', {
        settings: Object.fromEntries(
          namingKeys.filter((k) => k in draft.value).map((k) => [k, draft.value[k]]),
        ),
      }),
    );
    if (result) preview.value = result;
  }
  onMounted(load);
  return { request, op, draft, confirmation, preview, changed, changes, load, save, previewNaming };
}
