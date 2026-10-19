<script setup lang="ts">
import { watch, computed } from 'vue';
import { useCloud } from '../stores/cloud';
import { useRequest } from '../composables/useRequest';
import { readData, accountPath, object, records, type Data } from '../api/domain';
const props = withDefaults(defineProps<{ kind?: 'personal' | 'share' }>(), { kind: 'personal' });
const cloud = useCloud(),
  state = useRequest<Data>();
const rows = computed(() =>
  records(object(state.data.value?.emby_mappings).items).filter((r) => r.kind === props.kind),
);
function load() {
  if (cloud.id) return state.run((signal) => readData(accountPath(cloud.id, 'pipeline'), signal));
}
watch(
  () => cloud.id,
  () => {
    state.cancel();
    state.data.value = undefined;
    void load();
  },
  { immediate: true },
);
</script>
<template>
  <section class="panel workflow-config-card">
    <header class="section-heading">
      <h3>实际 Emby 入库映射</h3>
      <button class="button small" :disabled="state.loading.value" @click="load">重新核对</button>
    </header>
    <p v-if="state.error.value" class="inline-error">{{ state.error.value }}</p>
    <p v-if="!rows.length" class="muted-text">当前账号尚未配置 Emby 入库连接。</p>
    <article v-for="row in rows" :key="String(row.connection_id)" class="mapping-entry">
      <strong>{{ row.name }} · {{ row.enabled ? '已启用' : '未启用此库' }}</strong>
      <p class="path-text">NAS：{{ row.nas_root }}</p>
      <p class="path-text">Emby：{{ row.emby_root || '尚未配置' }}</p>
      <p v-if="row.sample_emby_path" class="path-text">示例：{{ row.sample_emby_path }}</p>
      <p v-if="!row.consistent" class="warning-text">
        反代路径与此连接的入库路径不同，请核对容器挂载。入库以此连接配置为准。
      </p>
      <small>{{ row.message }}</small>
    </article>
    <RouterLink to="/tasks/ingestion" class="text-link"
      >修改 Emby 连接路径与查看入库回执 →</RouterLink
    >
  </section>
</template>
