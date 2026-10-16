<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { Search, RefreshCw, CheckSquare } from '@lucide/vue';
import { mediaApi } from '../api/media';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { titleOf, posterUrl, date, badge } from '../composables/format';
import type { LibraryType, LibraryPage, LibraryItem, MediaCard } from '../types/api';
import PosterCard from '../components/PosterCard.vue';
import LoadState from '../components/LoadState.vue';
import ModalDialog from '../components/ModalDialog.vue';
import SingleDelete from '../components/SingleDelete.vue';
import MappingDelete from '../components/MappingDelete.vue';
import ResourceSearch from '../components/ResourceSearch.vue';
import { request as sendRequest } from '../api/client';
import { writeData } from '../api/domain';
import { useOperation } from '../composables/useOperation';
import OperationState from '../components/OperationState.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import PageHeading from '../components/PageHeading.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import { readData, records, type Data } from '../api/domain';
const episodes = useRequest<Data>(),
  comparisonProgress = useRequest<Data>(),
  refreshMode = ref('');
async function refreshLibrary() {
  const kind = refreshMode.value;
  const result = await operation.run(() =>
    sendRequest<Data>(
      `/api/emby/library?force=${kind === 'tmdb' ? 1 : 0}&with_tmdb=${kind === 'tmdb' ? 1 : 0}`,
      { timeoutMs: 120000 },
    ),
  );
  if (result) {
    refreshMode.value = '';
    await Promise.allSettled([load(), loadComparison()]);
  }
}
function loadComparison() {
  return comparisonProgress.run((signal) => readData('/api/tmdb/progress', signal));
}
useVisiblePoll(() => {
  if (
    comparisonProgress.data.value?.progress &&
    (comparisonProgress.data.value.progress as Data).running
  )
    return loadComparison().then(() => {
      if (!(comparisonProgress.data.value?.progress as Data)?.running) return load();
    });
}, 3000);
onMounted(loadComparison);
const episodeRows = computed(() => records(episodes.data.value?.episodes));
async function loadEpisodes() {
  const id = detail.data.value?.item?.id;
  if (id) await episodes.run((signal) => readData(`/api/emby/series/${id}/episodes`, signal));
}
function openResources() {
  const item = detail.data.value?.item;
  if (item) {
    resourceCard.value = { ...item, type: filters.media === 'series' ? 'tv' : 'movie' };
    detailOpen.value = false;
  }
}
const operation = useOperation(),
  resourceCard = ref<MediaCard>(),
  allSelected = ref(false),
  manualConfirm = ref(false);
const request = useRequest<LibraryPage>(),
  detail = useRequest<{ status: string; item?: LibraryItem }>();
const filters = reactive({
    media: 'series' as LibraryType,
    status: 'all',
    scope: 'all',
    keyword: '',
    category: '',
    category_mode: 'seg',
  }),
  search = ref(''),
  cards = ref<MediaCard[]>([]),
  offset = ref(0),
  hasMore = ref(false),
  total = ref(0);
const selectionMode = ref(false),
  selectedIds = ref(new Set<string>()),
  detailOpen = ref(false),
  compareOpen = ref(false);
const selectedCards = computed(() => cards.value.filter((c) => selectedIds.value.has(c.id!)));
const seasons = computed(() => {
  const item = detail.data.value?.item;
  const expected = new Map((item?.tmdb_info?.seasons || []).map((s) => [s.season, s.tmdb]));
  const actual = new Map((item?.seasons || []).map((s) => [s.season, s]));
  return [...new Set([...expected.keys(), ...actual.keys()])]
    .sort((a, b) => a - b)
    .map((season) => ({
      season,
      expected: expected.get(season),
      local: actual.get(season)?.local_eps,
      share: actual.get(season)?.share_eps,
    }));
});
async function load(more = false, background = false) {
  const next = more ? offset.value : 0;
  if (!more && !background) {
    cards.value = [];
    selectedIds.value.clear();
    allSelected.value = false;
  }
  const response = await request.run((signal) =>
    mediaApi.library({ ...filters, offset: next, limit: 40 }, signal),
  );
  if (!response) return;
  if (response.status === 'nocache') {
    cards.value = [];
    total.value = 0;
    hasMore.value = false;
    return;
  }
  cards.value = more ? [...cards.value, ...(response.rows || [])] : response.rows || [];
  offset.value = response.next_offset || 0;
  hasMore.value = !!response.has_more;
  total.value = response.total || 0;
  selectedIds.value = new Set(
    [...selectedIds.value].filter((id) => cards.value.some((c) => c.id === id)),
  );
}
async function open(card: MediaCard) {
  episodes.data.value = undefined;
  detailOpen.value = true;
  detail.data.value = undefined;
  await detail.run((signal) => mediaApi.detail(filters.media, card.id!, signal));
}
function toggle(id?: string) {
  allSelected.value = false;
  if (!id) return;
  const next = new Set(selectedIds.value);
  next.has(id) ? next.delete(id) : next.add(id);
  selectedIds.value = next;
}
function submitSearch() {
  const keyword = search.value.trim();
  if (filters.keyword === keyword) void load();
  else filters.keyword = keyword;
}
watch(filters, () => {
  detailOpen.value = false;
  detail.cancel();
  void load();
});
async function manualDone() {
  const item = detail.data.value?.item;
  if (!item) return;
  const r = await operation.run(() =>
    writeData('/api/manual_done', { id: item.id, name: item.name, done: !item._md }),
  );
  if (r) {
    manualConfirm.value = false;
    detailOpen.value = false;
    await load();
  }
}
async function resync() {
  const item = detail.data.value?.item;
  if (!item) return;
  await operation.run(() => writeData(`/api/emby/series/${item.id}/resync`));
  detailOpen.value = false;
  await load();
}
onMounted(() => void load());
useVisiblePoll(() => {
  if (offset.value <= 40 && !detailOpen.value && !selectionMode.value && !request.loading.value)
    return load(false, true);
}, 30000);
</script>
<template>
  <PageHeading title="片库映射" subtitle="对照本地与分享资源，查看已入库版本、季集和缺集。"
    ><button class="button" :disabled="request.loading.value" @click="load()">
      <RefreshCw :size="16" />更新视图</button
    ><button
      class="button"
      :disabled="operation.busy.value || operation.uncertain.value"
      @click="refreshMode = 'emby'"
    >
      同步 Emby 片库</button
    ><button
      class="button primary"
      :disabled="
        operation.busy.value ||
        operation.uncertain.value ||
        !!(comparisonProgress.data.value?.progress as Data)?.running
      "
      @click="refreshMode = 'tmdb'"
    >
      对照 TMDB
    </button></PageHeading
  >
  <section v-if="(comparisonProgress.data.value?.progress as Data)?.running" class="panel">
    <h2>TMDB 后台对照进度</h2>
    <EvidenceDetails :value="comparisonProgress.data.value?.progress" />
  </section>
  <div class="browse-tabs">
    <button
      :class="{ active: filters.media === 'series' }"
      @click="
        filters.media = 'series';
        filters.status = 'all';
      "
    >
      剧集片库</button
    ><button
      :class="{ active: filters.media === 'movies' }"
      @click="
        filters.media = 'movies';
        filters.status = 'all';
      "
    >
      电影片库</button
    ><span class="push-right muted-text">{{ total }} 部作品</span>
  </div>
  <div class="filter-panel library-filters">
    <form class="search-field" @submit.prevent="submitSearch()">
      <Search :size="17" /><input
        v-model="search"
        aria-label="搜索片库"
        placeholder="搜索片库"
      /><button class="button">搜索</button>
    </form>
    <select v-model="filters.scope" aria-label="片库范围">
      <option value="all">全部片库</option>
      <option value="local">本地库</option>
      <option value="share">分享库</option></select
    ><select v-if="filters.media === 'series'" v-model="filters.status" aria-label="剧集状态">
      <option value="all">全部状态</option>
      <option value="missing">存在缺集</option>
      <option value="aligned">已对齐</option>
      <option value="ongoing">连载中</option>
      <option value="unmatched">待对照</option>
      <option value="extra">存在多集</option></select
    ><input
      v-model.lazy="filters.category"
      aria-label="片库分类"
      placeholder="分类目录（可留空）"
    /><select v-model="filters.category_mode" aria-label="分类匹配方式">
      <option value="seg">目录段匹配</option>
      <option value="sub">路径包含</option></select
    ><button
      class="button"
      :class="{ active: selectionMode }"
      @click="
        selectionMode = !selectionMode;
        selectedIds.clear();
        allSelected = false;
      "
    >
      <CheckSquare :size="16" />选择对照
    </button>
  </div>
  <p class="data-caption">
    事实快照 {{ date(request.data.value?.facts_ts)
    }}<span v-if="request.data.value?.stale"> · 缓存待更新</span>
  </p>
  <div v-if="selectionMode" class="selection-toolbar">
    <span>{{
      allSelected ? `已选择全部筛选结果 ${total} 部` : `已选择 ${selectedIds.size} 部`
    }}</span
    ><button
      class="text-link"
      @click="
        allSelected = false;
        selectedIds = new Set(cards.map((c) => c.id!));
      "
    >
      选择已加载作品</button
    ><button
      class="text-link"
      @click="
        allSelected = true;
        selectedIds.clear();
      "
    >
      选择全部筛选结果</button
    ><button
      class="text-link"
      @click="
        allSelected = false;
        selectedIds.clear();
      "
    >
      清空</button
    ><button
      class="button primary push-right"
      :disabled="!selectedIds.size"
      @click="compareOpen = true"
    >
      查看所选状态
    </button>
  </div>
  <MappingDelete
    v-if="selectionMode"
    :media="filters.media"
    :target="filters.scope"
    :ids="[...selectedIds]"
    :all="allSelected"
    :filter="filters.status"
    :category="filters.category"
    :category-mode="filters.category_mode"
    :keyword="filters.keyword"
    @done="load()"
  />
  <LoadState
    :loading="request.loading.value && !cards.length"
    :error="request.error.value"
    :empty="!request.loading.value && !cards.length"
    :message="
      request.data.value?.status === 'nocache'
        ? '尚无片库快照，请先连接 Emby 并完成片库同步'
        : '没有符合条件的作品'
    "
    @retry="load()"
  />
  <div class="poster-grid">
    <PosterCard
      v-for="card in cards"
      :key="card.id"
      :card="card"
      :selectable="selectionMode"
      :selected="allSelected || selectedIds.has(card.id!)"
      @select="toggle(card.id)"
      @open="open(card)"
    />
  </div>
  <div v-if="hasMore" class="load-more">
    <button class="button" :disabled="request.loading.value" @click="load(true)">
      {{ request.loading.value ? '正在读取…' : '加载更多作品' }}
    </button>
  </div>
  <ModalDialog
    v-if="detailOpen"
    :title="detail.data.value?.item?.name || '作品详情'"
    @close="
      detailOpen = false;
      detail.cancel();
    "
    ><LoadState
      :loading="detail.loading.value"
      :error="detail.error.value"
      :empty="!detail.loading.value && !detail.data.value?.item"
      message="作品不在当前快照中"
      @retry="detailOpen = false" /><template v-if="detail.data.value?.item"
      ><div class="media-detail">
        <img
          v-if="posterUrl(detail.data.value.item)"
          :src="posterUrl(detail.data.value.item)"
          alt="作品海报"
        />
        <div>
          <p class="eyebrow">LIBRARY DETAILS · {{ detail.data.value.item.year }}</p>
          <h3>{{ detail.data.value.item.name }}</h3>
          <p>{{ badge(detail.data.value.item) || '电影资源' }}</p>
          <p>TMDB {{ detail.data.value.item.tmdb_id || '未匹配' }}</p>
          <div class="detail-stats">
            <div>
              <strong>{{ detail.data.value.item.local_eps ?? '—' }}</strong
              ><span>本地集数</span>
            </div>
            <div>
              <strong>{{ detail.data.value.item.share_eps ?? '—' }}</strong
              ><span>分享集数</span>
            </div>
            <div>
              <strong>{{
                detail.data.value.item.have_eps ?? detail.data.value.item.total_episodes ?? '—'
              }}</strong
              ><span>合并集数</span>
            </div>
          </div>
        </div>
      </div>
      <div v-if="seasons.length" class="season-list">
        <section v-for="season in seasons" :key="season.season" class="season-card">
          <strong>{{
            season.season === 0 ? '特别篇' : `S${String(season.season).padStart(2, '0')}`
          }}</strong
          ><span>TMDB 已播 {{ season.expected ?? '—' }} 集</span>
          <div
            v-for="(value, library) in { 本地库: season.local, 分享库: season.share }"
            :key="library"
          >
            <span>{{ library }}</span
            ><strong>{{ value ?? '—' }} 集</strong
            ><small>{{
              value == null || season.expected == null
                ? '待对照'
                : value < season.expected
                  ? `缺 ${season.expected - value} 集`
                  : value === season.expected
                    ? '已对齐'
                    : `超 ${value - season.expected} 集`
            }}</small>
          </div>
        </section>
      </div>
      <div v-if="detail.data.value.item.path" class="path-box">
        <small>Emby 媒体路径</small>{{ detail.data.value.item.path }}
      </div>
      <details v-if="episodeRows.length">
        <summary>实际分集路径与存在状态</summary>
        <EvidenceDetails :value="episodeRows" />
      </details>
      <button v-if="filters.media === 'series'" class="button" @click="loadEpisodes">
        读取实际分集明细
      </button></template
    ><template #footer
      ><template v-if="detail.data.value?.item"
        ><button class="button" @click="openResources">搜索资源候选</button
        ><button v-if="filters.media === 'series'" class="button" @click="manualConfirm = true">
          {{ detail.data.value.item._md ? '取消手动完结' : '手动完结' }}</button
        ><button
          v-if="filters.media === 'series'"
          class="button"
          :disabled="operation.busy.value || operation.uncertain.value"
          @click="resync"
        >
          从磁盘同步此剧状态
        </button>
        <div class="single-delete-actions">
          <SingleDelete
            v-if="detail.data.value.item.in_local"
            :media="filters.media"
            target="local"
            :item="detail.data.value.item"
            @done="load()"
          /><SingleDelete
            v-if="detail.data.value.item.in_share"
            :media="filters.media"
            target="share"
            :item="detail.data.value.item"
            @done="load()"
          /></div></template></template
  ></ModalDialog>
  <ModalDialog v-if="compareOpen" title="所选作品状态" @close="compareOpen = false"
    ><div class="comparison-list">
      <div v-for="card in selectedCards" :key="card.id">
        <strong>{{ titleOf(card) }}</strong
        ><span>{{ badge(card) || '电影资源' }}</span
        ><small
          >本地 {{ card.local_eps ?? (card.in_local ? '有资源' : '—') }} · 分享
          {{ card.share_eps ?? (card.in_share ? '有资源' : '—') }}</small
        >
      </div>
    </div></ModalDialog
  >
  <OperationState
    :busy="operation.busy.value"
    :error="operation.error.value || episodes.error.value"
    :uncertain="operation.uncertain.value"
    :receipt="operation.receipt.value"
  />
  <ModalDialog v-if="resourceCard" title="资源候选与获取" @close="resourceCard = undefined"
    ><ResourceSearch :card="resourceCard"
  /></ModalDialog>
  <ConfirmAction
    v-if="refreshMode"
    :title="refreshMode === 'tmdb' ? '启动 TMDB 对照' : '同步 Emby 片库'"
    :description="
      refreshMode === 'tmdb'
        ? '读取 Emby 与 TMDB 更新全库对照事实，后台执行，可能持续较长时间。'
        : '读取 Emby 当前媒体目录并更新片库事实，不执行媒体删除。'
    "
    :busy="operation.busy.value"
    :disabled="operation.uncertain.value"
    @close="refreshMode = ''"
    @confirm="refreshLibrary"
  /><ConfirmAction
    v-if="manualConfirm"
    title="手动标记完结"
    description="仅更新完结标记，不修改媒体文件。"
    :busy="operation.busy.value"
    :disabled="operation.uncertain.value"
    @close="manualConfirm = false"
    @confirm="manualDone"
  />
</template>
