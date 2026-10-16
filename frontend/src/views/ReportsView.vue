<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { readData, writeData, object, records, strings, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { date } from '../composables/format';
import PageHeading from '../components/PageHeading.vue';
import FormFields, { type Field } from '../components/FormFields.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import ModalDialog from '../components/ModalDialog.vue';
import OperationState from '../components/OperationState.vue';
const ingest = useRequest<Data>(),
  recordsRequest = useRequest<Data>(),
  morning = useRequest<Data>(),
  detail = useRequest<Data>(),
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
      v-for="(label, key) in { ingest: '近期入库', records: '执行记录', morning: '每日晨报' }"
      :key="key"
      :class="{ active: tab === key }"
      @click="tab = key"
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
    <h2>已确认的近期入库统计</h2>
    <EvidenceDetails :value="ingest.data.value" />
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
