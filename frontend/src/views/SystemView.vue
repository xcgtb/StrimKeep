<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { get } from '../api/client';
import { object, text } from '../api/domain';

const runtime = ref<Record<string, unknown>>({});
const version = ref('');
const loading = ref(true);
const error = ref('');

const readiness = computed(() => object(runtime.value.readiness));
const paths = computed(() => object(runtime.value.paths));
const bot = computed(() => object(runtime.value.bot));
const task = computed(() => object(runtime.value.task));
const storage = computed(() => object(runtime.value.storage));

function tone(ok: unknown) {
  return ok === true ? 'ok' : 'unknown';
}
async function load() {
  loading.value = true;
  error.value = '';
  try {
    runtime.value = object(await get<Record<string, unknown>>('/api/runtime/status'));
    version.value = text(runtime.value.version);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
onMounted(load);
</script>
<template>
  <section class="tab active">
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="activity">系统状态</h3>
        <button class="btn gray" data-icon="refresh" :disabled="loading" @click="load">刷新</button>
      </div>
      <p v-if="error" class="section-note">{{ error }}</p>
      <div class="meta-row"><span class="k">版本</span><span class="v">{{ version || '—' }}</span></div>
      <div class="meta-row">
        <span class="k">服务就绪</span>
        <span class="v">{{ readiness.ready === true ? '已就绪' : readiness.ready === false ? '初始化中' : '—' }}</span>
      </div>
      <div class="meta-row">
        <span class="k">Telegram</span>
        <span class="v">{{ bot.running === true ? '运行中' : bot.running === false ? '未运行' : '未配置' }}</span>
      </div>
      <div class="meta-row">
        <span class="k">当前任务</span>
        <span class="v">{{ text(task.name) || '空闲' }}</span>
      </div>
      <div class="meta-row">
        <span class="k">数据存储</span>
        <span class="v">{{ text(storage.state || '—') }}</span>
      </div>
    </div>

    <div class="panel">
      <h3>媒体目录</h3>
      <div v-for="(value, key) in paths" :key="key" class="meta-row">
        <span class="k">{{ text(key) }}</span>
        <span class="v">{{ text(value) }}</span>
      </div>
    </div>
  </section>
</template>
