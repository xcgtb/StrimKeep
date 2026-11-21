<script setup lang="ts">
defineProps<{ title: string; description?: string }>()
</script>

<template>
  <header class="page-header">
    <div class="page-header-copy">
      <h1>{{ title }}</h1>
      <p v-if="description">{{ description }}</p>
    </div>
    <div v-if="$slots.actions" class="page-header-actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px 24px;
  margin: 0 0 20px;
}
.page-header-copy { min-width: 0; }
.page-header h1 {
  margin: 0;
  color: var(--text);
  font-size: clamp(24px, 2.4vw, 31px);
  line-height: 1.2;
  letter-spacing: -.035em;
}
.page-header p {
  margin: 6px 0 0;
  max-width: 66ch;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.65;
}
.page-header-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}
.page-header-actions :deep(.el-button) { margin: 0; }
@media (max-width: 600px) {
  .page-header { gap: 10px 12px; margin-bottom: 16px; }
  .page-header p { font-size: 12px; }
}
</style>
