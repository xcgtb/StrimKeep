<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { get, post } from '../api/client';
import { compact, date, object, records, text } from '../api/domain';

const summary = ref<Record<string, unknown>>({});
const loading = ref(true);
const error = ref('');
const busy = ref(false);
const message = ref('');
const armed = ref(false);
const cleanArmed = ref(false);

const scan = computed(() => object(summary.value.scan));
const result = computed(() => object(scan.value.result));
const consistency = computed(() => object(summary.value.consistency));
const strm = computed(() => object(result.value.strm_counts));

const readyState = computed(() =>
  scan.value.usable
    ? '清单可执行'
    : scan.value.found
      ? scan.value.plan_state === 'done'
        ? '清理已执行'
        : '需要重新扫描'
      : '等待扫描',
);
const pendingCount = computed(
  () =>
    `待处理 ${compact(result.value.total_clean_cnt || 0)} 项 · ${compact(
      Number(result.value.del_local_files || 0) + Number(result.value.del_share_files || 0),
    )} 个 STRM`,
);
// 1.4.7 runtime.js renderGovTruth 的字段顺序，一字不改
const truth = computed(() => [
  ['扫描状态', scan.value.usable ? '可执行' : '需重新扫描'],
  ['身份冲突', `${compact(result.value.identity_conflict_count || 0)} 个 TMDB 标识冲突（不自动删除）`],
  ['待处理', `${compact(result.value.total_clean_cnt || 0)} 项 / ${compact(Number(result.value.del_local_files || 0) + Number(result.value.del_share_files || 0))} 文件`],
  ['保护 / 豁免', `${records(result.value.protected_items).length} / ${compact(result.value.exempted_count || 0)}`],
  ['静默跳过', `${compact(result.value.quiet_skipped || 0)} 项`],
  ['STRM', `本地 ${compact(strm.value.local)} · 分享 ${compact(strm.value.share)} · 合计 ${compact(strm.value.total)}`],
  ['片库快照', date(Number(object(consistency.value.library).ts))],
  ['24H 入库快照', date(Number(object(consistency.value.ingest).ts))],
  ['规则指纹', text(consistency.value.rule_sig).slice(0, 16) || '—'],
  ['扫描规则', scan.value.rule_aligned === false ? '已变化，请重新扫描' : '与当前规则一致'],
]);
const cards = computed(() => [
  { label: '受保护项', icon: 'shield', tone: 'green', value: records(result.value.protected_items).length, sub: '点击查看详情' },
  { label: '本地待清理', icon: 'download', tone: 'amber', value: result.value.del_local_cnt, sub: '分享画质达标' },
  { label: '分享待清理', icon: 'trash', tone: 'red', value: result.value.del_share_cnt, sub: '画质次级 / 落后' },
  { label: '规则豁免', icon: 'info', tone: 'blue', value: result.value.exempted_count, sub: '点击查看' },
]);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    summary.value = object(await get<Record<string, unknown>>('/api/governance/summary'));
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
async function scanNow() {
  if (!armed.value) {
    armed.value = true;
    return;
  }
  armed.value = false;
  busy.value = true;
  try {
    const data = await post<Record<string, unknown>>('/api/check');
    message.value = data.task_id ? `扫描任务已提交（${text(data.task_id)}）` : '扫描完成，正在刷新…';
    await load();
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function cleanNow() {
  if (!cleanArmed.value) {
    cleanArmed.value = true;
    return;
  }
  cleanArmed.value = false;
  busy.value = true;
  try {
    const data = await post<Record<string, unknown>>('/api/clean', { confirmed: true });
    message.value = data.task_id ? `清理任务已提交（${text(data.task_id)}）` : '清理完成，正在刷新…';
    await load();
  } catch (e) {
    message.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
onMounted(load);
</script>
<template>
  <section class="redo147">
    <div class="page-intro">
      <div>
        <span class="eyebrow">质量对比与去重</span>
        <h1>双库治理</h1>
        <p>扫描生成清单，复核删除范围，再执行清理。</p>
      </div>
      <button class="btn gray" type="button" @click="$router.push('/tasks')" data-icon="archive">计划存档</button>
    </div>

    <div class="panel">
      <div class="row-between">
        <h3 data-icon="scale">扫描与执行</h3>
        <div style="display: flex; gap: 8px">
          <button class="btn gray" :disabled="busy" @click="scanNow" data-icon="refresh">
            {{ busy ? '扫描中…' : armed ? '确认重新扫描' : '重新扫描' }}
          </button>
          <button class="btn red" :disabled="busy || !(scan && scan.usable)" data-icon="trash" @click="cleanNow">
            {{ cleanArmed ? '确认执行清理' : '执行清理' }}
          </button>
        </div>
      </div>
      <div class="governance-flow"><span><i>1</i>扫描比对</span><span><i>2</i>复核清单</span><span><i>3</i>执行清理</span></div>
      <p class="section-note">本地库同步清理 NAS 与对应 115 源文件；分享库清理 NAS。逐季匹配只处理选中季。</p>
      <div class="governance-readiness"><span>{{ readyState }}</span><span>{{ pendingCount }}</span></div>
      <p v-if="armed" class="section-note">再次点击确认：将遍历本地与分享 STRM 库生成清单，不会删除文件。</p>
      <p v-if="cleanArmed" class="section-note">再次点击确认：将按当前治理清单删除冗余 STRM（本地库联动删除源文件）。</p>
      <p v-if="message" class="section-note" role="status">{{ message }}</p>
      <p v-if="error" class="section-note">{{ error }}</p>
    </div>

    <div class="cards">
      <div v-for="card in cards" :key="card.label" class="card clickable">
        <div class="label" :data-icon="card.icon">{{ card.label }}</div>
        <div class="value" :class="card.tone">{{ card.value === undefined ? '—' : compact(card.value) }}</div>
        <div class="sub">{{ card.sub }}</div>
      </div>
    </div>

    <details class="panel governance-evidence" open>
      <summary><b>扫描依据与状态</b><span>{{ scan.ts ? '已扫描' : '尚无扫描' }}</span></summary>
      <div class="gov-reason-grid">
        <div v-for="[key, value] in truth" :key="key" class="meta-row">
          <span class="k">{{ key }}</span><span class="v">{{ value }}</span>
        </div>
      </div>
    </details>

    <div class="panel">
      <div class="segmented" style="margin-bottom: 14px">
        <button class="active">全部</button>
        <button>本地待清理</button>
        <button>分享待清理</button>
        <button>受保护</button>
        <button>规则豁免</button>
      </div>
      <div class="list-empty">{{ loading ? '读取中…' : '请点击右上角「重新扫描」开始' }}</div>
    </div>
  </section>
</template>
