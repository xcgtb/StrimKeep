<script setup lang="ts">
import { useId } from 'vue';
import type { Data } from '../api/domain';
import ChipList from './ChipList.vue';
export interface Field {
  key: string;
  label: string;
  type?: 'text' | 'secret' | 'number' | 'check' | 'lines' | 'area' | 'select' | 'chips';
  options?: { value: string; label: string }[];
  min?: number;
  max?: number;
  step?: number;
  hint?: string;
  disabled?: boolean;
}
const prefix = useId();
const props = defineProps<{ modelValue: Data; fields: Field[]; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: Data] }>();
function set(field: Field, event: Event) {
  const target = event.target as HTMLInputElement;
  let value: unknown = target.value;
  if (field.type === 'check') value = target.checked;
  else if (field.type === 'number') value = target.value === '' ? null : Number(target.value);
  else if (field.type === 'lines')
    value = target.value
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean);
  emit('update:modelValue', { ...props.modelValue, [field.key]: value });
}
function chipValue(field: Field): string[] {
  const v = props.modelValue[field.key];
  return Array.isArray(v) ? (v as string[]) : [];
}
function setChips(field: Field, next: string[]) {
  emit('update:modelValue', { ...props.modelValue, [field.key]: next });
}
function value(field: Field) {
  const v = props.modelValue[field.key];
  return field.type === 'lines' && Array.isArray(v) ? v.join('\n') : v == null ? '' : String(v);
}
</script>
<template>
  <div class="form-grid schema-form">
    <label
      v-for="field in fields"
      :key="field.key"
      :for="`${prefix}-${field.key}`"
      :class="{
        'checkbox-label': field.type === 'check',
        'wide-field': field.type === 'area' || field.type === 'lines',
      }"
      ><template v-if="field.type === 'check'"
        ><input
          :id="`${prefix}-${field.key}`"
          :aria-label="field.label"
          type="checkbox"
          :checked="!!modelValue[field.key]"
          :disabled="disabled || field.disabled"
          @change="set(field, $event)"
        />{{ field.label }}</template
      ><template v-else
        >{{ field.label
        }}<select
          v-if="field.type === 'select'"
          :id="`${prefix}-${field.key}`"
          :aria-label="field.label"
          :value="value(field)"
          :disabled="disabled || field.disabled"
          @change="set(field, $event)"
        >
          <option v-for="option in field.options" :key="option.value" :value="option.value">
            {{ option.label }}
          </option></select
        ><ChipList
          v-else-if="field.type === 'chips'"
          :model-value="chipValue(field)"
          :disabled="disabled || field.disabled"
          @update:model-value="setChips(field, $event)" /><textarea
          v-else-if="field.type === 'area' || field.type === 'lines'"
          :id="`${prefix}-${field.key}`"
          :aria-label="field.label"
          :value="value(field)"
          rows="4"
          :disabled="disabled || field.disabled"
          @change="set(field, $event)"
        ></textarea
        ><input
          v-else
          :id="`${prefix}-${field.key}`"
          :aria-label="field.label"
          :value="value(field)"
          :type="field.type === 'secret' ? 'password' : field.type === 'number' ? 'number' : 'text'"
          :min="field.min"
          :max="field.max"
          :step="field.step || 'any'"
          :disabled="disabled || field.disabled"
          autocomplete="off"
          @input="set(field, $event)" /></template
      ><small v-if="field.hint">{{ field.hint }}</small></label
    >
  </div>
</template>
