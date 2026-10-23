<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { Search, RefreshCw, ChevronRight, ListTodo } from '@lucide/vue';
import { get, query } from '../api/client';
import { useRequest } from '../composables/useRequest';
import { useActivityUpdates } from '../composables/useActivityUpdates';
import TaskRecovery from '../components/TaskRecovery.vue';
import TaskTimeline from '../components/TaskTimeline.vue';
import { date, stateLabel } from '../composables/format';
import type { Job, DiagnosticPage } from '../types/api';
import LoadState from '../components/LoadState.vue';
import ModalDialog from '../components/ModalDialog.vue';
import { useRoute } from 'vue-router';
import { useTasks } from '../stores/tasks';
import { useOperation } from '../composables/useOperation';
import { writeData, type Data } from '../api/domain';
import OperationState from '../components/OperationState.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
const operation = useOperation(),
  tracked = useTasks(),
  route = useRoute(),
  action = ref(''),
  diagnosticCategory = ref(''),
  diagnosticQuery = ref(''),
  managerId = ref(''),
  trackedOpen = ref<{ id: string; kind: 'cloud' | 'task' | 'delete' }>();
async function act() {
  const current = detail.data.value?.job;
  if (!current) return;
  let path = `/api/cloud/jobs/${current.id}/${action.value}`;
  let body: Data | undefined;
  if (['continue', 'stop', 'upgrades/continue'].includes(action.value)) body = { confirmed: true };
  if (action.value === 'maintenance/verify')
    path = `/api/cloud/jobs/${current.id}/maintenance/verify`;
  const r = await operation.run(() => writeData(path, body));
  if (r) {
    action.value = '';
    await Promise.allSettled([load(), loadDetail(), loadDiagnostics()]);
  }
}

const governance = useRequest<{ items: Data[] }>(),
  request = useRequest<{ items: Job[]; has_more?: boolean }>(),
  detail = useRequest<{ job: Job }>(),
  diagnostics = useRequest<{ diagnostics: DiagnosticPage }>();
const search = ref(''),
  pageIndex = ref(0),
  appliedSearch = ref(''),
  filter = ref('all'),
  jobId = ref(''),
  modalOpen = ref(false),
  diagnosticOffset = ref(0);
const rows = computed(() =>
  (request.data.value?.items || []).filter(
    (j) =>
      filter.value === 'all' ||
      (filter.value === 'active'
        ? ['queued', 'running'].includes(j.state)
        : filter.value === 'review'
          ? ['failed', 'uncertain', 'needs_review'].includes(j.state)
          : ['success', 'stopped', 'cancelled'].includes(j.state)),
  ),
);
const kinds: Record<string, string> = {
  inventory: '盘内库存扫描',
  organize_preview: '整理扫描预览',
  transfer: '分享转存',
  organize: '整理归档',
  workflow: '接收与整理',
  sync: 'STRM 同步',
  receive: '资源接收',
  intake: '资源接收',
  channel: '频道接收',
  publish: 'STRM 发布',
  categories: '分类目录',
  share_strm: '分享 STRM',
  maintenance: '目录维护',
};
const governanceKinds: Record<string, string> = {
  inter_check: '双库治理扫描',
  clean: '治理清理',
  tmdb: '片库对照',
  stats: '媒体统计',
  ingest: '入库扫描',
};
const governanceRows = computed(() =>
  (governance.data.value?.items || [])
    .filter((row) => {
      const state = String(row.status);
      const matches =
        filter.value === 'all' ||
        (filter.value === 'active'
          ? ['queued', 'running'].includes(state)
          : filter.value === 'review'
            ? ['error', 'failed', 'uncertain', 'needs_review'].includes(state)
            : ['success', 'stopped', 'cancelled'].includes(state));
      return (
        matches && (!appliedSearch.value || `${row.id} ${row.kind}`.includes(appliedSearch.value))
      );
    })
    .slice(0, 10),
);
function load() {
  void governance.run((signal) => get<{ items: Data[] }>('/api/tasks/recent', signal));
  return request.run((signal) =>
    get<{ items: Job[]; has_more?: boolean }>(
      query('/api/cloud/jobs', {
        query: appliedSearch.value,
        group: filter.value,
        offset: pageIndex.value * 50,
      }),
      signal,
    ),
  );
}
watch([filter, appliedSearch], () => {
  pageIndex.value = 0;
  void load();
});
function movePage(direction: number) {
  pageIndex.value = Math.max(0, pageIndex.value + direction);
  void load();
}
async function open(job: Job) {
  operation.invalidate();
  action.value = '';
  modalOpen.value = true;
  jobId.value = job.id;
  diagnosticOffset.value = 0;
  detail.data.value = undefined;
  diagnostics.data.value = undefined;
  await Promise.allSettled([loadDetail(), loadDiagnostics()]);
}
function loadDetail() {
  return detail.run((signal) =>
    get<{ job: Job }>(`/api/cloud/jobs/${encodeURIComponent(jobId.value)}`, signal),
  );
}
function loadDiagnostics() {
  return diagnostics.run((signal) =>
    get<{ diagnostics: DiagnosticPage }>(
      query(`/api/cloud/jobs/${encodeURIComponent(jobId.value)}/diagnostics`, {
        offset: diagnosticOffset.value,
        limit: 30,
        category: diagnosticCategory.value,
        query: diagnosticQuery.value,
      }),
      signal,
    ),
  );
}
async function verifyOriginal() {
  const result = await loadDetail();
  await loadDiagnostics();
  if (result) operation.reconcile(true);
}
function close() {
  modalOpen.value = false;
  detail.cancel();
  diagnostics.cancel();
}
onMounted(async () => {
  await load();
  const id = String(route.query.job || '');
  if (id) await open({ id, kind: '', state: 'uncertain' });
});
watch(
  () => route.query.job,
  async (value) => {
    const id = String(value || '');
    if (id && id !== jobId.value) await open({ id, kind: '', state: 'uncertain' });
  },
);
useActivityUpdates(async () => {
  await load();
  if (modalOpen.value) await Promise.allSettled([loadDetail(), loadDiagnostics()]);
});
</script>
<template>
  <section class="page-heading">
    <div>
      <span class="eyebrow">OPERATION CENTER</span>
      <h1>任务中心</h1>
      <p>查看执行进度、逐文件原因与需要人工核实的任务。</p>
    </div>
    <button class="button" :disabled="request.loading.value" @click="load">
      <RefreshCw :size="16" />刷新任务
    </button>
  </section>
  <form
    class="search-toolbar"
    @submit.prevent="
      appliedSearch = search.trim();
      load();
    "
  >
    <div class="search-field">
      <Search :size="17" /><input
        v-model="search"
        aria-label="搜索任务编号或消息"
        placeholder="搜索任务编号或消息"
      /><button class="button primary">搜索</button>
    </div>
  </form>
  <div class="browse-tabs">
    <button
      v-for="(label, key) in {
        all: '全部任务',
        active: '进行中',
        review: '需要处理',
        finished: '已结束',
      }"
      :key="key"
      :class="{ active: filter === key }"
      @click="filter = key"
    >
      {{ label }}
    </button>
  </div>
  <div class="action-row">
    <span class="data-caption"
      >第 {{ pageIndex + 1 }} 页 · 每页最多 50 条，筛选覆盖全部历史任务</span
    ><button
      class="button"
      :disabled="pageIndex === 0 || request.loading.value"
      @click="movePage(-1)"
    >
      上一页</button
    ><button
      class="button"
      :disabled="!request.data.value?.has_more || request.loading.value"
      @click="movePage(1)"
    >
      下一页
    </button>
  </div>
  <LoadState
    :loading="request.loading.value && !request.data.value"
    :error="request.error.value"
    :empty="!request.loading.value && !rows.length"
    message="当前筛选下没有任务"
    @retry="load"
  />
  <details class="panel">
    <summary>本次会话提交的任务与治理任务查询</summary>
    <div class="action-row">
      <input v-model="managerId" aria-label="治理任务编号" placeholder="治理任务编号" /><button
        class="button"
        :disabled="!managerId"
        @click="trackedOpen = { id: managerId, kind: 'task' }"
      >
        读取治理任务
      </button>
    </div>
    <div v-for="item in tracked.items" :key="item.id" class="automation-rule">
      <span>{{ item.id }} · {{ item.path }}</span
      ><button class="text-link" @click="trackedOpen = item">核实进度</button>
    </div>
  </details>
  <TaskReceipt
    v-if="trackedOpen"
    :id="trackedOpen.id"
    :kind="trackedOpen.kind"
    @close="trackedOpen = undefined"
  />
  <section v-if="governanceRows.length" class="panel">
    <header class="section-heading">
      <div>
        <h2>治理与扫描任务</h2>
        <p>Web、机器人和定时任务统一记录；重启后保留最近 200 条状态摘要。</p>
      </div>
    </header>
    <button
      v-for="row in governanceRows"
      :key="String(row.id)"
      class="task-row"
      @click="trackedOpen = { id: String(row.id), kind: 'task' }"
    >
      <span class="task-icon"><ListTodo :size="20" /></span>
      <div class="task-description">
        <strong>{{ governanceKinds[String(row.kind)] || row.kind }}</strong
        ><small>{{ date(Number(row.ts)) }} · {{ row.source }}</small>
      </div>
      <span class="state-pill" :class="String(row.status)">{{
        stateLabel(String(row.status))
      }}</span
      ><ChevronRight :size="17" />
    </button>
  </section>
  <section class="task-list">
    <button v-for="job in rows" :key="job.id" class="task-row" @click="open(job)">
      <span class="task-icon"><ListTodo :size="20" /></span>
      <div class="task-description">
        <strong>{{ kinds[job.kind] || job.kind }}</strong>
        <p>{{ job.message || '暂无任务消息' }}</p>
        <small>{{ date(job.updated || job.created) }}</small>
      </div>
      <span class="state-pill" :class="job.state">{{ stateLabel(job.state) }}</span
      ><ChevronRight :size="17" />
    </button>
  </section>
  <ModalDialog v-if="modalOpen" title="任务详情与诊断" @close="close"
    ><LoadState :loading="detail.loading.value" :error="detail.error.value" @retry="loadDetail" />
    <div v-if="detail.data.value?.job" class="task-detail-header">
      <span class="state-pill" :class="detail.data.value.job.state">{{
        stateLabel(detail.data.value.job.state)
      }}</span>
      <p>{{ detail.data.value.job.message }}</p>
      <details class="technical-details">
        <summary>任务编号（排障时查看）</summary>
        <code>{{ jobId }}</code>
      </details>
    </div>
    <section v-if="detail.data.value?.job.progress" class="panel task-progress">
      <h3>实时处理进度</h3>
      <p>
        {{ detail.data.value.job.progress.phase }} ·
        {{ detail.data.value.job.progress.current || '等待下一项' }}
      </p>
      <p>
        {{ detail.data.value.job.progress.processed ?? 0 }} /
        {{ detail.data.value.job.progress.total ?? '待统计' }}
      </p>
      <progress
        v-if="Number(detail.data.value.job.progress.total) > 0"
        :value="Number(detail.data.value.job.progress.processed || 0)"
        :max="Number(detail.data.value.job.progress.total)"
        aria-label="任务处理进度"
      />
      <p v-if="detail.data.value.job.activity?.label">{{ detail.data.value.job.activity.label }}</p>
    </section>
    <TaskRecovery
      v-if="
        detail.data.value?.job &&
        ['failed', 'uncertain', 'needs_review', 'stopped'].includes(detail.data.value.job.state) &&
        ['organize', 'workflow', 'transfer'].includes(detail.data.value.job.kind)
      "
      :id="jobId"
    />
    <TaskTimeline v-if="detail.data.value?.job" :id="jobId" />
    <details v-if="detail.data.value?.job.steps?.length">
      <summary>执行步骤与逐步回执</summary>
      <EvidenceDetails :value="detail.data.value.job.steps" />
    </details>
    <details v-if="detail.data.value?.job.upgrades?.length">
      <summary>洗版记录与保留原因</summary>
      <EvidenceDetails :value="detail.data.value.job.upgrades" />
    </details>
    <div v-if="detail.data.value?.job" class="action-row">
      <RouterLink
        v-if="detail.data.value.job.kind === 'organize_preview'"
        class="button primary"
        :to="{
          path: '/organize/workspace',
          query: { account: detail.data.value.job.preview_input?.account_id, preview_job: jobId },
        }"
        >查看整理预览</RouterLink
      >
      <button
        v-if="detail.data.value.job.state === 'queued'"
        class="button"
        @click="action = 'cancel'"
      >
        取消排队任务
      </button>
      <button
        v-if="
          detail.data.value.job.state === 'running' &&
          [
            'organize',
            'workflow',
            'transfer',
            'sync',
            'share_strm',
            'intake',
            'channel',
            'inventory',
            'organize_preview',
          ].includes(detail.data.value.job.kind)
        "
        class="button"
        @click="action = 'stop'"
      >
        {{
          detail.data.value.job.kind === 'organize_preview'
            ? '停止扫描预览'
            : detail.data.value.job.kind === 'inventory'
              ? '停止库存扫描'
              : detail.data.value.job.kind === 'channel'
                ? '安全停止并暂停频道'
                : detail.data.value.job.kind === 'intake'
                  ? '安全停止当前接收'
                  : '安全停止并暂停规则'
        }}
      </button>
      <button
        v-if="
          ['uncertain', 'needs_review'].includes(detail.data.value.job.state) &&
          detail.data.value.job.kind === 'transfer'
        "
        class="button"
        @click="action = 'verify'"
      >
        只读核实转存回执
      </button>
      <button
        v-if="
          ['uncertain', 'needs_review'].includes(detail.data.value.job.state) &&
          detail.data.value.job.kind === 'maintenance'
        "
        class="button"
        @click="action = 'maintenance/verify'"
      >
        只读核实回收站回执
      </button>
      <button
        v-if="
          detail.data.value.job.kind === 'transfer' &&
          !['queued', 'running'].includes(detail.data.value.job.state)
        "
        class="button"
        @click="action = 'continue'"
      >
        核实后继续整理
      </button>
      <button
        v-if="
          !['queued', 'running'].includes(detail.data.value.job.state) &&
          detail.data.value.job.upgrades?.length
        "
        class="button"
        @click="action = 'upgrades/continue'"
      >
        核实并继续待处理洗版
      </button>
    </div>
    <OperationState
      :busy="operation.busy.value"
      :error="operation.error.value"
      :uncertain="operation.uncertain.value"
      :receipt="operation.receipt.value"
    />
    <button v-if="operation.uncertain.value" class="text-link" @click="verifyOriginal">
      只读刷新原任务核实结果
    </button>
    <details v-if="detail.data.value?.job.result">
      <summary>执行结果、发布与通知回执</summary>
      <EvidenceDetails :value="detail.data.value.job.result" />
    </details>
    <h3 class="detail-section-title">逐文件诊断</h3>
    <form
      class="filter-panel"
      @submit.prevent="
        diagnosticOffset = 0;
        loadDiagnostics();
      "
    >
      <label
        >问题类别<select v-model="diagnosticCategory">
          <option value="">全部</option>
          <option value="error">执行异常</option>
          <option value="skipped">跳过</option>
          <option value="filtered">规则过滤</option>
          <option value="retained">版本保留</option>
          <option value="unknown">未识别</option>
          <option value="warning">提示</option>
        </select></label
      ><label>文件或原因<input v-model="diagnosticQuery" maxlength="200" /></label
      ><button class="button">筛选诊断</button>
    </form>
    <LoadState
      :loading="diagnostics.loading.value"
      :error="diagnostics.error.value"
      :empty="!diagnostics.loading.value && !diagnostics.data.value?.diagnostics.items.length"
      message="原记录中暂无逐文件诊断"
      @retry="loadDiagnostics"
    />
    <div v-if="!diagnostics.loading.value" class="diagnostic-list">
      <article
        v-for="(item, index) in diagnostics.data.value?.diagnostics.items"
        :key="index"
        class="diagnostic-card"
      >
        <div>
          <span class="state-pill needs_review">{{ item.label }}</span
          ><small>{{ item.stage }}</small>
        </div>
        <h4>{{ item.name }}</h4>
        <p>{{ item.reason }}</p>
        <small v-if="item.code">错误码：{{ item.code }}</small>
        <div v-if="item.rule" class="path-box">命中规则：{{ item.rule }}</div>
        <div v-if="item.path" class="path-box">{{ item.path }}</div>
        <div class="suggestion">处理建议：{{ item.suggestion }}</div>
      </article>
    </div>
    <div
      class="pagination"
      v-if="
        diagnostics.data.value && (diagnosticOffset || diagnostics.data.value.diagnostics.has_more)
      "
    >
      <button
        class="button"
        :disabled="diagnosticOffset === 0 || diagnostics.loading.value"
        @click="
          diagnosticOffset -= 30;
          loadDiagnostics();
        "
      >
        上一页</button
      ><span>{{ diagnosticOffset + 1 }} / {{ diagnostics.data.value.diagnostics.total }}</span
      ><button
        class="button"
        :disabled="!diagnostics.data.value.diagnostics.has_more || diagnostics.loading.value"
        @click="
          diagnosticOffset += 30;
          loadDiagnostics();
        "
      >
        下一页
      </button>
    </div>
    <details v-if="detail.data.value?.job.steps?.length" class="raw-details">
      <summary>原始步骤回执</summary>
      <pre>{{ JSON.stringify(detail.data.value.job.steps, null, 2) }}</pre>
    </details></ModalDialog
  >
  <ConfirmAction
    v-if="action"
    title="确认任务控制"
    :description="
      action === 'stop'
        ? '在当前文件组结束后安全停止，关联定时规则同时暂停。'
        : action === 'cancel'
          ? '只取消尚未开始的排队任务。'
          : action.includes('verify')
            ? '只读核实已经提交的操作，不重新发送原写入。'
            : action === 'continue'
              ? '核实转存回执后，仅继续后续整理；不重复转存。'
              : '重新核实已登记的洗版操作并继续后续步骤，可能删除旧版本源文件。'
    "
    :phrase="action === 'upgrades/continue' ? '继续洗版' : undefined"
    :busy="operation.busy.value"
    :disabled="operation.uncertain.value"
    @close="action = ''"
    @confirm="act"
  />
</template>
