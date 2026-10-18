<script setup lang="ts">
import { computed, onMounted } from 'vue';
import {
  Activity,
  Database,
  FolderOpen,
  LibraryBig,
  RefreshCw,
  CheckCircle2,
  CircleAlert,
  Clock3,
} from '@lucide/vue';
import { readData, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { date } from '../composables/format';
import PageHeading from '../components/PageHeading.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
const runtime = useRequest<Data>(),
  storage = useRequest<Data>(),
  health = useRequest<Data>(),
  paths = useRequest<Data>(),
  consistency = useRequest<Data>();
const statistics = computed(() => object(runtime.data.value?.statistics));
const cards = computed(() => [
  {
    title: '应用服务',
    value: runtime.error.value
      ? '读取失败'
      : object(runtime.data.value?.readiness).ready === true
        ? '已就绪'
        : '等待初始化',
    note:
      runtime.error.value || object(runtime.data.value?.readiness).phase || '服务启动与初始化状态',
    ok: object(runtime.data.value?.readiness).ready === true,
    icon: Activity,
  },
  {
    title: '数据存储',
    value: storage.error.value
      ? '读取失败'
      : object(storage.data.value?.storage).degraded
        ? '需要处理'
        : storage.data.value
          ? '已读取状态'
          : '等待状态',
    note: storage.error.value || '数据库和持久化记录',
    ok: !!storage.data.value && !object(storage.data.value.storage).degraded,
    icon: Database,
  },
  {
    title: '媒体统计',
    value: statistics.value.error
      ? '需要重新核验'
      : statistics.value.running
        ? '正在统计'
        : statistics.value.success_at
          ? '最近扫描成功'
          : '待初始化',
    note:
      statistics.value.error ||
      (statistics.value.success_at
        ? `上次成功 ${date(Number(statistics.value.success_at))}`
        : '等待一次完整目录统计'),
    ok: !!statistics.value.success_at && !statistics.value.error,
    icon: LibraryBig,
  },
]);
async function load() {
  await Promise.allSettled([
    runtime.run((signal) => readData('/api/runtime/status', signal)),
    storage.run((signal) => readData('/api/storage/status', signal)),
    health.run((signal) => readData('/api/library/health', signal)),
    paths.run((signal) => readData('/api/health', signal)),
    consistency.run((signal) => readData('/api/consistency', signal)),
  ]);
}
onMounted(load);
useVisiblePoll(() => runtime.run((signal) => readData('/api/runtime/status', signal)), 15000);
</script>
<template>
  <PageHeading title="系统状态" subtitle="查看运行任务、存储、目录与媒体统计的实际状态。"
    ><button class="button primary" @click="load">
      <RefreshCw :size="15" />更新状态
    </button></PageHeading
  >
  <div class="grid cols3 system-health-cards">
    <article v-for="card in cards" :key="card.title" class="card cardpad">
      <div class="between">
        <span class="metricicon" :class="card.ok ? 'green' : 'orange'"
          ><component :is="card.icon" :size="18" /></span
        ><CheckCircle2 v-if="card.ok" class="success-text" :size="18" /><CircleAlert
          v-else
          class="warning-text"
          :size="18"
        />
      </div>
      <h2>{{ card.title }}</h2>
      <strong>{{ card.value }}</strong>
      <p class="muted-text">{{ card.note }}</p>
    </article>
  </div>
  <div v-if="statistics.error" class="notice-bar">
    <CircleAlert :size="17" /><span>{{ statistics.error }}</span
    ><RouterLink to="/dashboard" class="text-link">前往总览重新核验 →</RouterLink>
  </div>
  <div class="two-column mt18">
    <section class="panel">
      <header class="section-heading">
        <h2><FolderOpen :size="17" /> NAS 目录</h2>
        <span class="muted-text">容器内实际映射</span>
      </header>
      <p v-if="paths.error.value" class="inline-error">{{ paths.error.value }}</p>
      <div v-for="(value, key) in object(paths.data.value?.paths)" :key="key" class="event">
        <span class="eventicon"><FolderOpen :size="17" /></span
        ><span class="eventinfo"
          ><b>{{
            {
              L_ROOT: '本地 STRM',
              S_ROOT: '分享 STRM',
              CLOUD_L_ROOT: '云端挂载',
              DATA_DIR: '应用数据',
            }[String(key)] || key
          }}</b>
          <p class="path-text">{{ value }}</p></span
        >
      </div>
      <p v-if="!Object.keys(object(paths.data.value?.paths)).length" class="muted-text">
        尚无目录状态
      </p>
      <p class="data-caption">目录可见性与完整扫描结果分别核验。</p>
    </section>
    <section class="panel">
      <header class="section-heading">
        <h2><Clock3 :size="17" /> 当前后台任务</h2>
        <RouterLink to="/tasks" class="text-link">全部任务 →</RouterLink>
      </header>
      <template v-if="runtime.data.value?.task"
        ><p>{{ object(runtime.data.value.task).kind }}</p>
        <p class="muted-text">{{ object(runtime.data.value.task).source }}</p>
        <RouterLink to="/tasks" class="button">查看执行进度</RouterLink></template
      >
      <p v-else class="muted-text">
        {{ runtime.data.value ? '当前没有执行中的管理任务。' : '任务状态尚未读取。' }}
      </p>
      <h3>运行资源</h3>
      <dl class="business-facts">
        <div>
          <dt>应用版本</dt>
          <dd>
            {{
              String(runtime.data.value?.version || '').match(/^\d+\.\d+\.\d+/)?.[0] || '本地开发'
            }}
          </dd>
        </div>
        <div>
          <dt>内存</dt>
          <dd>{{ object(runtime.data.value?.memory).rss_mib ?? '—' }} MiB</dd>
        </div>
      </dl>
    </section>
  </div>
  <section class="panel">
    <h2>进一步诊断</h2>
    <div
      v-for="(state, label) in {
        服务详情: runtime,
        数据存储: storage,
        片库健康: health,
        NAS路径与服务时区: paths,
        事实一致性: consistency,
      }"
      :key="label"
    >
      <p v-if="state.error.value" class="inline-error">{{ label }}：{{ state.error.value }}</p>
      <details class="technical-details">
        <summary>{{ label }}</summary>
        <EvidenceDetails :value="state.data.value" />
      </details>
    </div>
  </section>
</template>
