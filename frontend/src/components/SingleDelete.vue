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
const emit = defineEmits<{ done: [result: Data]; failed: [message: string] }>();
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
      :id="operationId"
      kind="delete"
      @done="
        submitted = false;
        emit('done', $event);
      "
      @failed="
        submitted = false;
        emit('failed', '后台删除失败，请展开执行明细核实，勿重复提交。');
      "
    /><ConfirmAction
      v-if="preview && Number(preview.count) > 0"
      title="确认删除作品"
      :description="
        target === 'local'
          ? '删除已核验的本地 STRM，按原后端规则联动删除匹配的源视频和附属文件；源文件删除失败时保留对应 STRM。'
          : '仅删除已核验的 NAS 分享库 STRM，保留分享源视频。'
      "
      :phrase="target === 'local' ? '删除本地库及源文件' : '删除分享库'"
      danger
      :busy="op.busy.value"
      :disabled="op.uncertain.value"
      @close="preview = undefined"
      @confirm="execute"
      ><p>作品：{{ item.name }} · 已核实 {{ preview.count }} 个文件</p>
      <EvidenceDetails :value="preview"
    /></ConfirmAction>
  </div>
</template>
