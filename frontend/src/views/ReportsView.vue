<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { readData, writeData, object, records, strings, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { date } from '../composables/format';
import PageHeading from '../components/PageHeading.vue';
import FormFields, { type Field } from '../components/FormFields.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import IngestSummary from '../components/IngestSummary.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import ModalDialog from '../components/ModalDialog.vue';
import OperationState from '../components/OperationState.vue';
const ingest = useRequest<Data>(),
  recordsRequest = useRequest<Data>(),
  morning = useRequest<Data>(),
  detail = useRequest<Data>(),
  watched = useRequest<Data>(),
  libraryStats = useRequest<Data>(),
  op = useOperation(),
  tab = ref('ingest'),
  form = ref<Data>({}),
  items = ref<string[]>([]),
  previewText = ref(''),
  confirmation = ref(''),
  detailOpen = ref(false),
  limit = ref(50);
const fields: Field[] = [
  { key: 'enabled', label: '启用定时晨报', type: 'check' },
  { key: 'hour', label: '发送小时（24小时制）', type: 'number', min: 0, max: 23 },
  { key: 'minute', label: '发送分钟', type: 'number', min: 0, max: 59 },
  { key: 'prescan_min', label: '提前预热分钟', type: 'number', min: 0, max: 120 },
];
async function load() {
  await Promise.allSettled([
    ingest.run((signal) => readData('/api/ingest?full=1', signal)),
    recordsRequest.run((signal) => readData(`/api/records?n=${limit.value}`, signal)),
    morning.run((signal) => readData('/api/morning', signal)),
  ]);
  const cfg = object(morning.data.value?.morning);
  form.value = Object.fromEntries(fields.map((f) => [f.key, cfg[f.key]]));
  items.value = strings(cfg.items);
}
async function preview(force = false) {
  const r = await op.run(() => writeData('/api/morning/preview', { items: items.value, force }));
  if (r) previewText.value = String(r.text || '');
}
function selectTab(key: string) {
  tab.value = key;
  if (key === 'watched') void watched.run((signal) => readData('/api/played', signal));
  if (key === 'stats') void libraryStats.run((signal) => readData('/api/library_stats', signal));
}
async function execute() {
  const r = await op.run(() =>
    writeData(
      confirmation.value === 'save' ? '/api/morning' : '/api/morning/send',
      confirmation.value === 'save'
        ? { ...form.value, items: items.value }
        : { items: items.value, force: false },
    ),
  );
  if (r) confirmation.value = '';
}
async function showRecord(id: string) {
  detailOpen.value = true;
  detail.data.value = undefined;
  await detail.run((signal) => readData(`/api/records/${encodeURIComponent(id)}`, signal));
}
onMounted(load);
</script>
<template>
  <PageHeading title="入库汇报与晨报" subtitle="查看实际入库、治理执行记录，以及可预览的通知内容。"
    ><button
      class="button"
      @click="
        load();
        op.reconcile();
      "
    >
      刷新汇报
    </button></PageHeading
  >
  <nav class="browse-tabs">
    <button
      v-for="(label, key) in {
        ingest: '近期入库',
        records: '执行记录',
        morning: '每日晨报',
        stats: 'STRM 分类统计',
        watched: '观看进度',
      }"
      :key="key"
      :class="{ active: tab === key }"
      @click="selectTab(key)"
    >
      {{ label }}
    </button>
  </nav>
  <OperationState
    :busy="op.busy.value"
    :error="
      op.error.value || ingest.error.value || recordsRequest.error.value || morning.error.value
    "
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <section v-if="tab === 'ingest'" class="panel">
    <h2>Emby 近期入库 · 分类明细</h2>
    <p class="muted-text">按真实 Emby 记录呈现本地库、分享库与作品分类；不显示后端原始 JSON。</p>
    <IngestSummary :value="ingest.data.value" />
  </section>
  <section v-if="tab === 'stats'" class="panel">
    <header class="section-heading">
      <h2>NAS STRM 分类统计</h2>
      <button class="button" :disabled="libraryStats.loading.value" @click="selectTab('stats')">
        读取分类统计
      </button>
    </header>
    <p class="muted-text">
      统计 NAS 文件，不等同于 Emby 作品数量；无缓存或缓存过期时后端可能扫描目录。
    </p>
    <p v-if="libraryStats.loading.value">正在读取分类统计…</p>
    <p v-if="libraryStats.error.value" class="inline-error">{{ libraryStats.error.value }}</p>
    <p v-if="libraryStats.data.value" class="data-caption">
      统计时间 {{ date(Number(libraryStats.data.value.ts)) }} · 本地
      {{ libraryStats.data.value.local_total ?? '—' }} · 分享
      {{ libraryStats.data.value.share_total ?? '—' }}
    </p>
    <div class="report-stats-table">
      <table>
        <thead>
          <tr>
            <th>分类</th>
            <th>本地 STRM</th>
            <th>分享 STRM</th>
            <th>合计</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in records(libraryStats.data.value?.rows)" :key="String(row.name)">
            <td>{{ row.name }}</td>
            <td>{{ row.local }}</td>
            <td>{{ row.share }}</td>
            <td>{{ row.total }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-if="libraryStats.data.value" class="muted-text">
      未归类：本地 {{ libraryStats.data.value.local_other ?? 0 }} · 分享
      {{ libraryStats.data.value.share_other ?? 0 }}
    </p>
  </section>
  <section v-if="tab === 'watched'" class="panel">
    <header class="section-heading">
      <h2>Emby 观看进度与新集提醒</h2>
      <button class="button" :disabled="watched.loading.value" @click="selectTab('watched')">
        读取观看进度
      </button>
    </header>
    <p class="muted-text">手动读取 Emby 用户的续播记录，以及相对于观看位置的新入库集数。</p>
    <p v-if="watched.loading.value">正在读取 Emby 观看记录…</p>
    <p v-if="watched.error.value" class="inline-error">{{ watched.error.value }}</p>
    <div class="plan-item-list">
      <article v-for="(row, i) in records(watched.data.value?.records)" :key="i">
        <div>
          <strong>{{ row.name }}</strong>
          <p>{{ row.text }}</p>
        </div>
        <span>{{ row.pct }}%</span>
      </article>
    </div>
    <p v-for="(row, i) in records(watched.data.value?.alerts)" :key="i" class="notice-bar">
      {{ row.text }}
    </p>
    <p
      v-if="
        watched.data.value &&
        !records(watched.data.value.records).length &&
        !records(watched.data.value.alerts).length
      "
      class="muted-text"
    >
      当前没有续播记录或新集提醒。
    </p>
  </section>
  <section v-if="tab === 'records'" class="panel">
    <header class="section-heading">
      <h2>执行历史</h2>
      <label
        >读取条数<select
          v-model.number="limit"
          @change="recordsRequest.run((signal) => readData(`/api/records?n=${limit}`, signal))"
        >
          <option :value="50">50</option>
          <option :value="100">100</option>
          <option :value="200">200</option>
        </select></label
      >
    </header>
    <div class="plan-item-list">
      <article v-for="row in records(recordsRequest.data.value?.records)" :key="String(row.id)">
        <div>
          <strong>{{ row.title || row.cat || row.category }}</strong>
          <p>{{ row.summary || row.message }}</p>
          <small>{{ row.time || (row.ts && date(row.ts as string | number)) }}</small>
        </div>
        <button class="button small" @click="showRecord(String(row.id))">依据与结果</button>
      </article>
    </div>
  </section>
  <section v-if="tab === 'morning'" class="panel">
    <h2>晨报设置</h2>
    <p class="muted-text">
      按服务端时区 {{ object(morning.data.value?.tz).name || '已配置时区' }} 发送。
    </p>
    <FormFields v-model="form" :fields="fields" :disabled="op.busy.value || op.uncertain.value" />
    <div class="source-checkboxes">
      <label
        v-for="(name, key) in {
          stats: '片库统计',
          subscriptions: '追更动态',
          emby_gap: '缺集概况',
        }"
        :key="key"
        class="checkbox-label"
        ><input v-model="items" type="checkbox" :value="key" />{{ name }}</label
      >
    </div>
    <div class="action-row">
      <button
        class="button primary"
        :disabled="
          !morning.data.value || !!morning.error.value || op.busy.value || op.uncertain.value
        "
        @click="confirmation = 'save'"
      >
        保存晨报设置</button
      ><button class="button" :disabled="op.busy.value || op.uncertain.value" @click="preview()">
        读取缓存预览</button
      ><button
        class="button"
        :disabled="op.busy.value || op.uncertain.value"
        @click="preview(true)"
      >
        现场扫描后预览</button
      ><button
        class="button"
        :disabled="op.busy.value || op.uncertain.value"
        @click="confirmation = 'send'"
      >
        立即发送晨报
      </button>
    </div>
    <pre v-if="previewText" class="report-preview">{{ previewText }}</pre>
  </section>
  <ModalDialog
    v-if="detailOpen"
    title="执行记录详情"
    @close="
      detailOpen = false;
      detail.cancel();
    "
    ><p v-if="detail.error.value" class="inline-error">{{ detail.error.value }}</p>
    <EvidenceDetails :value="detail.data.value?.record" /></ModalDialog
  ><ConfirmAction
    v-if="confirmation"
    title="晨报操作确认"
    :description="
      confirmation === 'save'
        ? '保存定时晨报设置；启用后按上述内容与时间发送。'
        : '将当前选择的晨报缓存发送到已配置 Telegram 接收位置。'
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirmation = ''"
    @confirm="execute"
  />
</template>
