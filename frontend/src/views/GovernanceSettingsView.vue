<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { readData, writeData, object, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import defaults from '../config/quality-defaults.json';
import FormFields, { type Field } from '../components/FormFields.vue';
import QualityRules from '../components/QualityRules.vue';
import PageHeading from '../components/PageHeading.vue';
import OperationState from '../components/OperationState.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
const strategy = useRequest<Data>(),
  cover = useRequest<Data>(),
  exemptions = useRequest<Data>(),
  auto = useRequest<Data>(),
  op = useOperation(),
  form = ref<Data>({}),
  rules = ref<Data[]>([]),
  automatic = ref<Data>({}),
  comparison = ref<Data>(),
  a = ref(''),
  b = ref(''),
  confirmation = ref('');
const fields: Field[] = [
  {
    key: 'decision',
    label: '决策策略',
    type: 'select',
    options: [
      { value: 'quality_first', label: '画质优先' },
      { value: 'keep_local', label: '优先保留本地' },
      { value: 'keep_share', label: '优先保留分享' },
    ],
  },
  {
    key: 'match_strategy',
    label: '作品匹配',
    type: 'select',
    options: [
      { value: 'title_year', label: '片名与年份' },
      { value: 'tmdb_first', label: 'TMDB 优先' },
    ],
  },
  {
    key: 'multi_season_protect',
    label: '多季保护',
    type: 'select',
    options: [
      { value: 'compare', label: '开启比较保护' },
      { value: 'off', label: '关闭' },
    ],
  },
  { key: 'tie_keep_local', label: '平局保留本地', type: 'check' },
  {
    key: 'season_replace_ratio',
    label: '分享达标比例（0.5～1）',
    type: 'number',
    min: 0.5,
    max: 1,
    step: 0.01,
  },
  {
    key: 'special_action',
    label: '特别篇处理',
    type: 'select',
    options: [
      { value: 'compare', label: '参与比较' },
      { value: 'ignore', label: '忽略' },
      { value: 'delete', label: '按删除策略处理' },
    ],
  },
  { key: 'exempt_keywords', label: '豁免关键词（每行一个）', type: 'lines' },
];
const autoFields: Field[] = [
  { key: 'enabled', label: '启用定期治理诊断', type: 'check' },
  { key: 'interval_hours', label: '诊断间隔（小时）', type: 'number', min: 1, max: 168 },
  { key: 'notify_when_clean', label: '没有待治理项也通知', type: 'check' },
  { key: 'only_on_change', label: '仅变化时通知', type: 'check' },
];
async function load() {
  const result = await Promise.allSettled([
    strategy.run((signal) => readData('/api/strategy', signal)),
    cover.run((signal) => readData('/api/cover-strategy', signal)),
    auto.run((signal) => readData('/api/governance/auto', signal)),
  ]);
  void result;
  form.value = structuredClone(object(strategy.data.value?.strategy));
  rules.value = structuredClone(records(object(cover.data.value?.strategy).rules));
  const cfg = object(auto.data.value?.settings);
  automatic.value = Object.fromEntries(autoFields.map((f) => [f.key, cfg[f.key]]));
  op.reconcile();
}
async function save() {
  const action = confirmation.value;
  const r = await op.run(() =>
    writeData(
      action === 'strategy'
        ? '/api/strategy'
        : action === 'cover'
          ? '/api/cover-strategy'
          : '/api/governance/auto',
      action === 'strategy'
        ? form.value
        : action === 'cover'
          ? { strategy: { rules: rules.value } }
          : automatic.value,
    ),
  );
  if (r) {
    confirmation.value = '';
    if (action === 'strategy') strategy.data.value = r;
    else if (action === 'cover') cover.data.value = r;
    else auto.data.value = r;
  }
}
async function test() {
  const r = await op.run(() => writeData('/api/cover-compare', { a: a.value, b: b.value }));
  if (r) comparison.value = r.compare as Data;
}
function restoreCover() {
  rules.value = structuredClone(defaults.cover.rules);
}
onMounted(load);
</script>
<template>
  <PageHeading title="七维与治理规则" subtitle="规则显式保存，更新镜像不会重置你的配置。"
    ><button class="button" @click="load">重新读取已保存配置</button></PageHeading
  ><OperationState
    :busy="op.busy.value"
    :error="op.error.value || strategy.error.value || cover.error.value || auto.error.value"
    :uncertain="op.uncertain.value"
  />
  <nav class="context-tabs">
    <RouterLink to="/governance">治理决策</RouterLink
    ><RouterLink to="/governance/rules">规则与巡检</RouterLink
    ><RouterLink to="/governance/cleanup">目录清理</RouterLink>
  </nav>
  <section class="panel">
    <h2>双库决策策略</h2>
    <FormFields
      v-model="form"
      :fields="fields"
      :disabled="op.busy.value || op.uncertain.value"
    /><button
      class="button primary"
      :disabled="
        !strategy.data.value || !!strategy.error.value || op.busy.value || op.uncertain.value
      "
      @click="confirmation = 'strategy'"
    >
      保存治理策略
    </button>
  </section>
  <section class="panel">
    <header class="section-heading">
      <h2>七维优先级</h2>
      <button class="button" @click="restoreCover()">恢复默认草稿</button>
    </header>
    <p class="muted-text">上方维度优先；同维档位越靠前优先。STRM 文件大小不参与视频体积比较。</p>
    <QualityRules v-model="rules" :disabled="op.busy.value || op.uncertain.value" /><button
      class="button primary"
      :disabled="!cover.data.value || !!cover.error.value || op.busy.value || op.uncertain.value"
      @click="confirmation = 'cover'"
    >
      保存七维规则
    </button>
  </section>
  <section class="panel">
    <h2>豁免目录核对</h2>
    <p class="muted-text">按照已保存的白名单关键词扫描实际目录，不删除文件。</p>
    <button
      class="button"
      :disabled="exemptions.loading.value"
      @click="exemptions.run((signal) => readData('/api/exempt/scan', signal))"
    >
      扫描豁免匹配
    </button>
    <p v-if="exemptions.error.value" class="inline-error">{{ exemptions.error.value }}</p>
    <EvidenceDetails :value="exemptions.data.value?.matches" />
  </section>
  <section class="panel">
    <h2>比较文件名</h2>
    <form class="form-grid" @submit.prevent="test">
      <label>分享版本文件名<input v-model="a" required /></label
      ><label>本地版本文件名<input v-model="b" required /></label
      ><button class="button" :disabled="op.busy.value || op.uncertain.value">
        按已保存规则比较
      </button>
    </form>
    <EvidenceDetails v-if="comparison" :value="comparison" />
  </section>
  <section class="panel">
    <h2>定期治理诊断与通知</h2>
    <p class="muted-text">按后端既有行为运行诊断与通知，不代表自动执行删除。</p>
    <FormFields
      v-model="automatic"
      :fields="autoFields"
      :disabled="op.busy.value || op.uncertain.value"
    /><button
      class="button primary"
      :disabled="!auto.data.value || !!auto.error.value || op.busy.value || op.uncertain.value"
      @click="confirmation = 'auto'"
    >
      保存诊断计划
    </button>
  </section>
  <ConfirmAction
    v-if="confirmation"
    title="保存治理规则"
    description="保存后，旧治理计划可能因规则指纹变化而失效。请重新诊断后再执行清理。"
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirmation = ''"
    @confirm="save"
  />
</template>
