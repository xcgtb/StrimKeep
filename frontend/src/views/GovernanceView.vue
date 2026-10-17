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
  search = ref('');
const visible = ref(200);
watch([group, search], () => {
  visible.value = 200;
});
const scan = computed(() => object(summary.data.value?.scan)),
  result = computed(() => object(scan.value.result));
// 扫描事实面板：字段与后端 /api/governance/summary 的返回值一一对应，
// 1.2.12 里这些信息是常驻展示的，Vue 版此前只取了其中 4 个计数。
const consistency = computed(() => object(summary.data.value?.consistency));
const strmCounts = computed(() => object(result.value.strm_counts));
const clock = (value: unknown) => (Number(value) > 0 ? date(Number(value)) : '暂无');
const truthRows = computed(() => {
  const scanState = scan.value.usable
    ? '可执行'
    : scan.value.found
      ? scan.value.plan_state === 'done'
        ? '已执行'
        : '需重新扫描'
      : '尚无扫描';
  const protectedCount = records(result.value.protected_items).length;
  return [
    { key: '扫描状态', value: scanState, tone: scan.value.usable ? 'ok' : 'warn' },
    {
      key: '身份冲突',
      value: `${Number(result.value.identity_conflict_count || 0)} 个 TMDB 标识冲突（不自动删除）`,
    },
    {
      key: '待处理',
      value: `${Number(result.value.total_clean_cnt || 0)} 项 / ${Number(result.value.del_local_files || 0) + Number(result.value.del_share_files || 0)} 文件`,
    },
    { key: '保护 / 豁免', value: `${protectedCount} / ${Number(result.value.exempted_count || 0)}` },
    { key: '静默跳过', value: `${Number(result.value.quiet_skipped || 0)} 项` },
    {
      key: 'STRM',
      value: `本地 ${Number(strmCounts.value.local || 0)} · 分享 ${Number(strmCounts.value.share || 0)} · 合计 ${Number(strmCounts.value.total || 0)}`,
    },
    { key: '片库快照', value: clock(object(consistency.value.library).ts) },
    { key: '24H 入库快照', value: clock(object(consistency.value.ingest).ts) },
    {
      key: '规则指纹',
      value: String(consistency.value.rule_sig || '').slice(0, 16) || '—',
    },
    {
      key: '扫描规则',
      value: scan.value.rule_aligned === false ? '已变化，请重新扫描' : '与当前规则一致',
      tone: scan.value.rule_aligned === false ? 'warn' : 'ok',
    },
  ];
});
const identityConflicts = computed(() => records(result.value.identity_conflicts));
const items = computed(() =>
  records(result.value[group.value]).filter((r) => JSON.stringify(r).includes(search.value)),
);
const planId = computed(() =>
  String(object(detail.data.value?.plan).id || scan.value.plan_id || ''),
);
async function load() {
  await Promise.allSettled([
    summary.run((signal) => readData('/api/governance/summary', signal)),
    plans.run((signal) => readData('/api/plans?limit=100', signal)),
  ]);
}
async function showPlan(id: string) {
  detailOpen.value = true;
  selected.value = undefined;
  detail.data.value = undefined;
  await detail.run((signal) => readData(`/api/plan/${encodeURIComponent(id)}`, signal));
}
async function execute() {
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
      ><button
        class="button primary"
        :disabled="op.busy.value || op.uncertain.value"
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
  <div class="stats-grid stats-grid-4">
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
  <section class="panel truth-panel">
    <header class="section-heading">
      <h2>扫描结果与状态</h2>
      <span class="muted-text" role="status">{{
        scan.ts ? `扫描于 ${date(Number(scan.ts))}` : '尚无扫描结果'
      }}</span>
    </header>
    <div class="truth-grid">
      <div v-for="row in truthRows" :key="row.key" class="truth-row">
        <span class="truth-key">{{ row.key }}</span>
        <span class="truth-value" :class="row.tone">{{ row.value }}</span>
      </div>
    </div>
    <div v-if="identityConflicts.length" class="truth-conflicts">
      <h3>TMDB 标识冲突 · {{ identityConflicts.length }} 组 · 仅提示，不进入自动清理</h3>
      <ul>
        <li v-for="(conflict, index) in identityConflicts" :key="index">
          {{ conflict.kind === 'movie' ? '电影' : '剧集' }}
          <template v-for="(identity, i) in records(conflict.identities)" :key="i">
            <span class="lib-tag" :class="identity.lib === 'local' ? 'local' : 'share'">{{
              identity.lib === 'local' ? '本地' : '分享'
            }}</span>
            《{{ identity.title || '—' }}》{{ identity.year ? `(${identity.year})` : '' }}
          </template>
        </li>
      </ul>
    </div>
  </section>
  <section class="panel">
    <header class="section-heading">
      <h2>当前诊断明细</h2>
      <div class="action-row">
        <button
          class="button"
          :disabled="!scan.plan_id || !scan.usable || op.busy.value || op.uncertain.value"
          @click="prepareClean(true)"
        >
          预演计划</button
        ><button
          class="button danger-text"
          :disabled="!scan.plan_id || !scan.usable || op.busy.value || op.uncertain.value"
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
          <p>{{ item.reason_label || item.reason || item.detail }}</p>
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
    <EvidenceDetails :value="selected || detail.data.value?.plan" /></ModalDialog
  ><ConfirmAction
    v-if="confirmation"
    :title="
      confirmation === 'scan'
        ? '立即治理扫描'
        : confirmation === 'dry'
          ? '预演治理计划'
          : '执行治理计划'
    "
    :description="
      confirmation === 'scan'
        ? '扫描并比较已配置本地与分享 STRM 库，只生成治理计划。'
        : confirmation === 'dry'
          ? '根据当前计划进行预演，不实际删除媒体。'
          : '执行当前可用计划：分享库只删除 NAS STRM；本地库按既有后端规则联动删除已核实源视频与附属文件。无备份，失败项按原逻辑保留。'
    "
    :phrase="confirmation === 'clean' ? '执行治理计划' : undefined"
    :danger="confirmation === 'clean'"
    :busy="op.busy.value"
    :disabled="op.uncertain.value || (confirmation !== 'scan' && !scan.usable)"
    @close="confirmation = ''"
    @confirm="execute"
    ><EvidenceDetails
      v-if="confirmation !== 'scan'"
      :value="{
        plan_id: scan.plan_id,
        del_local_cnt: result.del_local_cnt,
        del_share_cnt: result.del_share_cnt,
        del_local_files: result.del_local_files,
        del_share_files: result.del_share_files,
      }"
  /></ConfirmAction>
</template>
