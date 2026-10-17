<script setup lang="ts">
export interface ChipOption {
  value: string;
  label: string;
  count?: number | string;
}
withDefaults(
  defineProps<{
    label: string;
    modelValue: string;
    options: ChipOption[];
    scroll?: boolean;
  }>(),
  { scroll: false },
);
const emit = defineEmits<{ 'update:modelValue': [string] }>();
</script>
<template>
  <div class="chip-row">
    <span class="chip-row-label">{{ label }}</span>
    <div class="chip-track" :class="{ scrollable: scroll }" role="group" :aria-label="label">
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
