<script setup lang="ts" generic="T extends string">
defineProps<{ label: string; items: ReadonlyArray<{ id: T; label: string }> }>()
const selected = defineModel<T>({ required: true })
</script>

<template>
  <nav class="workspace-sections" :aria-label="label">
    <button v-for="item in items" :key="item.id" type="button"
      :aria-pressed="selected === item.id" @click="selected = item.id">
      {{ item.label }}
    </button>
  </nav>
</template>

<style scoped>
.workspace-sections {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  width: fit-content;
  max-width: 100%;
  margin: 0 0 16px;
  padding: 4px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--surface);
}
.workspace-sections button {
  padding: 8px 15px;
  min-height: 38px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
}
.workspace-sections button:hover { background: var(--surface-soft); color: var(--text); }
.workspace-sections button[aria-pressed="true"] {
  background: color-mix(in srgb, var(--accent) 13%, var(--surface));
  color: var(--accent);
  font-weight: 700;
}
.workspace-sections button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
@media (max-width: 600px) {
  .workspace-sections { width: 100%; }
  .workspace-sections button { flex: 1 1 auto; padding-inline: 9px; min-height: 42px; }
}
</style>
