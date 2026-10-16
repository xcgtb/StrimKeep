<script setup lang="ts">
import { ref } from 'vue';
import { records, strings, type Data } from '../api/domain';
const expanded = ref(new Set<string>());
function toggleDetails(key: string) {
  const x = new Set(expanded.value);
  x.has(key) ? x.delete(key) : x.add(key);
  expanded.value = x;
}
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
        <span class="rule-rank">{{ String(i + 1).padStart(2, '0') }}</span
        ><label class="rule-title"
          ><strong>{{ rule.label || names[String(rule.key)] || rule.key }}</strong
          ><small>{{
            rule.desc ||
            (rule.key === 'size'
              ? '比较 115 原视频真实大小，体积未知时跳过。'
              : '按当前档位顺序比较，未识别始终在最后。')
          }}</small
          ><input
            class="quality-switch"
            type="checkbox"
            :aria-label="String(names[String(rule.key)] || rule.key)"
            :checked="!!rule.enabled"
            :disabled="disabled"
            @change="patch(i, { enabled: ($event.target as HTMLInputElement).checked })"
        /></label>
        <div class="rule-actions">
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
            ↓</button
          ><button
            type="button"
            class="button small rule-details-toggle"
            :aria-label="`查看${names[String(rule.key)] || rule.key}档位`"
            :aria-expanded="expanded.has(String(rule.key))"
            @click="toggleDetails(String(rule.key))"
          >
            {{ expanded.has(String(rule.key)) ? '收起' : '详情' }}
          </button>
        </div>
      </header>
      <div v-if="expanded.has(String(rule.key))" class="quality-rule-detail">
        <p class="data-caption">
          {{ rule.enabled ? '此维度已启用，按当前优先级参与比较。' : '此维度已关闭，执行时跳过。' }}
        </p>
        <label v-if="rule.key === 'release_group'"
          >发布组列表（每行一个，越靠前越优先）<textarea
            :value="strings(rule.groups).join('\n')"
            :disabled="disabled"
            @change="groups(i, $event)"
          ></textarea>
        </label>
        <ol v-if="strings(rule.tiers).length" class="tier-list">
          <li v-for="(item, j) in strings(rule.tiers)" :key="item">
            <span class="tier-number">{{ j + 1 }}</span
            ><span>{{ item }}</span
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
        <p v-else-if="rule.key === 'size'" class="data-caption">
          使用真实视频体积比较，没有可排序档位。
        </p>
      </div>
    </article>
  </div>
</template>
