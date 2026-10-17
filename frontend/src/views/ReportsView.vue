<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { readData, writeData, object, records, strings, text, type Data } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import PageHeading from '../components/PageHeading.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import ModalDialog from '../components/ModalDialog.vue';
import OperationState from '../components/OperationState.vue';

const ingest = useRequest<Data>(),
  recordsRequest = useRequest<Data>(),
  morning = useRequest<Data>(),
  detail = useRequest<Data>(),
  op = useOperation(),
  tab = ref('ingest'),
  items = ref<string[]>([]),
  previewText = ref(''),
  confirmation = ref(''),
  detailOpen = ref(false),
  limit = ref(50);
const form = reactive({ enabled: true, hour: 9, minute: 0, prescan_min: 5 });

async function loadIngest(full: boolean, force: boolean) {
  return ingest.run((signal) =>
    readData(`/api/ingest?full=${full ? 1 : 0}&force=${force ? 1 : 0}`, signal),
  );
}
async function load() {
  await Promise.allSettled([
    loadIngest(true, false),
    recordsRequest.run((signal) => readData(`/api/records?n=${limit.value}`, signal)),
    morning.run((signal) => readData('/api/morning', signal)),
  ]);
  const cfg = object(morning.data.value?.morning);
  form.enabled = !!cfg.enabled;
  form.hour = Number(cfg.hour ?? 9);
  form.minute = Number(cfg.minute ?? 0);
  form.prescan_min = Number(cfg.prescan_min ?? 5);
  items.value = strings(cfg.items);
}
async function preview(force = false) {
  const r = await op.run(() => writeData('/api/morning/preview', { items: items.value, force }));
  if (r) previewText.value = text(r.text);
}
async function saveMorning() {
  const r = await op.run(() => writeData('/api/morning', { ...form, items: items.value }));
  if (r) await morning.run((signal) => readData('/api/morning', signal));
}
async function execute() {
  const isSave = confirmation.value === 'save';
  const r = await op.run(() =>
    writeData(
      isSave ? '/api/morning' : '/api/morning/send',
      isSave ? { ...form, items: items.value } : { items: items.value, force: false },
    ),
  );
  if (r) {
    confirmation.value = '';
    if (isSave) await morning.run((signal) => readData('/api/morning', signal));
  }
}
async function showRecord(id: string) {
  detailOpen.value = true;
  detail.data.value = undefined;
  await detail.run((signal) => readData(`/api/records/${encodeURIComponent(id)}`, signal));
}
onMounted(load);

/* ── 近 24 小时入库（发现内容分组） ── */
interface IngShow {
  name: string;
  count: number;
  seasons: string;
}
interface IngGroup {
  source: string;
  category: string;
  kind: 'tv' | 'movie';
  count: number;
  shows: IngShow[];
  movies: string[];
}
const ingData = computed(() => object(ingest.data.value));
const ingStats = computed(() => object(ingData.value.stats));
const ingTree = computed(() => object(ingData.value.tree));
const ingestText = computed(() => {
  const raw = ingest.data.value;
  if (!raw) return '';
  if (Object.keys(ingTree.value).length) return '';
  return text(raw.text || JSON.stringify(raw))
    .replace(/\*\*/g, '')
    .replace(/`/g, '');
});
const ingestHint = computed(() => {
  if (!ingest.data.value) return '—';
  const d = ingData.value;
  const age = d.cache_ts ? Math.round(Date.now() / 1000 - Number(d.cache_ts)) : null;
  const ageStr =
    age == null
      ? '—'
      : age < 60
        ? age + ' 秒前'
        : age < 3600
          ? Math.floor(age / 60) + ' 分钟前'
          : Math.floor(age / 3600) + ' 小时前';
  let s = (d.from_cache ? '📦 来自缓存' : '🔄 现场扫描') + ' · ' + ageStr;
  if (d.warning) s += ' · ⚠️ 扫描失败（' + text(d.warning) + '），以上为旧数据';
  return s;
});
function pad2(n: unknown): string {
  const num = Number(n);
  return Number.isFinite(num) ? String(num).padStart(2, '0') : String(n);
}
function seasonRanges(eps: number[]): string[] {
  const nums = eps.filter((n) => Number.isFinite(n)).sort((a, b) => a - b);
  const ranges: string[] = [];
  if (!nums.length) return ranges;
  let st = nums[0],
    pv = nums[0];
  for (let i = 1; i <= nums.length; i++) {
    if (nums[i] === pv + 1) {
      pv = nums[i];
      continue;
    }
    ranges.push(st === pv ? 'E' + pad2(st) : 'E' + pad2(st) + '–E' + pad2(pv));
    if (i < nums.length) st = pv = nums[i];
  }
  return ranges;
}
function showSeasons(detail: Data): string {
  const seasons = object(detail.seasons);
  return Object.keys(seasons)
    .sort((a, b) => (a === 'unknown' ? 1 : b === 'unknown' ? -1 : Number(a) - Number(b)))
    .map((sk) => {
      const sd = object(seasons[sk]);
      const eps = records(sd.episodes)
        .map((e) => Number(e))
        .sort((a, b) => a - b);
      const label = sk === 'unknown' ? '季未知' : 'S' + pad2(sk);
      if (eps.length) return label + ' ' + seasonRanges(eps).join('、');
      return label + ' +' + text(sd.count || 0) + '集';
    })
    .join(' · ');
}
function treeSources(...branches: Data[]): string[] {
  const s = new Set<string>(['本地影视库', '分享影视库']);
  for (const b of branches) Object.keys(b).forEach((k) => s.add(k));
  return [...s];
}
const ingestGroups = computed(() => {
  const tree = ingTree.value;
  const tv = object(tree.tv),
    mov = object(tree.mov);
  const tvD = object(tree.tv_detail),
    movD = object(tree.mov_detail);
  const tvGroups: IngGroup[] = [],
    movGroups: IngGroup[] = [];
  for (const src of treeSources(tv)) {
    for (const [cat, raw] of Object.entries(object(tv[src]))) {
      const detail = object(object(tvD[src])[cat]);
      const shows = Object.entries(object(raw))
        .map(([name, count]) => ({ name, count: Number(count) || 0, detail: object(detail[name]) }))
        .sort((a, b) => (Number(b.detail.last_ts) || 0) - (Number(a.detail.last_ts) || 0));
      tvGroups.push({
        source: src,
        category: cat,
        kind: 'tv',
        count: shows.length,
        shows: shows.map((s) => ({ name: s.name, count: s.count, seasons: showSeasons(s.detail) })),
        movies: [],
      });
    }
    for (const [cat, raw] of Object.entries(object(mov[src]))) {
      const dmap = object(object(movD[src])[cat]);
      const names = records(raw)
        .map((n) => String(n))
        .sort((a, b) => (Number(dmap[b]) || 0) - (Number(dmap[a]) || 0));
      movGroups.push({
        source: src,
        category: cat,
        kind: 'movie',
        count: names.length,
        shows: [],
        movies: names,
      });
    }
  }
  return { tv: tvGroups, mov: movGroups };
});
function shortSrc(src: string): string {
  return src.replace('影视库', '');
}

/* ── 执行记录（结构化卡片） ── */
interface ChipVM {
  k: string;
  v: string;
  on: boolean;
}
interface SumChip {
  b: string;
  label: string;
  warn?: boolean;
}
interface ItemVM {
  kind: string;
  title: string;
  season: string;
  reason: string;
  resultTone: string;
  resultText: string;
}
interface ItemGroupVM {
  src: string;
  cat: string;
  count: number;
  items: ItemVM[];
}
interface SubTag {
  tone: string;
  text: string;
}
interface SubVM {
  name: string;
  tags: SubTag[];
  info: string;
}
interface SubGroupVM {
  tag: string;
  label: string;
  count: number;
  shows: SubVM[];
}
interface DetVM {
  title: string;
  seasons: string;
  why: string;
  act: string;
}
interface RecCardVM {
  id: string;
  tone: string;
  category: string;
  time: string;
  rule: string;
  sumChips: SumChip[];
  groups: ItemGroupVM[];
  truncated: boolean;
  hints: string[];
  chips: ChipVM[];
  lead: string;
  emby: boolean | null;
  title: string;
  subs: SubGroupVM[];
  dets: DetVM[];
  detPlain: string[];
  detShort: string[];
  detLabel: string;
}
function recTone(cat: string): string {
  if (cat.includes('清理')) return 'clean';
  if (cat.includes('巡检') || cat.includes('扫描')) return 'scan';
  if (cat.includes('配置')) return 'config';
  if (cat.includes('搜片')) return 'search';
  return 'other';
}
function resBadge(res: Data): { tone: string; text: string } {
  const st = object(res).status;
  if (st === 'ok') return { tone: 'ok', text: '✓ 已删 ' + text(object(res).removed || 0) };
  if (st === 'partial') return { tone: 'warn', text: '⚠ 部分失败' };
  if (st === 'noop') return { tone: 'dim', text: '未产生变更' };
  return { tone: '', text: '' };
}
function seasonRange(list: string[]): string {
  const nums = list
    .map((s) => parseInt(s.slice(1), 10))
    .filter((n) => !isNaN(n))
    .sort((a, b) => a - b);
  if (!nums.length) return '';
  const out: string[] = [];
  let st = nums[0],
    pv = nums[0];
  const pad = (n: number) => 'S' + String(n).padStart(2, '0');
  for (let i = 1; i <= nums.length; i++) {
    if (nums[i] === pv + 1) {
      pv = nums[i];
      continue;
    }
    out.push(st === pv ? pad(st) : pad(st) + '–' + pad(pv));
    st = pv = nums[i];
  }
  return out.join('、');
}
const REC_DETAIL_RE =
  /^[\s├└│─┬┌]*(?:(\S+)\s+)?《(.+?)》\s*(S\d{1,3})?\s*[:：]\s*(.*?)\s*(?:→|➔|->)\s*(.+)$/;
function groupDetails(details: string[]): { dets: DetVM[]; plain: string[] } {
  const groups: DetVM[] = [];
  const plain: string[] = [];
  const index = new Map<string, DetVM>();
  for (const line of details) {
    const m = REC_DETAIL_RE.exec(line);
    if (!m) {
      if (line.trim()) plain.push(line.trim());
      continue;
    }
    const key = m[2] + '|' + m[4] + '|' + m[5];
    let g = index.get(key);
    if (!g) {
      g = { title: m[2], why: m[4], act: m[5], seasons: '' };
      index.set(key, g);
      groups.push(g);
    }
    if (m[3]) g.seasons = (g.seasons ? g.seasons + ',' : '') + m[3];
  }
  return { dets: groups.map((g) => ({ ...g, seasons: seasonRange(g.seasons.split(',').filter(Boolean)) })), plain };
}
function recSummary(
  title: string,
): { lead: string; chips: ChipVM[]; emby: boolean | null } | null {
  let rest = title,
    lead = '';
  const m = rest.match(/^(.{1,14}?)[：:]\s*(.+)$/);
  if (m) {
    lead = m[1];
    rest = m[2];
  }
  let emby: boolean | null = null;
  rest = rest.replace(
    /\s*[（(]\s*Emby\s*刷新\s*[:：]\s*(True|False)\s*[）)]\s*$/i,
    (_, v) => {
      emby = /true/i.test(v);
      return '';
    },
  );
  const chips: ChipVM[] = [];
  let ok = true;
  rest.split(/[,，]\s*/).forEach((p) => {
    const mm = p.trim().match(/^(.+?)\s*(\d+)\s*项$/);
    if (mm) chips.push({ k: mm[1], v: mm[2], on: mm[2] !== '0' });
    else ok = false;
  });
  if (!ok || !chips.length) return null;
  return { lead, chips, emby };
}
function subRows(ex: Data): SubGroupVM[] {
  const shows = records(ex.shows);
  if (!shows.length) return [];
  const bad = shows.filter((x) => x.status === 'error' || x.status === 'partial');
  const good = shows.filter((x) => x.status !== 'error' && x.status !== 'partial');
  const ch = good.filter((x) => x.changed);
  const keep = good.filter((x) => !x.changed);
  function toSub(x: Data): SubVM {
    const tags: SubTag[] = [];
    if (x.new) tags.push({ tone: 'ok', text: '🆕 新增 ' + text(x.new) });
    if (x.refilled) tags.push({ tone: 'ok', text: '✅ 补齐 ' + text(x.refilled) });
    if (x.newly_missing) tags.push({ tone: 'warn', text: '⚠ 缺集 ' + text(x.newly_missing) });
    const info: string[] = [];
    if (x.latest_ep) info.push('已通知至 ' + text(x.latest_ep));
    const tot = x.tmdb_declared || x.tmdb_total;
    if (x.have != null) info.push('本次读取 ' + text(x.have) + (tot != null ? ' / ' + text(tot) : '') + ' 集');
    if (x.error) info.push('失败/不完整：' + text(x.error));
    if (x.fallback_reason) info.push('Emby 失败，改用完整磁盘事实：' + text(x.fallback_reason));
    if (!x.changed && x.missing) info.push('仍缺 ' + text(x.missing) + ' 集' + (x.missing_eps ? '：' + text(x.missing_eps) : ''));
    return { name: text(x.name || ''), tags, info: info.join(' · ') };
  }
  function grp(tag: string, label: string, list: Data[]): SubGroupVM | null {
    if (!list.length) return null;
    return { tag, label, count: list.length, shows: list.map(toSub) };
  }
  return [grp('检查', '失败/不完整', bad), grp('更新', '有变化', ch), grp('订阅', '无变化', keep)].filter(
    (g): g is SubGroupVM => !!g,
  );
}
function buildCard(rec: Data): RecCardVM {
  let category = text(rec.category || '').trim();
  let title = text(rec.title || '').trim();
  ['库间查重巡检', '执行跨库清理', '模糊搜片'].forEach((p) => {
    if (!category && title.startsWith(p)) {
      category = p;
      title = title.slice(p.length);
    }
  });
  const tone = recTone(category);
  const ts = text(rec.ts || '');
  const time = ts.length >= 19 ? ts.slice(11, 19) : ts;
  const extra = object(rec.extra);
  const items = records(extra.items);
  const details = records(rec.details).map((d) => text(d));
  const card: RecCardVM = {
    id: text(rec.id || ''),
    tone,
    category: category || '记录',
    time,
    rule: text(rec.rule_sig || ''),
    sumChips: [],
    groups: [],
    truncated: !!extra.items_truncated,
    hints: [],
    chips: [],
    lead: '',
    emby: null,
    title,
    subs: subRows(extra),
    dets: [],
    detPlain: [],
    detShort: [],
    detLabel: '',
  };

  if (items.length) {
    const nl = extra.n_loc != null ? Number(extra.n_loc) : items.filter((x) => x.kind === 'loc').length;
    const ns = extra.n_shr != null ? Number(extra.n_shr) : items.filter((x) => x.kind === 'shr').length;
    card.sumChips = [
      { b: String(nl), label: '项释放本地' },
      { b: String(ns), label: '项淘汰分享' },
      { b: '', label: 'Emby 刷新 ' + (extra.refreshed ? '✅' : '❌') },
    ];
    if (extra.skipped) card.sumChips.push({ b: String(extra.skipped), label: '跳过' });
    if (extra.warnings) card.sumChips.push({ b: String(extra.warnings), label: '⚠ 警告', warn: true });
    const byKind: Record<string, { items: ItemVM[] }> = { loc: { items: [] }, shr: { items: [] } };
    items.forEach((it) => {
      const kind = it.kind === 'loc' ? 'loc' : 'shr';
      const rb = resBadge(object(it.result));
      byKind[kind].items.push({
        kind,
        title: text(it.title || it.text || ''),
        season: it.season != null ? 'S' + pad2(it.season) : '',
        reason: text(it.reason_label || it.detail || ''),
        resultTone: rb.tone,
        resultText: rb.text,
      });
    });
    (['loc', 'shr'] as const).forEach((k) => {
      if (byKind[k].items.length)
        card.groups.push({
          src: k === 'loc' ? '本地' : '分享',
          cat: k === 'loc' ? '释放本地 · 删除本地副本' : '淘汰分享 · 删除分享副本',
          count: byKind[k].items.length,
          items: byKind[k].items,
        });
    });
    card.hints = details.filter((l) => /^(├─|│)/.test(l) || /^⚠️/.test(l));
    return card;
  }

  const sum = recSummary(title);
  if (sum) {
    card.lead = sum.lead;
    card.chips = sum.chips;
    card.emby = sum.emby;
  } else {
    card.title = title;
  }

  if (details.length) {
    if (details.length <= 3 && details.every((l) => l.length <= 40 && !/《/.test(l))) {
      card.detShort = details;
    } else {
      const g = groupDetails(details);
      card.dets = g.dets;
      card.detPlain = g.plain;
      card.detLabel = g.dets.length
        ? '明细 · ' + g.dets.length + ' 部 / ' + details.length + ' 条'
        : '明细 · ' + details.length + ' 条';
    }
  }
  return card;
}
const recordCards = computed(() => records(recordsRequest.data.value?.records).map(buildCard));

/* ── 晨报 ── */
const tz = computed(() => object(morning.data.value?.tz));
const tzHint = computed(() => {
  const t = tz.value;
  if (!Object.keys(t).length) return '24 小时制';
  const now = text(t.now || '');
  return (
    '24 小时制 · 服务器时区 ' +
    text(t.offset) +
    '（' +
    text(t.name) +
    '），现在 ' +
    (now.length >= 16 ? now.slice(11, 16) : now)
  );
});
const MORNING_ITEMS: { key: string; label: string }[] = [
  { key: 'stats', label: '📊 近 24h 入库统计' },
  { key: 'subscriptions', label: '🔔 订阅更新（新集 / 缺集）' },
  { key: 'emby_gap', label: '📺 Emby 缺集速览' },
];

/* ── 记录详情 ── */
const detailRecord = computed(() => object(detail.data.value?.record));
const detailItems = computed(() => records(object(detailRecord.value.extra).items));
function detailBadge(res: Data): { tone: string; text: string } {
  const st = object(res).status;
  if (st === 'ok') return { tone: 'ok', text: '✓ 已删 ' + text(object(res).removed || 0) };
  if (st === 'partial') return { tone: 'warn', text: '⚠ 部分失败' };
  if (st === 'noop') return { tone: 'dim', text: '未产生变更' };
  return { tone: '', text: '' };
}
</script>

<template>
  <PageHeading title="入库汇报与晨报" subtitle="查看近 24 小时实际入库、治理执行记录，以及可预览的每日晨报。">
    <button class="btn" @click="load(); op.reconcile()">刷新汇报</button>
  </PageHeading>
  <nav class="browse-tabs">
    <button :class="{ active: tab === 'ingest' }" @click="tab = 'ingest'">近期入库</button
    ><button :class="{ active: tab === 'records' }" @click="tab = 'records'">执行记录</button
    ><button :class="{ active: tab === 'morning' }" @click="tab = 'morning'">每日晨报</button>
  </nav>
  <OperationState
    :busy="op.busy.value"
    :error="op.error.value || ingest.error.value || recordsRequest.error.value || morning.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />

  <section v-if="tab === 'ingest'" class="panel">
    <div class="row-between">
      <h3>近 24 小时入库</h3>
      <div class="btn-row">
        <button class="btn gray" @click="loadIngest(false, false)">读缓存</button
        ><button class="btn" @click="loadIngest(true, true)">立即扫描</button
        ><button class="btn gray" @click="loadIngest(true, false)">完整</button>
      </div>
    </div>
    <div class="ing-cache-hint">{{ ingestHint }}</div>
    <p v-if="ingest.loading.value" class="muted-text">读取入库缓存中…</p>
    <p v-if="ingest.error.value" class="inline-error">{{ ingest.error.value }}</p>
    <div v-else-if="ingestText" class="ing-plain">{{ ingestText }}</div>
    <template v-else-if="ingest.data.value">
      <div class="ing-sum">
        <span class="ing-chip"><b>+{{ text(ingStats.movies || 0) }}</b> 单片</span
        ><span class="ing-chip"><b>+{{ text(ingStats.series || 0) }}</b> 部连载</span
        ><span class="ing-chip"><b>+{{ text(ingStats.episodes || 0) }}</b> 集</span>
      </div>
      <div class="ing-h">连载剧集 · {{ text(ingStats.series || 0) }} 部</div>
      <details
        v-for="(g, i) in ingestGroups.tv"
        :key="'tv' + i"
        class="ing-grp"
        :open="g.count <= 8"
      >
        <summary
          ><span class="ing-src">{{ shortSrc(g.source) }}</span
          ><span class="ing-cat">{{ g.category }}</span
          ><span class="ing-cnt">{{ g.count }}部</span></summary
        >
        <ul class="ing-list">
          <li v-for="s in g.shows" :key="s.name"
            ><span class="n">《{{ s.name }}》</span><span class="b">+{{ s.count }}集</span
            ><span class="ing-season">{{ s.seasons }}</span></li
          >
        </ul>
      </details>
      <div v-if="!ingestGroups.tv.length" class="ing-empty">暂无新增剧集</div>
      <div class="ing-h">单片电影 · {{ text(ingStats.movies || 0) }} 部</div>
      <details
        v-for="(g, i) in ingestGroups.mov"
        :key="'mov' + i"
        class="ing-grp"
        :open="g.count <= 8"
      >
        <summary
          ><span class="ing-src">{{ shortSrc(g.source) }}</span
          ><span class="ing-cat">{{ g.category }}</span
          ><span class="ing-cnt">{{ g.count }}部</span></summary
        >
        <div class="ing-tags"><span v-for="n in g.movies" :key="n">《{{ n }}》</span></div>
      </details>
      <div v-if="!ingestGroups.mov.length" class="ing-empty">暂无新增电影</div>
    </template>
  </section>

  <section v-if="tab === 'records'" class="panel">
    <div class="row-between">
      <h3>执行历史</h3>
      <label class="limit-row"
        >读取条数<select
          v-model.number="limit"
          @change="recordsRequest.run((signal) => readData(`/api/records?n=${limit}`, signal))"
        >
          <option :value="50">50</option>
          <option :value="100">100</option>
          <option :value="200">200</option>
        </select></label
      >
    </div>
    <p v-if="recordsRequest.loading.value" class="muted-text">读取执行记录中…</p>
    <p v-if="recordsRequest.error.value" class="inline-error">{{ recordsRequest.error.value }}</p>
    <div v-for="card in recordCards" :key="card.id || card.time" class="rc" :class="'rc-' + card.tone">
      <div class="rc-top"
        ><span class="rc-tag">{{ card.category }}</span
        ><span class="rc-time">{{ card.time }}{{ card.rule ? ' · 规则 ' + card.rule : '' }}</span></div
      >
      <div v-if="card.sumChips.length" class="ing-sum rc-ing-sum">
        <span v-for="(c, i) in card.sumChips" :key="i" class="ing-chip" :class="{ warn: c.warn }"
          ><b v-if="c.b">{{ c.b }}</b> {{ c.label }}</span
        >
      </div>
      <details
        v-for="(g, i) in card.groups"
        :key="i"
        class="ing-grp"
        :open="g.count <= 5"
      >
        <summary
          ><span class="ing-src">{{ g.src }}</span
          ><span class="ing-cat">{{ g.cat }}</span
          ><span class="ing-cnt">{{ g.count }} 项</span></summary
        >
        <ul class="ing-list">
          <li v-for="(it, j) in g.items" :key="j" class="stk"
            ><span class="n"
              >《{{ it.title }}》<span v-if="it.season" class="rd-s">{{ it.season }}</span
              ><span v-if="it.resultText" class="rs" :class="it.resultTone">{{
                it.resultText
              }}</span></span
            ><span class="ing-season">{{ it.reason }}</span></li
          >
        </ul>
      </details>
      <div v-if="card.truncated" class="rd-plain">清单较长，仅保留前部分明细。</div>
      <div v-if="card.hints.length" class="rc-det">
        <details><summary>执行提示 · {{ card.hints.length }} 条</summary
          ><div class="rd-plain" v-for="(h, i) in card.hints" :key="i">{{ h }}</div></details
        >
      </div>
      <div v-if="card.chips.length || card.emby !== null" class="rc-sum"
        ><span v-if="card.lead" class="rc-lead">{{ card.lead }}</span
        ><span v-for="(c, i) in card.chips" :key="i" class="rc-chip" :class="{ on: c.on }"
          >{{ c.k }} <b>{{ c.v }}</b></span
        ><span v-if="card.emby !== null" class="rc-chip">Emby 刷新 {{ card.emby ? '✅' : '❌' }}</span></div
      >
      <div v-else-if="card.title" class="rc-title">{{ card.title }}</div>
      <div v-if="card.subs.length" class="rc-subs">
        <details v-for="(g, i) in card.subs" :key="i" class="ing-grp" :open="g.tag !== '订阅'">
          <summary
            ><span class="ing-src">{{ g.tag }}</span
            ><span class="ing-cat">{{ g.label }}</span
            ><span class="ing-cnt">{{ g.count }} 部</span></summary
          >
          <ul class="ing-list">
            <li v-for="s in g.shows" :key="s.name" class="stk"
              ><span class="n"
                >《{{ s.name }}》<span class="rc-tags"
                  ><span v-for="(t, j) in s.tags" :key="j" class="rs" :class="t.tone">{{
                    t.text
                  }}</span></span
                ></span
              ><span class="ing-season">{{ s.info }}</span></li
            >
          </ul>
        </details>
      </div>
      <div v-if="card.detShort.length" class="rc-sum">
        <span
          v-for="(l, i) in card.detShort"
          :key="i"
          class="rc-chip"
          :class="{ on: /(变化|失败|警告|错误)/.test(l) && /[1-9]/.test(l) }"
          >{{ l }}</span
        >
      </div>
      <div v-if="card.dets.length || card.detPlain.length" class="rc-det">
        <details>
          <summary>{{ card.detLabel }}</summary>
          <div v-for="(d, i) in card.dets" :key="i" class="rd-row"
            ><div class="rd-main"
              ><span class="rd-t">《{{ d.title }}》</span
              ><span v-if="d.seasons" class="rd-s">{{ d.seasons }}</span></div
            ><div class="rd-why">{{ d.why }} → {{ d.act }}</div></div
          >
          <div v-for="(p, i) in card.detPlain" :key="'p' + i" class="rd-plain">{{ p }}</div>
        </details>
      </div>
      <div class="rc-act"
        ><button
          v-if="card.id"
          class="btn sm gray"
          @click="showRecord(card.id)"
        >
          查看完整依据
        </button></div
      >
    </div>
    <p v-if="!recordsRequest.loading.value && !recordCards.length" class="list-empty"
      >暂无执行记录</p
    >
  </section>

  <section v-if="tab === 'morning'" class="panel">
    <div class="row-between">
      <h3>每日晨报</h3>
      <div class="btn-row">
        <button class="btn gray" :disabled="op.busy.value || op.uncertain.value" @click="preview()"
          >预览(缓存)</button
        ><button class="btn gray" :disabled="op.busy.value || op.uncertain.value" @click="preview(true)"
          >预览(现场扫)</button
        ><button class="btn" :disabled="op.busy.value || op.uncertain.value" @click="confirmation = 'send'"
          >立即发送</button
        >
      </div>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">启用每日晨报</div>
        <div class="desc">每天定时推送统计到 Telegram</div>
      </div>
      <label class="switch"
        ><input v-model="form.enabled" type="checkbox" @change="saveMorning" /><span
          class="slider"
        ></span
      ></label>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">推送时间</div>
        <div class="desc">{{ tzHint }}</div>
      </div>
      <div class="time-inputs">
        <input v-model.number="form.hour" type="number" min="0" max="23" @change="saveMorning" /><span
          >:</span
        ><input v-model.number="form.minute" type="number" min="0" max="59" @change="saveMorning" />
      </div>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">提前预扫（分钟）</div>
        <div class="desc">晨报时间前 N 分钟静默刷新入库缓存，发送时秒出</div>
      </div>
      <input
        v-model.number="form.prescan_min"
        class="num-input"
        type="number"
        min="0"
        max="60"
        @change="saveMorning"
      />
    </div>
  </section>

  <section v-if="tab === 'morning'" class="panel">
    <h3>晨报内容</h3>
    <label v-for="m in MORNING_ITEMS" :key="m.key" class="check-item"
      ><input v-model="items" type="checkbox" :value="m.key" @change="saveMorning" /> {{ m.label }}</label
    >
    <div v-if="morning.loading.value" class="muted-text">读取晨报设置中…</div>
    <p v-if="morning.error.value" class="inline-error">{{ morning.error.value }}</p>
  </section>

  <section v-if="tab === 'morning' && previewText" class="panel">
    <h3>预览</h3>
    <pre class="preview-box">{{ previewText }}</pre>
  </section>

  <ModalDialog
    v-if="detailOpen"
    title="执行记录详情"
    @close="
      detailOpen = false;
      detail.cancel();
    "
  >
    <p v-if="detail.error.value" class="inline-error">{{ detail.error.value }}</p>
    <template v-else-if="detailRecord">
      <div class="rc"
        ><div class="rc-top"
          ><span class="rc-tag">{{ text(detailRecord.category || detailRecord.title) }}</span
          ><span class="rc-time">{{ text(detailRecord.ts) }}</span></div
        ><div v-if="detailRecord.title" class="rc-title">{{ text(detailRecord.title) }}</div></div
      >
      <div v-if="!detailItems.length" class="muted-text">该记录没有逐项明细。</div>
      <div v-for="(it, i) in detailItems" :key="i" class="list-item keep">
        <div class="t">
          《{{ text(it.title || it.text || '') }}》<span v-if="it.season != null" class="rd-s"
            >S{{ pad2(it.season) }}</span
          ><span v-if="detailBadge(object(it.result)).text" class="rs" :class="detailBadge(object(it.result)).tone"
            >{{ detailBadge(object(it.result)).text }}</span
          >
        </div>
        <div class="d">{{ text(it.reason_label || it.detail || '') }}</div>
        <details v-if="it.text || it.detail" class="rc-det">
          <summary>完整明细</summary>
          <pre class="preview-box">{{ text(it.text || '') }}{{ it.detail ? '\n' + text(it.detail) : '' }}</pre>
        </details>
      </div>
    </template>
  </ModalDialog>

  <ConfirmAction
    v-if="confirmation === 'send'"
    title="发送晨报"
    description="立即将当前选择的晨报缓存发送到已配置的 Telegram 接收位置（不现场扫描）。"
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="confirmation = ''"
    @confirm="execute"
  />
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
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
}
.btn-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.num-input {
  width: 100px;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid var(--border-input);
  background: var(--surface-solid);
  color: var(--text);
  font-size: 13px;
  text-align: center;
}
.time-inputs {
  display: flex;
  gap: 6px;
  align-items: center;
  width: 140px;
}
.time-inputs input {
  width: 50px;
  text-align: center;
  padding: 6px 4px;
  border-radius: 8px;
  border: 1px solid var(--border-input);
  background: var(--surface-solid);
  color: var(--text);
  font-size: 13px;
}
.time-inputs span {
  color: var(--text-dim);
}
.limit-row {
  font-size: 12px;
  color: var(--text-mid);
  display: flex;
  gap: 8px;
  align-items: center;
}
.limit-row select {
  padding: 6px 8px;
  border-radius: 8px;
  border: 1px solid var(--border-input);
  background: var(--surface-solid);
  color: var(--text);
}

/* 近 24 小时入库 */
.ing-cache-hint {
  font-size: 11px;
  color: var(--text-dim);
  margin-bottom: 8px;
}
.ing-plain {
  white-space: normal;
  word-break: break-word;
  color: var(--text-mid);
  font-size: 13px;
  line-height: 1.7;
}
.ing-sum {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 6px;
}
.ing-chip {
  background: var(--surface-solid);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 12px;
  color: var(--text-mid);
}
.ing-chip b {
  color: var(--brand);
  font-size: 14px;
  margin-right: 2px;
}
.ing-chip.warn {
  border-color: var(--warn);
  color: var(--warn);
}
.ing-chip.warn b {
  color: var(--warn);
}
.ing-h {
  font-weight: 600;
  color: var(--text);
  margin: 16px 0 8px;
  font-size: 14px;
}
.ing-empty {
  color: var(--text-dim);
  font-size: 12px;
  padding: 6px 2px;
}
.ing-grp {
  background: var(--surface-solid);
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  margin-bottom: 8px;
  overflow: hidden;
}
.ing-grp > summary {
  list-style: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
}
.ing-grp > summary::-webkit-details-marker {
  display: none;
}
.ing-grp > summary::before {
  content: '▸';
  color: var(--text-dim);
  font-size: 11px;
  transition: transform 0.15s;
}
.ing-grp[open] > summary::before {
  transform: rotate(90deg);
}
.ing-src {
  font-size: 11px;
  color: var(--brand);
  background: var(--info-soft);
  border-radius: 6px;
  padding: 1px 8px;
  white-space: nowrap;
}
.ing-cat {
  flex: 1;
  font-weight: 600;
  color: var(--text);
  min-width: 0;
}
.ing-cnt {
  font-size: 12px;
  color: var(--text-dim);
  white-space: nowrap;
}
.ing-list {
  list-style: none;
  margin: 0;
  padding: 0 12px 8px;
}
.ing-list li {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 6px 0;
  border-top: 1px solid var(--border);
}
.ing-list .n {
  min-width: 0;
  word-break: break-word;
  color: var(--text);
}
.ing-list .b {
  white-space: nowrap;
  color: var(--ok);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.ing-season {
  display: block;
  flex: 1;
  min-width: 0;
  color: var(--text-dim);
  font-size: 11px;
  line-height: 1.5;
  text-align: right;
  word-break: break-word;
}
.ing-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 0 12px 10px;
}
.ing-tags span {
  background: var(--bg-dim);
  border-radius: 6px;
  padding: 3px 9px;
  font-size: 12px;
  color: var(--text);
  word-break: break-word;
}

/* 执行记录卡片 */
.rc {
  background: var(--surface-solid);
  border: 1px solid var(--border-strong);
  border-left: 4px solid var(--brand);
  border-radius: var(--r-md);
  padding: 12px 14px;
  margin-bottom: 8px;
}
.rc-clean {
  border-left-color: var(--err);
}
.rc-scan {
  border-left-color: var(--info);
}
.rc-config {
  border-left-color: var(--text-dim);
}
.rc-search {
  border-left-color: var(--ok);
}
.rc-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
}
.rc-tag {
  font-size: 12px;
  font-weight: 600;
  color: var(--text);
}
.rc-time {
  font-size: 11px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.rc-title {
  font-size: 13px;
  color: var(--text-mid);
  margin-top: 6px;
  line-height: 1.6;
  word-break: break-word;
}
.rc-sum {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
  align-items: center;
}
.rc-lead {
  font-size: 12px;
  color: var(--text-mid);
  margin-right: 2px;
}
.rc-chip {
  font-size: 12px;
  color: var(--text-dim);
  background: var(--bg-dim);
  border-radius: 999px;
  padding: 2px 10px;
}
.rc-chip b {
  font-variant-numeric: tabular-nums;
  color: var(--text-mid);
}
.rc-chip.on {
  background: var(--info-soft);
  color: var(--info);
}
.rc-chip.on b {
  color: var(--info);
}
.rc-ing-sum {
  margin: 10px 0 8px;
}
.rc .ing-grp {
  margin-top: 8px;
}
.rc .ing-grp .ing-cat {
  font-size: 13px;
}
.rc-subs {
  margin-top: 8px;
}
.rc-act {
  margin-top: 10px;
}
.rc-tags {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-left: 6px;
  vertical-align: 1px;
}
.rc-tags .rs {
  margin-left: 0;
}
.rc-det {
  margin-top: 10px;
}
.rc-det > details > summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--brand);
  list-style: none;
}
.rc-det > details > summary::-webkit-details-marker {
  display: none;
}
.rc-det > details > summary::before {
  content: '▸ ';
  font-size: 10px;
}
.rc-det > details[open] > summary::before {
  content: '▾ ';
}
.rd-row {
  padding: 8px 0;
  border-top: 1px solid var(--border);
}
.rd-main {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: baseline;
}
.rd-t {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  word-break: break-word;
}
.rd-s {
  font-size: 11px;
  color: var(--brand);
  background: var(--info-soft);
  border-radius: 6px;
  padding: 1px 7px;
  margin-left: 6px;
}
.rd-why {
  font-size: 12px;
  color: var(--text-dim);
  margin-top: 2px;
  word-break: break-word;
}
.rd-plain {
  font-size: 12px;
  color: var(--text-mid);
  padding: 4px 0;
  word-break: break-word;
}
.ing-list li.stk {
  flex-direction: column;
  align-items: stretch;
  gap: 2px;
}
.ing-list li.stk .ing-season {
  flex: none;
  text-align: left;
}
.rs {
  display: inline-block;
  margin-left: 6px;
  font-size: 11px;
  border-radius: 6px;
  padding: 0 6px;
  vertical-align: 1px;
}
.rs.ok {
  color: var(--ok);
  background: var(--bg-dim);
}
.rs.warn {
  color: var(--warn);
  background: var(--bg-dim);
}
.rs.dim {
  color: var(--text-dim);
  background: var(--bg-dim);
}

/* 晨报 */
.preview-box {
  margin-top: 10px;
  padding: 12px;
  border-radius: 10px;
  background: var(--bg-dim);
  color: var(--text);
  font-size: 12.5px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 50vh;
  overflow: auto;
}
</style>
