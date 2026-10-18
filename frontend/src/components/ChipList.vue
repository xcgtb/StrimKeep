<script setup lang="ts">
import { ref } from 'vue';

/**
 * 可增删的标签列表：扩展名、关键词这类「多值短文本」用它，
 * 比 textarea 每行一个好删、好加、不会误删相邻行。
 */
const props = defineProps<{ modelValue: string[]; placeholder?: string; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [string[]] }>();
const draft = ref('');

function normalize(text: string) {
  return text
    .split(/[,，\s]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function commit(next: string[]) {
  const seen = new Set<string>();
  emit(
    'update:modelValue',
    next.filter((v) => (seen.has(v) ? false : (seen.add(v), true))),
  );
}

function add() {
  const items = normalize(draft.value);
  if (!items.length) return;
  commit([...props.modelValue, ...items]);
  draft.value = '';
}

function remove(index: number) {
  commit(props.modelValue.filter((_, i) => i !== index));
}

function onKey(event: KeyboardEvent) {
  if (event.key === 'Enter' || event.key === ',' || event.key === '，') {
    event.preventDefault();
    add();
  } else if (event.key === 'Backspace' && !draft.value && props.modelValue.length) {
    remove(props.modelValue.length - 1);
  }
}
</script>

<template>
  <div class="chip-list" :class="{ disabled }">
    <span v-for="(item, index) in modelValue" :key="`${item}-${index}`" class="chip-item">
      {{ item }}
      <button
        type="button"
        class="chip-remove"
        :aria-label="`删除 ${item}`"
        :disabled="disabled"
        @click="remove(index)"
      >
        ×
      </button>
    </span>
    <input
      v-model="draft"
      class="chip-input"
      type="text"
      :placeholder="placeholder || '输入后回车添加'"
      :disabled="disabled"
      @keydown="onKey"
      @blur="add"
    />
  </div>
</template>

<style scoped>
.chip-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  padding: 8px 10px;
  min-height: 42px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--panel-solid);
}
.chip-list.disabled {
  opacity: 0.6;
}
.chip-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 6px 3px 9px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text);
  background: var(--accent-soft);
  border: 1px solid var(--border);
  border-radius: 999px;
}
.chip-remove {
  display: grid;
  place-items: center;
  width: 16px;
  height: 16px;
  padding: 0;
  font-size: 13px;
  line-height: 1;
  color: var(--muted);
  background: transparent;
  border: 0;
  border-radius: 50%;
  cursor: pointer;
}
.chip-remove:hover {
  color: var(--danger);
  background: var(--panel-solid);
}
.chip-input {
  flex: 1;
  min-width: 120px;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text);
  font-size: 13px;
}
</style>
