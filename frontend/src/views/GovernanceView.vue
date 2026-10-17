<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue';
import { readData, writeData, records, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { date } from '../composables/format';
import PageHeading from '../components/PageHeading.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import ModalDialog from '../components/ModalDialog.vue';
const summary = useRequest<Data>(),
  plans = useRequest<Data>(),
  detail = useRequest<Data>(),
  op = useOperation(),
  task = ref(''),
  confirmation = ref(''),
  selected = ref<Data>(),
  detailOpen = ref(false),
  group = ref('del_local_items'),
  search = ref(''),
  sourceModeDraft = ref('upstream');
const visible = ref(200);
watch([group, search], () => {
  visible.value = 200;
});
const scan = computed(() => object(summary.data.value?.scan)),
  result = computed(() => object(scan.value.result));
const sources = computed(() => records(summary.data.value?.sources));
const sourceMode = computed(() => String(summary.data.value?.source_mode || 'upstream'));
const modeLabels: Record<string, string> = {
  upstream: '上游 STRM · 路径治理',
  generated: '自产 STRM · 来源身份治理',
  combined: '兼容双源 · 分别核验',
};
const items = computed(() => {
  const keyword = search.value.trim().toLocaleLowerCase();
  return records(result.value[group.value]).filter((r) =>
    !keyword || [r.title, r.text, r.reason_label, r.reason, r.detail, r.season]
      .some((value) => String(value ?? '').toLocaleLowerCase().includes(keyword)),
  );
});
const reasonNames: Record<string, string> = {
  incomplete_delete_local: '个人库存在缺集或断层，优先保留完整版本',
  incomplete_delete_share: '分享库存在缺集或断层，优先保留完整版本',
  quality_delete_local: '分享版本综合画质更优',
  quality_delete_share: '个人版本综合画质更优',
  special_delete_local: '特别篇按已保存的 S00 策略处理',
  special_delete_share: '特别篇按已保存的 S00 策略处理',
  protected: '已触发多季或完整度保护',
  exempt: '命中白名单豁免',
};
function reasonOf(item: Data) {
  return String(item.reason_label || reasonNames[String(item.reason || '')] || item.detail ||
    '请查看该项的比较依据与路径');
}
function pathsOf(item: Data | undefined, key: string): string[] {
  const value = object(item?.meta)[key];
  return Array.isArray(value)
    ? value.filter((path): path is string => typeof path === 'string').slice(0, 12)
    : [];
}
const scanAttempt = computed(() => object(scan.value.scan_attempt));
const scanMessage = computed(() => {
  if (scanAttempt.value.status === 'failed')
    return `上次扫描失败：${String(scanAttempt.value.message || scanAttempt.value.error || '请查看任务日志及目录映射')}`;
  if (!scan.value.found) return '尚未生成治理计划；先检查目录状态，再执行扫描。';
  if (!scan.value.rule_aligned) return '规则已经变化，旧计划不可执行；请重新扫描。';
  if (!scan.value.file_guard_ready) return '计划缺少文件身份核验信息；请重新扫描。';
  if (scan.value.plan_state === 'executing') return '治理任务正在执行，请查看后台任务。';
  if (Number(scan.value.remaining_sec) <= 0) return '扫描计划已过期，请重新扫描。';
  if (!scan.value.usable) return '当前计划不可执行，先检查扫描结果、挂载或运行记录。';
  return '当前计划仍有效。先核对处理原因、源文件和目标库，再预演或执行。';
});
const planId = computed(() =>
  String(object(detail.data.value?.plan).id || scan.value.plan_id || ''),
);
async function load() {
  await Promise.allSettled([
    summary.run((signal) => readData('/api/governance/summary', signal)),
    plans.run((signal) => readData('/api/plans?limit=100', signal)),
  ]);
  if (summary.data.value?.source_mode) sourceModeDraft.value = sourceMode.value;
}
async function showPlan(id: string) {
  detailOpen.value = true;
  selected.value = undefined;
  detail.data.value = undefined;
  await detail.run((signal) => readData(`/api/plan/${encodeURIComponent(id)}`, signal));
}
async function execute() {
  if (confirmation.value === 'source') {
    if (!['upstream', 'generated', 'combined'].includes(sourceModeDraft.value)) return;
    const saved = await op.run(() =>
      writeData('/api/cloud/settings', { governance_source_mode: sourceModeDraft.value }),
    );
    if (saved) {
      confirmation.value = '';
      task.value = '';
      selected.value = undefined;
      detailOpen.value = false;
      detail.data.value = undefined;
      await load();
    }
    return;
  }
  let path = '/api/check',
    body: Data | undefined;
  if (confirmation.value !== 'scan') {
    path = '/api/clean';
    body = { plan_id: planId.value, dry_run: confirmation.value === 'dry' };
  }
  const r = await op.run(() => writeData(path, body));
  if (r) {
    task.value = String(r.task_id);
    confirmation.value = '';
    detailOpen.value = false;
  }
}
function prepareClean(dry: boolean) {
  detail.data.value = undefined;
  confirmation.value = dry ? 'dry' : 'clean';
}
onMounted(load);
</script>
<template>
  <PageHeading title="治理总览" subtitle="完整度优先、七维比较；扫描只产生计划，确认后才执行清理。"
    ><div class="action-row">
      <RouterLink to="/governance/rules" class="button">七维与治理规则</RouterLink
      ><RouterLink to="/governance/cleanup" class="button">目录与残留清理</RouterLink
      ><button
        class="button primary"
        :disabled="op.busy.value || op.uncertain.value || sourceModeDraft !== sourceMode"
        @click="confirmation = 'scan'"
      >
        立即治理扫描
      </button>
    </div></PageHeading
  ><OperationState
    :busy="op.busy.value"
    :error="op.error.value || summary.error.value || plans.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <div class="summary-strip">
    <span>最近诊断 {{ date(Number(scan.ts)) }}</span
    ><span class="state-pill" :class="{ success: scan.usable, needs_review: !scan.usable }">{{
      scan.usable ? '计划可核对执行' : '计划尚未生成或不可用'
    }}</span
    ><button
      class="text-link push-right"
      @click="
        load();
        op.reconcile();
      "
    >
      刷新并核实
    </button>
  </div>
  <section class="panel governance-workspace" aria-label="双库治理来源">
    <header class="section-heading">
      <div>
        <h2>治理来源与安全边界</h2>
        <p class="muted-text">两套治理机制保留，但选定的扫描范围和旧计划严格隔离；切换后重新扫描。</p>
      </div>
      <RouterLink class="button small" to="/settings/cloud?section=output">核对输出目录</RouterLink>
    </header>
    <div class="form-grid">
      <label>当前治理来源
        <select v-model="sourceModeDraft" :disabled="op.busy.value || op.uncertain.value" aria-label="选择治理来源">
          <option value="upstream">上游 STRM · 路径治理</option>
          <option value="generated">自产 STRM · 115 来源身份治理</option>
          <option value="combined">兼容双源 · 分别核验（高级）</option>
        </select>
      </label>
      <div class="gov-source-selector-actions">
        <p class="muted-text">正在使用：{{ modeLabels[sourceMode] || sourceMode }}</p>
        <button class="button primary" :disabled="sourceModeDraft === sourceMode || op.busy.value || op.uncertain.value || !summary.data.value" @click="confirmation = 'source'">
          保存并切换治理来源
        </button>
      </div>
    </div>
    <p class="muted-text">{{ summary.data.value?.source_mode_description }}</p>
    <p v-if="sourceModeDraft !== sourceMode" class="gov-scan-notice" role="status">
      尚未保存新范围。当前计划仍属于 {{ modeLabels[sourceMode] }}；保存后必须重新扫描。
    </p>
    <div class="gov-source-grid">
      <article v-for="source in sources" :key="String(source.label)" class="gov-source-card"
        :class="{ 'gov-source-off': !source.enabled }">
        <div class="gov-source-heading">
          <strong>{{ source.label }}</strong>
          <span class="state-pill" :class="{ success: source.enabled && source.present, needs_review: source.enabled && !source.present }">
            {{ !source.enabled ? '未参与' : source.present ? '目录可见' : '挂载缺失' }}
          </span>
        </div>
        <code :title="String(source.path)">{{ source.path || '尚未设置' }}</code>
        <small>{{ source.method === 'path'
          ? '上游路径治理：沿用旧路径归属与删除保护，不根据文件名猜测 115 ID'
          : '自产来源治理：必须核验已登记的 STRM 签名、115 文件身份；分享源不删除' }}</small>
      </article>
    </div>
    <p class="gov-scan-notice" role="status">{{ scanMessage }}</p>
    <p v-if="scan.usable && sources.some((source) => source.origin === 'generated' && !source.enabled)" class="muted-text">
      当前可用治理计划并不代表自生成库已参与。若已迁移到自己生成的 STRM，请先配置并重新扫描。
    </p>
    <p v-if="sources.some((source) => source.enabled && !source.present)" class="inline-error">
      当前启用的治理目录有不可见项。请先检查 Docker 映射；不要将挂载失效当成空库清理。
    </p>
  </section>
  <div class="stats-grid">
    <article
      v-for="(label, key) in {
        del_local_cnt: '本地待治理',
        del_share_cnt: '分享待治理',
        exempted_count: '白名单豁免',
        quiet_skipped: '静默期跳过',
      }"
      :key="key"
      class="stat-card"
    >
      <span class="stat-label">{{ label }}</span
      ><strong>{{ result[key] ?? '—' }}</strong>
    </article>
  </div>
  <section class="panel">
    <header class="section-heading">
      <h2>当前诊断明细</h2>
      <div class="action-row">
        <button
          class="button"
          :disabled="!scan.plan_id || !scan.usable || sourceModeDraft !== sourceMode || op.busy.value || op.uncertain.value"
          @click="prepareClean(true)"
        >
          预演计划</button
        ><button
          class="button danger-text"
          :disabled="!scan.plan_id || !scan.usable || sourceModeDraft !== sourceMode || op.busy.value || op.uncertain.value"
          @click="prepareClean(false)"
        >
          核对并执行计划
        </button>
      </div>
    </header>
    <nav class="browse-tabs">
      <button
        v-for="(label, key) in {
          del_local_items: '本地处理',
          del_share_items: '分享处理',
          protected_items: '受保护',
          exempted_items: '豁免项',
        }"
        :key="key"
        :class="{ active: group === key }"
        @click="group = key"
      >
        {{ label }} {{ records(result[key]).length }}
      </button>
    </nav>
    <input v-model="search" aria-label="搜索治理项目" placeholder="搜索作品或原因" />
    <div class="plan-item-list">
      <article v-for="(item, i) in items.slice(0, visible)" :key="i">
        <div>
          <strong>{{ item.title || item.name || item.text }}</strong>
          <p>{{ reasonOf(item) }}</p>
          <p class="gov-item-context">
            <span v-if="item.season !== undefined && item.season !== null">第 {{ item.season }} 季</span>
            <span v-if="item.files_count !== undefined">涉及 {{ item.files_count }} 个 STRM</span>
          </p>
        </div>
        <button
          class="button small"
          @click="
            selected = item;
            detailOpen = true;
          "
        >
          依据与路径
        </button>
      </article>
      <p v-if="!items.length" class="muted-text">当前分组暂无项目。</p>
    </div>
    <button v-if="items.length > visible" class="button" @click="visible += 200">
      加载更多治理项目（{{ visible }} / {{ items.length }}）
    </button>
    <details>
      <summary>诊断与一致性证据</summary>
      <EvidenceDetails :value="summary.data.value" />
    </details>
  </section>
  <TaskReceipt v-if="task" :id="task" kind="task" @done="load" />
  <section class="panel">
    <h2>治理计划与执行归档</h2>
    <div v-for="p in records(plans.data.value?.plans)" :key="String(p.id)" class="automation-rule">
      <div>
        <strong>{{ date(Number(p.ts)) }}</strong>
        <p>
          {{ p.state }} · 本地 {{ object(p.stats).loc ?? '—' }} / 分享
          {{ object(p.stats).shr ?? '—' }}
        </p>
      </div>
      <button class="button small" @click="showPlan(String(p.id))">查看计划详情</button>
    </div>
    <details>
      <summary>目录清理等执行归档</summary>
      <EvidenceDetails :value="plans.data.value?.archives" />
    </details>
  </section>
  <ModalDialog
    v-if="detailOpen"
    title="治理依据与计划详情"
    @close="
      detailOpen = false;
      detail.cancel();
    "
    ><p v-if="detail.error.value" class="inline-error">{{ detail.error.value }}</p>
    <template v-if="selected">
      <div class="gov-detail-intro">
        <strong>{{ selected.title || selected.text || '治理候选' }}</strong>
        <p>{{ reasonOf(selected) }}</p>
        <p class="muted-text">关联 STRM：{{ selected.files_count ?? '—' }} 个；所有删除须另行确认。</p>
      </div>
      <div v-if="pathsOf(selected, 'local_paths').length || pathsOf(selected, 'share_paths').length" class="gov-preview-paths">
        <div v-if="pathsOf(selected, 'local_paths').length">
          <strong>个人库核对路径</strong>
          <code v-for="path in pathsOf(selected, 'local_paths')" :key="path">{{ path }}</code>
        </div>
        <div v-if="pathsOf(selected, 'share_paths').length">
          <strong>分享库核对路径</strong>
          <code v-for="path in pathsOf(selected, 'share_paths')" :key="path">{{ path }}</code>
        </div>
      </div>
      <details open>
        <summary>比较依据与路径</summary>
        <EvidenceDetails :value="selected.meta" />
      </details>
      <details><summary>完整技术证据</summary><EvidenceDetails :value="selected" /></details>
    </template>
    <EvidenceDetails v-else :value="detail.data.value?.plan" /></ModalDialog
  ><ConfirmAction
    v-if="confirmation"
    :title="
      confirmation === 'source'
        ? '切换双库治理来源'
        : confirmation === 'scan'
        ? '立即治理扫描'
        : confirmation === 'dry'
          ? '预演治理计划'
          : '执行治理计划'
    "
    :description="
      confirmation === 'source'
        ? `将治理范围切换为「${modeLabels[sourceModeDraft]}」。原有计划不能用于新范围，必须重新扫描；不会立即扫描或删除文件。`
        : confirmation === 'scan'
        ? `按「${modeLabels[sourceMode]}」扫描个人与分享 STRM，仅生成计划，不删除文件。`
        : confirmation === 'dry'
          ? '根据当前计划进行预演，不实际删除媒体。'
          : '执行当前可用计划：分享库只删除 NAS STRM；本地库按既有后端规则联动删除已核实源视频与附属文件。无备份，失败项按原逻辑保留。'
    "
    :phrase="confirmation === 'clean' ? '执行治理计划' : undefined"
    :danger="confirmation === 'clean' || confirmation === 'source'"
    :busy="op.busy.value"
    :disabled="op.uncertain.value || (confirmation !== 'source' && sourceModeDraft !== sourceMode) || (confirmation !== 'scan' && confirmation !== 'source' && !scan.usable)"
    @close="confirmation = ''"
    @confirm="execute"
    ><EvidenceDetails
      v-if="confirmation === 'dry' || confirmation === 'clean'"
      :value="{
        plan_id: scan.plan_id,
        del_local_cnt: result.del_local_cnt,
        del_share_cnt: result.del_share_cnt,
        del_local_files: result.del_local_files,
        del_share_files: result.del_share_files,
      }"
  /></ConfirmAction>
</template>
