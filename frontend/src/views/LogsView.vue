<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue';
import { readData, query, records, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import PageHeading from '../components/PageHeading.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
const request = useRequest<Data>(),
  bot = useRequest<Data>(),
  filters = reactive({ source: '', query: '', level: '', task: '' }),
  rows = ref<Data[]>([]),
  follow = ref(true),
  cursor = ref(0);
let applied = { ...filters };
function apply() {
  applied = { ...filters };
  void load();
}
async function load(more = false) {
  const response = await request.run((signal) =>
    readData(query('/api/logs', { ...applied, n: 200, after: more ? cursor.value : 0 }), signal),
  );
  if (response) {
    rows.value = more
      ? [...rows.value, ...records(response.logs)].slice(-1000)
      : records(response.logs);
    cursor.value = Number(response.cursor || 0);
  }
}
onMounted(() => {
  void load();
  void bot.run((signal) => readData('/api/bot/status', signal));
});
useVisiblePoll(() => (follow.value ? load(true) : undefined), 5000);
</script>
<template>
  <PageHeading title="运行日志" subtitle="按来源、级别和任务查找原因，查看结构化事件与机器人状态。"
    ><a href="/api/logs/download" class="button" download="strimkeep-logs.txt"
      >下载日志</a
    ></PageHeading
  >
  <form class="filter-panel" @submit.prevent="apply">
    <label>来源<input v-model="filters.source" placeholder="例如 cloud" /></label
    ><label
      >级别<select v-model="filters.level">
        <option value="">全部</option>
        <option>DEBUG</option>
        <option>INFO</option>
        <option>WARNING</option>
        <option>ERROR</option>
        <option>CRITICAL</option>
      </select></label
    ><label>关键词<input v-model="filters.query" maxlength="200" /></label
    ><label>任务编号前缀<input v-model="filters.task" maxlength="32" pattern="[a-f0-9]*" /></label
    ><button class="button primary">应用筛选</button
    ><label class="checkbox-label"><input v-model="follow" type="checkbox" />实时跟随</label>
  </form>
  <p v-if="request.error.value" class="inline-error" role="alert">{{ request.error.value }}</p>
  <p v-if="request.data.value?.degraded" class="notice-bar">
    日志存储降级，当前可能只显示内存记录。
  </p>
  <details class="panel">
    <summary>Telegram 机器人健康</summary>
    <p v-if="bot.error.value" class="inline-error">{{ bot.error.value }}</p>
    <EvidenceDetails :value="bot.data.value?.bot" /><button
      class="button small"
      @click="bot.run((signal) => readData('/api/bot/status', signal))"
    >
      刷新机器人状态
    </button>
  </details>
  <section class="log-stream">
    <article
      v-for="(row, index) in rows"
      :key="String(row.id || index)"
      :class="String(row.level).toLowerCase()"
    >
      <header>
        <span>{{ row.ts }}</span
        ><strong>{{ row.level }}</strong
        ><span>{{ row.source }}</span
        ><RouterLink
          v-if="object(row.details).job_id"
          :to="{ path: '/tasks', query: { job: String(object(row.details).job_id) } }"
          class="text-link"
          >查看任务</RouterLink
        >
      </header>
      <p>{{ row.message }}</p>
      <details v-if="Object.keys(object(row.details)).length">
        <summary>事件详情与处理原因</summary>
        <EvidenceDetails :value="row.details" />
      </details>
    </article>
    <p v-if="!rows.length && !request.loading.value" class="muted-text">当前筛选下暂无日志。</p>
  </section>
</template>
