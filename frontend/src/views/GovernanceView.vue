<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue';
import { readData, writeData, records, object, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import GovernanceItemDetails from '../components/GovernanceItemDetails.vue';
import LoadState from '../components/LoadState.vue';
import { date, stateLabel } from '../composables/format';
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
  taskRunning = ref(false),
  now = ref(Date.now() / 1000),
  planVisible = ref(100),
  group = ref('all'),
  search = ref('');
const visible = ref(200);
watch([group, search], () => {
  visible.value = 200;
});
const groupLabels: Record<string, string> = {
  del_local_items: '本地处理',
  del_share_items: '分享处理',
  protected_items: '受保护',
  exempted_items: '豁免项',
};
const kindLabels: Record<string, string> = {
  loc: '删本地',
  shr: '删分享',
  keep: '受保护',
  exempt: '白名单豁免',
};
const kinds: Record<string, string> = {
  del_local_items: 'loc',
  del_share_items: 'shr',
  protected_items: 'keep',
  exempted_items: 'exempt',
};
const scan = computed(() => object(summary.data.value?.scan)),
  result = computed(() => object(scan.value.result));
// 扫描事实面板：字段与后端 /api/governance/summary 的返回值一一对应，
// 1.2.12 里这些信息是常驻展示的，Vue 版此前只取了其中 4 个计数。
const attempt = computed(() => object(scan.value.scan_attempt));
const scanArchived = computed(() => ['done', 'failed'].includes(String(scan.value.plan_state)));
const expired = computed(
  () =>
    Number(scan.value.ts) > 0 &&
    Number(scan.value.ttl_sec) > 0 &&
    now.value >= Number(scan.value.ts) + Number(scan.value.ttl_sec),
);
const canExecute = computed(
  () =>
    !!scan.value.usable &&
    !summary.loading.value &&
    !summary.error.value &&
    !expired.value &&
    scan.value.rule_aligned !== false &&
    scan.value.file_guard_ready !== false &&
    attempt.value.status !== 'failed' &&
    !['done', 'failed', 'executing', 'missing', 'expired'].includes(
      String(scan.value.plan_state),
    ) &&
    !taskRunning.value,
);
const planNotice = computed(() => {
  if (taskRunning.value) return '任务正在执行，请等待回执；执行期间不能重复提交计划。';
  if (attempt.value.status === 'failed')
    return `最近扫描失败：${attempt.value.message || '请检查日志后重新扫描'}。上次成功结果仅供参考，不能执行。`;
  if (!scan.value.found && !scan.value.ts) return '尚无成功扫描，请先点击「立即治理扫描」。';
  if (scan.value.rule_aligned === false) return '治理规则已变化，当前计划不可执行，请重新扫描。';
  if (scan.value.file_guard_ready === false) return '计划缺少文件核验快照，请重新扫描。';
  if (expired.value) return '计划已超过有效期，请重新扫描生成新计划。';
  if (['done', 'failed', 'executing', 'missing'].includes(String(scan.value.plan_state)))
    return {
      done: '该计划已执行，请重新扫描后再清理。',
      failed: '该计划执行失败，请核对实际结果并重新扫描。',
      executing: '该计划正在执行，请等待结果。',
      missing: '计划文件不可用，请重新扫描。',
    }[String(scan.value.plan_state)];
  if (!scan.value.plan_id) return '本次扫描没有待清理项。';
  if (canExecute.value)
    return `当前计划可执行，剩余有效期约 ${Math.max(0, Math.ceil(Math.min(Number(scan.value.ttl_sec), Number(scan.value.ts) + Number(scan.value.ttl_sec) - now.value) / 60))} 分钟。`;
  return '当前计划不可用，请重新扫描。';
});
const consistency = computed(() => object(summary.data.value?.consistency));
const strmCounts = computed(() => object(result.value.strm_counts));
const clock = (value: unknown) => (Number(value) > 0 ? date(Number(value)) : '暂无');
const truthRows = computed(() => {
  const scanState = canExecute.value
    ? '可执行'
    : scan.value.found
      ? scan.value.plan_state === 'done'
        ? '已执行'
        : '需重新扫描'
      : '尚无扫描';
  const protectedCount = records(result.value.protected_items).length;
  return [
    { key: '扫描状态', value: scanState, tone: canExecute.value ? 'ok' : 'warn' },
    {
      key: '身份冲突',
      value: `${Number(result.value.identity_conflict_count || 0)} 个 TMDB 标识冲突（不自动删除）`,
    },
    {
      key: '待处理',
      value: scanArchived.value
        ? '待重新扫描核实'
        : `${Number(result.value.total_clean_cnt || 0)} 项 / ${Number(result.value.del_local_files || 0) + Number(result.value.del_share_files || 0)} 文件`,
    },
    {
      key: scanArchived.value ? '上次扫描保护 / 豁免' : '保护 / 豁免',
      value: `${protectedCount} / ${Number(result.value.exempted_count || 0)}`,
    },
    { key: '静默跳过', value: `${Number(result.value.quiet_skipped || 0)} 项` },
    {
      key: 'STRM（扫描时）',
      value: `本地 ${Number(strmCounts.value.local || 0)} · 分享 ${Number(strmCounts.value.share || 0)} · 合计 ${Number(strmCounts.value.total || 0)}`,
    },
    { key: '片库快照', value: clock(object(consistency.value.library).ts) },
    { key: '24H 入库快照', value: clock(object(consistency.value.ingest).ts) },
    {
      key: '最近扫描尝试',
      value: attempt.value.ts
        ? `${clock(attempt.value.ts)} · ${attempt.value.status === 'failed' ? '失败' : '成功'}`
        : '暂无',
    },
    {
      key: '扫描规则',
      value: !scan.value.found
        ? '待扫描核验'
        : scan.value.rule_aligned === false
          ? '已变化，请重新扫描'
          : '与当前规则一致',
      tone: scan.value.rule_aligned === false ? 'warn' : 'ok',
    },
  ];
});
const identityConflicts = computed(() => records(result.value.identity_conflicts));
const items = computed<Data[]>(() =>
  (group.value === 'all'
    ? Object.keys(groupLabels).flatMap((key) =>
        records(result.value[key]).map((r) => ({ ...r, kind: r.kind || kinds[key] })),
      )
    : records(result.value[group.value]).map((r) => ({ ...r, kind: r.kind || kinds[group.value] }))
  ).filter((r) => JSON.stringify(r).toLowerCase().includes(search.value.toLowerCase())),
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
  planVisible.value = 100;
  detailOpen.value = true;
  selected.value = undefined;
  detail.data.value = undefined;
  await detail.run((signal) => readData(`/api/plan/${encodeURIComponent(id)}`, signal));
}
async function execute() {
  if (taskRunning.value || (confirmation.value !== 'scan' && !canExecute.value)) return;
  let path = '/api/check',
    body: Data | undefined;
  if (confirmation.value !== 'scan') {
    path = '/api/clean';
    body = { plan_id: planId.value, dry_run: confirmation.value === 'dry' };
  }
  const r = await op.run(() => writeData(path, body));
  if (r) {
    task.value = String(r.task_id || '');
    taskRunning.value = !!r.task_id;
    confirmation.value = '';
    detailOpen.value = false;
  }
}
function prepareClean(dry: boolean) {
  detail.data.value = undefined;
  confirmation.value = dry ? 'dry' : 'clean';
}
async function recoverTask() {
  try {
    const health = await readData('/api/health');
    const current = object(health.current_task);
    if (
      current.id &&
      ['inter_check', 'inter_clean'].includes(String(current.kind)) &&
      current.status === 'running'
    ) {
      task.value = String(current.id);
      taskRunning.value = true;
    }
  } catch {
    /* Summary and its own retry remain available if health cannot be read. */
  }
}
useVisiblePoll(() => {
  now.value = Date.now() / 1000;
  if (taskRunning.value)
    return summary.run((signal) => readData('/api/governance/summary', signal));
}, 3000);
onMounted(() => {
  void load();
  void recoverTask();
});
</script>
<template>
  <PageHeading title="治理总览" subtitle="完整度优先、七维比较；扫描只产生计划，确认后才执行清理。"
    ><div class="action-row">
      <RouterLink to="/governance/rules" class="button">七维与治理规则</RouterLink
      ><button
        class="button primary"
        :disabled="op.busy.value || op.uncertain.value || taskRunning"
        @click="confirmation = 'scan'"
      >
        {{ taskRunning ? '任务执行中…' : '立即治理扫描' }}
      </button>
    </div></PageHeading
  ><OperationState
    :busy="op.busy.value"
    :error="op.error.value || summary.error.value || plans.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <section class="panel governance-controls">
    <header class="section-heading">
      <div>
        <h2>扫描与执行</h2>
        <p>1 扫描比对 → 2 核对计划 → 3 手动执行清理</p>
      </div>
      <RouterLink to="/governance/cleanup" class="button">目录与残留清理</RouterLink>
    </header>
    <p class="notice-bar" :class="{ 'warning-text': !canExecute }" role="status">
      {{ planNotice }}
    </p>
    <div class="action-row">
      <button
        class="button"
        :disabled="!scan.plan_id || !canExecute || op.busy.value || op.uncertain.value"
        @click="prepareClean(true)"
      >
        预演计划</button
      ><button
        class="button danger-text"
        :disabled="!scan.plan_id || !canExecute || op.busy.value || op.uncertain.value"
        @click="prepareClean(false)"
      >
        核对并执行计划
      </button>
    </div>
  </section>
  <TaskReceipt
    v-if="task"
    :id="task"
    kind="task"
    @update="taskRunning = $event.status === 'running'"
    @done="
      taskRunning = false;
      load();
    "
  />
  <div class="summary-strip">
    <span>最近诊断 {{ date(Number(scan.ts)) }}</span
    ><span class="state-pill" :class="{ success: scan.usable, needs_review: !scan.usable }">{{
      scanArchived
        ? '计划已归档，待重新扫描'
        : scan.usable
          ? '计划可核对执行'
          : '计划尚未生成或不可用'
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
        protected_count: '受保护项',
        del_local_cnt: '本地待清理',
        del_share_cnt: '分享待清理',
        exempted_count: '白名单豁免',
      }"
      :key="key"
      class="stat-card"
    >
      <span class="stat-label">{{
        scanArchived && ['protected_count', 'exempted_count'].includes(key)
          ? `上次扫描 · ${label}`
          : label
      }}</span
      ><strong>{{
        scanArchived && ['del_local_cnt', 'del_share_cnt'].includes(key)
          ? '—'
          : key === 'protected_count'
            ? records(result.protected_items).length
            : (result[key] ?? '—')
      }}</strong>
      <small
        v-if="scanArchived && ['del_local_cnt', 'del_share_cnt'].includes(key)"
        class="muted-text"
        >待重新扫描核实</small
      >
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
      <h2>{{ scanArchived ? '执行前扫描快照' : '当前诊断明细' }}</h2>
    </header>
    <p v-if="scanArchived" class="notice-bar">
      该计划已{{ scan.plan_state === 'done' ? '执行' : '结束' }}，原扫描数据已归档。
      当前剩余项需重新扫描核实，不能将原数量继续作为待清理项。
    </p>
    <details class="governance-scan-items" :open="!scanArchived">
      <summary v-if="scanArchived">查看执行前扫描明细（历史快照）</summary>
      <nav class="browse-tabs">
        <button :class="{ active: group === 'all' }" @click="group = 'all'">
          全部 {{ Object.keys(groupLabels).reduce((n, key) => n + records(result[key]).length, 0) }}
        </button>
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
            <span
              class="state-pill"
              :class="{
                failed: ['loc', 'shr'].includes(String(item.kind)),
                success: item.kind === 'keep',
              }"
              >{{ kindLabels[String(item.kind)] }}</span
            ><strong>{{ item.text || item.title || item.name }}</strong>
            <p>
              {{
                item.reason_label || object(item.meta).reason_label || item.reason || item.detail
              }}
            </p>
            <small class="muted-text"
              >涉及
              {{ item.files_count ?? (Array.isArray(item.files) ? item.files.length : 0) }}
              个文件<span v-if="object(item.meta).match_basis">
                · {{ object(item.meta).match_basis }}</span
              ></small
            >
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
    </details>
  </section>
  <section class="panel">
    <h2>治理计划与执行归档</h2>
    <div v-for="p in records(plans.data.value?.plans)" :key="String(p.id)" class="automation-rule">
      <div>
        <strong>{{ date(Number(p.ts)) }}</strong>
        <p>
          {{ stateLabel(String(p.state)) }} · 本地 {{ object(p.stats).loc ?? '—' }} / 分享
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
    <LoadState :loading="detail.loading.value" />
    <GovernanceItemDetails v-if="selected" :item="selected" :archived="scanArchived" />
    <template v-else-if="detail.data.value?.plan"
      ><dl class="business-facts">
        <div>
          <dt>生成时间</dt>
          <dd>{{ clock(object(detail.data.value.plan).ts) }}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{{ stateLabel(String(object(detail.data.value.plan).state)) }}</dd>
        </div>
      </dl>
      <section v-if="object(detail.data.value.plan).executed_result" class="batch-summary">
        <h3>实际执行汇总</h3>
        <p>
          本地处理 {{ object(object(detail.data.value.plan).executed_result).loc ?? 0 }} 项 ·
          分享处理 {{ object(object(detail.data.value.plan).executed_result).shr ?? 0 }} 项 · 跳过
          {{ object(object(detail.data.value.plan).executed_result).skipped ?? 0 }} 项
        </p>
        <p v-if="object(object(detail.data.value.plan).executed_result).error" class="inline-error">
          {{ object(object(detail.data.value.plan).executed_result).error }}
        </p>
        <EvidenceDetails :value="object(object(detail.data.value.plan).executed_result).warnings" />
        <p class="muted-text">下方路径来自当时的计划快照；实际删除以执行回执和日志为准。</p>
      </section>
      <div class="plan-item-list">
        <details
          v-for="(action, index) in records(object(detail.data.value.plan).actions).slice(
            0,
            planVisible,
          )"
          :key="index"
        >
          <summary>
            {{ action.title || action.text || action.action_id }} ·
            {{ action.reason_label || action.reason }}
          </summary>
          <GovernanceItemDetails
            :item="action"
            :archived="object(detail.data.value.plan).state !== 'pending'"
          />
        </details>
      </div>
      <button
        v-if="records(object(detail.data.value.plan).actions).length > planVisible"
        class="button"
        @click="planVisible += 100"
      >
        继续显示计划项目
      </button>
      <details class="technical-details">
        <summary>完整计划回执</summary>
        <EvidenceDetails :value="detail.data.value.plan" /></details></template></ModalDialog
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
    :disabled="op.uncertain.value || taskRunning || (confirmation !== 'scan' && !canExecute)"
    @close="confirmation = ''"
    @confirm="execute"
    ><div v-if="confirmation !== 'scan'" class="batch-summary">
      <p>
        本地库：{{ result.del_local_cnt ?? 0 }} 项 /
        {{ result.del_local_files ?? 0 }} 个文件；分享库：{{ result.del_share_cnt ?? 0 }} 项 /
        {{ result.del_share_files ?? 0 }} 个文件。
      </p>
      <p>
        受保护 {{ records(result.protected_items).length }} 项，白名单豁免
        {{ result.exempted_count ?? 0 }} 项，不进入清理。
      </p>
      <p>{{ planNotice }}</p>
    </div>
  </ConfirmAction>
</template>
