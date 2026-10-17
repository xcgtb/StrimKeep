<script setup lang="ts">
import { computed, ref, onMounted } from 'vue';
import { readData, writeData, object, records, strings, type Data } from '../api/domain';
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
const keywords = computed({
  get: () => strings(form.value.exempt_keywords).join('\n'),
  set: (value: string) => { form.value.exempt_keywords = value.split('\n').map((s) => s.trim()).filter(Boolean); },
});
const decisionOptions = [
  { value: 'quality_first', label: '画质优先（默认）', note: '首先比较完整度，完整后按七维顺序择优。' },
  { value: 'keep_local', label: '优先保留本地', note: '尽量保留本地版，适用于本地片库需要保护的场景。' },
  { value: 'keep_share', label: '优先保留分享', note: '倾向保留分享版，仍遵守缺集保护与人工确认。' },
];
const matchOptions = [
  { value: 'title_year', label: '剧名＋年份', note: '按规范化名称与年份匹配。' },
  { value: 'tmdb_first', label: 'TMDB 优先', note: '优先使用可靠 TMDB ID，存在冲突时需要核对。' },
];
const specialOptions = [
  { value: 'compare', label: '画质比较', note: 'S00 按保留策略参与对照。' },
  { value: 'ignore', label: '忽略', note: 'S00 不参与治理清理。' },
  { value: 'delete', label: '清理', note: 'S00 按规则形成可人工确认的清理计划。' },
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
  <section id="governance-strategy" class="panel">
    <h2>治理决策策略</h2>
    <p class="muted-text">仅作用于双库治理的扫描和计划；修改后必须重新扫描，不会直接删除文件。</p>
    <div class="strategy-selection">
      <label v-for="option in decisionOptions" :key="option.value" class="strategy-option" :class="{ selected: form.decision === option.value }">
        <input v-model="form.decision" type="radio" name="governance-decision" :value="option.value" :disabled="op.busy.value || op.uncertain.value" />
        <span><strong>{{ option.label }}</strong><small>{{ option.note }}</small></span>
      </label>
    </div>
    <h3>作品身份匹配</h3>
    <div class="strategy-selection strategy-two">
      <label v-for="option in matchOptions" :key="option.value" class="strategy-option" :class="{ selected: form.match_strategy === option.value }">
        <input v-model="form.match_strategy" type="radio" name="governance-match" :value="option.value" :disabled="op.busy.value || op.uncertain.value" />
        <span><strong>{{ option.label }}</strong><small>{{ option.note }}</small></span>
      </label>
    </div>
    <div class="strategy-controls">
      <label class="checkbox-label"><input v-model="form.tie_keep_local" type="checkbox" :disabled="op.busy.value || op.uncertain.value" />七维平局保留本地版本</label>
      <label class="checkbox-label"><input v-model="form.multi_season_protect" type="checkbox" true-value="compare" false-value="off" :disabled="op.busy.value || op.uncertain.value" />多季合集保护（同季必须满足保护策略）</label>
      <div class="range-control">
        <label for="season-share-ratio">剧集分享达标率 <strong>{{ Math.round(Number(form.season_replace_ratio ?? 0.9) * 100) }}%</strong></label>
        <input id="season-share-ratio" v-model.number="form.season_replace_ratio" type="range" min="0.5" max="1" step="0.01" :disabled="op.busy.value || op.uncertain.value" />
        <div class="range-endpoints"><span>50% 宽松</span><span>100% 严格</span></div>
        <p class="muted-text">仅当分享版共同集达到设置比例，且分享集数满足完整度保护时，才考虑淘汰本地版本。</p>
      </div>
    </div>
    <h3>特别篇（S00）策略</h3>
    <div class="strategy-selection">
      <label v-for="option in specialOptions" :key="option.value" class="strategy-option" :class="{ selected: form.special_action === option.value }">
        <input v-model="form.special_action" type="radio" name="governance-s00" :value="option.value" :disabled="op.busy.value || op.uncertain.value" />
        <span><strong>{{ option.label }}</strong><small>{{ option.note }}</small></span>
      </label>
    </div><button
      class="button primary"
      :disabled="
        !strategy.data.value || !!strategy.error.value || op.busy.value || op.uncertain.value
      "
      @click="confirmation = 'strategy'"
    >
      保存治理策略
    </button>
  </section>
  <section id="governance-seven" class="panel">
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
  <section id="governance-whitelist" class="panel">
    <h2>白名单（豁免词）</h2>
    <label class="whitelist-editor">关键词（每行一个，命中作品跳过治理）
      <textarea v-model="keywords" rows="4" placeholder="百家讲坛" :disabled="op.busy.value || op.uncertain.value"></textarea>
    </label>
    <div class="action-row">
      <button class="button primary" :disabled="op.busy.value || op.uncertain.value || !strategy.data.value" @click="confirmation = 'strategy'">保存白名单及治理策略</button>
    </div>
    <p class="muted-text">保存后重新治理扫描。下方豁免预览只使用已保存规则，不会删除文件。</p>
    <h3>豁免目录核对</h3>
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
  <section id="governance-comparison" class="panel">
    <h2>七维比较测试</h2>
    <form class="form-grid" @submit.prevent="test">
      <label>分享版本文件名<input v-model="a" required /></label
      ><label>本地版本文件名<input v-model="b" required /></label
      ><button class="button" :disabled="op.busy.value || op.uncertain.value">
        按已保存规则比较
      </button>
    </form>
    <div v-if="comparison" class="comparison-result">
      <h3>比较结果（按已保存规则）</h3>
      <p v-if="comparison.winner || comparison.result" class="notice-bar">结果：{{ comparison.winner || comparison.result }}</p>
      <EvidenceDetails :value="comparison" />
    </div>
  </section>
  <section id="governance-schedule" class="panel">
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
