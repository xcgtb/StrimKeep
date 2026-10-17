<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { get } from '../api/client';
import { compact, object, records, text } from '../api/domain';

const loading = ref(true);
const error = ref('');
const overview = ref<Record<string, unknown>>({});
const rows = ref<Record<string, unknown>[]>([]);

const dashboard = computed(() => object(overview.value.dashboard));
const services = computed(() => object(dashboard.value.services));
const maxTotal = computed(() =>
  rows.value.reduce((max, row) => Math.max(max, Number(row.total || 0)), 1),
);
const totalStrm = computed(() => rows.value.reduce((sum, row) => sum + Number(row.total || 0), 0));

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = object(await get<Record<string, unknown>>('/api/overview'));
    overview.value = data;
    rows.value = records(object(data.library_stats).rows).slice().sort(
      (a, b) => Number(b.total || 0) - Number(a.total || 0),
    );
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
function ok(value: unknown) {
  return value === true;
}
onMounted(load);
</script>
<template>
  <section class="tab active cinema-overview">
    <div class="cinema-hero">
      <div class="cinema-topline">
        <span>我的媒体库 / 媒体总览</span>
        <div>
          <span role="status">{{ loading ? '读取缓存中' : '已更新' }}</span>
          <button type="button" data-icon="refresh" @click="load">刷新总览</button>
        </div>
      </div>
      <div class="cinema-title">
        <span class="cinema-eyebrow">STRIMKEEP · YOUR COLLECTION</span>
        <h1>你的私人片库</h1>
        <p>片库规模、剧集完整度与治理进展，一眼掌握。</p>
      </div>
      <div class="cinema-numbers">
        <div>
          <span>本地片库</span>
          <b><span>{{ dashboard.localCount || '—' }}</span><i class="u">个</i></b>
          <small>STRM 文件</small>
        </div>
        <div>
          <span>分享片库</span>
          <b><span>{{ dashboard.shareCount || '—' }}</span><i class="u">个</i></b>
          <small>STRM 文件</small>
        </div>
        <div>
          <span>合计</span><b>{{ compact(totalStrm) }}</b>
          <small>两库 STRM</small>
        </div>
        <div>
          <span>分类</span><b>{{ rows.length }}</b><small>媒体分类</small>
        </div>
      </div>
    </div>

    <p v-if="error" class="section-note">{{ error }}</p>

    <section class="cinema-distribution">
      <div class="cinema-section-heading">
        <h2>媒体分布</h2>
        <span class="cinema-unit">STRM 文件</span>
      </div>
      <div class="cinema-categories">
        <button
          v-for="(row, index) in rows"
          :key="text(row.name)"
          type="button"
          class="cinema-category"
        >
          <span class="cinema-category-dot" :class="'tone-' + (index % 5)"></span>
          <span class="nm">{{ text(row.name) }}</span>
          <span class="tr" aria-hidden="true"
            ><i :class="'tone-' + (index % 5)" :style="{ width: (Number(row.total || 0) / maxTotal) * 100 + '%' }"></i
          ></span>
          <b>{{ compact(row.total) }}</b>
        </button>
        <div v-if="!rows.length" class="list-empty">{{ loading ? '读取中…' : '暂无分类数据' }}</div>
      </div>
      <p class="cinema-category-summary">两库合计 {{ compact(totalStrm) }} 个 STRM</p>
    </section>

    <div class="cinema-services">
      <span><i class="dd" :class="{ ok: ok(object(services.emby).ok) }"></i>Emby <b>{{ ok(object(services.emby).ok) ? '正常' : '异常' }}</b></span>
      <span>TMDB <b>{{ ok(object(services.tmdb).ok) ? '正常' : '异常' }}</b></span>
      <span>Telegram <b>{{ ok(object(services.telegram).ok) ? '正常' : '未配置' }}</b></span>
    </div>
  </section>
</template>
