<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, reactive, watch } from 'vue';
import { useRoute } from 'vue-router';
import { Search, SlidersHorizontal, ArrowDown, Compass } from '@lucide/vue';
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
// Page state survives navigation back from another module without duplicating requests.
let exploreSnapshot:
  | { key: string; cards: MediaCard[]; page: number; total: number; scroll: number; at: number }
  | undefined;
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
  showFilters = ref(false),
  cards = ref<MediaCard[]>([]),
  page = ref(1),
  totalPages = ref(1),
  selected = ref<MediaCard>(),
  libraryNotice = ref(''),
  subscriptionError = ref(''),
  loadMarker = ref<HTMLElement>(),
  moreError = ref('');
let badgeController: AbortController | undefined,
  pageObserver: IntersectionObserver | undefined,
  generation = 0;
function filterKey() {
  return JSON.stringify(filters);
}
function rememberPosition() {
  if (cards.value.length)
    exploreSnapshot = {
      key: filterKey(),
      cards: cards.value.slice(),
      page: page.value,
      total: totalPages.value,
      scroll: window.scrollY,
      at: Date.now(),
    };
}
async function connectObserver() {
  await nextTick();
  pageObserver?.disconnect();
  if (!loadMarker.value || typeof IntersectionObserver === 'undefined') return;
  pageObserver = new IntersectionObserver(
    (entries) => {
      if (
        entries.some((entry) => entry.isIntersecting) &&
        !request.loading.value &&
        page.value < totalPages.value
      )
        void load(true);
    },
    { rootMargin: '850px 0px 850px 0px', threshold: 0 },
  );
  pageObserver.observe(loadMarker.value);
}
async function load(more = false) {
  if (more && (request.loading.value || page.value >= totalPages.value)) return;
  moreError.value = '';
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
  if (!response || version !== generation) {
    if (more && request.error.value) moreError.value = request.error.value;
    return;
  }
  if (response.status === 'pending') {
    libraryNotice.value = '影视数据正在后台加载，请稍后重新加载';
    return;
  }
  const combined = more ? [...cards.value, ...(response.cards || [])] : response.cards || [];
  cards.value = [...new Map(combined.map((c) => [`${c.type}:${c.tmdb_id}`, c])).values()];
  page.value = target;
  totalPages.value = response.total_pages || 1;
  rememberPosition();
  void connectObserver();
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
watch(filters, () => {
  exploreSnapshot = undefined;
  pageObserver?.disconnect();
  void load();
});
watch([() => cards.value.length, totalPages, page, loadMarker], () => void connectObserver());
watch(
  () => route.query,
  (query) => {
    search.value = String(query.q || '');
    filters.q = search.value;
    filters.media = String(query.media || 'all');
  },
);
onMounted(() => {
  if (
    exploreSnapshot &&
    exploreSnapshot.key === filterKey() &&
    Date.now() - exploreSnapshot.at < 180000
  ) {
    cards.value = exploreSnapshot.cards;
    page.value = exploreSnapshot.page;
    totalPages.value = exploreSnapshot.total;
    const previousScroll = exploreSnapshot.scroll;
    void nextTick(() => {
      window.scrollTo({ top: previousScroll, behavior: 'instant' });
      void connectObserver();
    });
    void updateBadges();
  } else void load();
  void loadSubscriptions();
  void catalog.run((signal) => readData('/api/cloud/resources/catalog', signal));
});
onBeforeUnmount(() => {
  rememberPosition();
  badgeController?.abort();
  pageObserver?.disconnect();
});
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
    <button
      class="button"
      type="button"
      :aria-expanded="showFilters"
      @click="showFilters = !showFilters"
    >
      <SlidersHorizontal :size="16" />筛选
    </button>
  </form>
  <div class="summary-strip explore-filter-summary">
    <span
      >地区 {{ filters.region === 'all' ? '全部' : filters.region }} ·
      {{ filters.genre || '全部题材' }} · {{ filters.year || '全部年份' }} ·
      {{ filters.sort === 'rating' ? '评分' : filters.sort === 'release' ? '最新' : '热度' }}</span
    >
    <button
      class="button small push-right"
      :aria-expanded="showFilters"
      @click="showFilters = !showFilters"
    >
      {{ showFilters ? '收起筛选' : '展开筛选' }}
    </button>
  </div>
  <div class="browse-tabs">
    <button
      v-for="(label, key) in { all: '综合搜索', movie: '电影', tv: '剧集' }"
      :key="key"
      :class="{ active: filters.media === key }"
      @click="filters.media = key"
    >
      {{ label }}</button
    ><span class="push-right muted-text">第 {{ page }} / {{ totalPages }} 页</span>
  </div>
  <div v-if="showFilters" class="filter-panel">
    <label
      >地区<select v-model="filters.region">
        <option value="all">全部地区</option>
        <option value="cn">中国大陆</option>
        <option value="hk">中国香港</option>
        <option value="tw">中国台湾</option>
        <option value="jp">日本</option>
        <option value="kr">韩国</option>
        <option value="us">欧美</option>
      </select></label
    ><label
      >排序<select v-model="filters.sort">
        <option value="popularity">热度优先</option>
        <option value="release">最新上映</option>
        <option value="rating">评分优先</option>
      </select></label
    ><label
      >年份<input
        v-model.lazy="filters.year"
        type="number"
        min="1900"
        :max="new Date().getFullYear() + 5"
        placeholder="全部年份" /></label
    ><label
      >题材<select v-model="filters.genre">
        <option value="">全部题材</option>
        <option
          v-for="genre in records(catalog.data.value?.genres)"
          :key="String(genre.name)"
          :value="genre.name"
        >
          {{ genre.name }}
        </option>
      </select></label
    >
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
    @retry="load()"
  />
  <div class="poster-grid discovery-grid">
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
  <div
    v-if="cards.length && page < totalPages"
    ref="loadMarker"
    class="load-more"
    aria-live="polite"
  >
    <button class="button" :disabled="request.loading.value" @click="load(true)">
      <ArrowDown :size="16" />{{
        request.loading.value
          ? '正在自动加载下一页…'
          : moreError
            ? '加载失败，点击重试'
            : '继续加载更多作品'
      }}
    </button>
  </div>
  <p v-if="moreError" class="notice-bar warning-text">下一页加载失败：{{ moreError }}</p>
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
