<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { object, text, type Data } from '../api/domain';
import { cloudSections } from '../config/cloudFields';
import { stateLabel } from '../composables/format';
const props = withDefaults(defineProps<{ value: unknown; depth?: number }>(), { depth: 0 });
const visible = ref(100),
  expanded = ref(false);
watch(
  () => props.value,
  () => {
    visible.value = 100;
    expanded.value = false;
  },
);
const labels: Record<string, string> = {
  name: '名称',
  id: '编号',
  state: '状态',
  status: '状态',
  message: '消息',
  reason: '原因',
  suggestion: '处理建议',
  source: '来源',
  target: '目标',
  path: '路径',
  local_path: 'STRM 路径',
  file_id: '文件编号',
  size: '大小',
  count: '数量',
  total: '总数',
  processed: '已处理',
  errors: '异常',
  skipped: '跳过',
  filtered: '过滤',
  items: '处理项目',
  media: '媒体',
  title: '作品',
  year: '年份',
  tmdb_id: 'TMDB',
  kind: '类型',
  revision: '配置版本',
  created: '创建时间',
  updated: '更新时间',
  success: '成功',
  failed: '失败',
  strm_removed: '已删除 STRM',
  cloud_removed: '已删除源文件',
  retained: '保留',
  files: '文件',
  notice: '说明',
  effect: '影响范围',
  blocked: '已阻止',
  enabled: '启用',
  source_kind: '来源类型',
  steps: '执行步骤',
  rule: '规则',
  stage: '阶段',
  local_eps: '本地集数',
  share_eps: '分享集数',
  output_root: '输出目录',
  source_cid: '来源目录',
  target_cid: '目标目录',
  action: '操作',
  preview_only: '仅预览',
  warnings: '提示',
  expires_in: '有效秒数',
  target_path: '归档路径',
  filename: '文件名',
  file_name: '文件名',
  source_fingerprints: '文件校验记录（防止误处理）',
  unknown_items: '未识别',
  quality_held: '版本保留',
  quality8: '八维比较',
  cleanup_candidates: '目录清理候选',
  series: '剧集',
  movies: '电影',
  seasons: '季',
  strm_count: 'STRM 数',
  batch_count: '批次数',
  summary: '摘要',
  result: '执行结果',
  plan_id: '计划编号',
  task_id: '任务编号',
  job_id: '任务编号',
  error: '错误',
  warning: '提示',
  phase: '阶段',
  current: '当前项目',
  submitted: '已提交',
  confirmed: '已核实',
  dry_run: '试运行',
  async_delete: '后台删除',
  source_preview_id: '预览编号',
  in_emby: 'Emby 已入库',
  source_name: '来源名称',
  step: '执行步骤',
  progress: '处理进度',
  activity: '当前活动',
  upgrades: '待替换的旧版本',
  replaced: '已换版回执',
  replacement_count: '待换版数量',
  comparisons: '分享库质量比较',
  reconciliation: '失效 NAS 引用清理结果',
  cleanup_enabled: '启用失效引用清理',
  removed: '已清理',
  deferred: '暂缓清理',
  generated: '补生成或更新',
  rebound: '同路径来源已切换',
  successor: '新播放引用',
  heartbeat_age: '心跳间隔',
  label: '说明',
  last_error: '最近错误',
  last_run: '上次运行',
  next_run: '下次运行',
  source_revision: '来源配置版本',
  sha1: '文件校验值',
  preid: '文件分段校验值',
  fingerprint: '文件校验记录',
  fingerprints: '文件校验记录',
  classification: '归档分类',
  quality8_rules: '八维质量规则',
  region_groups: '地区归组',
  personal_root: 'Emby 个人库目录',
  share_root: 'Emby 分享库目录',
  deleted: '已删除',
  moved: '已移动',
  renamed: '已重命名',
  notify: '允许通知',
  scan_state: '扫描状态',
  decision: '版本处理结果',
  strict_better: '新版本是否更优',
  evidence: '判断依据',
  comparison: '版本比较',
  directory: '目录',
  season: '季',
  episode: '集',
  ts: '记录时间',
  ...Object.fromEntries(
    cloudSections.flatMap((section) => section.fields.map((field) => [field.key, field.label])),
  ),
};
const entries = computed(() =>
  Object.entries(object(props.value)).filter(
    ([k]) => !/(cookie|token|api_key|password|sealed|sig$)/i.test(k),
  ),
);
const technicalKey = (key: string) =>
  /(^id$|^(?:job|plan|task|account|request|file)_id$|fingerprint|sha1|preid|hash|revision|snapshot|^source_sha$|^commit$|^branch$|^build_date$|^source_ref$)/i.test(
    key,
  );
const visibleEntries = computed(() => entries.value.filter(([key]) => !technicalKey(key)));
const technicalEntries = computed(() => entries.value.filter(([key]) => technicalKey(key)));
const list = computed(() => (Array.isArray(props.value) ? props.value : undefined));
const simple = (v: unknown) => v === null || typeof v !== 'object';
function label(key: string) {
  return labels[key] || key.replaceAll('_', ' ');
}
function shown(v: unknown) {
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (typeof v === 'string')
    return (
      (
        {
          preview: '仅预览',
          upgrade: '执行洗版',
          movie: '电影',
          tv: '剧集',
          share: '115 分享',
          magnet: '磁力',
          ed2k: 'ed2k',
          instant: '秒传',
          transfer: '转存',
          organize: '整理归档',
          sync: 'STRM 同步',
          intake: '资源接收',
          share_strm: '分享 STRM',
          channel: '频道接收',
          maintenance: '账号维护',
          workflow: '整理流程',
          publish: 'STRM 发布',
          categories: '分类目录',
        } as Record<string, string>
      )[v] || stateLabel(v)
    );
  return text(v);
}
const asData = (value: unknown): Data => object(value);
</script>
<template>
  <span v-if="simple(value)" class="evidence-value">{{ shown(value) }}</span>
  <div v-else-if="list" class="evidence-list">
    <div v-for="(entry, index) in list.slice(0, visible)" :key="index" class="evidence-item">
      <EvidenceDetails v-if="depth < 5 || expanded" :value="entry" :depth="depth + 1" /><span
        v-else
        >{{ shown(asData(entry).name || asData(entry).reason || '嵌套详情') }}</span
      >
    </div>
    <p v-if="list.length > 100" class="muted-text">
      已显示 {{ Math.min(visible, list.length) }} / {{ list.length }} 项。
    </p>
    <button v-if="list.length > visible" type="button" class="button small" @click="visible += 100">
      继续显示详情项目
    </button>
  </div>
  <dl v-else class="evidence-grid">
    <template v-for="[key, entry] in visibleEntries" :key="key"
      ><dt>{{ label(key) }}</dt>
      <dd v-if="simple(entry)">{{ shown(entry) }}</dd>
      <dd v-else>
        <details>
          <summary>查看{{ label(key) }}</summary>
          <EvidenceDetails v-if="depth < 5 || expanded" :value="entry" :depth="depth + 1" />
        </details></dd
    ></template>
  </dl>
  <details v-if="technicalEntries.length" class="technical-details">
    <summary>技术详情（排障时展开）</summary>
    <p class="data-caption">编号和文件校验用于核对原任务、防止误操作，无需手动填写。</p>
    <dl class="evidence-grid">
      <template v-for="[key, entry] in technicalEntries" :key="key">
        <dt>{{ label(key) }}</dt>
        <dd v-if="simple(entry)">{{ shown(entry) }}</dd>
        <dd v-else>
          <EvidenceDetails v-if="depth < 5 || expanded" :value="entry" :depth="depth + 1" /><span
            v-else
            >请展开深层详情</span
          >
        </dd>
      </template>
    </dl>
  </details>
  <button
    v-if="!simple(value) && depth >= 5 && !expanded"
    type="button"
    class="button small"
    @click="expanded = true"
  >
    展开深层详情
  </button>
</template>
