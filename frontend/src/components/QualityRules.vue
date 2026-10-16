<script setup lang="ts">
import { records, strings, type Data } from '../api/domain';
const props = defineProps<{ modelValue: Data[]; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: Data[]] }>();
const names: Record<string, string> = {
  release_group: '发布组',
  source: '资源类型',
  resolution: '分辨率',
  dolby: '动态范围',
  dynamic_range: '动态范围',
  fps: '帧率',
  bitdepth: '色深',
  audio: '音频',
  size: '真实源文件大小',
};
function patch(i: number, values: Data) {
  emit(
    'update:modelValue',
    props.modelValue.map((r, index) => (index === i ? { ...r, ...values } : r)),
  );
}
function move(i: number, delta: number) {
  const rows = [...props.modelValue],
    to = i + delta;
  if (to < 0 || to >= rows.length) return;
  [rows[i], rows[to]] = [rows[to]!, rows[i]!];
  emit('update:modelValue', rows);
}
function tier(i: number, j: number, delta: number) {
  const tiers = strings(props.modelValue[i]?.tiers),
    to = j + delta;
  if (to < 0 || to >= tiers.length || j === tiers.length - 1 || to === tiers.length - 1) return;
  [tiers[j], tiers[to]] = [tiers[to]!, tiers[j]!];
  patch(i, { tiers });
}
function groups(i: number, event: Event) {
  patch(i, {
    groups: (event.target as HTMLTextAreaElement).value
      .split('\n')
      .map((s) => s.trim())
      .filter(Boolean),
  });
}
</script>
<template>
  <div class="quality-rules">
    <article v-for="(rule, i) in records(modelValue)" :key="String(rule.key)" class="quality-rule">
      <header>
        <span class="rule-rank">{{ i + 1 }}</span
        ><label class="checkbox-label"
          ><input
            type="checkbox"
            :checked="!!rule.enabled"
            :disabled="disabled"
            @change="patch(i, { enabled: ($event.target as HTMLInputElement).checked })"
          />{{ rule.label || names[String(rule.key)] || rule.key }}</label
        >
        <div class="push-right">
          <button
            type="button"
            class="icon-button"
            :disabled="disabled || i === 0"
            :aria-label="`上移${names[String(rule.key)] || rule.key}`"
            @click="move(i, -1)"
          >
            ↑</button
          ><button
            type="button"
            class="icon-button"
            :disabled="disabled || i === modelValue.length - 1"
            :aria-label="`下移${names[String(rule.key)] || rule.key}`"
            @click="move(i, 1)"
          >
            ↓
          </button>
        </div>
      </header>
      <p v-if="rule.desc" class="muted-text">{{ rule.desc }}</p>
      <label v-if="rule.key === 'release_group'"
        >发布组列表（每行一个，越靠前越优先）<textarea
          :value="strings(rule.groups).join('\n')"
          :disabled="disabled"
          @change="groups(i, $event)"
        ></textarea>
      </label>
      <ol v-if="strings(rule.tiers).length" class="tier-list">
        <li v-for="(item, j) in strings(rule.tiers)" :key="item">
          <span>{{ item }}</span
          ><button
            type="button"
            :disabled="disabled || j === 0 || j === strings(rule.tiers).length - 1"
            :aria-label="`上移${item}`"
            @click="tier(i, j, -1)"
          >
            ↑</button
          ><button
            type="button"
            :disabled="disabled || j >= strings(rule.tiers).length - 2"
            :aria-label="`下移${item}`"
            @click="tier(i, j, 1)"
          >
            ↓
          </button>
        </li>
      </ol>
    </article>
  </div>
</template>
