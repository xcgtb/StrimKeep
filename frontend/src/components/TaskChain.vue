<script setup lang="ts">
import { watch } from 'vue';
import { readData, records, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useActivityUpdates } from '../composables/useActivityUpdates';
import { stateLabel } from '../composables/format';
const props = defineProps<{ id: string }>();
const state = useRequest<Data>();
function load() {
  if (!props.id || state.loading.value) return;
  return state.run((signal) =>
    readData(`/api/cloud/jobs/${encodeURIComponent(props.id)}/chain`, signal),
  );
}
watch(
  () => props.id,
  () => {
    state.cancel();
    state.data.value = undefined;
    void load();
  },
  { immediate: true },
);
useActivityUpdates(load);
</script>
<template>
  <section class="task-chain" aria-label="完整任务链">
    <header class="section-heading">
      <div>
        <span class="eyebrow">WORKFLOW</span>
        <h3>同一资源，完整进度</h3>
      </div>
      <span v-if="state.data.value?.state" class="state-pill">{{
        state.data.value.state === 'waiting'
          ? '等待回执'
          : stateLabel(String(state.data.value.state))
      }}</span>
    </header>
    <p v-if="state.error.value" class="inline-error">{{ state.error.value }}</p>
    <p class="muted-text">{{ state.data.value?.message }}</p>
    <ol class="chain-jobs">
      <li
        v-for="row in records(state.data.value?.jobs)"
        :key="String(row.id)"
        :class="{ selected: row.id === id }"
      >
        <span class="chain-dot" :class="String(row.state)"></span>
        <div>
          <strong>{{ row.label }}</strong
          ><small>{{ row.resolved_by ? '已转交后续任务' : stateLabel(String(row.state)) }}</small>
          <p>{{ row.message }}</p>
        </div>
        <RouterLink
          v-if="row.id !== id"
          class="text-link"
          :to="{ path: '/tasks', query: { job: String(row.id) } }"
          >查看</RouterLink
        >
      </li>
    </ol>
    <div class="chain-receipts">
      <div>
        <small>Emby 入库回执</small
        ><strong>{{ Number(object(state.data.value?.ingestion).confirmed || 0) }} 已确认</strong
        ><span>
          {{ Number(object(state.data.value?.ingestion).pending || 0) }} 等待中 ·
          {{
            Number(object(state.data.value?.ingestion).timeout || 0) +
            Number(object(state.data.value?.ingestion).stale || 0)
          }}
          待处理</span
        >
      </div>
      <div>
        <small>通知回执</small
        ><strong>{{ Number(object(state.data.value?.notifications).verified || 0) }} 已发送</strong
        ><span
          >{{
            Number(object(state.data.value?.notifications).unknown || 0) +
            Number(object(state.data.value?.notifications).dispatched || 0)
          }}
          未确认</span
        >
      </div>
    </div>
    <p v-if="!Object.keys(object(state.data.value?.ingestion)).length" class="data-caption">
      暂无本流程的入库回执；STRM 完成后仍需核实 Emby。
    </p>
    <p v-if="state.data.value?.truncated" class="warning-text">
      流程超过500个任务，仅展示已加载范围，请分批查看。
    </p>
  </section>
</template>
