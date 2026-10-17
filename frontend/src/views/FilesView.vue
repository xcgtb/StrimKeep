<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { get, query } from '../api/client';
import { object, records, text } from '../api/domain';

// 结构对齐 1.4.7 的 cloud-workbench：.cw-file-row / .cw-file-name / .cw-file-meta，
// 接口沿用 1.5.9 的 /api/cloud/files（account_id / cid / offset），两者参数一致。
const accounts = ref<Record<string, unknown>[]>([]);
const accountId = ref('');
const cid = ref('0');
const trail = ref<{ id: string; name: string }[]>([{ id: '0', name: '全部文件' }]);
const rows = ref<Record<string, unknown>[]>([]);
const total = ref(0);
const offset = ref(0);
const hasMore = ref(false);
const loading = ref(false);
const error = ref('');

const currentDir = computed(() => trail.value[trail.value.length - 1]);

function size(value: unknown) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes <= 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  let size = bytes;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}
function extension(name: string) {
  if (!name.includes('.')) return '文件';
  return name.split('.').pop()?.toUpperCase().slice(0, 12) || '文件';
}

async function loadAccounts() {
  try {
    const data = object(await get<Record<string, unknown>>('/api/cloud/status'));
    accounts.value = records(data.accounts || data.items);
    if (!accountId.value && accounts.value.length) accountId.value = text(accounts.value[0]!.id);
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function load(more = false) {
  if (!accountId.value) return;
  loading.value = true;
  error.value = '';
  const next = more ? offset.value : 0;
  try {
    const data = object(
      await get<Record<string, unknown>>(
        query('/api/cloud/files', { account_id: accountId.value, cid: cid.value, offset: next }),
      ),
    );
    const items = records(data.items);
    rows.value = more ? [...rows.value, ...items] : items;
    total.value = Number(data.total || 0);
    offset.value = Number(data.offset ?? next) + items.length;
    hasMore.value = data.has_more === true;
  } catch (e) {
    error.value = (e as Error).message;
    rows.value = [];
  } finally {
    loading.value = false;
  }
}
function enter(row: Record<string, unknown>) {
  if (row.is_dir !== true) return;
  trail.value = [...trail.value, { id: text(row.id), name: text(row.name) }];
  cid.value = text(row.id);
  offset.value = 0;
  void load();
}
function goTo(index: number) {
  trail.value = trail.value.slice(0, index + 1);
  cid.value = trail.value[index]!.id;
  offset.value = 0;
  void load();
}
watch(accountId, () => {
  trail.value = [{ id: '0', name: '全部文件' }];
  cid.value = '0';
  offset.value = 0;
  void load();
});
onMounted(async () => {
  await loadAccounts();
  if (accountId.value) await load();
});
</script>
<template>
  <section class="panel">
    <div class="row-between">
      <h3 data-icon="folder">115 文件管理</h3>
      <div style="display: flex; gap: 8px; align-items: center; flex: 0 0 auto">
        <select v-model="accountId" aria-label="115 账号">
          <option v-for="account in accounts" :key="text(account.id)" :value="text(account.id)">
            {{ text(account.name || account.id) }}
          </option>
        </select>
        <button
          class="btn gray"
          style="white-space: nowrap; flex: 0 0 auto"
          data-icon="refresh"
          :disabled="loading"
          @click="load()"
        >
          {{ loading ? '读取中…' : '刷新' }}
        </button>
      </div>
    </div>

    <p class="hint">根目录 · {{ total }} 项 · 当前：{{ currentDir.name }}</p>
    <p v-if="error" class="hint">{{ error }}</p>

    <div>
      <button v-if="trail.length > 1" class="btn gray" @click="goTo(trail.length - 2)">
        ↑ 返回上级
      </button>
      <div v-if="!rows.length && !loading" class="list-empty">
        {{ accountId ? '此目录为空' : '请先添加 115 账号' }}
      </div>

      <div
        v-for="row in rows"
        :key="text(row.id)"
        class="cw-file-row"
        :class="{ 'cw-file-leaf': row.is_dir !== true }"
        :style="{ cursor: row.is_dir === true ? 'pointer' : 'default' }"
        @click="enter(row)"
      >
        <span class="cw-file-name">
          <span class="cw-file-icon" aria-hidden="true">{{ row.is_dir === true ? '▸' : '' }}</span>
          {{ text(row.name) }}
        </span>
        <span class="cw-file-meta">
          {{ row.is_dir === true ? '目录' : `${extension(text(row.name))} · ${size(row.size)}` }}
        </span>
        <code>ID {{ text(row.id) }}</code>
      </div>
    </div>

    <div class="cw-file-pagination">
      <button class="btn gray" :disabled="offset === rows.length || !rows.length" @click="offset = 0; load()">
        回到第一页
      </button>
      <span class="hint">已加载 {{ rows.length }} / {{ total }}</span>
      <button class="btn gray" :disabled="!hasMore || loading" @click="load(true)">下一页</button>
    </div>
  </section>
</template>
