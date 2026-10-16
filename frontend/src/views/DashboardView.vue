<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ArrowUpRight,
  RefreshCw,
  HardDrive,
  LibraryBig,
  Clapperboard,
  Bell,
  CheckCircle2,
  CircleAlert,
  CloudDownload,
  ArrowDownUp,
  FileVideo,
  ShieldCheck,
  ArrowRight,
} from '@lucide/vue';
import { mediaApi } from '../api/media';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { number, date, titleOf } from '../composables/format';
import { useUi } from '../stores/ui';
import PosterCard from '../components/PosterCard.vue';
import LoadState from '../components/LoadState.vue';
import type { MediaCard, Overview, ExploreResponse } from '../types/api';
const overview = useRequest<Overview>(),
  recommendations = useRequest<ExploreResponse>(),
  router = useRouter(),
  ui = useUi(),
  refreshing = ref(false),
  recType = ref('movie');
const workflows = [
  { to: '/resources/receive', name: '接收资源', note: '分享 · 磁力 · 秒传', icon: CloudDownload },
  { to: '/organize/workspace', name: '识别整理', note: '命名 · 分类 · 归档', icon: ArrowDownUp },
  { to: '/media/strm', name: '生成与入库', note: 'STRM · 播放 · Emby', icon: FileVideo },
  { to: '/governance', name: '片库治理', note: '质量 · 去重 · 清理', icon: ShieldCheck },
];
const dashboard = computed(() => overview.data.value?.dashboard || {});
const stats = computed(() => [
  {
    label: '本地 STRM',
    value: dashboard.value.localCount,
    note: '个人媒体资源',
    icon: HardDrive,
    tone: 'blue',
  },
  {
    label: '分享 STRM',
    value: dashboard.value.shareCount,
    note: '分享媒体资源',
    icon: LibraryBig,
    tone: 'teal',
  },
  {
    label: '已入库剧集',
    value: dashboard.value.libraryHealth?.stats?.total_series,
    note: '来自 Emby 片库事实',
    icon: Clapperboard,
    tone: 'violet',
  },
  {
    label: '追更订阅',
    value: dashboard.value.subscriptions?.total,
    note: '持续关注的作品',
    icon: Bell,
    tone: 'orange',
  },
]);
function load() {
  return overview.run(mediaApi.overview);
}
function loadRecs() {
  return recommendations.run((signal) => mediaApi.recommendations(recType.value, signal));
}
async function refresh() {
  refreshing.value = true;
  try {
    await mediaApi.refreshOverview();
    ui.notify('刷新请求已提交，后台正在更新总览。');
    await load();
  } catch (e) {
    ui.notify((e as Error).message, 'error');
  } finally {
    refreshing.value = false;
  }
}
function explore(card: MediaCard) {
  void router.push({
    path: '/media/explore',
    query: { q: titleOf(card), media: card.type || 'all' },
  });
}
onMounted(() => {
  void load();
  void loadRecs();
});
useVisiblePoll(load, 15000);
</script>
<template>
  <section class="page-heading dashboard-heading">
    <div>
      <span class="eyebrow">媒体工作台</span>
      <h1>运行总览</h1>
      <p>资源有序流转，片库一目了然。</p>
    </div>
    <button
      class="button"
      :disabled="refreshing || overview.data.value?.refresh?.running"
      @click="refresh"
    >
      <RefreshCw
        :size="16"
        :class="{ spin: refreshing || overview.data.value?.refresh?.running }"
      />刷新总览
    </button>
  </section>
  <nav class="workflow-launcher" aria-label="媒体处理工作流">
    <RouterLink v-for="(flow, i) in workflows" :key="flow.to" :to="flow.to"
      ><span class="workflow-launch-icon"><component :is="flow.icon" :size="20" /></span>
      <div>
        <strong>{{ flow.name }}</strong
        ><small>{{ flow.note }}</small>
      </div>
      <ArrowRight v-if="i < workflows.length - 1" :size="15" class="workflow-arrow"
    /></RouterLink>
  </nav>
  <LoadState
    :loading="overview.loading.value && !overview.data.value"
    :error="overview.error.value"
    @retry="load"
  />
  <template v-if="overview.data.value"
    ><div
      v-if="
        overview.data.value.refresh?.running ||
        overview.data.value.refresh?.error ||
        overview.data.value.stale
      "
      class="notice-bar"
      role="status"
    >
      <span class="status-dot muted"></span
      >{{
        overview.data.value.refresh?.error ||
        overview.data.value.refresh?.phase ||
        '正在显示缓存快照，等待后台更新'
      }}<span class="push-right">快照 {{ date(overview.data.value.ts) }}</span>
    </div>
    <div v-for="warning in overview.data.value.refresh?.warnings" :key="warning" class="notice-bar">
      {{ warning }}
    </div>
    <div class="stats-grid">
      <article v-for="stat in stats" :key="stat.label" class="stat-card">
        <div class="stat-label">
          {{ stat.label
          }}<span class="stat-icon" :class="stat.tone"
            ><component :is="stat.icon" :size="18"
          /></span>
        </div>
        <strong>{{ number(stat.value) }}</strong
        ><span>{{ stat.note }}</span>
      </article>
    </div>
    <div class="dashboard-middle">
      <section class="panel activity-panel">
        <header class="section-heading">
          <div>
            <span class="eyebrow">片库动态</span>
            <h2>近期入库</h2>
          </div>
          <RouterLink class="text-link" to="/media/library"
            >查看片库<ArrowUpRight :size="15"
          /></RouterLink>
        </header>
        <p v-if="overview.data.value.activity?.ingest_error" class="inline-error">
          近期入库同步失败，保留上次快照。
        </p>
        <div v-if="overview.data.value.activity?.recent?.length" class="recent-grid">
          <PosterCard
            v-for="card in overview.data.value.activity.recent"
            :key="`${card.media_type}:${card.tmdb_id}:${card.title}`"
            :card="{ ...card, type: card.media_type }"
            @open="explore({ ...card, type: card.media_type })"
          />
        </div>
        <div v-else class="activity-empty">
          <Clapperboard :size="26" />
          <p>暂无近期入库记录</p>
          <small>Emby 确认入库后，这里会展示真实记录。</small>
        </div>
      </section>
      <section class="panel service-panel">
        <header class="section-heading">
          <div>
            <span class="eyebrow">运行状态</span>
            <h2>服务连接</h2>
          </div>
          <RouterLink to="/settings" class="text-link">管理<ArrowUpRight :size="15" /></RouterLink>
        </header>
        <div v-for="(service, name) in dashboard.services" :key="name" class="service-row">
          <div class="service-symbol">{{ String(name).slice(0, 1).toUpperCase() }}</div>
          <div>
            <strong>{{
              { emby: 'Emby 媒体服务', tmdb: 'TMDB 元数据', telegram: 'Telegram 通知' }[
                String(name)
              ] || name
            }}</strong
            ><small>{{
              service.host || (name === 'telegram' ? '入库与追更消息' : '影视识别与海报')
            }}</small>
          </div>
          <CheckCircle2 v-if="service.ok" :size="18" class="success-text" /><CircleAlert
            v-else
            :size="18"
            class="warning-text"
          /><span class="sr-only">{{ service.ok ? '配置可用' : '未就绪' }}</span>
        </div>
        <p v-if="!dashboard.services" class="muted-text">服务状态尚未同步</p>
        <div class="service-footnote">
          上次治理 <strong>{{ dashboard.lastScan?.time || '尚无记录' }}</strong
          ><small v-if="dashboard.lastScan"
            >待处理 {{ number(dashboard.lastScan.total) }} 项 · 受保护
            {{ number(dashboard.lastScan.protected) }} 项</small
          >
        </div>
      </section>
    </div></template
  >
  <section class="panel recommendation-panel">
    <header class="section-heading">
      <div>
        <span class="eyebrow">发现灵感</span>
        <h2>值得发现</h2>
        <p>为下一次观影，留一点期待。</p>
      </div>
      <div class="segmented">
        <button
          :class="{ active: recType === 'movie' }"
          @click="
            recType = 'movie';
            loadRecs();
          "
        >
          电影</button
        ><button
          :class="{ active: recType === 'tv' }"
          @click="
            recType = 'tv';
            loadRecs();
          "
        >
          剧集
        </button>
      </div>
    </header>
    <LoadState
      :loading="recommendations.loading.value"
      :error="recommendations.error.value"
      :empty="!recommendations.data.value?.cards?.length"
      @retry="loadRecs"
    />
    <div
      v-if="!recommendations.loading.value && !recommendations.error.value"
      class="poster-grid compact-grid"
    >
      <PosterCard
        v-for="card in recommendations.data.value?.cards?.slice(0, 6)"
        :key="card.tmdb_id"
        :card="card"
        @open="explore(card)"
      />
    </div>
  </section>
</template>
