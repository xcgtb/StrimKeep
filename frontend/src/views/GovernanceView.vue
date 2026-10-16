<script setup lang="ts">
import { ref, computed, onMounted } from 'vue';
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
const scan = computed(() => object(summary.data.value?.scan)),
  result = computed(() => object(scan.value.result));
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
  <PageHeading title="治理决策" subtitle="沿用七维与双库治理逻辑，先诊断，再核对计划执行。"
    ><div class="action-row">
      <RouterLink to="/governance/rules" class="button">七维与治理规则</RouterLink
      ><button
        class="button primary"
        :disabled="op.busy.value || op.uncertain.value"
        @click="confirmation = 'scan'"
      >
        诊断双库
      </button>
    </div></PageHeading
  ><OperationState
    :busy="op.busy.value"
    :error="op.error.value || summary.error.value || plans.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <nav class="context-tabs">
    <RouterLink to="/governance">治理决策</RouterLink
    ><RouterLink to="/governance/rules">规则与巡检</RouterLink
    ><RouterLink to="/governance/cleanup">目录与残留清理</RouterLink>
  </nav>
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
      <article v-for="(item, i) in items.slice(0, 200)" :key="i">
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
        ? '诊断双库'
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
