<script setup lang="ts">
import { computed, onMounted, watch } from 'vue';
import { readData, writeData, object, text, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import EvidenceDetails from './EvidenceDetails.vue';
import { stateLabel } from '../composables/format';
import { deletionOutcome } from '../composables/deletionOutcome';
const props = defineProps<{ id: string; kind?: 'cloud' | 'task' | 'delete' }>();
const request = useRequest<Data>();
const emit = defineEmits<{ done: [value: Data]; failed: [value: Data] }>();
let completed = '';
const row = computed(() => object(request.data.value?.job || request.data.value));
const result = computed(() => object(row.value.result));
const state = computed(() => text(row.value.state || row.value.status));
const outcome = computed(() => deletionOutcome(state.value, result.value, props.kind === 'delete'));
const isTerminal = computed(() => outcome.value !== 'running');
const isFailure = computed(() => outcome.value === 'failed' || outcome.value === 'needs_review');
const total = computed(() => Number(row.value.total || object(row.value.progress).total || 0));
const processed = computed(() =>
  Number(row.value.processed || object(row.value.progress).processed || 0),
);
const percentage = computed(() =>
  total.value > 0 ? Math.min(100, Math.round((processed.value / total.value) * 100)) : 0,
);
const message = computed(() => String(result.value.message || row.value.message || ''));
const phases: Record<string, string> = {
  preparing: '核验媒体来源',
  deleting: '正在逐项删除',
  done: '处理已结束',
  queued: '等待执行',
  running: '处理中',
  scanning: '扫描中',
};
watch([row, () => props.id], () => {
  if (!isTerminal.value || completed === props.id) return;
  completed = props.id;
  if (isFailure.value) emit('failed', row.value);
  else emit('done', row.value);
});
async function cancel() {
  try {
    await writeData(`/api/task/${encodeURIComponent(props.id)}/cancel`);
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
    completed = '';
    request.cancel();
    void load();
  },
);
useVisiblePoll(
  () => {
    if (!isTerminal.value) return load();
  },
  props.kind === 'delete' ? 1100 : 2500,
);
</script>
<template>
  <section class="panel task-receipt" role="status" aria-live="polite">
    <header class="section-heading">
      <div>
        <h2>{{ kind === 'delete' ? '删除执行进度' : '后台任务进度' }}</h2>
        <p>
          {{
            kind === 'delete'
              ? '以后台逐项回执为准，关闭提示不会终止任务。'
              : '操作已提交，以后台回执为准。'
          }}
        </p>
      </div>
      <button class="button small" :disabled="request.loading.value" @click="load">刷新结果</button>
    </header>
    <button
      v-if="kind === 'task' && request.data.value?.cancellable"
      class="button small"
      @click="cancel"
    >
      请求取消扫描
    </button>
    <p v-if="request.error.value" class="inline-error">
      {{ request.error.value }} · 请核实原任务，不要重复提交。
    </p>
    <template v-if="request.data.value">
      <p class="task-receipt-state" :class="{ 'warning-text': isFailure }">
        {{
          isFailure
            ? outcome === 'needs_review'
              ? '结果待核实：后端未确认删除成功'
              : '任务失败或已停止'
            : outcome === 'partial'
              ? '部分完成，请核对失败项目'
              : isTerminal
                ? '任务执行完毕'
                : phases[text(row.phase)] || stateLabel(state)
        }}
      </p>
      <div
        v-if="kind === 'delete'"
        class="delete-progress"
        :class="{ 'progress-failed': isFailure }"
      >
        <div class="progress-top">
          <strong>{{ total ? `已处理 ${processed} / ${total}` : '正在核验和准备删除' }}</strong
          ><span>{{ total ? `${percentage}%` : '—' }}</span>
        </div>
        <progress
          :max="Math.max(total, 1)"
          :value="Math.min(processed, Math.max(total, 1))"
          :aria-label="'删除进度'"
        />
        <div class="progress-metrics">
          <span
            >已删除 STRM <strong>{{ row.strm_removed ?? result.strm_removed ?? 0 }}</strong></span
          >
          <span
            >源文件 <strong>{{ row.cloud_removed ?? result.cloud_removed ?? 0 }}</strong></span
          >
          <span
            >保留 <strong>{{ row.retained ?? result.retained ?? 0 }}</strong></span
          >
          <span
            >异常 <strong>{{ row.error_count ?? result.failed ?? 0 }}</strong></span
          >
        </div>
        <p v-if="row.current_file">当前：{{ row.current_file }}</p>
      </div>
      <p v-if="message" :class="{ 'inline-error': isFailure }">{{ message }}</p>
      <p
        v-if="isTerminal && kind === 'delete' && !isFailure && Number(result.failed || 0) > 0"
        class="notice-bar"
      >
        部分作品处理失败，请展开执行明细核对，不要重新批量提交。
      </p>
      <details class="receipt-technical">
        <summary>查看执行记录与技术明细</summary>
        <p class="mono">任务编号 {{ id }}</p>
        <EvidenceDetails :value="row" />
      </details>
    </template>
    <p v-else class="muted-text">正在读取后台进度…</p>
  </section>
</template>
