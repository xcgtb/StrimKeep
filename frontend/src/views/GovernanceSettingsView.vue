<script setup lang="ts">
import { computed, ref, onMounted } from 'vue';
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
const decisionOptions = [
  { value: 'quality_first', label: '画质优先' },
  { value: 'keep_local', label: '优先保留本地' },
  { value: 'keep_share', label: '优先保留分享' },
];
const matchOptions = [
  { value: 'title_year', label: '片名与年份' },
  { value: 'tmdb_first', label: 'TMDB 优先' },
];
const seasonOptions = [
  { value: 'compare', label: '开启比较保护' },
  { value: 'off', label: '关闭' },
];
const specialOptions = [
  { value: 'compare', label: '参与比较' },
  { value: 'ignore', label: '忽略' },
  { value: 'delete', label: '按删除策略处理' },
];
const ratio = computed(() => {
  const raw = Number(form.value.season_replace_ratio);
  return Number.isFinite(raw) ? Math.min(1, Math.max(0.5, raw)) : 0.9;
});
function setRatio(event: Event) {
  form.value = {
    ...form.value,
    season_replace_ratio: Number((event.target as HTMLInputElement).value),
  };
}
const exemptText = computed({
  get: () =>
    Array.isArray(form.value.exempt_keywords)
      ? (form.value.exempt_keywords as string[]).join('\n')
      : '',
  set: (value: string) => {
    form.value = {
      ...form.value,
      exempt_keywords: value
        .split(/\r?\n/)
        .map((item) => item.trim())
        .filter(Boolean),
    };
  },
});
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
  <section class="panel">
    <header class="section-heading">
      <div>
        <h2>双库决策策略</h2>
        <p class="muted-text">
          决定本地库与分享库冲突时谁保留；保存后旧治理计划会因规则指纹变化而失效。
        </p>
      </div>
      <button
        class="button primary"
        :disabled="
          !strategy.data.value || !!strategy.error.value || op.busy.value || op.uncertain.value
        "
        @click="confirmation = 'strategy'"
      >
        保存治理策略
      </button>
    </header>
    <div class="decision-grid" :class="{ disabled: op.busy.value || op.uncertain.value }">
      <label class="decision-field">
        <span>决策策略</span>
        <select v-model="form.decision" :disabled="op.busy.value || op.uncertain.value">
          <option v-for="o in decisionOptions" :key="o.value" :value="o.value">
            {{ o.label }}
          </option>
        </select>
        <small>画质优先后按七维依次比较；保留类选项直接中止替换。</small>
      </label>
      <label class="decision-field">
        <span>作品匹配</span>
        <select v-model="form.match_strategy" :disabled="op.busy.value || op.uncertain.value">
          <option v-for="o in matchOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
        <small>TMDB 优先时按 TMDB 编号配对，片名与年份仅作兜底。</small>
      </label>
      <label class="decision-field">
        <span>多季保护</span>
        <select v-model="form.multi_season_protect" :disabled="op.busy.value || op.uncertain.value">
          <option v-for="o in seasonOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
        <small>开启后，整季被替换需要先通过比例阈值。</small>
      </label>
      <label class="decision-field switch-field">
        <span>平局保留本地</span>
        <input
          v-model="form.tie_keep_local"
          type="checkbox"
          class="quality-switch"
          :disabled="op.busy.value || op.uncertain.value"
        />
        <small>七维完全打平时不替换，保留本地版本。</small>
      </label>
      <div class="decision-field wide">
        <span>分享达标比例</span>
        <div class="ratio-row">
          <input
            :value="ratio"
            :style="{ '--ratio-fill': `${((ratio - 0.5) / 0.5) * 100}%` }"
            type="range"
            min="0.5"
            max="1"
            step="0.01"
            :disabled="op.busy.value || op.uncertain.value"
            aria-label="分享达标比例"
            @input="setRatio"
          />
          <output>{{ Math.round(ratio * 100) }}%</output>
        </div>
        <small>评分/集数达到本地该比例时，才认为分享版本可以整季替换。</small>
      </div>
      <label class="decision-field">
        <span>特别篇处理</span>
        <select v-model="form.special_action" :disabled="op.busy.value || op.uncertain.value">
          <option v-for="o in specialOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
        <small>特辑盘（S00）独立于正片季度单独判定。</small>
      </label>
      <label class="decision-field wide">
        <span>豁免关键词（白名单）</span>
        <textarea
          v-model="exemptText"
          rows="4"
          placeholder="每行一个关键词，命中的作品不参与治理"
          :disabled="op.busy.value || op.uncertain.value"
        ></textarea>
        <small>保存后可用下方「扫描豁免匹配」核对实际命中的目录。</small>
      </label>
    </div>
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
