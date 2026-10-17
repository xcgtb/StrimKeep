<script setup lang="ts">
import { ref, computed } from 'vue';
import { ArrowUpRight, ArrowRight, Search } from '@lucide/vue';
import { allNavigation, searchFunctions } from '../config/navigation';
import PageHeading from '../components/PageHeading.vue';
const search = ref(''),
  group = ref('全部');
const descriptions: Record<string, string> = {
  '/resources/receive': '把分享、磁力与秒传资源接收到指定位置。',
  '/resources/share': '免转存：从分享直接生成 STRM，保留来源视频。',
  '/resources/files': '按账号浏览网盘目录、文件和实际大小。',
  '/settings/accounts': '连接账号，维护授权与接收归档目录。',
  '/organize/naming': '编辑目录、电影与剧集命名模板。',
  '/organize/quality': '对真实视频做八维比较与洗版。',
  '/settings/cloud': '配置命名、分类、过滤、输出和通知。',
  '/resources/receive?tab=bot': '接收 Telegram 白名单用户提交的资源。',
  '/organize/workspace': '识别作品、修正信息，按规则命名归档。',
  '/organize/automation': '让接收、整理与同步按计划自动运行。',
  '/resources/channels': '订阅频道消息，筛选作品并自动接收。',
  '/media/strm': '管理播放引用、同步任务和来源关系。',
  '/tasks/ingestion': '查看 Emby 实际入库状态与失败原因。',
  '/governance': '对照两个片库，核对质量与清理计划。',
  '/governance/cleanup': '核对并清理空目录、附属文件和残留。',
  '/settings/external': '搜索上游资源、转存与管理获取订阅。',
  '/media/explore': '发现影视作品，查看详情和片库状态。',
  '/media/subscriptions': '关注新集入库，管理更新提醒。',
  '/tasks/reports': '查看入库、执行记录与每日晨报。',
};
const description = (to: string) => descriptions[to] || descriptions[to.split('?')[0]!] || '';
const tools = computed(() =>
  searchFunctions(search.value).filter(
    (n) => description(n.to) && (group.value === '全部' || n.group === group.value),
  ),
);
const groups = [
  '全部',
  ...new Set(allNavigation.filter((n) => description(n.to)).map((n) => n.group)),
];
const sections = computed(() =>
  groups
    .slice(1)
    .map((name) => ({ name, items: tools.value.filter((t) => t.group === name) }))
    .filter((s) => s.items.length),
);
const featured = [
  {
    path: '/resources/receive',
    step: '01',
    name: '接收资源',
    note: '从一个链接开始',
    label: '分享 · 磁力 · 秒传',
  },
  {
    path: '/organize/workspace',
    step: '02',
    name: '整理归档',
    note: '把资源变成有序片库',
    label: '识别 · 命名 · 分类',
  },
  {
    path: '/governance',
    step: '03',
    name: '片库治理',
    note: '让好版本留下来',
    label: '对照 · 核实 · 清理',
  },
];
</script>
<template>
  <PageHeading
    title="资源与工具"
    subtitle="找到需要的工具，让每一步操作都有清晰入口。"
    eyebrow="工作空间"
  />
  <div v-if="!search && group === '全部'" class="featured-workflows">
    <RouterLink v-for="item in featured" :key="item.path" :to="item.path"
      ><header>
        <span>{{ item.step }}</span
        ><ArrowUpRight :size="21" />
      </header>
      <h2>{{ item.name }}</h2>
      <p>{{ item.note }}</p>
      <small>{{ item.label }}</small></RouterLink
    >
  </div>
  <div class="tools-control">
    <div class="tools-search">
      <Search :size="18" /><input
        v-model="search"
        aria-label="筛选功能卡片"
        placeholder="搜索功能或用途"
      /><kbd>搜索</kbd>
    </div>
    <span class="muted-text">{{ tools.length }} 个工具</span>
  </div>
  <nav class="browse-tabs tool-tabs">
    <button v-for="g in groups" :key="g" :class="{ active: group === g }" @click="group = g">
      {{ g }}
    </button>
  </nav>
  <section v-for="section in sections" :key="section.name" class="tool-section">
    <header>
      <h2>{{ section.name }}</h2>
      <span>{{ section.items.length }} 个工具</span>
    </header>
    <div class="tool-grid">
      <RouterLink v-for="tool in section.items" :key="tool.to" :to="tool.to" class="tool-card"
        ><span class="tool-icon"><component :is="tool.icon" :size="22" /></span>
        <div>
          <h3>{{ tool.name }}</h3>
          <p>{{ description(tool.to) }}</p>
        </div>
        <ArrowRight :size="17" class="tool-arrow"
      /></RouterLink>
    </div>
  </section>
  <p v-if="!tools.length" class="muted-text">没有匹配的功能，换一个关键词试试。</p>
</template>
