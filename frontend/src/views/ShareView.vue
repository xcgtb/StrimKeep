<script setup lang="ts">
import { ref } from 'vue';
import { post } from '../api/client';
import { object, records, text } from '../api/domain';

const link = ref('');
const password = ref('');
const items = ref<Record<string, unknown>[]>([]);
const selected = ref<Set<string>>(new Set());
const plan = ref<Record<string, unknown>[]>([]);
const busy = ref(false);
const message = ref('');
const error = ref('');
const step = ref(1);

function keyOf(item: Record<string, unknown>) {
  return text(item.id || item.file_id || item.name);
}
async function preview() {
  if (!link.value.trim()) return;
  busy.value = true;
  message.value = '读取分享内容…';
  error.value = '';
  try {
    const data = object(
      await post<Record<string, unknown>>('/api/cloud/shares/preview', {
        link: link.value,
        password: password.value || undefined,
      }),
    );
    items.value = records(data.items || data.files || data.entries);
    selected.value = new Set();
    step.value = 2;
    message.value = `读取到 ${items.value.length} 项，勾选要生成 STRM 的内容`;
  } catch (e) {
    error.value = (e as Error).message;
    message.value = '';
  } finally {
    busy.value = false;
  }
}
async function recognize() {
  busy.value = true;
  message.value = '正在识别所选内容…';
  error.value = '';
  try {
    const chosen = items.value
      .filter((item) => selected.value.has(keyOf(item)))
      .map((item) => ({ id: text(item.id), file_id: text(item.file_id), name: text(item.name) }));
    const data = object(
      await post<Record<string, unknown>>('/api/cloud/shares/strm/preview', {
        link: link.value,
        password: password.value || undefined,
        items: chosen,
      }),
    );
    plan.value = records(data.plan || data.items);
    step.value = 3;
    message.value = `识别完成，共 ${plan.value.length} 项，确认后生成`;
  } catch (e) {
    error.value = (e as Error).message;
    message.value = '';
  } finally {
    busy.value = false;
  }
}
async function apply() {
  busy.value = true;
  message.value = '已提交生成任务…';
  try {
    await post('/api/cloud/shares/strm/apply', {});
    message.value = '已提交生成到 NAS 分享库';
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <section class="panel">
    <h3 data-icon="share">分享免转存 · 生成 STRM</h3>
    <div class="cw-share-guide">
      <strong>不转存，只在 NAS 创建播放入口</strong>
      <p>粘贴分享 → 选择内容 → 核对识别 → 生成。不会修改对方网盘；分享失效后无法播放。</p>
    </div>

    <div v-if="step === 1" class="cw-share-step">
      <h4><span>1</span>读取分享</h4>
      <div class="cw-form-grid">
        <label
          >115 分享链接或复制文案
          <input v-model="link" placeholder="粘贴分享链接" /></label
        >
        <label
          >提取码（链接已带可留空）
          <input v-model="password" /></label
        >
      </div>
      <button class="btn" :disabled="busy" @click="preview">读取分享内容</button>
    </div>

    <div v-if="step === 2" class="cw-share-step">
      <h4><span>2</span>选择要生成的内容</h4>
      <p class="hint">勾选视频，或勾选整个目录递归识别。这里不会转存。</p>
      <div v-for="item in items" :key="keyOf(item)" class="cw-file-row">
        <label style="display: flex; gap: 8px; align-items: center; cursor: pointer">
          <input
            type="checkbox"
            :checked="selected.has(keyOf(item))"
            @change="
              () => {
                const next = new Set(selected);
                next.has(keyOf(item)) ? next.delete(keyOf(item)) : next.add(keyOf(item));
                selected = next;
              }
            "
          />
          <span class="cw-file-name">{{ text(item.name) }}</span>
        </label>
        <span class="cw-file-meta">{{ item.is_dir ? '目录' : text(item.size) }}</span>
      </div>
      <button class="btn" :disabled="busy || !selected.size" @click="recognize">
        识别所选内容（{{ selected.size }}）
      </button>
    </div>

    <div v-if="step === 3" class="cw-share-step">
      <h4><span>3</span>核对结果并生成</h4>
      <div v-for="row in plan" :key="text(row.name || row.path)" class="cw-file-row">
        <span class="cw-file-name">{{ text(row.name || row.path) }}</span>
        <span class="cw-file-meta">{{ text(row.media_name || '') }} {{ text(row.season ? `S${row.season}` : '') }}</span>
      </div>
      <button class="btn" :disabled="busy" @click="apply">确认生成到 NAS 分享库</button>
    </div>

    <p class="hint" role="status">{{ message }}</p>
    <p v-if="error" class="hint">{{ error }}</p>
    <p class="hint">输出位置与播放地址在「全局设置」配置。单批最多识别 2000 个文件及目录。</p>
  </section>
</template>
