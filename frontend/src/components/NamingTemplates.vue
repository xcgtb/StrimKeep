<script setup lang="ts">
import { computed, nextTick, ref } from 'vue';
import { Search, BookOpen, Copy, Plus } from '@lucide/vue';
import type { Data } from '../api/domain';
import {
  namingTemplateFields,
  namingVariables,
  validateTemplate,
  sampleTemplate,
  type NamingTemplateKey,
  type NamingVariable,
} from '../config/namingVariables';
import defaults from '../config/naming-defaults.json';
import { useUi } from '../stores/ui';
import ModalDialog from './ModalDialog.vue';
const props = defineProps<{ modelValue: Data; disabled?: boolean }>();
const emit = defineEmits<{ 'update:modelValue': [value: Data] }>();
const ui = useUi(),
  target = ref<NamingTemplateKey>('tv_file_template'),
  search = ref(''),
  filter = ref('常用'),
  selected = ref<NamingVariable>();
const editors = new Map<string, HTMLInputElement>(),
  positions = new Map<string, [number, number]>();
const common = new Set([
  'title',
  'year',
  'tmdb_id',
  'category',
  'season',
  'season:02d',
  'episode',
  'episode:02d',
  'resolution',
  'source',
  'codec',
  'dynamic_range',
  'audio_codec',
  'release_group',
  'ext',
]);
const categories = ['作品信息', '季集信息', '画质版本', '文件属性'];
const catalog = computed(() =>
  categories
    .map((name) => ({
      name,
      items: namingVariables.filter(
        (v) =>
          v.category === name &&
          (filter.value === '全部' ||
            (filter.value === '常用' && common.has(v.key)) ||
            filter.value === name) &&
          (!search.value.trim() ||
            [v.key, v.name, v.explanation, v.example]
              .join(' ')
              .toLowerCase()
              .includes(search.value.trim().toLowerCase())),
      ),
    }))
    .filter((g) => g.items.length),
);
function value(key: NamingTemplateKey) {
  return String(props.modelValue[key] || '');
}
function remember(key: NamingTemplateKey) {
  const el = editors.get(key);
  if (el)
    positions.set(key, [el.selectionStart ?? el.value.length, el.selectionEnd ?? el.value.length]);
}
function update(key: NamingTemplateKey, text: string) {
  emit('update:modelValue', { ...props.modelValue, [key]: text });
}
async function insert(v: NamingVariable) {
  if (props.disabled) return;
  const key = target.value,
    text = value(key),
    token = `{${v.key}}`,
    [start, end] = positions.get(key) || [text.length, text.length];
  update(key, text.slice(0, start) + token + text.slice(end));
  selected.value = undefined;
  await nextTick();
  const el = editors.get(key);
  el?.focus();
  el?.setSelectionRange(start + token.length, start + token.length);
  remember(key);
}
async function copy(v: NamingVariable) {
  try {
    await navigator.clipboard.writeText(`{${v.key}}`);
    ui.notify('变量已复制');
  } catch {
    selected.value = v;
    ui.notify('当前浏览器无法复制，可在说明中选择变量文本。');
  }
}
function restore() {
  emit('update:modelValue', { ...props.modelValue, ...defaults });
}
</script>
<template>
  <div class="naming-studio">
    <section class="panel template-editor-panel">
      <header class="section-heading">
        <div>
          <h2>自定义识别与命名模板</h2>
          <p>电影、剧集、季目录与文件名分别设置，点击输入框选择插入位置。</p>
        </div>
        <BookOpen :size="21" />
      </header>
      <div class="naming-guide">
        <strong>模板怎么用？</strong>
        <p>
          选择输入框 → 查找下方变量 → 点击「插入模板」。视频文件名保留
          <code>{ext}</code>，季目录包含 <code>{season}</code>。
        </p>
      </div>
      <div class="name-template-list">
        <div
          v-for="field in namingTemplateFields"
          :key="field.key"
          class="name-template"
          :class="{ 'active-template': target === field.key }"
        >
          <label :for="`naming-${field.key}`"
            ><strong>{{ field.name }}</strong
            ><span v-if="target === field.key" class="state-pill">当前插入目标</span></label
          >
          <input
            :id="`naming-${field.key}`"
            :aria-label="field.name"
            :ref="
              (el) => {
                if (el) editors.set(field.key, el as HTMLInputElement);
                else editors.delete(field.key);
              }
            "
            :value="value(field.key)"
            :disabled="disabled"
            spellcheck="false"
            @focus="
              target = field.key;
              remember(field.key);
            "
            @click="remember(field.key)"
            @keyup="remember(field.key)"
            @select="remember(field.key)"
            @input="
              update(field.key, ($event.target as HTMLInputElement).value);
              remember(field.key);
            "
          />
          <small>{{ field.hint }}</small>
          <p v-if="validateTemplate(field.key, value(field.key))" class="inline-error">
            {{ validateTemplate(field.key, value(field.key)) }}
          </p>
          <div v-else class="name-sample">
            <span>示例输出</span><code>{{ sampleTemplate(value(field.key)) }}</code>
          </div>
        </div>
      </div>
      <div class="action-row">
        <button type="button" class="button" :disabled="disabled" @click="restore">
          恢复默认模板草稿</button
        ><span class="data-caption">示例使用说明值；真实输出请在保存前运行后端命名预览。</span>
      </div>
    </section>
    <section class="panel variable-dictionary">
      <header class="section-heading">
        <div>
          <h2>模板变量词典 · 中文解释与示例</h2>
          <p>按用途查找变量，将所需信息插入当前模板。</p>
        </div>
        <span class="module-tag">{{ namingVariables.length }} 个变量</span>
      </header>
      <div class="variable-controls">
        <label
          >插入目标<select v-model="target" aria-label="变量插入目标">
            <option v-for="field in namingTemplateFields" :key="field.key" :value="field.key">
              {{ field.name }}
            </option>
          </select></label
        >
        <div class="variable-search">
          <Search :size="17" /><input
            v-model="search"
            type="search"
            aria-label="搜索模板变量"
            placeholder="搜索：年份 / 季号 / 分辨率 / ext"
          />
        </div>
      </div>
      <nav class="variable-filter" aria-label="命名变量分类">
        <button
          v-for="name in ['常用', '全部', ...categories]"
          :key="name"
          type="button"
          :class="{ active: filter === name }"
          :aria-pressed="filter === name"
          @click="filter = name"
        >
          {{ name === '常用' ? '常用变量' : name === '全部' ? '全部变量' : name }}
        </button>
      </nav>
      <section v-for="group in catalog" :key="group.name" class="variable-group">
        <header>
          <h3>{{ group.name }}</h3>
          <span>{{ group.items.length }} 项</span>
        </header>
        <div class="naming-variable-grid">
          <article v-for="v in group.items" :key="v.key" class="naming-variable-card">
            <div>
              <code>{{ '{' + v.key + '}' }}</code
              ><strong>{{ v.name }}</strong>
            </div>
            <p>{{ v.explanation }}</p>
            <small
              >示例值：<em>{{ v.example }}</em></small
            >
            <div class="action-row">
              <button
                type="button"
                class="button small primary"
                :disabled="disabled"
                :aria-label="`将${v.name}插入模板`"
                @click="insert(v)"
              >
                <Plus :size="13" />插入模板</button
              ><button
                type="button"
                class="button small"
                :aria-label="`查看${v.name}说明`"
                @click="selected = v"
              >
                说明</button
              ><button
                type="button"
                class="button small"
                :aria-label="`复制${v.name}变量`"
                @click="copy(v)"
              >
                <Copy :size="12" />复制
              </button>
            </div>
          </article>
        </div>
      </section>
      <p v-if="!catalog.length" class="muted-text">
        没有匹配的变量，试试搜索中文含义或选择「全部变量」。
      </p>
    </section>
  </div>
  <ModalDialog v-if="selected" :title="`${selected.name} · 变量说明`" @close="selected = undefined"
    ><p>
      <code>{{ '{' + selected.key + '}' }}</code>
    </p>
    <p>{{ selected.explanation }}</p>
    <p>示例：{{ selected.example }}</p>
    <p>适用于：{{ selected.usage }}</p>
    <template #footer
      ><button class="button" @click="selected = undefined">关闭</button
      ><button class="button primary" :disabled="disabled" @click="insert(selected)">
        插入当前模板
      </button></template
    ></ModalDialog
  >
</template>
