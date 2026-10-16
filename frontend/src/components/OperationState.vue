<script setup lang="ts">
import EvidenceDetails from './EvidenceDetails.vue';
defineProps<{ busy?: boolean; error?: string; uncertain?: boolean; receipt?: unknown }>();
defineEmits<{ reconcile: [] }>();
</script>
<template>
  <p v-if="busy" class="notice-bar" role="status">请求处理中，请等待服务回执…</p>
  <div v-if="error" class="notice-bar error-notice" role="alert">
    <span>{{ error }}</span
    ><span v-if="uncertain">结果待核实，请先查看任务或重新读取服务状态。</span>
  </div>
  <details v-if="receipt" class="operation-result">
    <summary>查看服务回执</summary>
    <EvidenceDetails :value="receipt" />
  </details>
</template>
