<script setup lang="ts">
import { computed } from 'vue';
import { object, text, type Data } from '../api/domain';
const props = withDefaults(defineProps<{ value: unknown; depth?: number }>(), { depth: 0 });
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
  source_fingerprints: '源文件快照',
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
  in_emby: 'Emby 已入库',
  source_name: '来源名称',
};
const entries = computed(() =>
  Object.entries(object(props.value)).filter(
    ([k]) => !/(cookie|token|api_key|password|sealed|sig$)/i.test(k),
  ),
);
const list = computed(() => (Array.isArray(props.value) ? props.value : undefined));
const simple = (v: unknown) => v === null || typeof v !== 'object';
function label(key: string) {
  return labels[key] || key.replaceAll('_', ' ');
}
function shown(v: unknown) {
  return typeof v === 'boolean' ? (v ? '是' : '否') : text(v);
}
const asData = (value: unknown): Data => object(value);
</script>
<template>
  <span v-if="simple(value)" class="evidence-value">{{ shown(value) }}</span>
  <div v-else-if="list" class="evidence-list">
    <div v-for="(entry, index) in list.slice(0, 100)" :key="index" class="evidence-item">
      <EvidenceDetails v-if="depth < 5" :value="entry" :depth="depth + 1" /><span v-else>{{
        shown(asData(entry).name || asData(entry).reason || '嵌套详情')
      }}</span>
    </div>
    <p v-if="list.length > 100" class="muted-text">
      本视图展示前 100 项，共 {{ list.length }} 项。
    </p>
  </div>
  <dl v-else class="evidence-grid">
    <template v-for="[key, entry] in entries" :key="key"
      ><dt>{{ label(key) }}</dt>
      <dd v-if="simple(entry)">{{ shown(entry) }}</dd>
      <dd v-else>
        <details :open="depth < 1">
          <summary>查看{{ label(key) }}</summary>
          <EvidenceDetails v-if="depth < 5" :value="entry" :depth="depth + 1" />
        </details></dd
    ></template>
  </dl>
</template>
