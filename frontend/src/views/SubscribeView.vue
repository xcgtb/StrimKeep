<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { get, post } from '../api/client';
import { date, object, records, text } from '../api/domain';

const subscriptions = ref<Record<string, unknown>[]>([]);
const enabled = ref(false);
const checkTmdb = ref(false);
const loading = ref(false);
const error = ref('');
const message = ref('');
const lastCheck = ref<Record<string, unknown>>({});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = object(await get<Record<string, unknown>>('/api/subscriptions'));
    subscriptions.value = records(data.subscriptions);
    enabled.value = data.enabled === true;
    checkTmdb.value = data.check_tmdb === true;
    lastCheck.value = object(data.check);
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    loading.value = false;
  }
}
async function save() {
  try {
    await post('/api/subscriptions/settings', { enabled: enabled.value, check_tmdb: checkTmdb.value });
    message.value = '设置已保存';
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function checkNow() {
  message.value = '正在检查更新…';
  try {
    await post('/api/subscriptions/check');
    message.value = '检查任务已提交';
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  }
}
onMounted(load);
</script>
<template>
  <section class="tab active">
    <div class="panel">
      <div class="row-between">
        <h3 data-icon="bell">追更订阅</h3>
        <div style="display: flex; gap: 8px">
          <button class="btn" data-icon="refresh" @click="checkNow">立即检查</button>
        </div>
      </div>

      <div class="toggle-row">
        <div>
          <div class="lbl">启用追更通知</div>
          <div class="desc">新集由入库事实更新触发；实时模式使用 Emby 回调，只有已订阅作品才发送通知</div>
        </div>
        <label class="switch"
          ><input v-model="enabled" type="checkbox" @change="save" /><span class="slider"></span
        ></label>
      </div>
      <div class="toggle-row">
        <div>
          <div class="lbl">对照 TMDB 检查缺集</div>
          <div class="desc">同时报告本地比 TMDB 少的集数</div>
        </div>
        <label class="switch"
          ><input v-model="checkTmdb" type="checkbox" @change="save" /><span class="slider"></span
        ></label>
      </div>

      <p v-if="message" class="section-note" role="status">{{ message }}</p>
      <p v-if="error" class="section-note">{{ error }}</p>
      <p v-if="Number(lastCheck.ts)" class="section-note">
        上次检查 {{ date(Number(lastCheck.ts)) }} · 更新 {{ text(lastCheck.updates ?? 0) }} 部 · 发送
        {{ text(lastCheck.sent ?? 0) }}
      </p>
    </div>

    <div class="panel">
      <div v-if="!subscriptions.length" class="list-empty">
        {{ loading ? '加载中…' : '还没有订阅；在影视探索里对剧集点「追更订阅」即可添加' }}
      </div>
      <div v-for="item in subscriptions" :key="text(item.id || item.tmdb_id)" class="sub-item">
        <div class="name">{{ text(item.name) }}</div>
        <div class="meta">
          TMDB {{ text(item.tmdb_id) }} · 已入库 {{ text(item.have_eps ?? '—') }} 集
        </div>
      </div>
    </div>
  </section>
</template>
