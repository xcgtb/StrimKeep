<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import {
  ArrowUpRight,
  RefreshCw,
  HardDrive,
  LibraryBig,
  Clapperboard,
  Film,
  Tv2,
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
import { useActivityUpdates } from '../composables/useActivityUpdates';
import { useLibraryUpdates } from '../composables/useLibraryUpdates';
import { number, date, titleOf, stateLabel } from '../composables/format';
import { useUi } from '../stores/ui';
import { readData, writeData, records, type Data } from '../api/domain';
import MediaSources from '../components/MediaSources.vue';
import PlaybackMetrics from '../components/PlaybackMetrics.vue';
import PosterCard from '../components/PosterCard.vue';
import LoadState from '../components/LoadState.vue';
import type { MediaCard, Overview, ExploreResponse } from '../types/api';
const overview = useRequest<Overview>(),
  recommendations = useRequest<ExploreResponse>(),
  router = useRouter(),
  ui = useUi(),
  refreshing = ref(false),
  recType = ref('movie'),
  jobs = useRequest<Data>(),
  playback = useRequest<Data>();
function loadPlayback() {
  return playback.run((signal) => readData('/api/overview/playback', signal));
}
async function refreshPlayback() {
  try {
    await writeData('/api/overview/playback/refresh');
    await loadPlayback();
  } catch (e) {
    ui.notify((e as Error).message, 'error');
  }
}
const workflows = [
  { to: '/resources/receive', name: '接收资源', note: '分享 · 磁力 · 秒传', icon: CloudDownload },
  { to: '/organize/workspace', name: '识别整理', note: '命名 · 分类 · 归档', icon: ArrowDownUp },
  { to: '/media/strm', name: 'STRM 发布', note: '引用 · 播放 · 核实', icon: FileVideo },
  { to: '/tasks/ingestion', name: 'Emby 核验', note: '扫描 · 入库 · 回执', icon: CheckCircle2 },
  { to: '/governance', name: '片库治理', note: '质量 · 去重 · 清理', icon: ShieldCheck },
];
const dashboard = computed(() => overview.data.value?.dashboard || {});
const stats = computed(() => [
  {
    label: '电影作品',
    value: dashboard.value.libraryHealth?.stats?.total_movies,
    note: 'Emby 片库 · 部',
    icon: Film,
    tone: 'blue',
    to: '/media/library',
  },
  {
    label: '剧集作品',
    value: dashboard.value.libraryHealth?.stats?.total_series,
    note: 'Emby 片库 · 部',
    icon: Tv2,
    tone: 'violet',
    to: '/media/library',
  },
  {
    label: '已入库单集',
    value: dashboard.value.libraryHealth?.episodes,
    note: 'Emby 片库 · 集',
    icon: Clapperboard,
    tone: 'teal',
    to: '/media/library',
  },
  {
    label: '本地 STRM',
    value: dashboard.value.localCount,
    note: '上游 + 自建个人库',
    icon: HardDrive,
    tone: 'blue',
    to: '/media/strm',
  },
  {
    label: '分享 STRM',
    value: dashboard.value.shareCount,
    note: '上游 + 自建分享库',
    icon: LibraryBig,
    tone: 'teal',
    to: '/media/strm',
  },
  {
    label: '追更订阅',
    value: dashboard.value.subscriptions?.total,
    note: '正在关注的作品',
    icon: Bell,
    tone: 'orange',
    to: '/media/subscriptions',
  },
]);
const ingest = computed(() => dashboard.value.ingest || {});
const recent = computed(() => overview.data.value?.activity?.recent || []);
function load() {
  void jobs.run((signal) => readData('/api/cloud/jobs', signal));
  return overview.run(mediaApi.overview);
}
const jobLabels: Record<string, string> = {
  transfer: '链接转存',
  organize: '影视整理',
  workflow: '整理流程',
  sync: 'STRM 同步',
  intake: '资源接收',
  share_strm: '分享 STRM',
  maintenance: '账号维护',
};
const latestJobs = computed(() => records(jobs.data.value?.items).slice(0, 5));
const libraryCounts = computed(() => {
  const parse = (value: unknown) => Number(String(value || '0').replaceAll(',', ''));
  const local = parse(dashboard.value.localCount),
    share = parse(dashboard.value.shareCount);
  return { local, share, localRatio: local + share ? (local / (local + share)) * 100 : 0 };
});
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
  void loadPlayback();
});
useVisiblePoll(loadPlayback, 15000);
useActivityUpdates(() => jobs.run((signal) => readData('/api/cloud/jobs', signal)));
useVisiblePoll(load, 15000);
useLibraryUpdates(async () => {
  if (overview.loading.value) return false;
  return (await load()) ? undefined : false;
});
</script>
<template>
  <section class="page-heading pageheading dashboard-heading">
    <div>
      <span class="eyebrow">MEDIA OPERATIONS</span>
      <h1>运行总览</h1>
      <p>媒体资产、最近入库与运行动态。</p>
    </div>
    <div class="actions">
      <button
        class="button"
        :disabled="refreshing || overview.data.value?.refresh?.running"
        @click="refresh"
      >
        <RefreshCw
          :size="15"
          :class="{ spin: refreshing || overview.data.value?.refresh?.running }"
        />刷新总览</button
      ><RouterLink to="/organize/workspace" class="button primary"
        >打开整理工作台<ArrowUpRight :size="15"
      /></RouterLink>
    </div>
  </section>
  <section class="hero">
    <div class="hero-copy">
      <div class="eyebrow" style="color: #adc1ff">MEDIA OPERATIONS CONSOLE</div>
      <h2>每一份影视资源，都有清晰去处。</h2>
      <p>从资源接收、库内洗版到 STRM 入库，处理结果全程可追溯。</p>
    </div>
    <div class="hero-actions">
      <RouterLink to="/governance" class="btn">查看治理结果</RouterLink
      ><RouterLink to="/tasks" class="btn outline">全链路任务</RouterLink>
    </div>
  </section>
  <LoadState
    :loading="overview.loading.value && !overview.data.value"
    :error="overview.error.value"
    @retry="load"
  />
  <template v-if="overview.data.value">
    <div
      v-if="
        overview.data.value.refresh?.running ||
        overview.data.value.refresh?.error ||
        overview.data.value.stale
      "
      class="notice-bar"
      role="status"
    >
      {{
        overview.data.value.refresh?.error ||
        overview.data.value.refresh?.phase ||
        '正在显示上次快照，等待后台更新'
      }}<span class="push-right">快照 {{ date(overview.data.value.ts) }}</span>
    </div>
    <div v-for="warning in overview.data.value.refresh?.warnings" :key="warning" class="notice-bar">
      {{ warning }}
    </div>
    <div class="stats-grid metricrow">
      <RouterLink v-for="stat in stats" :key="stat.label" :to="stat.to" class="stat-card metric"
        ><div class="stat-label metrichead">
          {{ stat.label
          }}<span class="stat-icon metricicon" :class="stat.tone"
            ><component :is="stat.icon" :size="16"
          /></span>
        </div>
        <strong class="metricnum">{{ number(stat.value) }}</strong
        ><span class="metricsmall">{{ stat.note }}</span></RouterLink
      >
    </div>
    <MediaSources :snapshot="overview.data.value.media_sources" compact />
    <p v-if="dashboard.libraryHealth?.stale" class="data-caption">
      片库统计正在重新核验，当前显示上次快照。
    </p>
    <div class="grid cols2 dashboard-workspace">
      <div class="grid dashboard-primary">
        <section class="card cardpad" aria-label="最近入库">
          <header class="ctop">
            <div>
              <h2 class="ctitle">最近入库</h2>
              <p class="cdesc">Emby 已确认的媒体记录，点击海报查看作品。</p>
            </div>
            <RouterLink to="/tasks/reports" class="text-link"
              >查看全部<ArrowUpRight :size="14"
            /></RouterLink>
          </header>
          <p v-if="overview.data.value.activity?.ingest_error" class="inline-error">
            近期入库同步失败，保留上次记录。
          </p>
          <div v-if="recent.length" class="posters recent-poster-grid">
            <div
              v-for="(card, i) in recent.slice(0, 5)"
              :key="`${card.media_type}:${card.tmdb_id}:${i}`"
              class="recent-media-item"
            >
              <PosterCard
                :card="{ ...card, type: card.media_type }"
                priority
                compact
                hide-status
                @open="explore({ ...card, type: card.media_type })"
              />
              <p class="recent-media-fact">
                {{ card.episodes || '已入库'
                }}<span v-if="card.library"> · {{ card.library }}</span>
              </p>
            </div>
          </div>
          <div v-else class="empty">
            <Clapperboard :size="26" />
            <p>暂无已确认入库记录</p>
          </div>
          <div class="dashboard-ingest-note">
            <span>近 24 小时</span><strong>{{ number(ingest.movies) }} 部电影</strong
            ><strong>{{ number(ingest.series) }} 部剧集</strong
            ><strong>{{ number(ingest.episodes) }} 集</strong>
          </div>
        </section>
        <section class="card cardpad">
          <header class="ctop">
            <div>
              <h2 class="ctitle">续播与播放提醒</h2>
              <p class="cdesc">Emby 续播记录与已更新剧集。</p>
            </div>
            <button
              class="button small"
              :disabled="playback.loading.value || !!playback.data.value?.refreshing"
              @click="refreshPlayback"
            >
              {{ playback.data.value?.refreshing ? '同步中…' : '更新记录' }}
            </button>
          </header>
          <p v-if="playback.error.value || playback.data.value?.error" class="inline-error">
            {{ playback.error.value || playback.data.value?.error }}
          </p>
          <p v-if="playback.data.value?.ts" class="data-caption">
            快照 {{ date(Number(playback.data.value.ts)) }}
          </p>
          <div class="continue-watching-list">
            <article
              v-for="(row, index) in records(playback.data.value?.records).slice(0, 6)"
              :key="index"
            >
              <span class="eventicon"
                ><Film v-if="row.type === 'movie'" :size="20" /><Tv2 v-else :size="20"
              /></span>
              <div>
                <strong>{{ row.name }}</strong>
                <p>
                  {{ row.user
                  }}<span v-if="row.s">
                    · S{{ String(row.s).padStart(2, '0') }}E{{
                      String(row.e).padStart(2, '0')
                    }}</span
                  >
                </p>
                <progress
                  :value="Number(row.pct || 0)"
                  max="100"
                  :aria-label="`${row.name} 观看进度`"
                /><small>已观看 {{ row.pct }}%</small>
              </div>
            </article>
          </div>
          <p
            v-for="(row, index) in records(playback.data.value?.alerts).slice(0, 3)"
            :key="index"
            class="rule-note"
          >
            {{ row.text }}
          </p>
          <p
            v-if="
              !records(playback.data.value?.records).length &&
              !playback.data.value?.refreshing &&
              !playback.data.value?.error
            "
            class="muted-text"
          >
            暂无可续播记录。
          </p>
        </section>
        <section class="card cardpad library-distribution">
          <header class="ctop">
            <div>
              <h2 class="ctitle">双库播放引用</h2>
              <p class="cdesc">来自已核验的 NAS STRM 统计。</p>
            </div>
            <RouterLink to="/media/library" class="text-link">片库映射 →</RouterLink>
          </header>
          <div class="between">
            <span><i class="distribution-dot"></i>本地 {{ dashboard.localCount || '—' }}</span
            ><span
              ><i class="distribution-dot share"></i>分享 {{ dashboard.shareCount || '—' }}</span
            >
          </div>
          <div class="distribution-track">
            <span :style="{ width: `${libraryCounts.localRatio}%` }"></span>
          </div>
        </section>
      </div>
      <div class="grid dashboard-secondary">
        <section class="card cardpad">
          <header class="ctop">
            <div>
              <h2 class="ctitle">运行动态</h2>
              <p class="cdesc">最近网盘任务与实际执行状态。</p>
            </div>
            <RouterLink to="/logs" class="text-link">日志 →</RouterLink>
          </header>
          <p v-if="jobs.error.value" class="inline-error">{{ jobs.error.value }}</p>
          <RouterLink
            v-for="job in latestJobs"
            :key="String(job.id)"
            :to="{ path: '/tasks', query: { job: String(job.id) } }"
            class="event"
            ><span
              class="eventicon"
              :class="{
                green: job.state === 'success',
                orange: ['failed', 'uncertain', 'needs_review'].includes(String(job.state)),
              }"
              ><CheckCircle2 v-if="job.state === 'success'" :size="17" /><CircleAlert
                v-else
                :size="17" /></span
            ><span class="eventinfo"
              ><b>{{ jobLabels[String(job.kind)] || job.kind }}</b>
              <p>{{ job.message || stateLabel(String(job.state)) }}</p></span
            ><time>{{ date(Number(job.updated || job.created)) }}</time></RouterLink
          >
          <div v-if="!latestJobs.length && !jobs.error.value" class="empty">暂无任务动态</div>
        </section>
        <section class="card cardpad">
          <header class="ctop">
            <div>
              <h2 class="ctitle">服务状态</h2>
              <p class="cdesc">配置与服务回执，失败原因可继续核查。</p>
            </div>
            <RouterLink to="/system" class="text-link">详情 →</RouterLink>
          </header>
          <div v-for="(service, name) in dashboard.services" :key="name" class="event">
            <span class="eventicon" :class="service.ok ? 'green' : 'orange'"
              ><CheckCircle2 v-if="service.ok" :size="17" /><CircleAlert v-else :size="17" /></span
            ><span class="eventinfo"
              ><b>{{
                { emby: 'Emby 媒体服务', tmdb: 'TMDB 元数据', telegram: 'Telegram 通知' }[
                  String(name)
                ] || name
              }}</b>
              <p>{{ service.host || (service.ok ? '配置可用' : '等待配置或核验') }}</p></span
            >
          </div>
          <p v-if="!dashboard.services" class="muted-text">服务状态尚未同步</p>
        </section>
        <section class="card cardpad">
          <header class="ctop">
            <div>
              <h2 class="ctitle">媒体生产链路</h2>
              <p class="cdesc">接收、整理、生成与实际入库分别确认。</p>
            </div>
            <RouterLink to="/tasks" class="btn small">打开任务</RouterLink>
          </header>
          <nav class="flowline" aria-label="媒体处理工作流">
            <RouterLink v-for="(flow, i) in workflows" :key="flow.to" :to="flow.to" class="flowstep"
              ><span class="num"><component :is="flow.icon" :size="17" /></span
              ><span
                ><strong>{{ flow.name }}</strong
                ><small>{{ flow.note }}</small></span
              ><ArrowRight v-if="i < workflows.length - 1" :size="13" class="flowarr"
            /></RouterLink>
          </nav>
        </section>
        <section class="card cardpad">
          <h2 class="ctitle">快捷操作</h2>
          <div class="quick-action-grid">
            <RouterLink to="/resources/receive?tab=batch"
              ><CloudDownload :size="18" />链接转存</RouterLink
            ><RouterLink to="/organize/workspace"><ArrowDownUp :size="18" />影视整理</RouterLink
            ><RouterLink to="/media/strm"><FileVideo :size="18" />生成 STRM</RouterLink
            ><RouterLink to="/governance"><ShieldCheck :size="18" />双库治理</RouterLink>
          </div>
        </section>
      </div>
    </div>
  </template>
  <section class="panel recommendation-panel">
    <header class="section-heading">
      <div>
        <h2>值得发现</h2>
        <p>发现下一部想看的作品。</p>
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
        priority
        @open="explore(card)"
      />
    </div>
  </section>
  <PlaybackMetrics v-if="overview.data.value" />
</template>
