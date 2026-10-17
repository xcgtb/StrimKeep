<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { get, post } from '../api/client';
import { compact, query, records, text } from '../api/domain';

const media = ref<'series' | 'movies'>('series');
const status = ref('all');
const scope = ref('all');
const keyword = ref('');
const cards = ref<Record<string, unknown>[]>([]);
const stats = ref<Record<string, number>>({});
const total = ref(0);
const loading = ref(false);
const error = ref('');

const statusLabels: Record<string, string> = {
  all: '全部',
  aligned: '完整',
  missing: '缺集',
  extra: '超集',
  ongoing: '在更',
  unmatched: '未匹配',
  no_tmdb: '未匹配 TMDB',
  pending: '待对照',
};

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await get<Record<string, unknown>>(
      query('/api/emby/library/page', {
        media: media.value,
        status: status.value,
        scope: scope.value,
        keyword: keyword.value,
        offset: 0,
        limit: 60,
      }),
    );
    cards.value = records(data.rows);
    stats.value = (data.stats as Record<string, number>) || {};
    total.value = Number(data.total || 0);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
function pick(target: 'media' | 'status' | 'scope', value: string) {
  if (target === 'media') media.value = value as 'series' | 'movies';
  if (target === 'status') status.value = value;
  if (target === 'scope') scope.value = value;
  void load();
}
function badge(card: Record<string, unknown>) {
  return statusLabels[text(card.status)] || text(card.status);
}
// 批量删除
const selectMode = ref(false);
const selected = ref<Set<string>>(new Set());
const previewing = ref(false);
const batchSummary = ref<Record<string, unknown>>({});
const batchMessage = ref('');

function toggleSelect(card: Record<string, unknown>) {
  const id = text(card.id || card.tmdb_id);
  const next = new Set(selected.value);
  next.has(id) ? next.delete(id) : next.add(id);
  selected.value = next;
}
async function previewBatch() {
  if (!selected.value.size) return;
  previewing.value = true;
  batchMessage.value = '';
  batchSummary.value = {};
  try {
    const data = await post<Record<string, unknown>>(
      `/api/emby/${media.value === 'series' ? 'series' : 'movies'}/batch-delete/preview`,
      { ids: [...selected.value] },
    );
    batchSummary.value = data;
    batchMessage.value = `已核验 ${compact(data.count || data.strm_count || 0)} 个 STRM`;
  } catch (e) {
    batchMessage.value = (e as Error).message;
  } finally {
    previewing.value = false;
  }
}
async function executeBatch() {
  try {
    await post<Record<string, unknown>>(
      `/api/emby/${media.value === 'series' ? 'series' : 'movies'}/batch-delete/execute`,
      { ids: [...selected.value] },
    );
    batchMessage.value = '已提交批量删除任务';
    selected.value = new Set();
    batchSummary.value = {};
    selectMode.value = false;
    await load();
  } catch (e) {
    batchMessage.value = (e as Error).message;
  }
}
onMounted(load);
</script>
<template>
  <section class="tab active">
    <div class="map-r1">
      <div class="segmented">
        <button :class="{ active: media === 'series' }" @click="pick('media', 'series')">
          剧集 <i class="n">{{ compact(stats.total_series) }}</i>
        </button>
        <button :class="{ active: media === 'movies' }" @click="pick('media', 'movies')">
          电影 <i class="n">{{ compact(stats.total_movies) }}</i>
        </button>
      </div>
      <input
        v-model="keyword"
        class="map-search"
        type="text"
        placeholder="搜索片名..."
        @keydown.enter="load()"
      />
      <button class="btn gray" data-icon="refresh" @click="load()">读缓存</button>
      <button class="btn" data-icon="refresh" @click="load()">重新对照</button>
    </div>

    <div class="map-r2">
      <div class="segmented">
        <button :class="{ active: status === 'all' }" @click="pick('status', 'all')">
          全部 <i class="n">{{ compact(total) }}</i>
        </button>
        <button
          v-if="media === 'series'"
          :class="{ active: status === 'aligned' }"
          @click="pick('status', 'aligned')"
        >
          <b class="dd ok"></b>完整 <i class="n">{{ compact(stats.aligned) }}</i>
        </button>
        <button
          v-if="media === 'series'"
          :class="{ active: status === 'missing' }"
          @click="pick('status', 'missing')"
        >
          <b class="dd err"></b>缺集 <i class="n">{{ compact(stats.missing) }}</i>
        </button>
        <button
          v-if="media === 'series'"
          :class="{ active: status === 'extra' }"
          @click="pick('status', 'extra')"
        >
          <b class="dd warn"></b>超集 <i class="n">{{ compact(stats.extra) }}</i>
        </button>
        <button
          v-if="media === 'series'"
          :class="{ active: status === 'ongoing' }"
          @click="pick('status', 'ongoing')"
        >
          <b class="dd info"></b>在更 <i class="n">{{ compact(stats.ongoing) }}</i>
        </button>
        <button :class="{ active: status === 'unmatched' }" @click="pick('status', 'unmatched')">
          <b class="dd dim"></b>未匹配 <i class="n">{{ compact(stats.unmatched) }}</i>
        </button>
      </div>
      <div class="segmented">
        <button :class="{ active: scope === 'all' }" @click="pick('scope', 'all')">本地+分享</button>
        <button :class="{ active: scope === 'local' }" @click="pick('scope', 'local')">仅本地</button>
        <button :class="{ active: scope === 'share' }" @click="pick('scope', 'share')">仅分享</button>
      </div>
    </div>

    <p v-if="error" class="section-note">{{ error }}</p>

    <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 8px 0">
      <button class="btn gray" :class="{ active: selectMode }" @click="selectMode = !selectMode; selected = new Set()">
        {{ selectMode ? '退出多选' : '多选清理' }}
      </button>
      <template v-if="selectMode">
        <span class="hint">已选 {{ selected.size }} 部</span>
        <button class="btn" :disabled="!selected.size || previewing" @click="previewBatch">
          {{ previewing ? '核验中…' : '预览批量删除' }}
        </button>
        <button
          v-if="Number(batchSummary.count || batchSummary.strm_count || 0) > 0"
          class="btn danger"
          @click="executeBatch"
        >
          确认执行删除
        </button>
      </template>
    </div>
    <p v-if="batchMessage" class="section-note" role="status">{{ batchMessage }}</p>

    <div class="poster-grid">
      <article v-for="card in cards" :key="text(card.id)" class="poster-card" :style="selectMode && selected.has(text(card.id || card.tmdb_id)) ? 'outline:2px solid var(--brand);outline-offset:-2px' : ''" @click="selectMode && toggleSelect(card)">
        <div class="poster-wrap">
          <img v-if="card.poster" :src="text(card.poster)" :alt="text(card.name)" loading="lazy" />
          <span class="badge-type">{{ media === 'series' ? '剧集' : '电影' }}</span>
          <span class="badge-status">{{ badge(card) }}</span>
        </div>
        <div class="info">
          <div class="t">{{ text(card.name) }}</div>
          <div class="meta">
            <span
              >{{ text(card.year) }} · 本地 {{ text(card.local_eps || '—') }} / 分享
              {{ text(card.share_eps || '—') }}</span
            >
          </div>
        </div>
      </article>
    </div>
    <div v-if="!cards.length && !loading" class="list-empty">没有符合条件的作品</div>
  </section>
</template>
