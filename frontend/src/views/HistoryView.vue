<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { get } from '../api/client';
import { date, object, records, text } from '../api/domain';

const logs = ref<Record<string, unknown>[]>([]);
const level = ref('');
const source = ref('');
const keyword = ref('');
const loading = ref(false);
const paused = ref(false);
const error = ref('');
let timer: number | undefined;

const filtered = computed(() =>
  logs.value.filter((row) => {
    if (level.value && text(row.level) !== level.value) return false;
    if (source.value && !text(row.source).startsWith(source.value)) return false;
    if (keyword.value) {
      const needle = keyword.value.toLowerCase();
      if (!`${text(row.message)} ${text(row.details)}`.toLowerCase().includes(needle)) return false;
    }
    return true;
  }),
);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = object(await get<Record<string, unknown>>('/api/logs?limit=200'));
    logs.value = records(data.logs);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
onMounted(() => {
  void load();
  timer = window.setInterval(() => {
    if (!paused.value) void load();
  }, 5000);
});
onUnmounted(() => window.clearInterval(timer));
</script>
<template>
  <section class="tab active">
    <div class="panel log-panel">
      <div class="row-between log-toolbar">
        <div>
          <h3 data-icon="list">日志中心</h3>
          <div class="log-status">转存、整理、命名、STRM、Emby 入库与机器人实时日志</div>
        </div>
        <div class="log-controls">
          <select v-model="source" aria-label="日志模块">
            <option value="">全部模块</option>
            <option value="cloud">我的网盘全流程</option>
            <option value="cloud_transfer">分享转存</option>
            <option value="cloud_recognize">影视识别</option>
            <option value="cloud_probe">媒体信息</option>
            <option value="cloud_naming">重命名</option>
            <option value="cloud_organize">整理归档</option>
            <option value="cloud_strm">STRM 生成</option>
            <option value="cloud_emby">Emby 入库</option>
            <option value="cloud_notify">通知发送</option>
            <option value="cloud_governance">七维源删除</option>
            <option value="bot">机器人</option>
          </select>
          <input v-model="keyword" placeholder="搜索文件名或任务号" aria-label="日志关键词" />
          <select v-model="level" aria-label="日志级别">
            <option value="">全部级别</option>
            <option value="INFO">信息</option>
            <option value="WARNING">警告 / 待核实</option>
            <option value="ERROR">错误</option>
          </select>
          <button class="btn gray sm" @click="load">刷新</button>
          <button class="btn gray sm" @click="paused = !paused">
            {{ paused ? '恢复自动刷新' : '暂停自动刷新' }}
          </button>
        </div>
      </div>
      <p v-if="error" class="section-note">{{ error }}</p>
      <div class="runtime-logs">
        <div v-if="!filtered.length" class="list-empty">
          {{ loading ? '读取中…' : '没有符合条件的日志' }}
        </div>
        <div v-for="row in filtered" :key="text(row.id)" class="log-row">
          <div class="log-meta">
            <time>{{ date(Number(row.ts)) }}</time>
            <span class="log-source">{{ text(row.source) }}</span>
            <span class="log-level" :class="'log-' + text(row.level).toLowerCase()">{{ text(row.level) }}</span>
          </div>
          <pre>{{ text(row.message) }}</pre>
        </div>
      </div>
    </div>
  </section>
</template>
