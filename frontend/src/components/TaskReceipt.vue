<script setup lang="ts">
import { computed, onMounted, watch } from 'vue';
import { readData, writeData, object, text, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import EvidenceDetails from './EvidenceDetails.vue';
import { stateLabel } from '../composables/format';
const props = defineProps<{ id: string; kind?: 'cloud' | 'task' | 'delete' }>();
const request = useRequest<Data>();
const job = computed(() => object(request.data.value?.job || request.data.value));
const progress = computed(() => object(job.value.progress));
const fraction = computed(() => {
  const total = Number(progress.value.total);
  const processed = Number(progress.value.processed);
  if (!Number.isFinite(total) || total <= 0 || !Number.isFinite(processed)) return undefined;
  return Math.min(100, Math.max(0, Math.round((processed / total) * 100)));
});
const emit = defineEmits<{ done: [value: Data] }>();
let completed = '';
watch(request.data, (d) => {
  const row = object(d?.job || d);
  const status = String(row.state || row.status);
  if (
    ['success', 'failed', 'cancelled', 'stopped', 'done', 'error'].includes(status) &&
    completed !== props.id
  ) {
    completed = props.id;
    emit('done', row);
  }
});
async function cancel() {
  try {
    await writeData(`/api/task/${props.id}/cancel`);
    await load();
  } catch (e) {
    request.error.value = (e as Error).message;
  }
}
function load() {
  return request.run((signal) =>
    readData(
      props.kind === 'task'
        ? `/api/task/${encodeURIComponent(props.id)}`
        : props.kind === 'delete'
          ? `/api/emby/delete/progress/${encodeURIComponent(props.id)}`
          : `/api/cloud/jobs/${encodeURIComponent(props.id)}`,
      signal,
      props.kind === 'task' ? ['error', 'failed'] : [],
    ),
  );
}
onMounted(load);
watch(
  () => props.id,
  () => {
    request.data.value = undefined;
    void load();
  },
);
useVisiblePoll(() => {
  const d = object(request.data.value?.job || request.data.value);
  if (
    !['success', 'failed', 'cancelled', 'stopped', 'done', 'error'].includes(
      String(d.state || d.status),
    )
  )
    return load();
}, 2500);
</script>
<template>
  <section class="panel task-receipt" role="status">
    <header class="section-heading">
      <div>
        <h2>后台任务</h2>
        <p>请求已接受，实际结果以后台回执为准。</p>
      </div>
      <button class="button small" :disabled="request.loading.value" @click="load()">
        核实状态
      </button>
    </header>
    <button
      v-if="kind === 'task' && request.data.value?.cancellable"
      class="button small"
      @click="cancel"
    >
      请求取消扫描
    </button>
    <p v-if="job.message" class="task-user-message">{{ job.message }}</p>
    <div v-if="progress.phase" class="task-live-progress">
      <div class="action-row">
        <strong>{{ progress.phase }}</strong>
        <span v-if="fraction !== undefined">{{ fraction }}%</span>
        <span v-if="progress.current" class="muted-text">{{ progress.current }}</span>
      </div>
      <div v-if="fraction !== undefined" class="task-progress-track" role="progressbar"
        :aria-label="String(progress.phase)" :aria-valuenow="fraction" aria-valuemin="0" aria-valuemax="100">
        <div :style="{ width: fraction + '%' }" /></div>
    </div>
    <p v-if="request.error.value" class="inline-error">
      {{ request.error.value }} · 请核实原任务，勿重复提交。
    </p>
    <p v-if="request.data.value" class="task-receipt-state">
      {{
        stateLabel(
          text(
            object(request.data.value.job || request.data.value).state ||
              object(request.data.value.job || request.data.value).status,
          ),
        )
      }}
    </p>
    <details v-if="request.data.value" class="task-technical-details">
      <summary>查看处理明细与技术回执</summary>
      <p class="data-caption">任务编号：{{ id }}</p>
      <EvidenceDetails :value="request.data.value.job || request.data.value" />
    </details>
  </section>
</template>
