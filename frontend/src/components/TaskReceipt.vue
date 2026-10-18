<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { readData, writeData, object, text, strings, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import EvidenceDetails from './EvidenceDetails.vue';
import { stateLabel } from '../composables/format';
const props = defineProps<{ id: string; kind?: 'cloud' | 'task' | 'delete'; target?: string }>();
const request = useRequest<Data>();
const emit = defineEmits<{ done: [value: Data]; update: [value: Data]; close: [] }>();
const hidden = ref(false);
const terminal = computed(() => {
  const row = object(request.data.value?.job || request.data.value);
  return ['success', 'failed', 'cancelled', 'stopped', 'done', 'error'].includes(
    String(row.state || row.status),
  );
});
// 批量删除的进度原本只以原始字段呈现，这里换成 1.2.12 的进度条 + 业务计数。
const deleteProgress = computed(() => {
  if (props.kind !== 'delete' || !request.data.value) return undefined;
  const row = object(request.data.value);
  const outcome = object(row.result);
  const total = Number(row.total || 0);
  const processed = Number(row.processed || 0);
  return {
    total,
    processed,
    batch: Number(row.batch_index || 0),
    batches: Number(row.batch_total || 0),
    phase: String(
      row.phase_label ||
        row.message ||
        row.current ||
        (
          {
            preparing: '正在核验执行范围…',
            deleting: '正在逐部重新核验并删除…',
            done: '执行已结束，正在展示实际结果',
          } as Record<string, string>
        )[String(row.phase)] ||
        '',
    ),
    done: row.state === 'done',
    message: String(outcome.message || ''),
    failed: outcome.status === 'error' || outcome.status === 'failed',
    strm_removed: Number(outcome.strm_removed ?? row.strm_removed ?? 0),
    cloud_removed: Number(outcome.cloud_removed ?? row.cloud_removed ?? 0),
    retained: Number(outcome.retained ?? row.retained ?? 0),
    error_count: Number(outcome.failed ?? row.error_count ?? 0),
  };
});
let completed = '';
watch(request.data, (d) => {
  const row = object(d?.job || d);
  const status = String(row.state || row.status);
  if (d) emit('update', row);
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
    hidden.value = false;
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
  <div v-if="kind === 'delete' && hidden" class="task-receipt-collapsed" role="status">
    <span>{{
      terminal ? '删除任务已结束，可查看结果。' : '进度已隐藏，后台删除任务不受影响。'
    }}</span>
    <button class="button small" @click="hidden = false">
      {{ terminal ? '查看删除结果' : '查看删除进度' }}
    </button>
    <button v-if="terminal" class="button small" @click="emit('close')">关闭结果</button>
  </div>
  <section v-else class="panel task-receipt" role="status">
    <header class="section-heading">
      <div>
        <h2>{{ kind === 'delete' ? '片库删除进度与结果' : '后台任务' }}</h2>
        <p>请求已接受，实际结果以后台回执为准。</p>
      </div>
      <div class="task-receipt-actions">
        <button class="button small" :disabled="request.loading.value" @click="load()">
          核实状态
        </button>
        <button v-if="kind === 'delete' && !terminal" class="button small" @click="hidden = true">
          隐藏进度（删除继续）
        </button>
      </div>
    </header>
    <button
      v-if="kind === 'task' && request.data.value?.cancellable"
      class="button small"
      @click="cancel"
    >
      请求取消扫描
    </button>
    <p v-if="kind !== 'delete'" class="mono">{{ id }}</p>
    <p v-if="kind === 'delete'" class="muted-text">
      目标：{{
        target === 'local' ? '本地库（按规则联动删除源文件）' : '分享库（仅删除 STRM）'
      }}。结果以实际执行回执为准。
    </p>
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
      <p
        v-if="deleteProgress.message"
        :class="deleteProgress.failed ? 'inline-error' : 'muted-text'"
      >
        {{ deleteProgress.message }}
      </p>
      <p v-if="deleteProgress.phase" class="muted-text">{{ deleteProgress.phase }}</p>
      <p v-if="deleteProgress.batches" class="muted-text">
        当前第 {{ deleteProgress.batch }} / {{ deleteProgress.batches }} 批
      </p>
      <progress
        aria-label="片库删除进度"
        :value="deleteProgress.processed"
        :max="deleteProgress.total || 1"
      ></progress>
      <div class="task-progress-counts">
        <span
          >已处理 <b>{{ deleteProgress.processed }} / {{ deleteProgress.total }}</b></span
        >
        <span
          >STRM 已删除 <b>{{ deleteProgress.strm_removed }}</b></span
        >
        <span v-if="target === 'local'"
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
    <template v-if="kind === 'task' && request.data.value">
      <p v-if="request.data.value.error" class="inline-error">{{ request.data.value.error }}</p>
      <div
        v-if="strings(request.data.value.logs).length"
        class="task-log"
        aria-label="扫描与执行日志"
      >
        <p v-for="(line, index) in strings(request.data.value.logs).slice(-20)" :key="index">
          {{ line }}
        </p>
      </div>
      <div v-if="request.data.value.result" class="task-result-summary">
        <p v-for="(line, index) in strings(object(request.data.value.result).detail)" :key="index">
          {{ line }}
        </p>
        <p v-if="object(request.data.value.result).loc_cnt != null">
          本地已处理 {{ object(request.data.value.result).loc_cnt }} · 分享已处理
          {{ object(request.data.value.result).sh_cnt ?? 0 }}
        </p>
      </div>
    </template>
    <details v-if="request.data.value" class="technical-details">
      <summary>任务回执与核验明细</summary>
      <EvidenceDetails :value="request.data.value.job || request.data.value" />
    </details>
    <div v-if="kind === 'delete' && terminal" class="task-receipt-footer">
      <button class="button" @click="emit('close')">关闭结果</button>
    </div>
  </section>
</template>
