<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { Folder, FileVideo, File, ChevronRight, ArrowUp, RefreshCw, HardDrive } from '@lucide/vue';
import { get, query } from '../api/client';
import { useRequest } from '../composables/useRequest';
import type { CloudAccount, CloudFile, FilePage } from '../types/api';
import LoadState from '../components/LoadState.vue';
const accounts = useRequest<{ accounts: CloudAccount[] }>(),
  files = useRequest<FilePage>(),
  accountId = ref(''),
  cid = ref('0'),
  rows = ref<CloudFile[]>([]),
  offset = ref(0),
  breadcrumbs = ref([{ cid: '0', name: '全部文件' }]);
async function load(more = false) {
  if (!accountId.value) return;
  if (!more) rows.value = [];
  const result = await files.run((signal) =>
    get<FilePage>(
      query('/api/cloud/files', {
        account_id: accountId.value,
        cid: cid.value,
        offset: more ? offset.value : 0,
      }),
      signal,
    ),
  );
  if (result) {
    rows.value = more ? [...rows.value, ...result.items] : result.items;
    offset.value = (result.offset || 0) + result.items.length;
  }
}
function enter(file: CloudFile) {
  if (!file.is_dir) return;
  cid.value = file.id;
  breadcrumbs.value.push({ cid: file.id, name: file.name });
  void load();
}
function navigate(index: number) {
  const point = breadcrumbs.value[index]!;
  cid.value = point.cid;
  breadcrumbs.value = breadcrumbs.value.slice(0, index + 1);
  void load();
}
function size(value?: number) {
  if (value == null) return '—';
  if (value < 1024) return `${value} B`;
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4);
  return `${(value / 1024 ** unit).toFixed(1)} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`;
}
async function loadAccounts() {
  const result = await accounts.run((signal) =>
    get<{ accounts: CloudAccount[] }>('/api/cloud/status', signal),
  );
  if (result?.accounts.length) accountId.value = result.accounts[0]!.id;
}
watch(accountId, () => {
  files.cancel();
  rows.value = [];
  files.data.value = undefined;
  breadcrumbs.value = [{ cid: '0', name: '全部文件' }];
  cid.value = '0';
  void load();
});
onMounted(loadAccounts);
</script>
<template>
  <section class="page-heading">
    <div>
      <span class="eyebrow">CLOUD RESOURCES</span>
      <h1>115 文件管理</h1>
      <p>浏览网盘目录和文件，清楚查看每一级的实际内容。</p>
    </div>
    <select
      v-if="accounts.data.value?.accounts.length"
      v-model="accountId"
      aria-label="选择网盘账号"
    >
      <option v-for="account in accounts.data.value.accounts" :key="account.id" :value="account.id">
        {{ account.name || account.uid || account.id }}
      </option>
    </select>
  </section>
  <LoadState
    :loading="accounts.loading.value"
    :error="accounts.error.value"
    :empty="!accounts.loading.value && !accounts.data.value?.accounts.length"
    message="尚未连接 115 账号"
    @retry="loadAccounts"
  />
  <section v-if="accountId" class="panel file-panel">
    <div class="file-toolbar">
      <button
        class="icon-button"
        aria-label="返回上级目录"
        :disabled="breadcrumbs.length < 2 || files.loading.value"
        @click="navigate(breadcrumbs.length - 2)"
      >
        <ArrowUp :size="18" />
      </button>
      <nav class="file-breadcrumb" aria-label="文件目录">
        <template v-for="(point, index) in breadcrumbs" :key="point.cid"
          ><ChevronRight v-if="index" :size="14" /><button @click="navigate(index)">
            {{ point.name }}
          </button></template
        >
      </nav>
      <button
        class="icon-button push-right"
        aria-label="刷新目录"
        :disabled="files.loading.value"
        @click="load()"
      >
        <RefreshCw :size="17" />
      </button>
    </div>
    <div class="file-summary">
      <HardDrive :size="15" /><span
        >当前目录 {{ breadcrumbs[breadcrumbs.length - 1]?.name }} ·
        {{ files.data.value?.total ?? '—' }} 项</span
      >
    </div>
    <LoadState
      :loading="files.loading.value && !rows.length"
      :error="files.error.value"
      :empty="!files.loading.value && !rows.length"
      message="当前目录为空"
      @retry="load"
    />
    <div v-if="rows.length" class="file-table">
      <div class="file-table-head"><span>名称</span><span>类型</span><span>大小</span></div>
      <div v-for="file in rows" :key="file.id" class="file-row">
        <button v-if="file.is_dir" class="file-name" @click="enter(file)">
          <Folder :size="21" class="folder-icon" /><strong>{{ file.name }}</strong
          ><ChevronRight :size="15" /></button
        ><span v-else class="file-name"
          ><FileVideo
            v-if="/\.(mkv|mp4|avi|mov|ts)$/i.test(file.name)"
            :size="21"
            class="accent"
          /><File v-else :size="21" /><span>{{ file.name }}</span></span
        ><span>{{ file.is_dir ? '文件夹' : '文件' }}</span
        ><span>{{ file.is_dir ? '—' : size(file.size) }}</span>
      </div>
    </div>
    <div v-if="files.data.value?.has_more" class="load-more">
      <button class="button" :disabled="files.loading.value" @click="load(true)">
        加载更多文件
      </button>
    </div>
  </section>
</template>
