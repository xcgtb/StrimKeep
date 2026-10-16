<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { readData, writeData, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import PageHeading from '../components/PageHeading.vue';
import FormFields, { type Field } from '../components/FormFields.vue';
import ResourceSearch from '../components/ResourceSearch.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
const request = useRequest<Data>(),
  op = useOperation(),
  history = useRequest<Data>(),
  detail = useRequest<Data>(),
  subscriptions = useRequest<Data>(),
  events = useRequest<Data>(),
  form = ref<Data>({}),
  tab = ref('search'),
  keyword = ref(''),
  page = ref(1);
const fields: Field[] = [
  { key: 'enabled', label: '启用外部资源服务', type: 'check' },
  { key: 'url', label: '服务地址' },
  { key: 'token', label: 'Bearer Token（留空保留已保存值）', type: 'secret' },
  { key: 'max_points', label: '单次积分上限', type: 'number', min: 0, max: 10000 },
  { key: 'clear_token', label: '清除已保存令牌', type: 'check' },
];
async function load() {
  const r = await request.run((signal) => readData('/api/upstream/config', signal));
  if (r) {
    form.value = { ...object(r.settings), token: '', clear_token: false };
    delete form.value.has_token;
    op.reconcile();
  }
}
async function save() {
  const r = await op.run(() => writeData('/api/upstream/config', form.value));
  form.value.token = '';
  if (r) await load();
}
async function loadHistory() {
  await history.run((signal) =>
    readData(
      `/api/upstream/history?keyword=${encodeURIComponent(keyword.value)}&page=${page.value}`,
      signal,
    ),
  );
}
async function loadSubscriptions() {
  await Promise.allSettled([
    subscriptions.run((signal) => readData('/api/upstream/resource-subscriptions', signal)),
    events.run((signal) => readData('/api/upstream/subscription-events', signal)),
  ]);
}
onMounted(load);
</script>
<template>
  <PageHeading title="外部资源服务" subtitle="保留现有外部资源搜索、获取订阅、转存与历史能力。" />
  <nav class="browse-tabs">
    <button
      v-for="(label, key) in {
        search: '搜索与资源获取',
        subscriptions: '资源订阅动态',
        history: '上游整理历史',
        settings: '连接设置',
      }"
      :key="key"
      :class="{ active: tab === key }"
      @click="
        tab = key;
        if (key === 'subscriptions') loadSubscriptions();
      "
    >
      {{ label }}
    </button>
  </nav>
  <ResourceSearch v-if="tab === 'search'" /><OperationState
    :busy="op.busy.value"
    :error="
      op.error.value ||
      request.error.value ||
      history.error.value ||
      subscriptions.error.value ||
      events.error.value
    "
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <section v-if="tab === 'settings'" class="panel">
    <h2>外部服务配置</h2>
    <FormFields v-model="form" :fields="fields" :disabled="op.busy.value || op.uncertain.value" />
    <div class="action-row">
      <button class="button" @click="load">重新读取</button
      ><button
        class="button"
        :disabled="op.busy.value || op.uncertain.value"
        @click="op.run(() => writeData('/api/upstream/test', form))"
      >
        测试接口能力</button
      ><button
        class="button primary"
        :disabled="
          !request.data.value || !!request.error.value || op.busy.value || op.uncertain.value
        "
        @click="save"
      >
        保存外部服务连接
      </button>
    </div>
  </section>
  <section v-if="tab === 'history'" class="panel">
    <form
      class="search-toolbar"
      @submit.prevent="
        page = 1;
        loadHistory();
      "
    >
      <input
        v-model="keyword"
        aria-label="搜索外部整理记录"
        placeholder="搜索整理记录片名"
        required
      /><button class="button">搜索历史</button>
    </form>
    <EvidenceDetails :value="history.data.value?.items" />
    <div v-for="r in (history.data.value?.items as Data[]) || []" :key="String(r.id)">
      <button
        class="text-link"
        @click="detail.run((signal) => readData(`/api/upstream/history/${r.id}`, signal))"
      >
        查看 {{ r.title }} 原始处理字段
      </button>
    </div>
    <EvidenceDetails :value="detail.data.value?.fields" />
    <div class="pagination">
      <button
        class="button"
        :disabled="page <= 1"
        @click="
          page--;
          loadHistory();
        "
      >
        上一页</button
      ><span>{{ page }}</span
      ><button
        class="button"
        :disabled="((history.data.value?.items as Data[]) || []).length < 20"
        @click="
          page++;
          loadHistory();
        "
      >
        下一页
      </button>
    </div>
  </section>
  <section v-if="tab === 'subscriptions'" class="panel">
    <h2>资源获取订阅</h2>
    <EvidenceDetails :value="subscriptions.data.value" />
    <h3 class="detail-section-title">最近动态</h3>
    <EvidenceDetails :value="events.data.value?.items" />
  </section>
</template>
