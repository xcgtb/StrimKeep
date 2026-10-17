<script setup lang="ts">
export interface ChipOption {
  value: string;
  label: string;
  count?: number | string;
}
const props = withDefaults(
  defineProps<{
    label: string;
    modelValue: string;
    options: ChipOption[];
    /** 横向滚动（年份），对齐 1.2.12：滚到尾部再追加更多选项 */
    scroll?: boolean;
  }>(),
  { scroll: false },
);
const emit = defineEmits<{ 'update:modelValue': [string]; 'load-more': [] }>();
// 1.2.12 explore.js 的 year-scroll：距右端 80px 内追加下一批。
function onScroll(event: Event) {
  if (!props.scroll) return;
  const track = event.target as HTMLElement;
  if (track.scrollLeft + track.clientWidth >= track.scrollWidth - 80) emit('load-more');
}
</script>
<template>
  <div class="chip-row">
    <span class="chip-row-label">{{ label }}</span>
    <div
      class="chip-track"
      :class="{ scrollable: scroll }"
      role="group"
      :aria-label="label"
      @scroll="onScroll"
    >
      <button
        v-for="option in options"
        :key="option.value"
        type="button"
        class="chip"
        :class="{ active: modelValue === option.value }"
        :aria-pressed="modelValue === option.value"
        @click="emit('update:modelValue', option.value)"
      >
        {{ option.label
        }}<b v-if="option.count !== undefined && option.count !== ''">{{ option.count }}</b>
      </button>
    </div>
  </div>
</template>
