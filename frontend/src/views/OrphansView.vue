<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { get, post } from '../api/client';
import { compact, object, records, text } from '../api/domain';

const path = ref('');
const scope = ref('all');
const limit = ref(100);
const scanning = ref(false);
const cleaning = ref(false);
const message = ref('');
const error = ref('');
const candidates = ref<Record<string, unknown>[]>([]);
const selected = ref<Set<string>>(new Set());
const scanned = ref(false);

const selection = computed(() => [...selected.value]);
const allSelected = computed(
  () => candidates.value.length > 0 && selection.value.length === candidates.value.length,
);

async function load() {
  // 目录清理没有列表接口，进入页面先不扫描，避免大库空转
  candidates.value = [];
  scanned.value = false;
  message.value = '点击「预览扫描」开始，更换范围只选择目录。';
}
async function scan() {
  scanning.value = true;
  error.value = '';
  message.value = '正在扫描，媒体库较大时可能需要几十秒…';
  try {
    const data = object(
      await post<Record<string, unknown>>('/api/wash/empty-dirs/scan', {
        path: scope.value === 'all' ? '' : path.value,
        scope: scope.value,
        limit: limit.value,
      }),
    );
    candidates.value = records(data.preview || data.items || data.dirs);
    scanned.value = true;
    selected.value = new Set();
    message.value = `扫描完成：命中 ${candidates.value.length} 项（上限 ${limit.value}）`;
  } catch (e) {
    error.value = (e as Error).message;
    message.value = '';
  } finally {
    scanning.value = false;
  }
}
function toggle(item: Record<string, unknown>) {
  const key = text(item.path);
  const next = new Set(selected.value);
  next.has(key) ? next.delete(key) : next.add(key);
  selected.value = next;
}
function toggleAll() {
  selected.value = allSelected.value
    ? new Set()
    : new Set(candidates.value.map((item) => text(item.path)));
}
async function clean() {
  if (!selection.value.length) return;
  cleaning.value = true;
  error.value = '';
  try {
    const items = candidates.value.filter((item) => selected.value.has(text(item.path)));
    await post<Record<string, unknown>>('/api/wash/empty-dirs/clean', { items });
    message.value = `已提交清理 ${items.length} 项`;
    await scan();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    cleaning.value = false;
  }
}
onMounted(load);
</script>
<template>
  <section class="tab active">
    <div class="panel">
      <h3 data-icon="folder">目录清理</h3>
      <p class="section-note">
        只扫描选中目录及其子目录，找出为空或只含 NFO、图片、字幕等已知附属文件的媒体目录（{tmdb-xxx} 命名）。
        含 STRM、视频、未知文件或软链接的目录会保留；读取失败时扫描会报错。
      </p>
      <p class="section-note">可扫描整个媒体库（本地库＋分享库），也可单独选择一个库、分类或片名目录。</p>
      <p class="section-note">
        预览只扫描 NAS。本地库执行清理时，通过 CD2 清理对应 115 目录的附属文件与空目录；分享库只清理 NAS。
      </p>

      <div style="display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px">
        <select v-model="scope" style="padding: 6px 10px">
          <option value="all">整个媒体库</option>
          <option value="local">仅本地库</option>
          <option value="share">仅分享库</option>
        </select>
        <input
          v-model="path"
          :disabled="scope === 'all'"
          placeholder="可选：指定子目录绝对路径"
          style="padding: 6px 10px; min-width: 220px"
        />
        <input v-model.number="limit" type="number" min="1" max="1000" style="width: 84px; padding: 6px 10px" />
        <button class="btn" :disabled="scanning" data-icon="search" @click="scan">
          {{ scanning ? '扫描中…' : '预览扫描' }}
        </button>
        <button
          class="btn danger"
          :disabled="cleaning || !selection.length"
          data-icon="trash"
          @click="clean"
        >
          {{ cleaning ? '清理中…' : `执行清理（${selection.length}）` }}
        </button>
      </div>

      <div class="section-note" role="status">{{ message }}</div>
      <p v-if="error" class="section-note">{{ error }}</p>
      <div v-if="scanned" class="section-note">
        命中 {{ compact(candidates.length) }} 项
        <button v-if="candidates.length" class="btn gray sm" style="margin-left: 10px" @click="toggleAll">
          {{ allSelected ? '取消全选' : '全选' }}
        </button>
      </div>
    </div>

    <div class="panel">
      <div v-if="!candidates.length" class="list-empty">
        {{ scanned ? '没有发现可清理的空目录或附属文件残留' : '选择整个媒体库、单个库或子目录后预览扫描' }}
      </div>
      <label v-for="item in candidates" :key="text(item.path)" class="plan-row" style="cursor: pointer">
        <input
          type="checkbox"
          :checked="selected.has(text(item.path))"
          @change="toggle(item)"
          style="margin-right: 8px"
        />
        <span>{{ text(item.path) }}</span>
        <span style="color: var(--text-mid); display: block; font-size: 12px">
          {{ text(item.rule || item.reason || item.type) }} · {{ item.lib === 'local' ? '本地库' : '分享库' }}
        </span>
      </label>
    </div>
  </section>
</template>
