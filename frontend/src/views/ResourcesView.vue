<script setup lang="ts">
import { ref, computed } from 'vue';
import { allNavigation } from '../config/navigation';
import PageHeading from '../components/PageHeading.vue';
const search = ref(''),
  group = ref('全部');
const descriptions: Record<string, string> = {
  '/resources/receive': '接收 115 分享、磁力、ed2k 和秒传，预览目标后提交。',
  '/resources/share': '读取分享内容，生成独立分享库 STRM，保留源分享。',
  '/resources/files': '浏览账号目录与真实文件，查看大小和路径。',
  '/settings/accounts': '扫码授权、Cookie、目录映射、签到与回收站维护。',
  '/organize/workspace': '识别影视、纠正 TMDB、命名归档、生成 STRM。',
  '/organize/automation': '把接收目录变成有明确时间和来源的自动化规则。',
  '/resources/channels': '配置 Telegram 频道过滤、历史读取与用户会话。',
  '/media/strm': '核实播放引用、源关系与 NAS 输出，手动同步。',
  '/tasks/ingestion': '逐作品核实 Emby 入库，独立管理入库连接。',
  '/governance': '对照双库，生成七维治理计划并核对执行。',
  '/governance/cleanup': '预览与清理空目录、附属文件和已确认残留。',
  '/settings/external': '使用现有外部服务搜索、转存与资源获取订阅。',
  '/media/explore': '搜索影视作品、查看入库状态和资源候选。',
  '/media/subscriptions': '管理追更提醒与低频校准。',
  '/tasks/reports': '查看入库、执行归档，预览并设置晨报。',
};
const tools = computed(() =>
  allNavigation.filter(
    (n) =>
      descriptions[n.to] &&
      (group.value === '全部' || n.group === group.value) &&
      (n.name + descriptions[n.to]).includes(search.value.trim()),
  ),
);
const groups = [
  '全部',
  ...new Set(allNavigation.filter((n) => descriptions[n.to]).map((n) => n.group)),
];
</script>
<template>
  <PageHeading
    title="资源与工具"
    subtitle="按工作流组织已接通的功能，从资源接入到媒体治理。"
    eyebrow="RESOURCE WORKSPACE"
  />
  <div class="search-toolbar">
    <input v-model="search" aria-label="筛选功能卡片" placeholder="搜索功能名称或用途" /><span
      class="muted-text"
      >{{ tools.length }} 个功能入口</span
    >
  </div>
  <nav class="browse-tabs">
    <button v-for="g in groups" :key="g" :class="{ active: group === g }" @click="group = g">
      {{ g }}
    </button>
  </nav>
  <div class="module-grid">
    <article v-for="tool in tools" :key="tool.to" class="panel module-card">
      <header>
        <span class="module-icon"><component :is="tool.icon" :size="24" /></span>
        <div>
          <h2>{{ tool.name }}</h2>
          <span class="module-tag">{{ tool.group }}</span>
        </div>
      </header>
      <p>{{ descriptions[tool.to] }}</p>
      <RouterLink :to="tool.to" class="button primary">进入工作区 →</RouterLink>
    </article>
  </div>
</template>
