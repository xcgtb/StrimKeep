<script setup lang="ts">
import { ref, reactive, onMounted, nextTick, watch } from 'vue';
import { readData, query, records, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useActivityUpdates } from '../composables/useActivityUpdates';
import { useRoute } from 'vue-router';
import PageHeading from '../components/PageHeading.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
const request = useRequest<Data>(),
  bot = useRequest<Data>(),
  filters = reactive({
    source: '',
    query: '',
    level: '',
    task: String(useRoute().query.task || '').slice(0, 32),
  }),
  rows = ref<Data[]>([]),
  follow = ref(true),
  autoScroll = ref(true),
  cursor = ref(0),
  stream = ref<HTMLElement>();
let applied = { ...filters };
function quickLevel(value: string) {
  filters.level = value;
  apply();
}
function apply() {
  applied = { ...filters };
  cursor.value = 0;
  rows.value = [];
  request.cancel();
  void load();
}
async function load(more = false) {
  if (request.loading.value) return;
  const response = await request.run((signal) =>
    readData(query('/api/logs', { ...applied, n: 200, after: more ? cursor.value : 0 }), signal),
  );
  if (response) {
    const next = records(response.logs);
    if (more) {
      const seen = new Set(rows.value.map((row) => String(row.id)));
      rows.value = [...rows.value, ...next.filter((row) => !seen.has(String(row.id)))].slice(-1000);
    } else rows.value = next;
    cursor.value = Number(response.cursor || 0);
    if (autoScroll.value && follow.value) {
      await nextTick();
      stream.value?.scrollTo({ top: stream.value.scrollHeight });
    }
  }
}
function clearVisible() {
  request.cancel();
  rows.value = [];
  // Keep the cursor so previously displayed events do not reappear.
  // Only clears this browser view; never deletes persistent server logs.
}
onMounted(() => {
  void load();
  void bot.run((signal) => readData('/api/bot/status', signal));
});
watch(follow, (enabled) => {
  if (enabled) void load(true);
});
const live = useActivityUpdates(() => (follow.value ? load(true) : undefined));
</script>
<template>
  <PageHeading
    title="实时日志中心"
    subtitle="任务事件实时推送；断线自动补读，暂停跟随不影响后台执行。"
  >
    <a href="/api/logs/download" class="button" download="strimkeep-logs.txt">导出日志</a>
  </PageHeading>
  <div class="log-toolbar panel">
    <div class="log-console-status">
      <span class="status-dot" :class="{ muted: !follow }"></span>
      <strong>{{ follow ? '实时跟随中' : '已暂停跟随' }}</strong>
      <small
        >{{ rows.length }} 条 ·
        {{ live.connected.value ? '事件推送已连接' : '自动重连 · 15 秒补读' }}</small
      >
      <button class="button small" @click="follow = !follow">{{ follow ? '暂停' : '继续' }}</button>
      <button class="button small" :disabled="request.loading.value" @click="load(true)">
        立即更新
      </button>
      <button class="button small" @click="clearVisible">清空屏幕</button>
    </div>
    <div class="filterbar log-level-chips">
      <button
        v-for="(label, level) in { '': '全部', INFO: '信息', WARNING: '警告', ERROR: '异常' }"
        :key="level"
        class="chip"
        :class="{ active: filters.level === level }"
        @click="quickLevel(level)"
      >
        {{ label }}
      </button>
    </div>
    <details class="log-advanced-filters">
      <summary>按模块、关键词与任务筛选</summary>
      <form class="log-filter-grid" @submit.prevent="apply">
        <label
          >模块来源<input v-model="filters.source" placeholder="全部 / cloud / bot 等"
        /></label>
        <label
          >日志级别<select v-model="filters.level">
            <option value="">全部</option>
            <option>DEBUG</option>
            <option>INFO</option>
            <option>WARNING</option>
            <option>ERROR</option>
            <option>CRITICAL</option>
          </select></label
        >
        <label
          >关键词<input v-model="filters.query" maxlength="200" placeholder="搜索文件名或异常原因"
        /></label>
        <label
          >任务编号<input
            v-model="filters.task"
            maxlength="32"
            pattern="[a-f0-9]*"
            placeholder="可选"
        /></label>
        <button class="button primary">应用筛选</button>
      </form>
    </details>
    <label class="checkbox-label"
      ><input v-model="autoScroll" type="checkbox" />自动滚动到最新日志</label
    >
  </div>
  <p v-if="request.error.value" class="inline-error" role="alert">{{ request.error.value }}</p>
  <p v-if="request.data.value?.degraded" class="notice-bar">
    日志数据库暂不可用，展示可能不完整的内存记录。
  </p>
  <section
    ref="stream"
    class="log-stream live-log-console"
    aria-label="服务器实时业务日志"
    role="log"
    aria-live="off"
  >
    <article
      v-for="(row, index) in rows"
      :key="String(row.id || index)"
      :class="String(row.level).toLowerCase()"
    >
      <header>
        <span>{{ row.ts }}</span
        ><strong>{{ row.level }}</strong
        ><span>{{ row.source }}</span>
        <RouterLink
          v-if="object(row.details).job_id"
          :to="{ path: '/tasks', query: { job: String(object(row.details).job_id) } }"
          class="text-link"
          >查看任务</RouterLink
        >
      </header>
      <p>{{ row.message }}</p>
      <details v-if="Object.keys(object(row.details)).length">
        <summary>技术详情与原因</summary>
        <EvidenceDetails :value="row.details" />
      </details>
    </article>
    <p v-if="!rows.length && !request.loading.value" class="muted-text">
      当前无日志，或筛选条件未匹配到事件。
    </p>
  </section>
  <details class="panel">
    <summary>Telegram 机器人状态</summary>
    <p v-if="bot.error.value" class="inline-error">{{ bot.error.value }}</p>
    <EvidenceDetails :value="bot.data.value?.bot" /><button
      class="button small"
      @click="bot.run((signal) => readData('/api/bot/status', signal))"
    >
      刷新机器人状态
    </button>
  </details>
</template>
