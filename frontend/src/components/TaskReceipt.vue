<script setup lang="ts">
import { onMounted, watch } from 'vue';
import { readData, writeData, object, text, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import EvidenceDetails from './EvidenceDetails.vue';
import { stateLabel } from '../composables/format';
const props = defineProps<{ id: string; kind?: 'cloud' | 'task' | 'delete' }>();
const request = useRequest<Data>();
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
    <EvidenceDetails
      v-if="request.data.value"
      :value="request.data.value.job || request.data.value"
    />
  </section>
</template>
