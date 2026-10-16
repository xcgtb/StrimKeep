<script setup lang="ts">
import { AlertCircle, LoaderCircle, Inbox } from '@lucide/vue';
defineProps<{ loading?: boolean; error?: string; empty?: boolean; message?: string }>();
defineEmits<{ retry: [] }>();
</script>
<template>
  <div v-if="error" class="state-panel error-state" role="alert">
    <AlertCircle :size="24" /><strong>暂时无法加载</strong>
    <p>{{ error }}</p>
    <button class="button" @click="$emit('retry')">重新加载</button>
  </div>
  <div v-else-if="loading" class="state-panel" role="status">
    <LoaderCircle class="spin" :size="24" />
    <p>正在读取数据…</p>
  </div>
  <div v-else-if="empty" class="state-panel">
    <Inbox :size="28" /><strong>{{ message || '暂无数据' }}</strong
    ><slot />
  </div>
</template>
