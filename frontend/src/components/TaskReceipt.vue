<script setup lang="ts">
import { computed, onMounted, watch } from 'vue';
import { readData, writeData, object, text, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import EvidenceDetails from './EvidenceDetails.vue';
import { stateLabel } from '../composables/format';
const props = defineProps<{ id: string; kind?: 'cloud' | 'task' | 'delete' }>();
const request = useRequest<Data>();
const emit = defineEmits<{ done: [value: Data] }>();
// 批量删除的进度原本只以原始字段呈现，这里换成 1.2.12 的进度条 + 业务计数。
const deleteProgress = computed(() => {
  if (props.kind !== 'delete' || !request.data.value) return undefined;
  const row = object(request.data.value);
  const total = Number(row.total || 0);
  const processed = Number(row.processed || 0);
  return {
    total,
    processed,
    percent: total ? Math.min(100, Math.round((processed / total) * 100)) : 0,
    strm_removed: Number(row.strm_removed || 0),
    cloud_removed: Number(row.cloud_removed || 0),
    retained: Number(row.retained || 0),
    error_count: Number(row.error_count || 0),
  };
});
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
    <p class="mono">{{ id }}</p>
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
    <div v-if="deleteProgress" class="task-progress">
      <div class="task-progress-bar">
        <i :style="{ width: deleteProgress.percent + '%' }"></i>
      </div>
      <div class="task-progress-counts">
        <span
          >已处理 <b>{{ deleteProgress.processed }} / {{ deleteProgress.total }}</b></span
        >
        <span
          >STRM 已删除 <b>{{ deleteProgress.strm_removed }}</b></span
        >
        <span
          >115 源文件已删除 <b>{{ deleteProgress.cloud_removed }}</b></span
        >
        <span
          >保留 <b>{{ deleteProgress.retained }}</b></span
        >
        <span v-if="deleteProgress.error_count"
          >失败 <b>{{ deleteProgress.error_count }}</b></span
        >
      </div>
    </div>
    <EvidenceDetails
      v-if="request.data.value"
      :value="request.data.value.job || request.data.value"
    />
  </section>
</template>
