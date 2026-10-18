<script setup lang="ts">
import { ref } from 'vue';
import { FolderOpen, ChevronRight, ArrowUp } from '@lucide/vue';
import { readData, records, query, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import ModalDialog from './ModalDialog.vue';
import LoadState from './LoadState.vue';
const props = defineProps<{ modelValue?: unknown; label: string; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: string] }>();
const open = ref(false),
  path = ref(''),
  request = useRequest<Data>();
async function browse(value = '') {
  path.value = value;
  await request.run((signal) =>
    readData(query('/api/cloud/output-directories', { path: value }), signal),
  );
}
function show() {
  open.value = true;
  void browse();
}
function select() {
  if (!request.data.value?.path || request.error.value) return;
  emit('update:modelValue', String(request.data.value.path));
  open.value = false;
  request.cancel();
}
</script>
<template>
  <label class="local-output-picker"
    >{{ label }}
    <div class="dircontrols">
      <input
        :value="modelValue ?? ''"
        :aria-label="label"
        :disabled="disabled"
        placeholder="容器内绝对路径"
        @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      /><button
        type="button"
        class="button"
        :aria-label="`选择${label}`"
        :disabled="disabled"
        @click="show"
      >
        <FolderOpen :size="15" />选择
      </button>
    </div>
    <small>使用 Compose 已挂载到容器内的目录；新增宿主机目录需先添加挂载。</small></label
  >
  <ModalDialog
    v-if="open"
    title="选择容器 STRM 输出目录"
    @close="
      open = false;
      request.cancel();
    "
  >
    <p class="muted-text">以下为容器可见的挂载目录，选择不会移动或删除文件。</p>
    <div class="actions">
      <button class="button small" @click="browse()">全部挂载目录</button
      ><button
        v-if="request.data.value?.parent"
        class="button small"
        @click="browse(String(request.data.value.parent))"
      >
        <ArrowUp :size="14" />上一级
      </button>
    </div>
    <p class="path-box">{{ path || '请选择挂载根目录' }}</p>
    <LoadState
      :loading="request.loading.value"
      :error="request.error.value"
      @retry="browse(path)"
    />
    <p
      v-if="
        !path &&
        !request.loading.value &&
        !request.error.value &&
        request.data.value &&
        !records(request.data.value.roots).length
      "
      class="muted-text"
    >
      尚未发现可浏览的媒体挂载。请先在 Compose 中挂载输出目录，或手动填写已配置的容器路径。
    </p>
    <div class="directory-list">
      <button
        v-for="row in records(path ? request.data.value?.items : request.data.value?.roots)"
        :key="String(row.path)"
        class="folderrow"
        :disabled="row.available === false"
        @click="browse(String(row.path))"
      >
        <FolderOpen :size="17" /><span>{{ row.name || row.path }}</span
        ><ChevronRight :size="14" />
      </button>
    </div>
    <p
      v-if="
        path &&
        !request.loading.value &&
        !request.error.value &&
        !records(request.data.value?.items).length
      "
      class="muted-text"
    >
      此目录没有可选子目录。
    </p>
    <p v-if="request.data.value?.truncated" class="warning-text">
      目录较多，仅显示前 1000 项；可以直接填写完整输出路径。
    </p>
    <template #footer
      ><button
        class="button"
        @click="
          open = false;
          request.cancel();
        "
      >
        取消</button
      ><button
        class="button primary"
        :disabled="
          !path || request.loading.value || !!request.error.value || !request.data.value?.path
        "
        @click="select"
      >
        使用当前目录
      </button></template
    >
  </ModalDialog>
</template>
