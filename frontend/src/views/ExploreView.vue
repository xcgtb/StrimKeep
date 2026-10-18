<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, reactive, watch } from 'vue';
import { useRoute } from 'vue-router';
import { Search, SlidersHorizontal, ArrowDown, Compass } from '@lucide/vue';
import FilterChips from '../components/FilterChips.vue';
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
import { readData, records, type Data } from '../api/domain';
const resourceCard = ref<MediaCard>();
const catalog = useRequest<Data>();
const route = useRoute(),
  ui = useUi(),
  subscriptions = useSubscriptions(),
  request = useRequest<ExploreResponse>();
const filters = reactive({
  media: String(route.query.media || 'movie'),
  region: 'all',
  sort: 'popularity',
  year: '',
  genre: '',
  q: String(route.query.q || ''),
});
const search = ref(filters.q),
  showFilters = ref(true),
  cards = ref<MediaCard[]>([]),
  page = ref(1),
  totalPages = ref(1),
  selected = ref<MediaCard>(),
  libraryNotice = ref(''),
  subscriptionError = ref('');

// 筛选选项与 1.2.12 保持一致：地区 / 类型 / 题材 / 年份 / 排序 全部常驻可见。
const regionOptions = [
  { value: 'all', label: '全部' },
  { value: 'cn', label: '大陆' },
  { value: 'hk', label: '香港' },
  { value: 'tw', label: '台湾' },
  { value: 'jp', label: '日本' },
  { value: 'kr', label: '韩国' },
  { value: 'us', label: '欧美' },
];
const mediaOptions = [
  { value: 'all', label: '全部' },
  { value: 'movie', label: '电影' },
  { value: 'tv', label: '剧集' },
];
const sortOptions = [
  { value: 'popularity', label: '热度' },
  { value: 'release', label: '最新' },
  { value: 'rating', label: 'TMDB 评分' },
];
const fallbackGenres = [
  '动作',
  '冒险',
  '喜剧',
  '犯罪',
  '纪录',
  '剧情',
  '家庭',
  '奇幻',
  '历史',
  '恐怖',
  '音乐',
  '悬疑',
  '爱情',
  '科幻',
  '惊悚',
  '战争',
  '西部',
  '动画',
  '儿童',
  '新闻',
  '真人秀',
  '肥皂剧',
  '脱口秀',
  '电视电影',
];
const genreOptions = computed(() => {
  const names = records(catalog.data.value?.genres)
    .map((g) => String(g.name || ''))
    .filter(Boolean);
  const list = names.length ? names : fallbackGenres;
  return [{ value: '', label: '全部' }, ...list.map((name) => ({ value: name, label: name }))];
});
// 年份档位对齐 1.2.12 explore.js：初始 30 个，滚到尾部再追加 20 个，下限 1900。
const YEAR_FLOOR = 1900;
const yearCount = ref(30);
const yearTop = new Date().getFullYear() + 1;
const yearOptions = computed(() => {
  const options = [{ value: '', label: '全部' }];
  for (let index = 0; index < yearCount.value; index += 1) {
    const year = yearTop - index;
    if (year < YEAR_FLOOR) break;
    options.push({ value: String(year), label: String(year) });
  }
  // 已选年份若在已加载区间之外，补一个，避免回显丢失
  if (filters.year && !options.some((option) => option.value === filters.year))
    options.push({ value: filters.year, label: filters.year });
  return options;
});
function loadMoreYears() {
  if (yearTop - yearCount.value < YEAR_FLOOR) return;
  yearCount.value += 20;
}
const filterSummary = computed(() => {
  const parts: string[] = [];
  const region = regionOptions.find((o) => o.value === filters.region);
  parts.push(filters.region === 'all' ? '全部地区' : region?.label || '全部地区');
  const media = mediaOptions.find((o) => o.value === filters.media);
  if (filters.media !== 'all') parts.push(media?.label || '');
  if (filters.genre) parts.push(filters.genre);
  if (filters.year) parts.push(`${filters.year} 年`);
  const sort = sortOptions.find((o) => o.value === filters.sort);
  parts.push(sort?.label || '热度');
  return parts.filter(Boolean).join(' · ');
});
let badgeController: AbortController | undefined,
  generation = 0;
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
  void catalog.run((signal) => readData('/api/cloud/resources/catalog', signal));
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
  <form class="search-toolbar" @submit.prevent="submitSearch()">
    <div class="search-field">
      <Search :size="18" /><input
        v-model="search"
        aria-label="搜索影视作品"
        placeholder="搜索电影、剧集名称…"
      /><button class="button primary">搜索</button>
    </div>
  </form>
  <div class="chip-panel">
    <div class="chip-panel-head">
      <strong>{{ filterSummary }}</strong>
      <span class="push-right muted-text">第 {{ page }} / {{ totalPages }} 页</span>
      <button
        class="text-link"
        type="button"
        :aria-expanded="showFilters"
        @click="showFilters = !showFilters"
      >
        <SlidersHorizontal :size="14" />{{ showFilters ? '收起筛选' : '展开筛选' }}
      </button>
    </div>
    <template v-if="showFilters">
      <FilterChips v-model="filters.region" label="地区" :options="regionOptions" />
      <FilterChips v-model="filters.media" label="类型" :options="mediaOptions" />
      <FilterChips v-model="filters.genre" label="题材" :options="genreOptions" />
      <FilterChips
        v-model="filters.year"
        label="年份"
        :options="yearOptions"
        scroll
        @load-more="loadMoreYears"
      />
      <FilterChips v-model="filters.sort" label="排序" :options="sortOptions" />
    </template>
  </div>
  <p v-if="filters.media === 'all' && !filters.q" class="notice-bar">
    输入片名可同时搜索电影与剧集；当前浏览展示热门电影。
  </p>
  <p v-if="libraryNotice" class="notice-bar" role="status">{{ libraryNotice }}</p>
  <p v-if="catalog.error.value || live.error.value" class="notice-bar">
    {{ catalog.error.value || live.error.value }}
  </p>
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
      compact
      @open="selected = card"
    />
  </div>
  <div v-if="cards.length && page < totalPages" class="load-more">
    <button class="button" :disabled="request.loading.value" @click="load(true)">
      <ArrowDown :size="16" />{{ request.loading.value ? '正在加载…' : '加载更多作品' }}
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
          :aria-pressed="subscriptions.has(selected.tmdb_id)"
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
