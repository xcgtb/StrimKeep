<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, reactive, watch, computed } from 'vue';
import { useRoute } from 'vue-router';
import { Search, ArrowDown, Compass, ChevronDown } from '@lucide/vue';
import { mediaApi } from '../api/media';
import { useRequest } from '../composables/useRequest';
import { useLibraryUpdates } from '../composables/useLibraryUpdates';
import { useSubscriptions } from '../stores/subscriptions';
import { useUi } from '../stores/ui';
import { titleOf, posterUrl, badge } from '../composables/format';
import type { MediaCard, ExploreResponse } from '../types/api';
import PosterCard from '../components/PosterCard.vue';
import LoadState from '../components/LoadState.vue';
import ModalDialog from '../components/ModalDialog.vue';
import ResourceSearch from '../components/ResourceSearch.vue';

const resourceCard = ref<MediaCard>();
const route = useRoute(),
  ui = useUi(),
  subscriptions = useSubscriptions(),
  request = useRequest<ExploreResponse>();

// classic 筛选选项（与后端 engine.GENRE_MAP 对齐）
const REGIONS = [
  { v: 'all', label: '全部' },
  { v: 'cn', label: '大陆' },
  { v: 'hk', label: '香港' },
  { v: 'tw', label: '台湾' },
  { v: 'jp', label: '日本' },
  { v: 'kr', label: '韩国' },
  { v: 'us', label: '欧美' },
];
const MEDIA_OPTIONS = [
  { v: 'all', label: '全部' },
  { v: 'movie', label: '电影' },
  { v: 'tv', label: '剧集' },
];
const SORT_OPTIONS = [
  { v: 'popularity', label: '热度' },
  { v: 'release', label: '最新' },
  { v: 'rating', label: 'TMDB 评分' },
];
const GENRES = [
  { v: '', media: 'both', label: '全部' },
  { v: '动作', media: 'both', label: '动作' },
  { v: '冒险', media: 'movie', label: '冒险' },
  { v: '喜剧', media: 'both', label: '喜剧' },
  { v: '犯罪', media: 'both', label: '犯罪' },
  { v: '纪录', media: 'both', label: '纪录' },
  { v: '剧情', media: 'both', label: '剧情' },
  { v: '家庭', media: 'both', label: '家庭' },
  { v: '奇幻', media: 'movie', label: '奇幻' },
  { v: '历史', media: 'movie', label: '历史' },
  { v: '恐怖', media: 'movie', label: '恐怖' },
  { v: '音乐', media: 'movie', label: '音乐' },
  { v: '悬疑', media: 'both', label: '悬疑' },
  { v: '爱情', media: 'movie', label: '爱情' },
  { v: '科幻', media: 'both', label: '科幻' },
  { v: '惊悚', media: 'movie', label: '惊悚' },
  { v: '战争', media: 'both', label: '战争' },
  { v: '西部', media: 'both', label: '西部' },
  { v: '动画', media: 'both', label: '动画' },
  { v: '儿童', media: 'tv', label: '儿童' },
  { v: '新闻', media: 'tv', label: '新闻' },
  { v: '真人秀', media: 'tv', label: '真人秀' },
  { v: '肥皂剧', media: 'tv', label: '肥皂剧' },
  { v: '脱口秀', media: 'tv', label: '脱口秀' },
  { v: '电视电影', media: 'movie', label: '电视电影' },
];

const filters = reactive({
  media: String(route.query.media || 'all'),
  region: 'all',
  sort: 'popularity',
  year: '',
  genre: '',
  q: String(route.query.q || ''),
});
const search = ref(filters.q),
  showFilters = ref(false),
  cards = ref<MediaCard[]>([]),
  page = ref(1),
  totalPages = ref(1),
  selected = ref<MediaCard>(),
  libraryNotice = ref(''),
  subscriptionError = ref('');
let badgeController: AbortController | undefined,
  generation = 0;

// 题材 chip 随「类型」联动：隐藏不适用题材，类型切换后若当前题材失效则重置
const genreOptions = computed(() =>
  GENRES.filter((g) => g.media === 'both' || filters.media === 'all' || g.media === filters.media),
);
const yearOptions = computed(() => {
  const years: number[] = [];
  for (let y = new Date().getFullYear() + 1; y >= 1900; y--) years.push(y);
  return years;
});
const filterSummaryText = computed(() => {
  const region = REGIONS.find((r) => r.v === filters.region)?.label || '全部地区';
  const media = MEDIA_OPTIONS.find((m) => m.v === filters.media)?.label || '电影';
  const genre = filters.genre || '全题材';
  const year = filters.year ? `${filters.year} 年` : '全部年份';
  const sort = SORT_OPTIONS.find((s) => s.v === filters.sort)?.label || '热度';
  return [region, media, genre, year, sort].join(' · ');
});

type FilterKey = 'media' | 'region' | 'sort' | 'year' | 'genre';
function selectFilter(key: FilterKey, value: string) {
  if (key === 'media') {
    filters.media = value;
    const active = GENRES.find((g) => g.v === filters.genre);
    if (active && active.media !== 'both' && active.media !== value && value !== 'all')
      filters.genre = '';
  } else {
    filters[key] = value;
  }
  // 切筛选时清空搜索，回到浏览模式（与 classic 一致）
  filters.q = '';
  search.value = '';
}

async function load(more = false) {
  const version = ++generation;
  badgeController?.abort();
  const target = more ? page.value + 1 : 1;
  if (!more) {
    cards.value = [];
    page.value = 1;
    totalPages.value = 1;
  }
  const response = await request.run((signal) =>
    mediaApi.explore({ ...filters, page: target }, signal),
  );
  if (!response || version !== generation) return;
  if (response.status === 'pending') {
    libraryNotice.value = '影视数据正在后台加载，请稍后重新加载';
    return;
  }
  const combined = more ? [...cards.value, ...(response.cards || [])] : response.cards || [];
  cards.value = [...new Map(combined.map((c) => [`${c.type}:${c.tmdb_id}`, c])).values()];
  page.value = target;
  totalPages.value = response.total_pages || 1;
  libraryNotice.value = response.library_error
    ? '片库状态同步失败，当前角标可能未更新。'
    : response.library_refreshing
      ? '片库状态后台同步中'
      : '';
}
async function updateBadges() {
  if (!cards.value.length || request.loading.value) return;
  const version = generation;
  badgeController?.abort();
  badgeController = new AbortController();
  try {
    const updates = new Map<string, MediaCard>();
    let response: ExploreResponse = {};
    for (let offset = 0; offset < cards.value.length; offset += 200) {
      response = await mediaApi.badges(
        cards.value.slice(offset, offset + 200),
        badgeController.signal,
      );
      if (version !== generation) return false;
      for (const card of response.cards || []) updates.set(`${card.type}:${card.tmdb_id}`, card);
    }
    if (version !== generation) return;
    cards.value = cards.value.map((c) => ({ ...c, ...updates.get(`${c.type}:${c.tmdb_id}`) }));
    libraryNotice.value = response.library_error
      ? '片库状态同步失败，请稍后重试。'
      : response.library_refreshing
        ? '片库状态后台同步中'
        : '';
  } catch (e) {
    if (!(e instanceof DOMException && e.name === 'AbortError') && version === generation)
      libraryNotice.value = (e as Error).message;
    return false;
  }
}
async function toggle(card: MediaCard) {
  try {
    await subscriptions.toggle(card);
    ui.notify(subscriptions.has(card.tmdb_id) ? '已添加追更订阅' : '已移除追更订阅', 'success');
  } catch (e) {
    ui.notify((e as Error).message, 'error');
  }
}
async function loadSubscriptions() {
  try {
    await subscriptions.load();
    subscriptionError.value = '';
  } catch (e) {
    subscriptionError.value = (e as Error).message;
  }
}
function submitSearch() {
  const q = search.value.trim();
  if (filters.q === q) void load();
  else filters.q = q;
}
watch(filters, () => void load());
watch(
  () => route.query,
  (query) => {
    search.value = String(query.q || '');
    filters.q = search.value;
    filters.media = String(query.media || 'all');
  },
);
onMounted(() => {
  void load();
  void loadSubscriptions();
});
onBeforeUnmount(() => badgeController?.abort());
const live = useLibraryUpdates(async () => {
  if (request.loading.value || !cards.value.length) return false;
  return updateBadges();
});
</script>
<template>
  <section class="page-heading">
    <div>
      <span class="eyebrow">MEDIA DISCOVERY</span>
      <h1>影视探索</h1>
      <p>发现值得收藏的作品，随时掌握片库与追更状态。</p>
    </div>
    <span class="heading-symbol"><Compass :size="30" /></span>
  </section>

  <form class="search-hero" @submit.prevent="submitSearch()">
    <Search :size="18" />
    <input
      v-model="search"
      aria-label="搜索影视作品"
      placeholder="搜索 TMDB 电影/剧集，如「三体」「Oppenheimer」"
    />
    <button class="search-btn" type="submit"><Search :size="15" />搜索</button>
  </form>

  <div class="filter-summary" :class="{ open: showFilters }">
    <span class="fs-text">{{ filterSummaryText }}</span>
    <button
      class="fs-toggle"
      type="button"
      :aria-expanded="showFilters"
      @click="showFilters = !showFilters"
    >
      <span>{{ showFilters ? '收起筛选' : '展开筛选' }}</span
      ><ChevronDown :size="14" />
    </button>
  </div>

  <div v-if="showFilters" class="explore-filters">
    <div class="filter-row">
      <span class="filter-label">地区</span>
      <div class="filter-chips">
        <button
          v-for="opt in REGIONS"
          :key="opt.v"
          class="chip"
          :class="{ active: filters.region === opt.v }"
          @click="selectFilter('region', opt.v)"
        >
          {{ opt.label }}
        </button>
      </div>
    </div>
    <div class="filter-row">
      <span class="filter-label">类型</span>
      <div class="filter-chips">
        <button
          v-for="opt in MEDIA_OPTIONS"
          :key="opt.v"
          class="chip"
          :class="{ active: filters.media === opt.v }"
          @click="selectFilter('media', opt.v)"
        >
          {{ opt.label }}
        </button>
      </div>
    </div>
    <div class="filter-row">
      <span class="filter-label">题材</span>
      <div class="filter-chips">
        <button
          v-for="opt in genreOptions"
          :key="opt.v"
          class="chip"
          :class="{ active: filters.genre === opt.v }"
          @click="selectFilter('genre', opt.v)"
        >
          {{ opt.label }}
        </button>
      </div>
    </div>
    <div class="filter-row">
      <span class="filter-label">年份</span>
      <div class="filter-chips year-scroll">
        <button
          class="chip"
          :class="{ active: filters.year === '' }"
          @click="selectFilter('year', '')"
        >
          全部
        </button>
        <button
          v-for="year in yearOptions"
          :key="year"
          class="chip"
          :class="{ active: filters.year === String(year) }"
          @click="selectFilter('year', String(year))"
        >
          {{ year }}
        </button>
      </div>
    </div>
    <div class="filter-row">
      <span class="filter-label">排序</span>
      <div class="filter-chips">
        <button
          v-for="opt in SORT_OPTIONS"
          :key="opt.v"
          class="chip"
          :class="{ active: filters.sort === opt.v }"
          @click="selectFilter('sort', opt.v)"
        >
          {{ opt.label }}
        </button>
      </div>
    </div>
  </div>

  <p v-if="filters.media === 'all' && !filters.q" class="notice-bar">
    输入片名可同时搜索电影与剧集；当前浏览展示热门电影。
  </p>
  <p v-if="libraryNotice" class="notice-bar" role="status">{{ libraryNotice }}</p>
  <p v-if="live.error.value" class="notice-bar">{{ live.error.value }}</p>
  <p v-if="subscriptionError || subscriptions.uncertain" class="notice-bar warning-text">
    {{ subscriptionError || '订阅保存结果待核实，请重新读取订阅后再操作。'
    }}<button class="text-link" @click="loadSubscriptions">重新读取订阅</button>
  </p>

  <LoadState
    :loading="request.loading.value && !cards.length"
    :error="request.error.value"
    :empty="!request.loading.value && !cards.length"
    message="没有找到相关作品"
    @retry="load(page > 1)"
  />

  <div class="poster-grid">
    <PosterCard
      v-for="(card, index) in cards"
      :key="`${card.type}:${card.tmdb_id}`"
      :card="card"
      :priority="index < 6"
      :subscribable="card.type === 'tv'"
      :subscribed="subscriptions.has(card.tmdb_id)"
      :busy="subscriptions.busy || !!subscriptionError || subscriptions.uncertain"
      @open="selected = card"
      @subscribe="toggle(card)"
    />
  </div>

  <div v-if="cards.length" class="load-more-footer">
    <span class="load-more-note" role="status">
      已显示 {{ cards.length }} 部{{ page < totalPages ? ' · 点击加载更多' : ' · 已全部加载' }}
    </span>
    <button
      v-if="page < totalPages"
      class="btn"
      :disabled="request.loading.value"
      @click="load(true)"
    >
      <ArrowDown :size="16" />{{ request.loading.value ? '正在加载…' : '加载更多' }}
    </button>
  </div>

  <ModalDialog v-if="selected" :title="titleOf(selected)" @close="selected = undefined"
    ><div class="media-detail">
      <img v-if="posterUrl(selected)" :src="posterUrl(selected)" alt="作品海报" />
      <div>
        <p class="eyebrow">
          {{ selected.type === 'tv' ? 'TV SERIES' : 'MOVIE' }} · {{ selected.year || '年份未知' }}
        </p>
        <h3>{{ titleOf(selected) }}</h3>
        <button
          class="button"
          @click="
            resourceCard = selected;
            selected = undefined;
          "
        >
          搜索资源候选与转存
        </button>
        <p>TMDB {{ selected.tmdb_id }}</p>
        <p>{{ badge(selected) || '片库暂无已确认的入库记录' }}</p>
        <p v-if="selected.eps">
          本地 {{ selected.eps.local ?? '—' }} 集 · 分享 {{ selected.eps.share ?? '—' }} 集 · 合并
          {{ selected.eps.have ?? '—' }} 集
        </p>
        <p v-if="selected.rating">TMDB 评分 {{ selected.rating.toFixed(1) }}</p>
        <button
          v-if="selected.type === 'tv'"
          class="button primary"
          :disabled="subscriptions.busy || !!subscriptionError || subscriptions.uncertain"
          @click="toggle(selected)"
        >
          {{ subscriptions.has(selected.tmdb_id) ? '取消追更' : '添加追更订阅' }}
        </button>
      </div>
    </div></ModalDialog
  >
  <ModalDialog v-if="resourceCard" title="资源候选与获取" @close="resourceCard = undefined"
    ><ResourceSearch :card="resourceCard"
  /></ModalDialog>
</template>

<style scoped>
/* 搜索框（classic search-hero） */
.search-hero {
  background: var(--surface-solid);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 7px 7px 7px 16px;
  margin-bottom: 16px;
  display: flex;
  gap: 8px;
  align-items: center;
  color: var(--text-dim);
  box-shadow: var(--shadow-sm);
  transition:
    box-shadow 0.2s var(--ease),
    border-color 0.2s var(--ease);
}
.search-hero:focus-within {
  border-color: rgba(0, 122, 255, 0.4);
  box-shadow:
    0 0 0 4px rgba(0, 122, 255, 0.08),
    var(--shadow-md);
}
.search-hero input {
  flex: 1;
  border: none;
  background: transparent;
  padding: 12px 8px;
  font-size: 14px;
  color: var(--text);
  min-width: 0;
}
.search-hero input:focus {
  outline: none;
}
.search-hero input::placeholder {
  color: var(--text-dim);
}
.search-btn {
  background: var(--grad-brand);
  color: #fff;
  border: none;
  border-radius: 10px;
  padding: 10px 22px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  font-family: inherit;
  font-size: 13px;
  font-weight: 600;
  box-shadow: 0 4px 12px rgba(0, 122, 255, 0.3);
  white-space: nowrap;
}
.search-btn:hover {
  background: var(--grad-brand-hover);
}

/* 筛选摘要（classic filter-summary） */
.filter-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px 14px;
  margin-bottom: 12px;
  box-shadow: var(--shadow-sm);
}
.fs-text {
  font-size: 12.5px;
  color: var(--text-mid);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  flex: 1;
  min-width: 0;
}
.fs-toggle {
  background: var(--surface-hover);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-mid);
  display: flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
  transition: all 0.18s var(--ease);
}
.fs-toggle:hover {
  background: var(--surface-active);
  color: var(--text);
}
.fs-toggle svg {
  transition: transform 0.2s var(--ease);
}
.filter-summary.open .fs-toggle svg {
  transform: rotate(180deg);
}

/* 筛选面板（classic #exploreFilters） */
.explore-filters {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 4px 14px;
  margin-bottom: 16px;
  box-shadow: var(--shadow-sm);
}

/* 年份 chips 横向滚动 */
.year-scroll {
  flex-wrap: nowrap;
  overflow-x: auto;
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
  max-width: 100%;
}
.year-scroll .chip {
  flex-shrink: 0;
}

/* 加载更多（classic load-more-footer） */
.load-more-footer {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 16px 0 24px;
}
.load-more-footer .btn {
  min-width: 180px;
  min-height: 44px;
}
.load-more-note {
  font-size: 13px;
  color: var(--text-dim);
  text-align: center;
  line-height: 1.6;
}
</style>
