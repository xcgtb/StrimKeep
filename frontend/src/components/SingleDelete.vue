<script setup lang="ts">
import { ref } from 'vue';
import type { LibraryItem } from '../types/api';
import { writeData, uuid, type Data } from '../api/domain';
import { useOperation } from '../composables/useOperation';
import ConfirmAction from './ConfirmAction.vue';
import TaskReceipt from './TaskReceipt.vue';
import EvidenceDetails from './EvidenceDetails.vue';
import OperationState from './OperationState.vue';
const props = defineProps<{
  item: LibraryItem;
  media: 'series' | 'movies';
  target: 'local' | 'share';
}>();
const emit = defineEmits<{ done: [] }>();
const op = useOperation(() =>
    JSON.stringify([props.item.id, props.item.tmdb_id, props.media, props.target]),
  ),
  preview = ref<Data>(),
  operationId = ref(''),
  submitted = ref(false);
function path() {
  return props.media === 'series'
    ? `/api/emby/series/${encodeURIComponent(props.item.id || '')}/delete`
    : '/api/emby/movie/delete_by_tmdb';
}
function body() {
  return {
    target: props.target,
    tmdb_id: props.item.tmdb_id,
    series_name: props.item.name,
    name: props.item.name,
    media_type: props.media === 'series' ? '剧集' : '电影',
  };
}
async function prepare() {
  preview.value = undefined;
  const r = await op.run(() => writeData(path(), { ...body(), dry_run: true }));
  if (r) preview.value = r;
}
async function execute() {
  if (!preview.value || !Number(preview.value.count)) return;
  operationId.value = uuid();
  submitted.value = true;
  const r = await op.run(() =>
    writeData(path(), {
      ...body(),
      dry_run: false,
      async_delete: true,
      operation_id: operationId.value,
      source_preview_id: preview.value?.source_preview_id,
    }),
  );
  if (r) preview.value = undefined;
  else if (!op.uncertain.value) {
    submitted.value = false;
    operationId.value = '';
  }
}
function closeResult() {
  if (submitted.value || op.busy.value) return;
  operationId.value = '';
  op.reconcile(true);
}
</script>
<template>
  <div>
    <button
      class="button danger-text"
      :disabled="
        op.busy.value ||
        op.uncertain.value ||
        submitted ||
        (media === 'movies' ? !item.tmdb_id : !item.id)
      "
      @click="prepare"
    >
      预览删除{{ target === 'local' ? '本地库' : '分享库' }}</button
    ><OperationState
      :busy="op.busy.value"
      :error="op.error.value"
      :uncertain="op.uncertain.value"
    />
    <p v-if="preview && !Number(preview.count)" class="notice-bar">
      未找到此库的实际文件。可从磁盘同步映射状态后核对。
    </p>
    <TaskReceipt
      v-if="operationId"
      :target="target"
      :id="operationId"
      kind="delete"
      @close="closeResult"
      @done="
        submitted = false;
        op.reconcile(true);
        emit('done');
      "
    /><ConfirmAction
      v-if="preview && Number(preview.count) > 0"
      title="确认删除作品"
      :description="
        target === 'local'
          ? '删除已核验的本地 STRM，上游按原挂载链路、自建按已登记的 115 身份联动删除源视频和附属文件；源文件删除失败时保留对应 STRM。'
          : '仅删除已核验的 NAS 分享库 STRM，保留分享源视频。'
      "
      :phrase="target === 'local' ? '删除本地库及源文件' : '删除分享库'"
      danger
      :busy="op.busy.value"
      :disabled="op.uncertain.value"
      @close="preview = undefined"
      @confirm="execute"
      ><div class="batch-summary">
        <p>
          作品：<b>{{ item.name }}</b> · 已核验 <b>{{ preview.count }}</b> 个 STRM，目标：<b>{{
            target === 'local' ? '本地库（联动删除源文件）' : '分享库（仅删 NAS STRM）'
          }}</b>
        </p>
        <p class="muted-text">
          以上为预览核验结果，确认后才会真正删除；执行中会再次核验，已变化的文件自动跳过。
        </p>
      </div>
      <p v-if="preview.source_counts" class="notice-bar">
        来源范围：<span v-for="(count, name) in preview.source_counts" :key="name"
          >{{ name }} {{ count }} 个；</span
        >
      </p>
      <details>
        <summary>核验明细与证据</summary>
        <EvidenceDetails :value="preview" />
      </details>
    </ConfirmAction>
  </div>
</template>
