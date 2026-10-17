<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue';
import { readData, writeData, records, object, strings, text, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { date } from '../composables/format';
import PageHeading from '../components/PageHeading.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import OperationState from '../components/OperationState.vue';
import TaskReceipt from '../components/TaskReceipt.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import ModalDialog from '../components/ModalDialog.vue';

const GROUPS = [
  { key: 'del_local_items', label: '本地待清理', type: 'loc' },
  { key: 'del_share_items', label: '分享待清理', type: 'shr' },
  { key: 'protected_items', label: '受保护', type: 'keep' },
  { key: 'exempted_items', label: '规则豁免', type: 'exempt' },
] as const;
type GroupType = (typeof GROUPS)[number]['type'];
const PLAN_TTL_MS = 2 * 3600 * 1000;

const summary = useRequest<Data>(),
  plans = useRequest<Data>(),
  detail = useRequest<Data>(),
  op = useOperation(),
  task = ref(''),
  confirmation = ref(''),
  selected = ref<Data>(),
  selectedType = ref<GroupType>('loc'),
  detailOpen = ref(false),
  planOpen = ref(false),
  group = ref('all'),
  search = ref('');
const visible = ref(200);
watch([group, search], () => {
  visible.value = 200;
});
const scan = computed(() => object(summary.data.value?.scan)),
  result = computed(() => object(scan.value.result));
const items = computed(() => {
  const pool: (Data & { _type: GroupType })[] =
    group.value === 'all'
      ? GROUPS.flatMap((g) =>
          records(result.value[g.key]).map(
            (r) => ({ ...r, _type: g.type }) as Data & { _type: GroupType },
          ),
        )
      : records(result.value[group.value]).map(
          (r) =>
            ({
              ...r,
              _type: (GROUPS.find((g) => g.key === group.value)?.type || 'loc') as GroupType,
            }) as Data & { _type: GroupType },
        );
  return pool.filter((r) => JSON.stringify(r).includes(search.value));
});
const keepCount = computed(() => records(result.value.protected_items).length);
const locCount = computed(() => records(result.value.del_local_items).length);
const shrCount = computed(() => records(result.value.del_share_items).length);
const exemptCount = computed(() => {
  const c = result.value.exempted_count;
  return c != null ? Number(c) : records(result.value.exempted_items).length;
});
const planId = computed(() =>
  String(object(detail.data.value?.plan).id || scan.value.plan_id || ''),
);
const planDetail = computed(() => object(detail.data.value?.plan));
const meta = computed(() => object(selected.value?.meta));
const compare = computed(() => object(meta.value.compare));
const localPaths = computed(() => strings(meta.value.local_paths));
const sharePaths = computed(() => strings(meta.value.share_paths));
const libs = computed(() => strings(meta.value.libs));
const tmdb = computed(() => strings(meta.value.tmdb));
const localTmdb = computed(() => strings(meta.value.local_tmdb));
const shareTmdb = computed(() => strings(meta.value.share_tmdb));
const keywords = computed(() => strings(meta.value.keywords));
const members = computed(() => strings(meta.value.members));
const localTag = computed(() =>
  selectedType.value === 'loc' ? '将删除' : selectedType.value === 'shr' ? '保留' : '',
);
const shareTag = computed(() =>
  selectedType.value === 'shr' ? '将删除' : selectedType.value === 'loc' ? '保留' : '',
);
const planStatList = computed(() => {
  const s = object(planDetail.value.stats);
  return [
    { label: '待删本地', n: s.loc, cls: 'amber' },
    { label: '淘汰分享', n: s.shr, cls: 'red' },
    { label: '受保护', n: s.keep, cls: 'green' },
    { label: '白名单豁免', n: s.exempt, cls: 'blue' },
  ];
});

async function load() {
  await Promise.allSettled([
    summary.run((signal) => readData('/api/governance/summary', signal)),
    plans.run((signal) => readData('/api/plans?limit=100', signal)),
  ]);
}
function openItem(item: Data, type: GroupType) {
  selected.value = item;
  selectedType.value = type;
  detailOpen.value = true;
}
async function showPlan(id: string) {
  planOpen.value = true;
  detail.data.value = undefined;
  await detail.run((signal) => readData(`/api/plan/${encodeURIComponent(id)}`, signal));
}
function openPlanAction(a: Data) {
  const files = a.files as unknown[] | undefined;
  openItem(
    {
      text: a.text,
      detail: a.detail || a.text,
      reason_label: a.reason_label,
      title: a.title,
      season: a.season,
      meta: object(a.meta),
      files_count: files?.length ?? 0,
    },
    (String(a.kind) as GroupType) || 'loc',
  );
}
function planActions(kind?: string) {
  return records(object(detail.data.value?.plan).actions).filter((a) => !kind || a.kind === kind);
}
function planStateOf(p: Data) {
  let st = String(p.state || 'pending');
  if (st === 'pending' && p.ts && Date.now() - Number(p.ts) * 1000 > PLAN_TTL_MS) st = 'expired';
  return st;
}
function planStateLabel(p: Data) {
  const m: Record<string, string> = {
    pending: '待执行',
    executing: '执行中',
    done: '已完成',
    failed: '执行失败',
    stale: '规则已变更',
    expired: '已过期',
  };
  return m[planStateOf(p)] || '已过期';
}
function planStateCls(p: Data) {
  const m: Record<string, string> = {
    pending: 'amber',
    executing: 'blue',
    done: 'green',
    failed: 'red',
    stale: 'indigo',
    expired: 'gray',
  };
  return m[planStateOf(p)] || 'gray';
}
function ago(ts: unknown) {
  const t = Number(ts);
  if (!t) return '';
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - t));
  if (sec < 60) return '刚刚';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分钟前';
  if (sec < 86400) return Math.floor(sec / 3600) + ' 小时前';
  return Math.floor(sec / 86400) + ' 天前';
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
    planOpen.value = false;
  }
}
function prepareClean(dry: boolean) {
  detail.data.value = undefined;
  confirmation.value = dry ? 'dry' : 'clean';
}
onMounted(load);
</script>

<template>
  <PageHeading
    title="双库治理"
    subtitle="扫描生成清单，复核删除范围，再执行清理。本地库联动清理 NAS 与对应 115 源文件；分享库只清理 NAS。"
  >
    <div class="action-row">
      <RouterLink to="/governance/rules" class="btn gray">七维与治理规则</RouterLink>
      <button
        class="btn"
        :disabled="op.busy.value || op.uncertain.value"
        @click="confirmation = 'scan'"
      >
        立即扫描双库
      </button>
      <button
        class="btn red"
        :disabled="!scan.plan_id || !scan.usable || op.busy.value || op.uncertain.value"
        @click="prepareClean(false)"
      >
        执行清理
      </button>
    </div>
  </PageHeading>

  <OperationState
    :busy="op.busy.value"
    :error="op.error.value || summary.error.value || plans.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />

  <section class="panel">
    <div class="row-between">
      <h3>扫描与执行</h3>
      <div class="action-row">
        <button
          class="btn"
          :disabled="op.busy.value || op.uncertain.value"
          @click="confirmation = 'scan'"
        >
          重新扫描
        </button>
        <button
          class="btn gray"
          :disabled="!scan.plan_id || !scan.usable || op.busy.value || op.uncertain.value"
          @click="prepareClean(true)"
        >
          预演计划
        </button>
        <button
          class="btn red"
          :disabled="!scan.plan_id || !scan.usable || op.busy.value || op.uncertain.value"
          @click="prepareClean(false)"
        >
          执行清理
        </button>
      </div>
    </div>
    <div class="governance-flow">
      <span><i>1</i>扫描比对</span>
      <span><i>2</i>复核清单</span>
      <span><i>3</i>执行清理</span>
    </div>
    <p class="section-note">
      本地库同步清理 NAS 与对应 115 源文件；分享库清理 NAS。逐季匹配只处理选中季。
    </p>
    <div class="governance-readiness">
      <span>最近诊断 {{ date(Number(scan.ts)) }}</span>
      <span>{{ scan.usable ? '计划可核对执行' : '计划尚未生成或不可用' }}</span>
      <span v-if="scan.plan_id">计划 plan_{{ scan.plan_id }}</span>
    </div>
  </section>

  <div class="cards">
    <div class="card clickable" @click="group = 'protected_items'">
      <div class="label">受保护项</div>
      <div class="value green">{{ keepCount }}</div>
      <div class="sub">点击查看详情</div>
    </div>
    <div class="card clickable" @click="group = 'del_local_items'">
      <div class="label">本地待清理</div>
      <div class="value amber">{{ locCount }}</div>
      <div class="sub">分享画质达标</div>
    </div>
    <div class="card clickable" @click="group = 'del_share_items'">
      <div class="label">分享待清理</div>
      <div class="value red">{{ shrCount }}</div>
      <div class="sub">画质次级 / 落后</div>
    </div>
    <div class="card clickable" @click="group = 'exempted_items'">
      <div class="label">规则豁免</div>
      <div class="value blue">{{ exemptCount }}</div>
      <div class="sub">点击查看</div>
    </div>
  </div>

  <section class="panel">
    <div class="segmented" style="margin-bottom: 14px">
      <button :class="{ active: group === 'all' }" @click="group = 'all'">全部</button>
      <button
        v-for="g in GROUPS"
        :key="g.key"
        :class="{ active: group === g.key }"
        @click="group = g.key"
      >
        {{ g.label }} {{ records(result[g.key]).length }}
      </button>
    </div>
    <input
      v-model="search"
      class="gov-search"
      aria-label="搜索治理项目"
      placeholder="搜索作品或原因"
    />
    <div>
      <div
        v-for="(item, i) in items.slice(0, visible)"
        :key="i"
        class="list-item clickable"
        :class="item._type"
        @click="openItem(item, item._type as GroupType)"
      >
        <div class="t">{{ item.title || item.text }}</div>
        <div class="d">{{ item.reason_label || item.detail }}</div>
      </div>
      <div v-if="!items.length" class="list-empty">
        {{ result.plan_id ? '当前筛选下无匹配项' : '请点击右上角「重新扫描」开始' }}
      </div>
    </div>
    <div v-if="items.length > visible" style="text-align: center; margin-top: 14px">
      <button class="btn gray" @click="visible += 200">
        加载更多治理项目（{{ visible }} / {{ items.length }}）
      </button>
    </div>
  </section>

  <details class="panel governance-evidence">
    <summary>
      <b>扫描依据与状态</b><span>规则对齐 {{ scan.rule_aligned ? '是' : '否' }}</span>
    </summary>
    <div class="gov-truth">
      <div class="meta-row">
        <span class="k">扫描时间</span><span class="v">{{ date(Number(scan.ts)) }}</span>
      </div>
      <div class="meta-row">
        <span class="k">计划状态</span><span class="v">{{ text(scan.plan_state) }}</span>
      </div>
      <div class="meta-row">
        <span class="k">本地 / 分享 STRM</span
        ><span class="v"
          >{{ object(result.strm_counts).local ?? '—' }} /
          {{ object(result.strm_counts).share ?? '—' }}</span
        >
      </div>
      <div class="meta-row">
        <span class="k">身份冲突</span
        ><span class="v">{{ result.identity_conflict_count ?? 0 }}</span>
      </div>
    </div>
    <EvidenceDetails :value="summary.data.value" />
  </details>

  <TaskReceipt v-if="task" :id="task" kind="task" @done="load" />

  <section class="panel">
    <div class="row-between" style="margin-bottom: 14px">
      <h3>治理计划与执行归档</h3>
      <button class="btn gray sm" @click="load">刷新</button>
    </div>
    <p class="section-note">
      治理计划与清理结果集中保存在这里。待执行计划 2
      小时内有效；清理结果和已执行计划长期保留，可回看具体明细。
    </p>
    <div v-for="p in records(plans.data.value?.plans)" :key="String(p.id)" class="plan-card">
      <div class="plan-head">
        <span class="plan-time"
          >{{ date(Number(p.ts)) }} <span class="dim">{{ ago(p.ts) }}</span></span
        >
        <span class="pchip" :class="planStateCls(p)">{{ planStateLabel(p) }}</span>
      </div>
      <div class="pchips">
        <span class="pchip" :class="Number(object(p.stats).loc) > 0 ? 'amber' : 'gray'"
          >待删本地 {{ object(p.stats).loc ?? 0 }}</span
        >
        <span class="pchip" :class="Number(object(p.stats).shr) > 0 ? 'red' : 'gray'"
          >淘汰分享 {{ object(p.stats).shr ?? 0 }}</span
        >
        <span class="pchip" :class="Number(object(p.stats).keep) > 0 ? 'green' : 'gray'"
          >受保护 {{ object(p.stats).keep ?? 0 }}</span
        >
        <span class="pchip" :class="Number(object(p.stats).exempt) > 0 ? 'blue' : 'gray'"
          >白名单豁免 {{ object(p.stats).exempt ?? 0 }}</span
        >
      </div>
      <div class="plan-foot">
        <span class="dim">plan_{{ p.id }}</span>
        <button class="btn sm gray" @click="showPlan(String(p.id))">查看详情</button>
      </div>
    </div>
    <div v-if="!records(plans.data.value?.plans).length" class="list-empty">暂无治理计划记录</div>
    <details style="margin-top: 14px">
      <summary>目录清理等执行归档</summary>
      <EvidenceDetails :value="plans.data.value?.archives" />
    </details>
  </section>

  <ModalDialog
    v-if="detailOpen"
    :title="
      selectedType === 'keep'
        ? '受保护项详情'
        : selectedType === 'exempt'
          ? '规则豁免详情'
          : '治理依据详情'
    "
    @close="detailOpen = false"
  >
    <div class="meta-row">
      <span class="k">标题</span><span class="v">{{ text(selected?.text) }}</span>
    </div>
    <div v-if="selected?.reason_label" class="meta-row">
      <span class="k">原因</span><span class="v">{{ text(selected?.reason_label) }}</span>
    </div>
    <div v-if="meta.title" class="meta-row">
      <span class="k">剧名</span><span class="v">{{ text(meta.title) }}</span>
    </div>
    <div v-if="meta.season != null" class="meta-row">
      <span class="k">季</span><span class="v">S{{ String(meta.season).padStart(2, '0') }}</span>
    </div>
    <div v-if="meta.local_seasons != null" class="meta-row">
      <span class="k">本地季数</span><span class="v">{{ text(meta.local_seasons) }}</span>
    </div>
    <div v-if="meta.share_seasons != null" class="meta-row">
      <span class="k">分享季数</span><span class="v">{{ text(meta.share_seasons) }}</span>
    </div>
    <div v-if="Object.keys(compare).length" class="meta-row stacked">
      <div class="k">
        画质对比依据<span v-if="compare.episode != null"
          >（代表集 E{{ String(compare.episode).padStart(2, '0') }}）</span
        >
      </div>
      <div class="v compare">
        <div>分享：{{ text(compare.share_name) }}</div>
        <div>本地：{{ text(compare.local_name) }}</div>
      </div>
    </div>
    <div v-if="libs.length" class="meta-row">
      <span class="k">所在库</span><span class="v">{{ libs.join(' / ') }}</span>
    </div>
    <div v-if="meta.match_basis" class="meta-row">
      <span class="k">匹配来源</span><span class="v">{{ text(meta.match_basis) }}</span>
    </div>
    <div v-if="meta.match_basis_detail" class="meta-row">
      <span class="k">匹配依据</span><span class="v">{{ text(meta.match_basis_detail) }}</span>
    </div>
    <details v-if="localPaths.length" class="path-block">
      <summary class="path-hd">
        <span class="k"
          >本地{{ meta.season != null ? ' · 第 ' + String(meta.season) + ' 季' : '' }} ·
          {{ meta.local_count ?? localPaths.length }} 个文件</span
        >
        <span v-if="localTag" class="path-tag" :class="localTag === '将删除' ? 'del' : 'keep'">{{
          localTag
        }}</span>
      </summary>
      <div class="path-list">
        <div v-for="(p, j) in localPaths" :key="j">{{ p }}</div>
      </div>
    </details>
    <details v-if="sharePaths.length" class="path-block">
      <summary class="path-hd">
        <span class="k"
          >分享{{ meta.season != null ? ' · 第 ' + String(meta.season) + ' 季' : '' }} ·
          {{ meta.share_count ?? sharePaths.length }} 个文件</span
        >
        <span v-if="shareTag" class="path-tag" :class="shareTag === '将删除' ? 'del' : 'keep'">{{
          shareTag
        }}</span>
      </summary>
      <div class="path-list">
        <div v-for="(p, j) in sharePaths" :key="j">{{ p }}</div>
      </div>
    </details>
    <div v-if="tmdb.length" class="meta-row">
      <span class="k">TMDB</span>
      <span class="v"
        >{{ tmdb.join(' / ')
        }}<b v-if="!meta.tmdb_consistent" style="color: var(--warn)"> ⚠ 标识不一致</b></span
      >
    </div>
    <div v-if="localTmdb.length" class="meta-row">
      <span class="k">本地 TMDB</span><span class="v">{{ localTmdb.join(' / ') }}</span>
    </div>
    <div v-if="shareTmdb.length" class="meta-row">
      <span class="k">分享 TMDB</span><span class="v">{{ shareTmdb.join(' / ') }}</span>
    </div>
    <div v-if="meta.identity_source" class="meta-row">
      <span class="k">身份来源</span><span class="v">{{ text(meta.identity_source) }}</span>
    </div>
    <div v-if="keywords.length" class="meta-row">
      <span class="k">命中关键词</span><span class="v">{{ keywords.join(', ') }}</span>
    </div>
    <div v-if="members.length" class="meta-row stacked">
      <div class="k">包含 {{ meta.member_count ?? members.length }} 项</div>
      <div class="ing-tags">
        <span v-for="(m, j) in members" :key="j">{{ m }}</span>
      </div>
    </div>
    <div class="meta-row">
      <span class="k">涉及文件数</span><span class="v">{{ selected?.files_count ?? 0 }}</span>
    </div>
  </ModalDialog>

  <ModalDialog
    v-if="planOpen"
    title="治理计划详情"
    @close="
      planOpen = false;
      detail.cancel();
    "
  >
    <p v-if="detail.error.value" class="inline-error">{{ detail.error.value }}</p>
    <template v-if="detail.loading.value">
      <div class="list-empty">加载中…</div>
    </template>
    <template v-else-if="planDetail.id">
      <div class="meta-row">
        <span class="k">状态</span><span class="v">{{ planStateLabel(planDetail) }}</span>
      </div>
      <div class="meta-row">
        <span class="k">生成时间</span
        ><span class="v">{{ date(Number(planDetail.ts)) }}（{{ ago(planDetail.ts) }}）</span>
      </div>
      <div v-if="planDetail.executed_at" class="meta-row">
        <span class="k">执行时间</span
        ><span class="v">{{ date(Number(planDetail.executed_at)) }}</span>
      </div>
      <div class="pchips" style="margin: 10px 0">
        <span v-for="c in planStatList" :key="c.label" class="pchip" :class="c.cls"
          >{{ c.label }} {{ c.n ?? 0 }}</span
        >
      </div>
      <div class="plan-actions">
        <template v-for="g in GROUPS" :key="g.key">
          <template v-if="planActions(g.type).length">
            <div class="plan-kind">{{ g.label }}（{{ planActions(g.type).length }}）</div>
            <div
              v-for="(a, j) in planActions(g.type)"
              :key="j"
              class="plan-row"
              @click="openPlanAction(a)"
            >
              <div class="t">
                {{ a.title || a.action_id
                }}<span v-if="a.season != null" class="dim"> · 第 {{ a.season }} 季</span>
              </div>
              <div class="d">{{ a.reason_label || a.reason }}</div>
            </div>
          </template>
        </template>
        <div v-if="!planActions().length" class="list-empty">这份计划没有待处理项</div>
      </div>
    </template>
  </ModalDialog>

  <ConfirmAction
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
  >
    <div v-if="confirmation !== 'scan'">
      <div class="meta-row">
        <span class="k">计划编号</span><span class="v">{{ text(scan.plan_id) }}</span>
      </div>
      <div class="meta-row">
        <span class="k">本地待治理</span
        ><span class="v"
          >{{ result.del_local_cnt ?? 0 }} 项 / {{ result.del_local_files ?? 0 }} 文件</span
        >
      </div>
      <div class="meta-row">
        <span class="k">分享待治理</span
        ><span class="v"
          >{{ result.del_share_cnt ?? 0 }} 项 / {{ result.del_share_files ?? 0 }} 文件</span
        >
      </div>
    </div>
  </ConfirmAction>
</template>

<style scoped>
.row-between {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.row-between h3 {
  margin: 0;
  font-size: 15px;
}
.section-note {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.8;
  margin: 8px 0 14px;
}
.governance-flow {
  display: flex;
  gap: 20px;
  border-top: 1px solid var(--border);
  padding-top: 16px;
  margin-top: 16px;
}
.governance-flow > span {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 12px;
  color: var(--text-mid);
}
.governance-flow i {
  font-size: 10px;
  font-style: normal;
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  background: var(--info-soft);
  color: var(--info);
  font-weight: 700;
}
.governance-readiness {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.governance-readiness > span {
  font-size: 11px;
  background: var(--surface-hover);
  padding: 5px 10px;
  border-radius: 6px;
  color: var(--text-mid);
}
.gov-search {
  width: 100%;
  margin-bottom: 14px;
  padding: 10px 13px;
  border: 1px solid var(--border-input);
  border-radius: 9px;
  background: var(--surface-solid);
  color: var(--text);
  font-size: 13px;
}
.governance-evidence summary {
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  list-style: none;
  font-size: 13px;
}
.governance-evidence summary span {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-dim);
  margin-left: auto;
}
.governance-evidence[open] summary {
  margin-bottom: 16px;
}
.gov-truth {
  margin-bottom: 14px;
}
/* 详情弹窗结构 */
.meta-row {
  display: flex;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  font-size: 12px;
  gap: 12px;
}
.meta-row:last-child {
  border-bottom: none;
}
.meta-row .k {
  color: var(--text-dim);
  flex-shrink: 0;
}
.meta-row .v {
  font-weight: 500;
  text-align: right;
  word-break: break-all;
  color: var(--text);
  min-width: 0;
}
.meta-row.stacked {
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}
.meta-row.stacked .v {
  text-align: left;
}
.meta-row .compare {
  font-size: 11px;
  color: var(--text-dim);
  line-height: 1.7;
}
.path-block {
  margin-top: 8px;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 8px 12px;
}
.path-block summary {
  list-style: none;
  cursor: pointer;
}
.path-block summary::-webkit-details-marker {
  display: none;
}
.path-hd {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.path-hd .k {
  flex: 1;
  min-width: 0;
  color: var(--text-dim);
  font-size: 12px;
}
.path-tag {
  font-size: 11px;
  padding: 1px 9px;
  border-radius: 10px;
  font-weight: 600;
}
.path-tag.del {
  background: rgba(239, 68, 68, 0.18);
  color: #ef4444;
}
.path-tag.keep {
  background: rgba(16, 185, 129, 0.18);
  color: #10b981;
}
.path-list {
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.7;
  word-break: break-all;
  color: var(--text-mid);
  max-height: 220px;
  overflow: auto;
}
.ing-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  max-height: 220px;
  overflow-y: auto;
}
.ing-tags span {
  background: var(--bg-dim);
  border-radius: 6px;
  padding: 3px 9px;
  font-size: 12px;
  color: var(--text);
  word-break: break-word;
}
/* 计划归档卡片 */
.plan-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--surface);
  padding: 14px 16px;
  margin-bottom: 12px;
}
.plan-head,
.plan-foot {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.plan-time {
  font-weight: 700;
  font-size: 14px;
}
.plan-foot {
  margin-top: 10px;
  font-size: 11px;
  color: var(--text-dim);
}
.dim {
  color: var(--text-dim);
  font-weight: 400;
}
.pchips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 10px 0;
}
.pchip {
  display: inline-flex;
  align-items: center;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.pchip.amber {
  color: #b45309;
  background: rgba(217, 119, 6, 0.13);
}
.pchip.red {
  color: #b91c1c;
  background: rgba(220, 38, 38, 0.12);
}
.pchip.green {
  color: #047857;
  background: rgba(22, 163, 74, 0.12);
}
.pchip.blue {
  color: #1d4ed8;
  background: rgba(37, 99, 235, 0.12);
}
.pchip.indigo {
  color: #6d28d9;
  background: rgba(124, 58, 237, 0.13);
}
.pchip.gray {
  color: var(--text-dim);
  background: var(--surface-hover);
}
/* 计划详情动作列表 */
.plan-actions {
  max-height: 380px;
  overflow-y: auto;
  background: var(--surface-hover);
  border-radius: 10px;
  padding: 4px 10px;
}
.plan-kind {
  font-size: 12px;
  font-weight: 700;
  margin: 10px 0 4px;
  color: var(--text-mid);
}
.plan-row {
  padding: 7px 0;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
}
.plan-row .t {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.plan-row .d {
  color: var(--text-mid);
  font-size: 12px;
  margin-top: 2px;
}
</style>
