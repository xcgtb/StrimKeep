<script setup lang="ts">
import { ref } from 'vue';

/**
 * 折叠分区：图标 + 标题 + 一句人话说明 + 收起/展开。
 * 统一「标题讲清楚是什么、说明讲清楚会影响什么」的结构，避免字段直接裸奔。
 */
const props = withDefaults(
  defineProps<{
    title: string;
    desc?: string;
    open?: boolean;
    /** danger 用于含删除/不可逆操作的分区 */
    tone?: 'default' | 'danger';
  }>(),
  { open: true, tone: 'default' },
);
const expanded = ref(props.open);
</script>

<template>
  <section class="sc" :class="`sc-${tone}`">
    <header class="sc-head">
      <span class="sc-icon" aria-hidden="true"><slot name="icon" /></span>
      <div class="sc-text">
        <h3 class="sc-title">{{ title }}</h3>
        <p v-if="desc" class="sc-desc">{{ desc }}</p>
      </div>
      <button
        type="button"
        class="sc-toggle"
        :aria-expanded="expanded"
        :aria-label="`${expanded ? '收起' : '展开'}${title}`"
        @click="expanded = !expanded"
      >
        {{ expanded ? '收起' : '展开' }}
      </button>
    </header>
    <div v-show="expanded" class="sc-body"><slot /></div>
  </section>
</template>

<style scoped>
.sc {
  background: var(--panel-solid);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow);
  overflow: hidden;
}
.sc-danger {
  border-color: color-mix(in srgb, var(--danger) 32%, var(--border));
}
.sc-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px 18px;
  background: var(--subtle);
  border-bottom: 1px solid var(--border);
}
.sc-icon {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  border-radius: 9px;
  background: var(--panel-solid);
  border: 1px solid var(--border);
  color: var(--accent);
}
.sc-text {
  flex: 1;
  min-width: 0;
}
.sc-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
  line-height: 1.4;
}
.sc-desc {
  margin: 4px 0 0;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--muted);
}
.sc-toggle {
  flex: 0 0 auto;
  padding: 5px 12px;
  font-size: 12.5px;
  color: var(--muted);
  background: var(--panel-solid);
  border: 1px solid var(--border);
  border-radius: 8px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.sc-toggle:hover {
  color: var(--accent);
  border-color: var(--accent);
}
.sc-body {
  padding: 18px;
}
.sc-body :deep(> * + *) {
  margin-top: 14px;
}
@media (max-width: 720px) {
  .sc-head {
    padding: 14px;
  }
  .sc-body {
    padding: 14px;
  }
  .sc-desc {
    font-size: 12px;
  }
}
</style>
