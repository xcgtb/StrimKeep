<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { readData, writeData, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { useOperation } from '../composables/useOperation';
import { date, number } from '../composables/format';
import OperationState from './OperationState.vue';
const metrics = useRequest<Data>(),
  op = useOperation(),
  days = ref(7);
function load() {
  return metrics.run((signal) =>
    readData(`/api/overview/playback/metrics?days=${days.value}`, signal),
  );
}
async function toggle() {
  const r = await op.run(() =>
    writeData('/api/overview/playback/collection', { enabled: !metrics.data.value?.enabled }),
  );
  if (r) await load();
}
async function verifyCollection() {
  const r = await load();
  if (r) op.reconcile(true);
}
const trend = computed(() => records(metrics.data.value?.trend));
const ceiling = computed(() => Math.max(1, ...trend.value.map((r) => Number(r.seconds))));
function duration(n: unknown) {
  const seconds = Math.round(Number(n || 0));
  return seconds < 60
    ? `${seconds} 秒`
    : seconds < 3600
      ? `${Math.floor(seconds / 60)} 分钟`
      : `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟`;
}
onMounted(load);
useVisiblePoll(load, 15000);
</script>
<template>
  <section class="card cardpad playback-metrics" aria-label="真实播放统计">
    <header class="ctop">
      <div>
        <span class="eyebrow">PLAYBACK</span>
        <h2 class="ctitle">播放统计</h2>
        <p class="cdesc">{{ metrics.data.value?.source || 'Emby 播放会话' }}</p>
      </div>
      <div class="action-row">
        <select v-model="days" aria-label="播放统计范围" @change="load">
          <option :value="7">近7天</option>
          <option :value="30">近30天</option>
          <option :value="90">近90天</option></select
        ><button
          class="button small"
          :disabled="
            metrics.loading.value ||
            op.busy.value ||
            op.uncertain.value ||
            !metrics.data.value?.configured
          "
          @click="toggle"
        >
          {{ metrics.data.value?.enabled ? '暂停采集' : '启用采集' }}
        </button>
      </div>
    </header>
    <OperationState
      :busy="op.busy.value"
      :error="op.error.value || metrics.error.value"
      :uncertain="op.uncertain.value"
    />
    <button
      v-if="op.uncertain.value"
      class="button"
      :disabled="metrics.loading.value"
      @click="verifyCollection"
    >
      重新读取采集状态
    </button>
    <p v-if="!metrics.data.value?.configured" class="notice-bar">
      先在系统设置配置 Emby 连接，再启用播放采集。<RouterLink
        class="text-link"
        to="/settings/system"
        >配置连接 →</RouterLink
      >
    </p>
    <p v-if="metrics.data.value?.error" class="inline-error">{{ metrics.data.value.error }}</p>
    <div class="playback-totals">
      <div>
        <small>观看人数</small><strong>{{ number(metrics.data.value?.viewers) }}<em>人</em></strong>
      </div>
      <div>
        <small>观测观看时长</small><strong>{{ duration(metrics.data.value?.seconds) }}</strong>
      </div>
      <div>
        <small>有效播放段</small><strong>{{ number(metrics.data.value?.plays) }}<em>段</em></strong>
      </div>
    </div>
    <div class="playback-trend" role="img" :aria-label="`近${days}天观测观看时长趋势，按UTC日期`">
      <div
        v-for="row in trend"
        :key="String(row.day)"
        :title="`${row.day} · ${duration(row.seconds)}`"
      >
        <span :style="{ height: `${(Number(row.seconds) / ceiling) * 100}%` }"></span
        ><small v-if="days === 7 || trend.indexOf(row) % 7 === 0">{{
          String(row.day).slice(5)
        }}</small>
      </div>
    </div>
    <details class="technical-details">
      <summary>逐日播放数据（UTC）</summary>
      <div class="metrics-table">
        <table>
          <thead>
            <tr>
              <th>日期</th>
              <th>时长</th>
              <th>人数</th>
              <th>播放段</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in trend" :key="String(row.day)">
              <td>{{ row.day }}</td>
              <td>{{ duration(row.seconds) }}</td>
              <td>{{ row.viewers }}</td>
              <td>{{ row.plays }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
    <div class="playback-lists">
      <div>
        <h3>热门作品</h3>
        <ol>
          <li
            v-for="(row, i) in records(metrics.data.value?.rankings).slice(0, 5)"
            :key="String(row.item_id)"
          >
            <span>{{ i + 1 }}</span
            ><strong>{{ row.title }}</strong
            ><small>{{ duration(row.seconds) }}</small>
          </li>
        </ol>
        <p v-if="!records(metrics.data.value?.rankings).length" class="muted-text">
          启用后，观测到的播放将出现在这里。
        </p>
      </div>
      <div>
        <h3>活跃观众</h3>
        <ol>
          <li
            v-for="row in records(metrics.data.value?.users).slice(0, 5)"
            :key="String(row.user_id)"
          >
            <strong>{{ row.name }}</strong
            ><small>{{ duration(row.seconds) }}</small>
          </li>
        </ol>
      </div>
    </div>
    <div v-if="records(metrics.data.value?.active).length" class="active-playback">
      <strong>正在播放</strong>
      <p v-for="(row, i) in records(metrics.data.value?.active)" :key="i">
        {{ row.user }} · {{ row.title }}
        <span class="module-tag">{{ row.paused ? '已暂停' : '播放中' }}</span>
      </p>
    </div>
    <p class="data-caption">
      {{
        metrics.data.value?.enabled
          ? metrics.data.value?.stale
            ? '采集等待恢复'
            : '采集中'
          : '采集已暂停'
      }}
      · 最近观测 {{ date(Number(metrics.data.value?.last_poll))
      }}<span v-if="metrics.data.value?.gaps">
        · 已记录 {{ metrics.data.value.gaps }} 次采集空档</span
      >
    </p>
    <p class="data-caption">
      {{ metrics.data.value?.coverage
      }}<span v-if="metrics.data.value?.collected_since">
        记录起点 {{ date(Number(metrics.data.value.collected_since)) }}。</span
      >
    </p>
  </section>
</template>
