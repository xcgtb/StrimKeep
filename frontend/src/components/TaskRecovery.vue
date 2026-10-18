<script setup lang="ts">
import { ref, watch } from 'vue';
import { writeData, readData, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import OperationState from './OperationState.vue';
import ConfirmAction from './ConfirmAction.vue';
const props = defineProps<{ id: string }>();
const checking = useRequest<Data>();
const op = useOperation(() => props.id),
  preview = ref<Data>(),
  confirming = ref(false);
watch(
  () => props.id,
  () => {
    checking.cancel();
    preview.value = undefined;
    confirming.value = false;
    op.invalidate();
  },
);
async function inspect() {
  const r = await op.run(() =>
    writeData(`/api/cloud/jobs/${encodeURIComponent(props.id)}/recovery/preview`),
  );
  if (r) preview.value = r;
}
async function resume() {
  const r = await op.run(() =>
    writeData(`/api/cloud/jobs/${encodeURIComponent(props.id)}/recovery/apply`, {
      preview_id: preview.value?.preview_id,
      confirmed: true,
    }),
  );
  if (op.error.value) confirming.value = false;
  if (r) {
    confirming.value = false;
    preview.value = r;
  }
}
async function verifyOriginal() {
  const id = props.id;
  const r = await checking.run((signal) =>
    readData(`/api/cloud/jobs/${encodeURIComponent(id)}/chain`, signal),
  );
  if (r && id === props.id) {
    const owner = records(r.jobs).find((row) => row.id === id);
    preview.value = owner?.resolved_by
      ? { job_id: owner.resolved_by, message: '后续任务已存在，请查看原回执' }
      : undefined;
    confirming.value = false;
    op.reconcile(true);
  }
}
</script>
<template>
  <section class="recovery-panel" aria-label="核实与恢复">
    <header class="section-heading">
      <div>
        <h3>从已核实的位置继续</h3>
        <p class="muted-text">先读取实际文件状态，再确认剩余范围。</p>
      </div>
      <button class="button" :disabled="op.busy.value || op.uncertain.value" @click="inspect">
        核对恢复范围
      </button>
    </header>
    <OperationState
      :busy="op.busy.value"
      :error="checking.error.value || op.error.value"
      :uncertain="op.uncertain.value"
      :receipt="op.receipt.value"
    />
    <button
      v-if="op.uncertain.value"
      class="button"
      :disabled="checking.loading.value"
      @click="verifyOriginal"
    >
      只读核实原任务
    </button>
    <template v-if="preview"
      ><p>{{ preview.message }}</p>
      <p
        v-for="(row, index) in (preview.blocked as string[]) || []"
        :key="index"
        class="warning-text"
      >
        {{ row }}
      </p>
      <ul class="recovery-files">
        <li v-for="(row, index) in records(preview.items)" :key="index">
          <strong>{{ row.name }}</strong
          ><span>归档至 {{ row.target }} · 关联文件 {{ row.followers }} 个</span>
        </li>
      </ul>
      <button
        v-if="preview.preview_id && !(preview.blocked as unknown[])?.length"
        class="button primary"
        :disabled="op.busy.value || op.uncertain.value"
        @click="confirming = true"
      >
        确认继续以上项目
      </button>
      <RouterLink
        v-if="preview.job_id"
        class="button primary"
        :to="{ path: '/tasks', query: { job: String(preview.job_id) } }"
        >查看后续任务</RouterLink
      >
      <RouterLink
        v-if="(preview.blocked as unknown[])?.length"
        class="text-link"
        to="/organize/workspace"
        >返回整理页重新预览 →</RouterLink
      ></template
    >
    <ConfirmAction
      v-if="confirming"
      title="继续已核实项目"
      description="仅执行本次预览内的剩余文件组。提交前会再次核对文件和账号，原任务回执保留。"
      :busy="op.busy.value"
      :disabled="op.uncertain.value"
      @close="confirming = false"
      @confirm="resume"
    />
  </section>
</template>
