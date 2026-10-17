<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { get } from '../api/client';
import { compact, date, object, text } from '../api/domain';

const stats = ref<Record<string, unknown>>({});
const summary = ref('');
const cacheTs = ref(0);
const fromCache = ref(false);
const loading = ref(false);
const error = ref('');

const rows = computed(() => {
  const source = object(stats.value);
  return [
    { label: '电影', value: source.movies },
    { label: '剧集', value: source.series },
    { label: '集数', value: source.episodes },
  ];
});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = object(await get<Record<string, unknown>>('/api/stats'));
    stats.value = object(data.stats);
    summary.value = text(data.text);
    cacheTs.value = Number(data.cache_ts || 0);
    fromCache.value = data.from_cache === true;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
onMounted(load);
</script>
<template>
  <section class="tab active">
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="file">近 24 小时入库</h3>
        <div style="display: flex; gap: 8px; flex-wrap: wrap">
          <button class="btn gray" data-icon="refresh" :disabled="loading" @click="load">
            {{ loading ? '读取中…' : '读缓存' }}
          </button>
        </div>
      </div>
      <div class="section-note">
        {{ cacheTs ? `快照 ${date(cacheTs)}${fromCache ? ' · 来自缓存' : ''}` : '—' }}
      </div>
      <p v-if="error" class="section-note">{{ error }}</p>
    </div>

    <div class="cards">
      <div v-for="row in rows" :key="row.label" class="card">
        <div class="label">{{ row.label }}</div>
        <div class="value">{{ compact(row.value) }}</div>
        <div class="sub">近 24 小时</div>
      </div>
    </div>

    <div class="panel">
      <h3>入库速报</h3>
      <pre v-if="summary" class="section-note" style="white-space: pre-wrap; font-family: inherit">{{
        summary
      }}</pre>
      <div v-else class="list-empty">{{ loading ? '读取中…' : '暂无 24 小时入库数据' }}</div>
    </div>
  </section>
</template>
