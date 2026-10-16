<script setup lang="ts">
import { CloudDownload, ArrowDownUp, FileVideo, Tv, ShieldCheck, ArrowRight } from '@lucide/vue';
import PageHeading from '../components/PageHeading.vue';
const stages = [
  {
    name: '资源接收',
    to: '/resources/receive',
    icon: CloudDownload,
    description: '从分享、磁力或秒传接收资源，核实转存回执。',
    links: [
      ['/resources/channels', '频道监控'],
      ['/resources/share', '分享免转存'],
    ],
  },
  {
    name: '识别与整理',
    to: '/organize/workspace',
    icon: ArrowDownUp,
    description: '识别作品，预览命名与分类计划，再确认归档。',
    links: [
      ['/organize/naming', '自定义命名模板'],
      ['/organize/quality', '整理八维与洗版'],
    ],
  },
  {
    name: 'STRM 发布',
    to: '/media/strm',
    icon: FileVideo,
    description: '生成播放引用，核实来源与 NAS 输出。',
    links: [['/settings/cloud?section=output', '输出与播放设置']],
  },
  {
    name: 'Emby 入库',
    to: '/tasks/ingestion',
    icon: Tv,
    description: '检查 Emby 实际收到的作品、路径与入库回执。',
    links: [['/tasks/reports', '入库汇报']],
  },
  {
    name: '片库治理',
    to: '/governance',
    icon: ShieldCheck,
    description: '先核对完整度，再按七维质量规则预演和执行计划。',
    links: [
      ['/governance/rules', '七维治理规则'],
      ['/governance/cleanup', '目录与残留清理'],
    ],
  },
];
</script>
<template>
  <PageHeading
    title="业务流程图"
    subtitle="从资源接收到片库治理，按处理阶段找到需要的工作区。"
    eyebrow="工作台"
  />
  <div class="workflow-map">
    <section v-for="(stage, i) in stages" :key="stage.to" class="panel workflow-stage">
      <header>
        <span class="workflow-stage-number">{{ String(i + 1).padStart(2, '0') }}</span
        ><component :is="stage.icon" :size="23" />
      </header>
      <h2>{{ stage.name }}</h2>
      <p>{{ stage.description }}</p>
      <RouterLink :to="stage.to" class="button primary"
        >进入工作区<ArrowRight :size="15"
      /></RouterLink>
      <div class="workflow-stage-links">
        <RouterLink v-for="[to, name] in stage.links" :key="to" :to="to!">{{ name }} →</RouterLink>
      </div>
    </section>
  </div>
  <section class="panel">
    <h2>处理记录与运行状态</h2>
    <p class="muted-text">任务中心查看执行进度和结果，日志中心追踪逐步原因。</p>
    <div class="action-row">
      <RouterLink to="/tasks" class="button">查看任务中心</RouterLink
      ><RouterLink to="/logs" class="button">查看实时日志</RouterLink>
    </div>
  </section>
</template>
