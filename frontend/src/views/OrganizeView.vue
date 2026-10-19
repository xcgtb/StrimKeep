<script setup lang="ts">
import { ref, watch, computed } from 'vue';
import { request } from '../api/client';
import { useInventorySource } from '../composables/useInventorySource';
import { useCloud } from '../stores/cloud';
import { readData, writeData, accountPath, object, records, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { FolderInput, FolderCheck, FolderSearch, RotateCcw } from '@lucide/vue';
import PageHeading from '../components/PageHeading.vue';
import AccountPicker from '../components/AccountPicker.vue';
import DirectoryPicker from '../components/DirectoryPicker.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import MediaOverride from '../components/MediaOverride.vue';
const inventorySource = useInventorySource();
const cloud = useCloud(),
  op = useOperation(() => cloud.id),
  preview = useRequest<Data>(),
  config = useRequest<Data>();
const source = ref(''),
  target = ref(''),
  unknown = ref(''),
  sourceName = ref(''),
  targetName = ref(''),
  unknownName = ref(''),
  generate = ref(true),
  overrides = ref<Record<string, Data>>({}),
  fixing = ref<{ id: string; name: string }>(),
  plan = ref<Data>(),
  confirm = ref(''),
  job = ref(''),
  jobState = ref<Data>();
const directoryCards = [
  {
    key: 'source',
    title: '待整理目录',
    role: '扫描入口',
    description: '扫描此目录及子目录，识别其中的视频和随行文件。',
    icon: FolderInput,
  },
  {
    key: 'unknown',
    title: '未识别目录（可选）',
    role: '人工核对',
    description: '启用未识别隔离后，识别失败的资源移入这里，便于修正后重试。',
    icon: FolderSearch,
  },
  {
    key: 'target',
    title: '整理归档目录',
    role: '归档目标',
    description: '按已保存的电影、剧集和地区规则生成子目录，归档后联动 STRM。',
    icon: FolderCheck,
  },
];
const directoryValues = computed(() => ({ source, target, unknown }));
const directoryNames = computed(() => ({
  source: sourceName,
  target: targetName,
  unknown: unknownName,
}));
const directoriesChanged = computed(() =>
  ['source', 'target', 'unknown'].some(
    (key) =>
      (directoryValues.value[key as keyof typeof directoryValues.value].value || '') !==
      String(object(object(config.data.value?.directories)[key]).id || ''),
  ),
);
function selectDirectory(key: string, value: { id: string; name: string }) {
  directoryValues.value[key as keyof typeof directoryValues.value].value = value.id;
  directoryNames.value[key as keyof typeof directoryNames.value].value = value.name;
}
const previewFilter = ref(''),
  previewSearch = ref('');
const candidates = computed<Data[]>(() => [
  ...records(plan.value?.items).map((row) => ({ ...row, previewStatus: 'planned' })),
  ...records(plan.value?.skipped).map((row) => ({ ...row, previewStatus: 'skipped' })),
  ...records(plan.value?.unknown_items).map((row) => ({ ...row, previewStatus: 'unknown' })),
]);
const visibleCandidates = computed(() =>
  candidates.value.filter(
    (row) =>
      (!previewFilter.value || row.previewStatus === previewFilter.value) &&
      (!previewSearch.value.trim() ||
        JSON.stringify(row).toLowerCase().includes(previewSearch.value.trim().toLowerCase())),
  ),
);
const previewLabel = (status: unknown) =>
  status === 'planned' ? '待整理' : status === 'skipped' ? '已跳过' : '待识别';
function episodeLabel(media: Data) {
  if (media.kind !== 'tv' || media.season == null || media.episode == null) return '';
  return `S${String(media.season).padStart(2, '0')}E${String(media.episode).padStart(2, '0')}`;
}
function invalidate() {
  preview.cancel();
  plan.value = undefined;
  previewFilter.value = '';
  previewSearch.value = '';
  confirm.value = '';
  op.reconcile();
}
watch([source, target, unknown, generate], invalidate);
async function defaults() {
  if (!cloud.id) return;
  const aid = cloud.id;
  const r = await config.run((signal) => readData(accountPath(aid, 'directories'), signal));
  if (r && aid === cloud.id) {
    const d = object(r.directories);
    source.value = String(object(d.source).id || '');
    sourceName.value = String(object(d.source).name || '');
    if (inventorySource.value) {
      source.value = inventorySource.value.id;
      sourceName.value = inventorySource.value.name;
    }
    target.value = String(object(d.target).id || '');
    targetName.value = String(object(d.target).name || '');
    unknown.value = String(object(d.unknown).id || '');
    unknownName.value = String(object(d.unknown).name || '');
  }
}
async function saveDirectories() {
  if (!config.data.value) return;
  const r = await op.run(() =>
    writeData(accountPath(cloud.id, 'directories'), {
      directories: {
        source: source.value || null,
        target: target.value || null,
        unknown: unknown.value || null,
      },
      expected_revision: config.data.value?.revision,
    }),
  );
  if (r) await defaults();
}
async function load() {
  plan.value = undefined;
  const r = await preview.run((signal) =>
    request<Data>('/api/cloud/plans', {
      method: 'POST',
      timeoutMs: 120000,
      signal,
      body: {
        account_id: cloud.id,
        source_cid: source.value,
        target_cid: target.value,
        unknown_cid: unknown.value || null,
        overrides: overrides.value,
      },
    }),
  );
  if (r) plan.value = object(r.plan);
}
async function execute() {
  const action = confirm.value;
  if (action === 'stop') {
    const result = await op.run(() =>
      writeData(`/api/cloud/jobs/${job.value}/stop`, { confirmed: true }),
    );
    if (result) confirm.value = '';
    return;
  }
  const r = await op.run(() =>
    writeData(
      action === 'categories' ? '/api/cloud/categories/create' : '/api/cloud/plans/apply',
      action === 'categories'
        ? { account_id: cloud.id, target_cid: target.value, confirmed: true }
        : { plan_id: plan.value?.id, confirmed: true, generate: generate.value },
    ),
  );
  if (r) {
    job.value = String(r.job_id);
    confirm.value = '';
    plan.value = undefined;
  }
}
watch(
  () => cloud.id,
  () => {
    config.cancel();
    invalidate();
    source.value = '';
    target.value = '';
    unknown.value = '';
    job.value = '';
    overrides.value = {};
    sourceName.value = '';
    targetName.value = '';
    unknownName.value = '';
    fixing.value = undefined;
    void defaults();
  },
  { immediate: true },
);
</script>
<template>
  <PageHeading
    title="媒体整理"
    subtitle="识别、纠正、命名与归档先预览，核对本批范围后执行。"
  /><AccountPicker />
  <p v-if="inventorySource" class="notice-bar">
    已带入盘内库存目录；先扫描预览。本次选择不会自动保存为定时整理来源。
  </p>
  <div class="flow-scope">
    <span class="badge blue">个人库</span
    ><strong>识别 → 八维洗版 → 命名归档 → STRM → Emby 入库</strong>
  </div>
  <OperationState
    :busy="preview.loading.value || op.busy.value"
    :error="preview.error.value || op.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <div v-if="cloud.id" class="organize-layout">
    <section class="panel organize-main">
      <header class="section-heading">
        <div>
          <h2>开始一次整理</h2>
          <p>选好来源和归档位置，先看预览再执行。</p>
        </div>
        <span class="module-tag">手动整理</span>
      </header>
      <ol class="workflow-steps">
        <li
          v-for="(step, i) in [
            { name: '选择来源', note: '设置目录' },
            { name: '识别预览', note: '核对命名' },
            { name: '修正识别', note: '调整作品' },
            { name: '确认归档', note: '提交任务' },
          ]"
          :key="step.name"
          :class="{ active: i === (fixing ? 2 : job ? 3 : plan || preview.loading.value ? 1 : 0) }"
        >
          <span>{{ i + 1 }}</span>
          <div>
            <strong>{{ step.name }}</strong
            ><small>{{ step.note }}</small>
          </div>
        </li>
      </ol>
      <div class="organize-directory-cards">
        <article
          v-for="(card, index) in directoryCards"
          :key="card.key"
          class="directory-purpose-card"
        >
          <header>
            <span class="directory-purpose-icon"><component :is="card.icon" :size="20" /></span>
            <h3>{{ index + 1 }} · {{ card.title }}</h3>
            <span class="module-tag">{{ card.role }}</span>
          </header>
          <DirectoryPicker
            :model-value="directoryValues[card.key as keyof typeof directoryValues].value"
            :name="directoryNames[card.key as keyof typeof directoryNames].value"
            :account-id="cloud.id"
            :label="card.title"
            hide-label
            :disabled="op.busy.value || op.uncertain.value || preview.loading.value"
            @selected="(value) => selectDirectory(card.key, value)"
          />
          <p>{{ card.description }}</p>
          <button
            v-if="card.key === 'unknown' && unknown"
            class="text-link"
            :disabled="op.busy.value || op.uncertain.value"
            @click="
              unknown = '';
              unknownName = '';
            "
          >
            清空可选目录
          </button>
        </article>
      </div>
      <label class="checkbox-label"
        ><input
          v-model="generate"
          type="checkbox"
          :disabled="op.busy.value || op.uncertain.value"
        />整理归档后生成 STRM</label
      >
      <p class="muted-text">目录仅在影视整理模块保存；未保存的选择不会改变定时任务。</p>
      <p v-if="config.error.value" class="inline-error">{{ config.error.value }}</p>
      <div class="action-row">
        <button
          class="button"
          :disabled="op.busy.value || op.uncertain.value || preview.loading.value"
          @click="defaults"
        >
          <RotateCcw :size="14" />撤销目录修改</button
        ><button
          class="button"
          :disabled="
            !directoriesChanged ||
            !config.data.value ||
            !!config.error.value ||
            !source ||
            !target ||
            source === target ||
            op.busy.value ||
            op.uncertain.value
          "
          @click="saveDirectories"
        >
          保存整理目录</button
        ><button
          class="button primary"
          :disabled="
            !source ||
            !target ||
            source === target ||
            preview.loading.value ||
            op.busy.value ||
            op.uncertain.value
          "
          @click="load"
        >
          扫描并预览整理</button
        ><button
          class="button"
          :disabled="!target || op.busy.value || op.uncertain.value"
          @click="confirm = 'categories'"
        >
          按已保存分类创建目录
        </button>
      </div>
    </section>
    <aside class="organize-guide">
      <div class="panel">
        <span class="eyebrow">整理前核对</span>
        <h2>让归档符合你的习惯</h2>
        <p>目录在本页独立保存；先核对版本与命名，再确认归档。</p>
        <RouterLink :to="'/organize/naming'"
          ><span>命名模板<small>目录与文件如何命名</small></span
          ><span>↗</span></RouterLink
        ><RouterLink :to="{ path: '/settings/cloud', query: { section: 'classification' } }"
          ><span>归档分类<small>类型、地区与年份</small></span
          ><span>↗</span></RouterLink
        ><RouterLink :to="{ path: '/settings/cloud', query: { section: 'output' } }"
          ><span>STRM 输出<small>NAS 与 Emby 路径</small></span
          ><span>↗</span></RouterLink
        ><RouterLink to="/organize/quality"
          ><span>整理八维与洗版<small>维度排序、版本预览、共存策略</small></span
          ><span>↗</span></RouterLink
        ><RouterLink to="/organize/automation" class="text-link">设置自动整理 →</RouterLink>
      </div>
    </aside>
  </div>
  <section v-if="plan" class="panel preview-panel">
    <header class="section-heading">
      <div>
        <h2>本批识别结果</h2>
        <p>计划有效期 {{ plan.expires_in }} 秒，修改目录或识别后需要重新预览。</p>
      </div>
      <button
        class="button primary"
        :disabled="op.busy.value || op.uncertain.value"
        @click="confirm = 'apply'"
      >
        核对并整理
      </button>
    </header>
    <div class="preview-toolbar">
      <label
        >显示结果<select v-model="previewFilter" aria-label="筛选整理结果">
          <option value="">全部 {{ candidates.length }}</option>
          <option value="planned">待整理 {{ records(plan.items).length }}</option>
          <option value="skipped">已跳过 {{ records(plan.skipped).length }}</option>
          <option value="unknown">待识别 {{ records(plan.unknown_items).length }}</option>
        </select></label
      >
      <label
        >查找文件<input
          v-model="previewSearch"
          placeholder="原文件名或目标片名"
          aria-label="查找整理文件"
      /></label>
    </div>
    <p v-if="!visibleCandidates.length" class="muted-text">没有符合条件的文件。</p>
    <div class="plan-item-list">
      <article
        v-for="(row, i) in visibleCandidates"
        :key="String(object(row.source).id || row.file_id || i)"
      >
        <div class="preview-comparison">
          <span class="badge" :class="row.previewStatus === 'planned' ? 'blue' : ''">{{
            previewLabel(row.previewStatus)
          }}</span>
          <small>原文件</small
          ><strong class="path-text">{{ object(row.source).name || row.name }}</strong>
          <template v-if="row.previewStatus === 'planned'">
            <small>整理后文件名</small
            ><strong class="path-text">{{ object(row.media).name }}</strong>
            <small>归档子目录</small>
            <p class="path-text">{{ object(row.media).directory }}</p>
            <p class="muted-text">
              {{ object(row.media).title }} {{ object(row.media).year || '' }}
              {{ episodeLabel(object(row.media))
              }}<template v-if="object(row.media).tmdb_id">
                · TMDB {{ object(row.media).tmdb_id }}</template
              ><template v-if="records(row.followers).length">
                · 随行文件 {{ records(row.followers).length }} 个</template
              >
            </p>
          </template>
          <p v-if="row.reason" class="warning-text">{{ row.reason }}</p>
        </div>
        <button
          class="button small"
          @click="
            fixing = {
              id: String(object(row.source).id || row.file_id),
              name: String(object(row.source).name || row.name),
            }
          "
        >
          修正识别
        </button>
      </article>
    </div>
    <details>
      <summary>过滤、版本与清理建议</summary>
      <EvidenceDetails
        :value="{
          filtered: plan.filtered,
          quality8: plan.quality8,
          quality_held: plan.quality_held,
          cleanup_candidates: plan.cleanup_candidates,
        }"
      />
    </details>
  </section>
  <section v-if="job" class="organize-current-job">
    <div v-if="['queued', 'running'].includes(String(jobState?.state))" class="action-row">
      <button
        class="button"
        :disabled="op.busy.value || op.uncertain.value"
        @click="confirm = 'stop'"
      >
        安全停止本次整理</button
      ><span class="muted-text">当前文件组完成后停止，已完成的归档保留。</span>
    </div>
    <TaskReceipt :id="job" @update="(value) => (jobState = value)" />
  </section>
  <ConfirmAction
    v-if="confirm"
    :title="
      confirm === 'stop'
        ? '安全停止整理'
        : confirm === 'categories'
          ? '创建分类目录'
          : '执行整理归档'
    "
    :description="
      confirm === 'stop'
        ? '当前文件组完成后停止，关联定时规则同时暂停；请在任务中心核实最终结果。'
        : confirm === 'categories'
          ? '在当前归档目录下按已保存分类配置创建文件夹。'
          : '按本次预览对已识别资源改名并移动到归档目录，依据已保存洗版策略处理版本；生成 STRM 不等于 Emby 已确认入库。'
    "
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirm = ''"
    @confirm="execute"
    ><EvidenceDetails
      v-if="plan"
      :value="{
        count: records(plan.items).length,
        unknown_items: plan.unknown_items,
        quality8: plan.quality8,
      }" /></ConfirmAction
  ><MediaOverride
    v-if="fixing"
    :file-id="fixing.id"
    :name="fixing.name"
    @close="fixing = undefined"
    @save="
      (v) => {
        overrides[v.id] = v.choice;
        fixing = undefined;
        invalidate();
        load();
      }
    "
  />
</template>

<style scoped>
.preview-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin: 18px 0;
}
.preview-toolbar label {
  display: grid;
  gap: 6px;
  flex: 1;
  min-width: 180px;
}
.preview-comparison {
  min-width: 0;
  display: grid;
  gap: 6px;
}
.preview-comparison > .badge {
  justify-self: start;
}
.preview-comparison small {
  color: var(--muted);
}
.preview-comparison p {
  margin: 0;
}
.preview-comparison .path-text {
  overflow-wrap: anywhere;
}
</style>
