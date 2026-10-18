<script setup lang="ts">
import { ref, watch, computed, watchEffect } from 'vue';
import { readData, writeData, object, records, uuid, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import ConfirmAction from './ConfirmAction.vue';
import TaskReceipt from './TaskReceipt.vue';
import EvidenceDetails from './EvidenceDetails.vue';
import OperationState from './OperationState.vue';
const props = withDefaults(
  defineProps<{
    media: 'series' | 'movies';
    target: string;
    ids: string[];
    all?: boolean;
    filter?: string;
    category?: string;
    categoryMode?: string;
    keyword?: string;
  }>(),
  { filter: 'all', category: '', categoryMode: 'seg', keyword: '' },
);
const emit = defineEmits<{ done: []; busy: [value: boolean] }>();
const op = useOperation(),
  previewRequest = useRequest<Data>(),
  previewJob = ref(''),
  preview = ref<Data>(),
  confirmation = ref(false),
  operationTarget = ref(''),
  operationId = ref(''),
  submitted = ref(false);
let generation = 0;
watchEffect(() => emit('busy', submitted.value || op.busy.value || !!previewJob.value));
const enabled = computed(
  () =>
    ['local', 'share'].includes(props.target) &&
    (props.all || props.ids.length > 0) &&
    (props.all || props.ids.length <= (props.target === 'local' ? 20 : 600)),
);
const phrase = computed(() => (props.target === 'local' ? '删除本地库及源文件' : '删除分享库'));
const previewSeries = computed(() => records(preview.value?.series).slice(0, 8));
function invalidate() {
  generation++;
  previewRequest.cancel();
  previewJob.value = '';
  preview.value = undefined;
  confirmation.value = false;
}
watch(
  () =>
    JSON.stringify([
      props.media,
      props.target,
      props.ids,
      props.all,
      props.filter,
      props.category,
      props.categoryMode,
      props.keyword,
    ]),
  invalidate,
);
async function start() {
  if (submitted.value) return;
  operationId.value = '';
  invalidate();
  const gen = generation;
  const body = {
    target: props.target,
    scope: props.target,
    filter: props.filter,
    category: props.category,
    category_mode: props.categoryMode,
    keyword: props.keyword,
    selection: props.all ? 'all' : 'selected',
    ...(props.all ? {} : { [props.media === 'movies' ? 'movie_ids' : 'series_ids']: props.ids }),
  };
  const r = await op.run(() =>
    writeData(`/api/emby/${props.media}/batch-delete/preview/start`, body),
  );
  if (r && gen === generation) {
    previewJob.value = String(r.job_id);
    await poll();
  }
}
async function poll() {
  if (!previewJob.value || preview.value) return;
  const gen = generation;
  const r = await previewRequest.run((signal) =>
    readData(
      `/api/emby/${props.media}/batch-delete/preview/status?job_id=${encodeURIComponent(previewJob.value)}`,
      signal,
      ['error'],
    ),
  );
  if (!r || gen !== generation) return;
  if (r.status === 'error') {
    previewJob.value = '';
    previewRequest.error.value = String(r.message || '预览核验失败，尚未提交删除');
    return;
  }
  if (r.status === 'done') {
    const result = object(r.result);
    previewJob.value = '';
    if (result.status !== 'success') {
      previewRequest.error.value = String(result.message || '预览核验失败');
      return;
    }
    preview.value = result;
    confirmation.value = true;
  }
}
async function execute() {
  if (!preview.value || !enabled.value) return;
  operationTarget.value = props.target;
  operationId.value = uuid();
  submitted.value = true;
  const result = await op.run(() =>
    writeData(`/api/emby/${props.media}/batch-delete/execute`, {
      preview_id: preview.value?.preview_id,
      target: props.target,
      confirm: true,
      confirmation: phrase.value,
      operation_id: operationId.value,
    }),
  );
  if (result) {
    confirmation.value = false;
    preview.value = undefined;
  } else if (!op.uncertain.value) {
    operationId.value = '';
    submitted.value = false;
  }
}
useVisiblePoll(poll, 2000);
</script>
<template>
  <div class="mapping-delete">
    <button
      class="button danger-text"
      :disabled="!enabled || op.busy.value || !!previewJob || op.uncertain.value || submitted"
      @click="start"
    >
      {{
        previewJob
          ? '正在核验删除预览…'
          : `预览批量删除 · ${target === 'local' ? '本地库' : '分享库'}`
      }}
    </button>
    <p v-if="!['local', 'share'].includes(target)" class="muted-text">
      先筛选为仅本地库或仅分享库，再选择删除范围。
    </p>
    <OperationState
      :busy="op.busy.value"
      :error="op.error.value || previewRequest.error.value"
      :uncertain="op.uncertain.value"
    />
    <p v-if="previewRequest.error.value && previewJob" class="notice-bar">
      预览核验未完成，请重新核实；尚未提交删除。<button class="button small" @click="poll">
        重试核验
      </button>
    </p>
    <p v-if="previewJob" class="notice-bar">
      只读核验 {{ previewRequest.data.value?.processed ?? 0 }} /
      {{ previewRequest.data.value?.total ?? '—' }} 项，尚未执行删除。
    </p>
    <TaskReceipt
      v-if="operationId"
      :target="operationTarget"
      :id="operationId"
      kind="delete"
      @done="
        submitted = false;
        op.reconcile(true);
        emit('done');
      "
    /><ConfirmAction
      v-if="confirmation && preview"
      title="确认片库批量删除"
      :description="
        target === 'local'
          ? '删除预览核实的本地 STRM，并按原后端规则联动删除已匹配源视频和附属文件；源删除失败时保留对应 STRM。'
          : '仅删除预览核实的 NAS 分享库 STRM；不删除分享来源视频。超过单批上限的分享项目由后端分批核实执行。'
      "
      :phrase="phrase"
      danger
      :busy="op.busy.value"
      :disabled="op.uncertain.value || !enabled"
      @close="
        confirmation = false;
        preview = undefined;
      "
      @confirm="execute"
      ><div class="batch-summary">
        <p>
          共 <b>{{ preview.count ?? '—' }}</b> 部 / <b>{{ preview.strm_count ?? '—' }}</b> 个
          STRM，分 <b>{{ preview.batch_count || 1 }}</b> 批执行（每批上限
          {{ preview.batch_size || '—' }} 部）。
        </p>
        <p class="muted-text">执行中会再次核验，已变化的文件自动跳过。</p>
        <ul v-if="previewSeries.length" class="batch-titles">
          <li v-for="row in previewSeries" :key="String(row.name)">
            {{ row.name }}<span>{{ row.count }} 个 STRM</span>
          </li>
        </ul>
        <p v-if="preview.notice" class="batch-notice">⚠️ {{ preview.notice }}</p>
      </div>
      <details>
        <summary>核验明细与证据</summary>
        <EvidenceDetails :value="preview" /></details
    ></ConfirmAction>
  </div>
</template>
