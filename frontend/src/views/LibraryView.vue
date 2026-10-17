<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, nextTick, reactive, ref, watch } from 'vue';
import { Search, RefreshCw, CheckSquare } from '@lucide/vue';
import { mediaApi } from '../api/media';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { useLibraryUpdates } from '../composables/useLibraryUpdates';
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
import { readData, records, object, type Data } from '../api/domain';
const episodes = useRequest<Data>(),
  comparisonProgress = useRequest<Data>(),
  nasSearch = useRequest<Data>(),
  nasKeyword = ref(''),
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
  compareOpen = ref(false),
  deleteNotice = ref(''),
  lastDeletion = ref<Data>(),
  pagingMarker = ref<HTMLElement>();
let pagingObserver: IntersectionObserver | undefined;
const statusTabs = [
  { key: 'all', name: '全部' },
  { key: 'aligned', name: '完整' },
  { key: 'missing', name: '缺集' },
  { key: 'extra', name: '超集' },
  { key: 'ongoing', name: '连载中' },
  { key: 'unmatched', name: '待对照' },
];
async function connectPaging() {
  await nextTick();
  pagingObserver?.disconnect();
  if (!pagingMarker.value || typeof IntersectionObserver === 'undefined') return;
  pagingObserver = new IntersectionObserver(
    (entries) => {
      if (
        entries.some((entry) => entry.isIntersecting) &&
        !request.loading.value &&
        hasMore.value &&
        !detailOpen.value &&
        !selectionMode.value
      )
        void load(true);
    },
    { rootMargin: '700px 0px 700px 0px' },
  );
  pagingObserver.observe(pagingMarker.value);
}
const selectedCards = computed(() =>
  cards.value.filter((c) => allSelected.value || selectedIds.value.has(c.id!)),
);
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
  if (more && (request.loading.value || !hasMore.value)) return;
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
  void connectPaging();
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
  if (allSelected.value) {
    // A partial deselection from 'all filtered' must not silently shrink to loaded rows.
    allSelected.value = false;
    selectedIds.value = new Set(cards.value.map((card) => card.id!).filter(Boolean));
  }
  if (!id) return;
  const next = new Set(selectedIds.value);
  next.has(id) ? next.delete(id) : next.add(id);
  selectedIds.value = next;
}
async function afterSingleDelete(receipt: Data) {
  lastDeletion.value = receipt;
  // The existing Python delete handler has already patched Emby mapping caches;
  // fetch those verified facts before removing a poster from the UI.
  deleteNotice.value = '删除已执行，正在同步作品状态和海报…';
  const previous = detail.data.value?.item;
  await load();
  if (request.error.value) {
    deleteNotice.value = '删除成功，但片库刷新失败，请点击「更新视图」核实。';
    return;
  }
  detailOpen.value = false;
  const failed = Number(object(receipt.result).failed || receipt.error_count || 0);
  deleteNotice.value =
    failed > 0
      ? `《${previous?.name || '作品'}》有 ${failed} 个失败项目，片库已刷新，请检查执行记录。`
      : `已核实删除《${previous?.name || '作品'}》，片库列表已更新。`;
}
async function afterBatchDelete(receipt: Data) {
  lastDeletion.value = receipt;
  deleteNotice.value = '后台执行完成，正在读取删除后的片库状态…';
  await load();
  if (request.error.value) {
    deleteNotice.value = '批量删除已执行，片库刷新失败，请重新读取并核实结果。';
    return;
  }
  const failed = Number(object(receipt.result).failed || receipt.error_count || 0);
  deleteNotice.value =
    failed > 0
      ? `批量处理有 ${failed} 个失败项目；片库已刷新，请展开执行记录逐部核对。`
      : '批量执行已完成，片库状态已更新；处理记录可在任务回执中展开。';
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
onBeforeUnmount(() => pagingObserver?.disconnect());
watch(
  [hasMore, pagingMarker, () => selectionMode.value, () => cards.value.length],
  () => void connectPaging(),
);
async function refreshLoaded() {
  if (request.loading.value || detailOpen.value || operation.busy.value) return false;
  const count = Math.max(cards.value.length, 40),
    current = { ...filters };
  const response = await request.run(async (signal) => {
    let rows: MediaCard[] = [],
      result: LibraryPage;
    do {
      result = await mediaApi.library(
        { ...current, offset: rows.length, limit: Math.min(80, count - rows.length) },
        signal,
      );
      rows = [...rows, ...(result.rows || [])];
    } while (rows.length < count && result.has_more && (result.rows || []).length);
    return { ...result, rows };
  });
  if (!response) return false;
  cards.value = response.rows || [];
  total.value = response.total || 0;
  offset.value = response.next_offset || 0;
  hasMore.value = !!response.has_more;
  selectedIds.value = new Set(
    [...selectedIds.value].filter((id) => cards.value.some((c) => c.id === id)),
  );
}
const live = useLibraryUpdates(refreshLoaded);
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
  <details class="panel nas-library-search">
    <summary>在 NAS 的 STRM 目录中检索作品</summary>
    <p class="muted-text">
      直接按目录名检索本地库和分享库，适用于尚未被 Emby 收录的资源。点击查询时读取目录。
    </p>
    <form
      class="search-toolbar"
      @submit.prevent="
        nasSearch.run((signal) =>
          readData(`/api/search?q=${encodeURIComponent(nasKeyword.trim())}`, signal),
        )
      "
    >
      <input
        v-model="nasKeyword"
        aria-label="NAS 目录关键词"
        maxlength="200"
        placeholder="输入作品目录关键词"
        required
      />
      <button class="button" :disabled="!nasKeyword.trim() || nasSearch.loading.value">
        查询 NAS 目录
      </button>
    </form>
    <p v-if="nasSearch.error.value" class="inline-error">{{ nasSearch.error.value }}</p>
    <pre v-if="nasSearch.data.value?.text" class="report-preview">{{
      nasSearch.data.value.text
    }}</pre>
  </details>
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
  <nav v-if="filters.media === 'series'" class="mapping-status-tabs" aria-label="剧集完整度筛选">
    <button
      v-for="tab in statusTabs"
      :key="tab.key"
      type="button"
      :class="{ active: filters.status === tab.key }"
      @click="filters.status = tab.key"
    >
      {{ tab.name
      }}<span v-if="filters.scope === 'all' && !filters.keyword && !filters.category">{{
        tab.key === 'all'
          ? (request.data.value?.stats?.total_series ?? '')
          : (request.data.value?.stats?.[tab.key] ?? '')
      }}</span>
    </button>
  </nav>
  <div class="filter-panel library-filters">
    <form class="search-field" @submit.prevent="submitSearch()">
      <Search :size="17" /><input
        v-model="search"
        aria-label="搜索片库"
        placeholder="搜索片库"
      /><button class="button">搜索</button>
    </form>
    <div class="mapping-scope-tabs" aria-label="片库范围">
      <button
        type="button"
        :class="{ active: filters.scope === 'all' }"
        @click="filters.scope = 'all'"
      >
        本地＋分享
      </button>
      <button
        type="button"
        :class="{ active: filters.scope === 'local' }"
        @click="filters.scope = 'local'"
      >
        仅本地
      </button>
      <button
        type="button"
        :class="{ active: filters.scope === 'share' }"
        @click="filters.scope = 'share'"
      >
        仅分享
      </button>
    </div>
    <input
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
  <p v-if="deleteNotice" class="notice-bar" role="status">{{ deleteNotice }}</p>
  <section
    v-if="lastDeletion"
    class="panel mapping-delete-result"
    aria-label="最近一次删除执行记录"
  >
    <header class="section-heading">
      <h2>最近一次删除执行记录</h2>
      <button class="button small" @click="lastDeletion = undefined">收起记录</button>
    </header>
    <div class="progress-metrics">
      <span
        >STRM 已删除
        <strong>{{
          object(lastDeletion.result).strm_removed ?? lastDeletion.strm_removed ?? 0
        }}</strong></span
      >
      <span
        >源文件已删除
        <strong>{{
          object(lastDeletion.result).cloud_removed ?? lastDeletion.cloud_removed ?? 0
        }}</strong></span
      >
      <span
        >失败项
        <strong>{{
          object(lastDeletion.result).failed ?? lastDeletion.error_count ?? 0
        }}</strong></span
      >
    </div>
    <details>
      <summary>查看后台实际执行明细及错误</summary>
      <EvidenceDetails :value="lastDeletion" />
    </details>
  </section>
  <p class="data-caption">
    事实快照 {{ date(request.data.value?.facts_ts)
    }}<span v-if="request.data.value?.stale"> · 缓存待更新</span> ·
    {{ live.connected.value ? '实时变化已连接' : '变化检查与断线回退已启用' }}
  </p>
  <p v-if="live.error.value" class="notice-bar">
    {{ live.error.value }}<button class="text-link" @click="live.poll">重新读取变化</button>
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
      :disabled="!allSelected && !selectedIds.size"
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
    @done="afterBatchDelete"
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
  <div class="poster-grid library-poster-grid">
    <PosterCard
      v-for="(card, index) in cards"
      :key="card.id"
      :card="card"
      :priority="index < 6"
      :selectable="selectionMode"
      :selected="allSelected || selectedIds.has(card.id!)"
      @select="toggle(card.id)"
      @open="selectionMode ? toggle(card.id) : open(card)"
    />
  </div>
  <div v-if="hasMore" ref="pagingMarker" class="load-more">
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
            @done="afterSingleDelete"
          /><SingleDelete
            v-if="detail.data.value.item.in_share"
            :media="filters.media"
            target="share"
            :item="detail.data.value.item"
            @done="afterSingleDelete"
          /></div></template></template
  ></ModalDialog>
  <ModalDialog v-if="compareOpen" title="所选作品状态" @close="compareOpen = false"
    ><p v-if="allSelected" class="notice-bar">
      已选中全部
      {{ total }} 部筛选作品。以下仅展示当前已加载的卡片；删除核验以服务端全部筛选结果为准。
    </p>
    <div class="comparison-list">
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
