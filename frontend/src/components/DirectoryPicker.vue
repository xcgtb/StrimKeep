<script setup lang="ts">
import { ref, watch } from 'vue';
import { get, query } from '../api/client';
import { useRequest } from '../composables/useRequest';
import type { FilePage, CloudFile } from '../types/api';
import ModalDialog from './ModalDialog.vue';
import LoadState from './LoadState.vue';
const props = defineProps<{
  accountId: string;
  modelValue?: string | null;
  label?: string;
  name?: string;
}>();
const emit = defineEmits<{
  'update:modelValue': [value: string];
  selected: [value: { id: string; name: string }];
}>();
const open = ref(false),
  cid = ref('0'),
  crumbs = ref([{ id: '0', name: '根目录' }]),
  files = useRequest<FilePage>();
const items = ref<CloudFile[]>([]),
  offset = ref(0);
async function load(more = false) {
  const r = await files.run((signal) =>
    get<FilePage>(
      query('/api/cloud/files', {
        account_id: props.accountId,
        cid: cid.value,
        offset: more ? offset.value : 0,
      }),
      signal,
    ),
  );
  if (r) {
    items.value = more ? [...items.value, ...r.items] : r.items;
    offset.value = (r.offset || 0) + r.items.length;
  }
}
function show() {
  open.value = true;
  cid.value = '0';
  crumbs.value = [{ id: '0', name: '根目录' }];
  items.value = [];
  void load();
}
function enter(f: CloudFile) {
  if (!f.is_dir) return;
  cid.value = f.id;
  crumbs.value.push({ id: f.id, name: f.name });
  items.value = [];
  void load();
}
function back(i: number) {
  crumbs.value = crumbs.value.slice(0, i + 1);
  cid.value = crumbs.value[i]!.id;
  void load();
}
function select() {
  const value = { id: cid.value, name: crumbs.value.map((c) => c.name).join(' / ') };
  emit('update:modelValue', value.id);
  emit('selected', value);
  open.value = false;
  files.cancel();
}
watch(
  () => props.accountId,
  () => {
    open.value = false;
    files.cancel();
    items.value = [];
  },
);
</script>
<template>
  <div class="directory-field">
    <label
      >{{ label || '115 目录' }}
      <div class="directory-selection">
        <input
          :value="name || (modelValue ? `已选目录 · ${modelValue}` : '尚未选择目录')"
          readonly
        /><button class="button" type="button" :disabled="!accountId" @click="show">
          选择目录
        </button>
      </div></label
    >
  </div>
  <ModalDialog
    v-if="open"
    title="选择 115 目录"
    @close="
      open = false;
      files.cancel();
    "
    ><nav class="file-breadcrumb">
      <button v-for="(c, i) in crumbs" :key="c.id" @click="back(i)">{{ c.name }} /</button>
    </nav>
    <p class="notice-bar">当前选择：{{ crumbs.map((c) => c.name).join(' / ') }}</p>
    <LoadState
      :loading="files.loading.value && !items.length"
      :error="files.error.value"
      :empty="!files.loading.value && !items.some((f) => f.is_dir)"
      message="当前目录没有子文件夹，仍可选择当前目录"
      @retry="load()"
    />
    <div class="directory-list">
      <button v-for="f in items.filter((f) => f.is_dir)" :key="f.id" @click="enter(f)">
        <span>📁 {{ f.name }}</span
        ><span>进入 →</span>
      </button>
    </div>
    <button
      v-if="files.data.value?.has_more"
      class="button"
      :disabled="files.loading.value"
      @click="load(true)"
    >
      更多目录</button
    ><template #footer
      ><button class="button" @click="open = false">取消</button
      ><button
        class="button primary"
        :disabled="files.loading.value || !!files.error.value"
        @click="select"
      >
        使用当前目录
      </button></template
    ></ModalDialog
  >
</template>
