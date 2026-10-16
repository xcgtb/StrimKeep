<script setup lang="ts">
import { ArrowRight } from '@lucide/vue';
defineProps<{ active?: number }>();
const stages = [
  { to: '/resources/receive', title: '接收资源' },
  { to: '/organize/workspace', title: '识别与整理' },
  { to: '/media/strm', title: 'STRM 发布' },
  { to: '/tasks/ingestion', title: 'Emby 入库' },
  { to: '/governance', title: '片库治理' },
];
</script>
<template>
  <nav class="pipeline-rail" aria-label="媒体业务流程">
    <template v-for="(stage, i) in stages" :key="stage.to"
      ><RouterLink
        :to="stage.to"
        :class="{ active: active === i }"
        :aria-current="active === i ? 'step' : undefined"
        ><span>{{ i + 1 }}</span
        ><strong>{{ stage.title }}</strong></RouterLink
      ><ArrowRight v-if="i < stages.length - 1" :size="14"
    /></template>
  </nav>
</template>
