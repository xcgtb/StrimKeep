<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { readData, writeData, object, records, strings, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import defaults from '../config/quality-defaults.json';
import PageHeading from '../components/PageHeading.vue';
import OperationState from '../components/OperationState.vue';
import ConfirmAction from '../components/ConfirmAction.vue';

const strategy = useRequest<Data>(),
  cover = useRequest<Data>(),
  exemptions = useRequest<Data>(),
  auto = useRequest<Data>(),
  op = useOperation();

const form = ref<Data>({}),
  rules = ref<Data[]>([]),
  automatic = ref<Data>({}),
  comparison = ref<Data>(),
  a = ref(''),
  b = ref(''),
  confirmation = ref(''),
  exemptInput = ref(''),
  coverGroupInput = ref(''),
  openRules = ref(new Set<string>()),
  ratioPct = ref(90);

const exemptKeywords = computed(() => strings(form.value.exempt_keywords));
const exemptMatches = computed(() => records(exemptions.data.value?.matches));
const cmpDims = computed(() => records(comparison.value?.dims));

const ruleName = (key: unknown) =>
  (
    {
      release_group: '发布组',
      source: '资源类型',
      resolution: '分辨率',
      dolby: '动态范围',
      dynamic_range: '动态范围',
      fps: '帧率',
      bitdepth: '色深',
      audio: '音频',
      size: '真实源文件大小',
    } as Record<string, string>
  )[String(key)] || String(key);

const ratioDesc = computed(() => {
  const pct = ratioPct.value;
  const need = Math.ceil((20 * pct) / 100 - 1e-9);
  return `逐集比画质：分享在两库共同集里「达标」的占比 ≥ ${pct}%，且分享集数不少于本地，才删本地腾网盘，否则删分享（例：共同 20 集需 ${need} 集达标）。多季保护「开启」档在两边季集合相同时要求每一季都达到；选了「保留本地 / 保留分享」时不使用此阈值。`;
});

function strategyBody() {
  return {
    decision: form.value.decision || 'quality_first',
    match_strategy: form.value.match_strategy || 'title_year',
    multi_season_protect: form.value.multi_season_protect || 'compare',
    tie_keep_local: !!form.value.tie_keep_local,
    season_replace_ratio: Number(form.value.season_replace_ratio) || 0.9,
    special_action: form.value.special_action || 'compare',
    exempt_keywords: strings(form.value.exempt_keywords),
  };
}

async function saveStrategy() {
  const r = await op.run(() => writeData('/api/strategy', strategyBody()));
  if (r) {
    strategy.data.value = r;
    form.value = structuredClone(object(r.strategy));
  }
}

async function load() {
  const result = await Promise.allSettled([
    strategy.run((signal) => readData('/api/strategy', signal)),
    cover.run((signal) => readData('/api/cover-strategy', signal)),
    auto.run((signal) => readData('/api/governance/auto', signal)),
  ]);
  void result;
  form.value = structuredClone(object(strategy.data.value?.strategy));
  const rt = Number(form.value.season_replace_ratio);
  ratioPct.value = Math.round((Number.isFinite(rt) && rt >= 0.5 && rt <= 1 ? rt : 0.9) * 100);
  rules.value = structuredClone(records(object(cover.data.value?.strategy).rules));
  const cfg = object(auto.data.value?.settings);
  automatic.value = {
    enabled: !!cfg.enabled,
    interval_hours: cfg.interval_hours,
    only_on_change: !!cfg.only_on_change,
    notify_when_clean: !!cfg.notify_when_clean,
  };
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
        ? strategyBody()
        : action === 'cover'
          ? { strategy: { rules: rules.value.map(stripRule) } }
          : automatic.value,
    ),
  );
  if (r) {
    confirmation.value = '';
    if (action === 'strategy') {
      strategy.data.value = r;
      form.value = structuredClone(object(r.strategy));
    } else if (action === 'cover') {
      cover.data.value = r;
      rules.value = structuredClone(records(object(r.strategy).rules));
    } else auto.data.value = r;
  }
}

function stripRule(r: Data): Data {
  return { key: r.key, enabled: !!r.enabled, tiers: r.tiers ?? [], groups: r.groups ?? [] };
}

/* ── 策略选项（classic：即时 confirm + 保存） ── */
function pickDecision(v: string) {
  if (form.value.decision === v) return;
  const label = { quality_first: '画质优先', keep_local: '保留本地', keep_share: '保留分享' }[v] || v;
  if (!confirm(`确定将决策模型切换为「${label}」？\n\n这会影响双库治理的保留策略。`)) return;
  form.value.decision = v;
  void saveStrategy();
}
function pickMatch(v: string) {
  if (form.value.match_strategy === v) return;
  const label = { title_year: '剧名+年份（安全）', tmdb_first: 'TMDB 优先（高召回）' }[v] || v;
  if (!confirm(`确定将配对策略切换为「${label}」？\n\n切换后旧治理计划会失效，需重新扫描生成新计划。`)) return;
  form.value.match_strategy = v;
  void saveStrategy();
}
function pickMulti(v: string) {
  if (form.value.multi_season_protect === v) return;
  const label = { off: '关闭', compare: '开启' }[v] || v;
  if (!confirm(`确定将多季合集保护切换为「${label}」？\n\n这会直接影响多季合集是否被删。`)) return;
  form.value.multi_season_protect = v;
  void saveStrategy();
}
function pickSpecial(v: string) {
  if (form.value.special_action === v) return;
  const label = { compare: '画质对比', ignore: '忽略', delete: '清理' }[v] || v;
  if (!confirm(`确定将特别篇（S00）策略切换为「${label}」？`)) return;
  form.value.special_action = v;
  void saveStrategy();
}
function onTieToggle(event: Event) {
  const want = (event.target as HTMLInputElement).checked;
  if (!confirm(`确定将「平局保留本地」切换为「${want ? '保留本地' : '保留分享'}」？`)) return;
  form.value.tie_keep_local = want;
  void saveStrategy();
}
function onRatioChange() {
  const pct = ratioPct.value;
  if (!confirm(`确定将剧集分享达标率阈值设为 ${pct}%？\n\n保存后旧治理计划会失效，需重新扫描。`)) {
    const rt = Number(form.value.season_replace_ratio);
    ratioPct.value = Math.round((Number.isFinite(rt) && rt >= 0.5 && rt <= 1 ? rt : 0.9) * 100);
    return;
  }
  form.value.season_replace_ratio = pct / 100;
  void saveStrategy();
}

/* ── 画质对比规则（7 维）草稿编辑 ── */
function patchRule(i: number, values: Data) {
  rules.value = rules.value.map((r, index) => (index === i ? { ...r, ...values } : r));
}
function onToggleRule(i: number, event: Event) {
  patchRule(i, { enabled: (event.target as HTMLInputElement).checked });
}
function moveRule(i: number, dir: number) {
  const j = i + dir;
  if (j < 0 || j >= rules.value.length) return;
  const rows = [...rules.value];
  [rows[i], rows[j]] = [rows[j]!, rows[i]!];
  rules.value = rows;
}
function toggleTiers(key: string) {
  const next = new Set(openRules.value);
  next.has(key) ? next.delete(key) : next.add(key);
  openRules.value = next;
}
function moveTier(key: string, ti: number, dir: number) {
  const i = rules.value.findIndex((r) => r.key === key);
  if (i < 0) return;
  const tiers = strings(rules.value[i]?.tiers);
  const j = ti + dir;
  if (j < 0 || j >= tiers.length || ti === tiers.length - 1 || j === tiers.length - 1) return;
  [tiers[j], tiers[ti]] = [tiers[ti]!, tiers[j]!];
  patchRule(i, { tiers });
}
function moveGroup(gi: number, dir: number) {
  const i = rules.value.findIndex((r) => r.key === 'release_group');
  if (i < 0) return;
  const groups = strings(rules.value[i]?.groups);
  const j = gi + dir;
  if (j < 0 || j >= groups.length) return;
  [groups[j], groups[gi]] = [groups[gi]!, groups[j]!];
  patchRule(i, { groups });
}
function delGroup(gi: number) {
  const i = rules.value.findIndex((r) => r.key === 'release_group');
  if (i < 0) return;
  patchRule(i, { groups: strings(rules.value[i]?.groups).filter((_, index) => index !== gi) });
}
function addGroup() {
  const v = coverGroupInput.value.trim();
  if (!v) return;
  const i = rules.value.findIndex((r) => r.key === 'release_group');
  if (i < 0) return;
  const groups = strings(rules.value[i]?.groups);
  if (!groups.includes(v)) {
    groups.push(v);
    patchRule(i, { groups, enabled: true });
  }
  coverGroupInput.value = '';
}
function restoreCover() {
  rules.value = structuredClone(defaults.cover.rules);
}
async function discardCover() {
  await cover.run((signal) => readData('/api/cover-strategy', signal));
  rules.value = structuredClone(records(object(cover.data.value?.strategy).rules));
}

/* ── 白名单 ── */
function addExempt() {
  const v = exemptInput.value.trim();
  if (!v) return;
  const kws = strings(form.value.exempt_keywords);
  if (!kws.includes(v)) {
    kws.push(v);
    form.value.exempt_keywords = kws;
    void saveExempt();
  }
  exemptInput.value = '';
}
function removeExempt(k: string) {
  form.value.exempt_keywords = strings(form.value.exempt_keywords).filter((x) => x !== k);
  void saveExempt();
}
async function saveExempt() {
  const r = await op.run(() =>
    writeData('/api/strategy', { exempt_keywords: strings(form.value.exempt_keywords) }),
  );
  if (r) {
    strategy.data.value = r;
    form.value = structuredClone(object(r.strategy));
    void scanExempt();
  }
}
function scanExempt() {
  return exemptions.run((signal) => readData('/api/exempt/scan', signal));
}

/* ── 对比测试 ── */
async function runCompare() {
  const r = await op.run(() => writeData('/api/cover-compare', { a: a.value, b: b.value }));
  if (r) comparison.value = object(r.compare);
}
const CMP_VERDICT: Record<string, string> = {
  a: '分享胜',
  b: '本地胜',
  tie: '打平',
  skip: '跳过',
  after: '—',
};
const cmpHead = computed(() => {
  const c = comparison.value;
  if (!c) return '';
  const result = Number(c.result);
  if (result === 0) {
    const tie = c.tie_keep_local ? '平局保留本地' : '平局保留分享';
    return `全部打平 → 按「${
      c.decision !== 'quality_first'
        ? (c.decision === 'keep_local' ? '保留本地' : '保留分享') + '」决策'
        : tie + '」开关'
    }`;
  }
  const decided = cmpDims.value.find((d) => d.key === c.decided_by);
  return `${result > 0 ? '分享版胜' : '本地版胜'}，决胜项：${decided?.label || c.decided_by}`;
});
function verdictColor(v: unknown) {
  return v === 'a' ? '#10b981' : v === 'b' ? '#f59e0b' : 'var(--text-dim)';
}

onMounted(load);
</script>
<template>
  <PageHeading title="七维与治理规则" subtitle="规则显式保存，更新镜像不会重置你的配置。"
    ><button class="btn gray" @click="load">重新读取已保存配置</button></PageHeading
  >
  <OperationState
    :busy="op.busy.value"
    :error="op.error.value || strategy.error.value || cover.error.value || auto.error.value"
    :uncertain="op.uncertain.value"
  />

  <section class="panel">
    <div class="row-between">
      <h3>治理策略</h3>
    </div>
    <p class="section-note">决策模型影响本地/分享两库的保留策略</p>

    <div class="strategy-option" :class="{ on: form.decision === 'quality_first' }" @click="pickDecision('quality_first')">
      <div class="t">画质优先（默认）</div>
      <div class="d">对比两库画质，画质高者胜出；平局按「平局策略」</div>
    </div>
    <div class="strategy-option" :class="{ on: form.decision === 'keep_local' }" @click="pickDecision('keep_local')">
      <div class="t">保留本地</div>
      <div class="d">本地库永远优先，分享库只做补充；电影、特别篇、剧集（两库都有的季）一律删分享</div>
    </div>
    <div class="strategy-option" :class="{ on: form.decision === 'keep_share' }" @click="pickDecision('keep_share')">
      <div class="t">保留分享</div>
      <div class="d">分享库永远优先，省 115 网盘空间；剧集仅当分享这一季覆盖本地全部集时才删本地</div>
    </div>

    <div class="opt-group">
      <div class="lbl">双库配对策略</div>
      <div class="opt-grid">
        <div class="strategy-option" :class="{ on: form.match_strategy === 'title_year' }" @click="pickMatch('title_year')">
          <div class="t">剧名+年份</div>
          <div class="d">安全，不怕 TMDB 写错</div>
        </div>
        <div class="strategy-option" :class="{ on: form.match_strategy === 'tmdb_first' }" @click="pickMatch('tmdb_first')">
          <div class="t">TMDB 优先</div>
          <div class="d">高召回，冲突回退剧名</div>
        </div>
      </div>
    </div>

    <div class="opt-group">
      <div class="lbl">多季合集保护</div>
      <div class="opt-grid">
        <div class="strategy-option" :class="{ on: form.multi_season_protect === 'off' }" @click="pickMulti('off')">
          <div class="t">关闭</div>
          <div class="d">逐季独立择优</div>
        </div>
        <div class="strategy-option" :class="{ on: form.multi_season_protect === 'compare' }" @click="pickMulti('compare')">
          <div class="t">开启</div>
          <div class="d">多季剧不掏空任何一方</div>
        </div>
      </div>
    </div>

    <div class="toggle-row">
      <div>
        <div class="lbl">平局保留本地</div>
        <div class="desc">仅当「画质对比规则」逐维比较后完全打平时才生效：开 = 保留本地，关 = 保留分享。「保留本地 / 保留分享」决策模式下不会用到。</div>
      </div>
      <label class="switch"><input type="checkbox" :checked="!!form.tie_keep_local" @change="onTieToggle" /><span class="slider"></span></label>
    </div>

    <div class="ratio-block">
      <div class="row-between">
        <div class="lbl">剧集分享达标率阈值</div>
        <div class="ratio-val">{{ ratioPct }}%</div>
      </div>
      <input
        v-model.number="ratioPct"
        type="range"
        min="50"
        max="100"
        step="5"
        class="ratio-slider"
        @change="onRatioChange"
      />
      <div class="ratio-scale"><span>50% 宽松</span><span>100% 严格（须全部达标）</span></div>
      <div class="desc">{{ ratioDesc }}</div>
    </div>
  </section>

  <section class="panel">
    <h3>特别篇（S00）策略</h3>
    <p class="section-note">Emby 中的 S00 / Specials 如何处理</p>
    <div class="opt-grid three">
      <div class="strategy-option" :class="{ on: form.special_action === 'compare' }" @click="pickSpecial('compare')">
        <div class="t">画质对比</div>
        <div class="d">多季合集时随合集决定</div>
      </div>
      <div class="strategy-option" :class="{ on: form.special_action === 'ignore' }" @click="pickSpecial('ignore')">
        <div class="t">忽略</div>
        <div class="d">S00 不参与治理</div>
      </div>
      <div class="strategy-option" :class="{ on: form.special_action === 'delete' }" @click="pickSpecial('delete')">
        <div class="t">清理</div>
        <div class="d">全删本地+分享</div>
      </div>
    </div>
  </section>

  <section class="panel">
    <div class="row-between">
      <h3>画质对比规则 <span class="critical-badge">关键策略</span></h3>
    </div>
    <p class="warn-text">保存后立即生效（配置热加载），直接决定治理时「删本地还是删分享」。调整顺序/开关只是草稿，点「保存规则」并二次确认后才会生效。</p>
    <p class="section-note">双库治理比较「分享版 vs 本地版」的唯一依据（没有隐藏打分）。按从上到下逐项比较：某一项两边档位不同，排在前面的档位胜出；这一项两边相同或都未识别，才继续看下一项；全部打平交给上面的「平局保留本地」。关闭一项只是跳过它，不会反向优先。</p>

    <div v-if="!rules.length" class="list-empty">加载中...</div>
    <div v-for="(r, i) in rules" :key="String(r.key)" class="cover-rule" :class="{ dimmed: !r.enabled }">
      <div class="cover-rule-head">
        <span class="rank">{{ String(i + 1).padStart(2, '0') }}</span>
        <div class="cover-rule-title">
          <div class="t">{{ r.label || ruleName(r.key) }}</div>
          <div class="d">{{ r.desc || '' }}</div>
        </div>
        <label class="switch"><input type="checkbox" :checked="!!r.enabled" @change="onToggleRule(i, $event)" /><span class="slider"></span></label>
        <button class="btn gray sm" :disabled="i === 0" @click="moveRule(i, -1)">↑</button>
        <button class="btn gray sm" :disabled="i === rules.length - 1" @click="moveRule(i, 1)">↓</button>
        <button class="btn gray sm" @click="toggleTiers(String(r.key))">
          {{ openRules.has(String(r.key)) ? '收起' : r.key === 'release_group' ? '设置列表' : '设置顺序' }}
        </button>
      </div>
      <div v-if="openRules.has(String(r.key))" class="cover-rule-body">
        <div class="tiers-hint">越靠前优先级越高</div>
        <template v-if="r.key === 'release_group'">
          <div v-for="(g, gi) in strings(r.groups)" :key="g" class="tier-row">
            <span class="rank">{{ gi + 1 }}</span><span class="tier-name">{{ g }}</span>
            <button class="btn gray sm" :disabled="gi === 0" @click="moveGroup(gi, -1)">↑</button>
            <button class="btn gray sm" :disabled="gi === strings(r.groups).length - 1" @click="moveGroup(gi, 1)">↓</button>
            <button class="btn gray sm" @click="delGroup(gi)">×</button>
          </div>
          <div class="group-add">
            <input v-model="coverGroupInput" placeholder="新增发布组，例如 CMCTV" @keydown.enter="addGroup" />
            <button class="btn gray" @click="addGroup">添加</button>
          </div>
          <div class="tiers-hint">发布组取自文件名末尾「-组名」。列表为空时本项不参与比较；不在列表里的排最后。</div>
        </template>
        <template v-else>
          <div v-for="(t, ti) in strings(r.tiers)" :key="t" class="tier-row">
            <span class="rank">{{ ti + 1 }}</span><span class="tier-name">{{ t }}</span>
            <button class="btn gray sm" :disabled="ti === 0 || ti === strings(r.tiers).length - 1" @click="moveTier(String(r.key), ti, -1)">↑</button>
            <button class="btn gray sm" :disabled="ti >= strings(r.tiers).length - 2" @click="moveTier(String(r.key), ti, 1)">↓</button>
          </div>
        </template>
      </div>
    </div>

    <div class="btn-row">
      <button class="btn" :disabled="!cover.data.value || !!cover.error.value || op.busy.value || op.uncertain.value" @click="confirmation = 'cover'">保存规则…</button>
      <button class="btn gray" @click="discardCover">放弃改动</button>
      <button class="btn gray" @click="restoreCover">恢复默认…</button>
    </div>

    <div class="compare-block">
      <div class="lbl">对比测试</div>
      <div class="section-note">填入两个文件名，按「当前已保存」的规则逐项看谁胜、在哪一项决出。</div>
      <input v-model="a" placeholder="分享版文件名，如 Show.S01E01.2160p.WEB-DL.HDR10-HHWEB" />
      <input v-model="b" placeholder="本地版文件名，如 Show.S01E01.1080p.BluRay.Remux-CMCT" />
      <button class="btn gray" :disabled="op.busy.value || op.uncertain.value" @click="runCompare">开始对比</button>
      <div v-if="comparison" class="cmp-result">
        <div class="cmp-head">{{ cmpHead }}</div>
        <div class="cmp-scroll">
          <table class="cmp-table">
            <thead><tr><th>维度</th><th>分享版</th><th>本地版</th><th>结果</th></tr></thead>
            <tbody>
              <tr v-for="d in cmpDims" :key="String(d.key)" :style="{ opacity: d.verdict === 'skip' || d.verdict === 'after' ? 0.5 : 1 }">
                <td>{{ d.label }}</td>
                <td>{{ d.a }}</td>
                <td>{{ d.b }}</td>
                <td :style="{ color: verdictColor(d.verdict), whiteSpace: 'nowrap' }">
                  {{ CMP_VERDICT[String(d.verdict)] || '' }}<template v-if="d.verdict === 'skip'">（{{ d.reason }}）</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </section>

  <section class="panel">
    <h3>白名单（豁免词）</h3>
    <p class="section-note">命中白名单的剧集：扫描时跳过清理，标记为「豁免」，显示在治理页</p>
    <div class="tag-input">
      <input v-model="exemptInput" placeholder="输入关键词，如 百家讲坛" @keydown.enter="addExempt" />
      <button class="btn" @click="addExempt">添加</button>
    </div>
    <div class="tag-list">
      <span v-if="!exemptKeywords.length" class="tag-empty">暂无关键词</span>
      <span v-for="k in exemptKeywords" :key="k" class="tag">{{ k }}<span class="rm" @click="removeExempt(k)">×</span></span>
    </div>
    <div class="section-note">命中预览：</div>
    <div class="btn-row">
      <button class="btn gray" :disabled="exemptions.loading.value" @click="scanExempt">扫描双库</button>
    </div>
    <p v-if="exemptions.error.value" class="inline-error">{{ exemptions.error.value }}</p>
    <div v-if="exemptions.loading.value" class="list-empty">扫描中...</div>
    <div v-else-if="exemptMatches.length">
      <div v-for="(x, xi) in exemptMatches" :key="xi" class="list-item exempt">
        <div class="t">🛡️ 《{{ x.title }}》</div>
        <div class="d">命中「{{ x.keyword }}」 · 库: {{ strings(x.libs).join('/') }}<template v-if="strings(x.seasons).length"> · 季: {{ strings(x.seasons).map((s) => (s === '0' ? 'S00(特别篇)' : 'S' + String(Number(s)).padStart(2, '0'))).join(', ') }}</template><template v-if="x.strm_count"> · {{ x.strm_count }} 个文件</template></div>
      </div>
    </div>
    <div v-else class="list-empty">双库中未命中白名单</div>
  </section>

  <section class="panel">
    <h3>定期治理诊断与通知</h3>
    <p class="section-note">按后端既有行为运行诊断与通知，不代表自动执行删除。</p>
    <div class="toggle-row">
      <div>
        <div class="lbl">启用定期治理诊断</div>
        <div class="desc">后台定期扫描双库，发现待清理项时通知</div>
      </div>
      <label class="switch"><input v-model="automatic.enabled" type="checkbox" /><span class="slider"></span></label>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">诊断间隔（小时）</div>
        <div class="desc">1 ~ 168，建议 6</div>
      </div>
      <div class="num-field"><input v-model.number="automatic.interval_hours" type="number" min="1" max="168" /></div>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">清单无变化不重复通知</div>
        <div class="desc">待清理清单与上次通知相同时保持安静</div>
      </div>
      <label class="switch"><input v-model="automatic.only_on_change" type="checkbox" /><span class="slider"></span></label>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">无待清理项也通知</div>
        <div class="desc">默认只在有待清理项时通知</div>
      </div>
      <label class="switch"><input v-model="automatic.notify_when_clean" type="checkbox" /><span class="slider"></span></label>
    </div>
    <div class="btn-row">
      <button class="btn" :disabled="!auto.data.value || !!auto.error.value || op.busy.value || op.uncertain.value" @click="confirmation = 'auto'">保存诊断计划</button>
    </div>
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

<style scoped>
.row-between {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.section-note {
  font-size: 12px;
  color: var(--text-dim);
  margin: 6px 0 12px;
  line-height: 1.7;
}
.lbl {
  font-size: 13px;
  color: var(--text-mid);
  font-weight: 600;
  margin-bottom: 8px;
}
.opt-group {
  margin-top: 16px;
}
.opt-grid {
  display: flex;
  gap: 8px;
}
.opt-grid .strategy-option {
  flex: 1;
  margin: 0;
  padding: 10px 8px;
  text-align: center;
}
.opt-grid.three .strategy-option .t {
  font-size: 12px;
}
.opt-grid .strategy-option .d {
  font-size: 10px;
}
.ratio-block {
  margin-top: 14px;
}
.ratio-val {
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.ratio-slider {
  width: 100%;
  margin: 8px 0 4px;
}
.ratio-scale {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-dim);
}
.critical-badge {
  font-size: 10px;
  font-weight: 600;
  color: #dc2626;
  border: 1px solid #dc2626;
  border-radius: 6px;
  padding: 1px 6px;
  margin-left: 6px;
}
.warn-text {
  font-size: 12px;
  color: #dc2626;
  margin: 4px 0 6px;
}
.cover-rule {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 10px 12px;
  margin: 8px 0;
}
.cover-rule.dimmed {
  opacity: 0.7;
}
.cover-rule-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.rank {
  width: 22px;
  color: var(--text-dim);
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  text-align: center;
  flex-shrink: 0;
}
.cover-rule-title {
  flex: 1;
  min-width: 140px;
}
.cover-rule-title .t {
  font-size: 13px;
  font-weight: 600;
}
.cover-rule-title .d {
  font-size: 10px;
  color: var(--text-dim);
}
.cover-rule-body {
  margin-top: 8px;
}
.tiers-hint {
  font-size: 11px;
  color: var(--text-dim);
  margin: 2px 0 6px;
}
.tier-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin: 4px 0;
}
.tier-name {
  flex: 1;
}
.group-add {
  display: flex;
  gap: 6px;
  margin-top: 6px;
}
.group-add input {
  flex: 1;
}
.btn-row {
  display: flex;
  gap: 8px;
  margin-top: 10px;
  flex-wrap: wrap;
}
.compare-block {
  margin-top: 16px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.compare-block input {
  width: 100%;
  margin-bottom: 6px;
}
.cmp-result {
  margin-top: 8px;
}
.cmp-head {
  font-size: 12px;
  font-weight: 600;
  margin-bottom: 4px;
}
.cmp-scroll {
  overflow-x: auto;
}
.cmp-table {
  width: 100%;
  font-size: 11px;
  border-collapse: collapse;
}
.cmp-table th {
  color: var(--text-dim);
  text-align: left;
  padding: 3px 6px;
  font-weight: 600;
}
.cmp-table td {
  padding: 3px 6px;
}
.tag-empty {
  color: var(--text-dim);
  font-size: 12px;
}
.num-field {
  width: 100px;
}
</style>
