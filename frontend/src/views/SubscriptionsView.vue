<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue';
import { Bell, Pause, Play, Trash2, Settings2 } from '@lucide/vue';
import { useSubscriptions } from '../stores/subscriptions';
import { useRequest } from '../composables/useRequest';
import { useUi } from '../stores/ui';
import { posterUrl } from '../composables/format';
import type { Subscription, SubscriptionsResponse } from '../types/api';
import LoadState from '../components/LoadState.vue';
import ModalDialog from '../components/ModalDialog.vue';
import { useOperation } from '../composables/useOperation';
import { useLibraryUpdates } from '../composables/useLibraryUpdates';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { readData, writeData, type Data } from '../api/domain';
import OperationState from '../components/OperationState.vue';
import EvidenceDetails from '../components/EvidenceDetails.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
const operation = useOperation(),
  checkOpen = ref(false),
  status = useRequest<Data>();
async function loadStatus() {
  return status.run((signal) => readData('/api/subscriptions/status', signal));
}
async function check() {
  const r = await operation.run(() => writeData('/api/subscriptions/check'));
  if (r) {
    checkOpen.value = false;
    await Promise.allSettled([load(), loadStatus()]);
  }
}
useVisiblePoll(loadStatus, 15000);
const store = useSubscriptions(),
  request = useRequest<SubscriptionsResponse>(),
  ui = useUi(),
  removing = ref<Subscription>(),
  settingsOpen = ref(false);
const form = reactive({ enabled: true, check_tmdb: true, calibration_interval_hours: 6 });
async function load() {
  const value = await request.run((signal) => store.load(signal));
  if (value)
    Object.assign(form, {
      enabled: value.enabled,
      check_tmdb: value.check_tmdb,
      calibration_interval_hours: value.calibration_interval_hours,
    });
}
async function toggle(sub: Subscription) {
  try {
    await store.mutate((rows) =>
      rows.map((s) => (s.id === sub.id ? { ...s, enabled: !sub.enabled } : s)),
    );
    ui.notify('订阅状态已保存', 'success');
  } catch (e) {
    ui.notify((e as Error).message, 'error');
  }
}
async function remove() {
  if (!removing.value) return;
  const id = removing.value.id;
  try {
    await store.mutate((rows) => rows.filter((s) => s.id !== id));
    removing.value = undefined;
    ui.notify('已移除订阅', 'success');
  } catch (e) {
    ui.notify((e as Error).message, 'error');
  }
}
async function save() {
  const result = await operation.run(() => writeData('/api/subscriptions/settings', { ...form }));
  if (result) {
    settingsOpen.value = false;
    ui.notify('追更设置已保存', 'success');
    await load();
  }
}
useLibraryUpdates(async () => {
  if (store.busy || settingsOpen.value) return false;
  await Promise.allSettled([load(), loadStatus()]);
  return request.error.value ? false : undefined;
});
onMounted(() => {
  void load();
  void loadStatus();
});
</script>
<template>
  <section class="page-heading">
    <div>
      <span class="eyebrow">KEEP FOLLOWING</span>
      <h1>追更订阅</h1>
      <p>关注喜欢的剧集，在确认入库后接收更新提醒。</p>
    </div>
    <button
      class="button"
      :disabled="request.loading.value || !!request.error.value"
      @click="settingsOpen = true"
    >
      <Settings2 :size="16" />追更设置
    </button>
  </section>
  <OperationState
    :busy="operation.busy.value"
    :error="operation.error.value || status.error.value"
    :uncertain="operation.uncertain.value"
    :receipt="operation.receipt.value"
  />
  <div class="action-row">
    <button
      class="button"
      :disabled="operation.busy.value || operation.uncertain.value"
      @click="checkOpen = true"
    >
      立即检查订阅</button
    ><button
      class="button"
      @click="
        loadStatus();
        load();
      "
    >
      刷新订阅状态
    </button>
    <details>
      <summary>检查进度与最近回执</summary>
      <EvidenceDetails :value="status.data.value" />
    </details>
  </div>
  <div class="summary-strip">
    <span class="summary-symbol"><Bell :size="20" /></span><strong>{{ store.items.length }}</strong
    ><span>部订阅</span><span class="separator"></span
    ><span class="status-dot" :class="{ muted: !request.data.value?.enabled }"></span
    ><span>{{ request.data.value?.enabled ? '追更已启用' : '追更已暂停或未读取' }}</span
    ><RouterLink class="text-link push-right" to="/media/explore">去发现新剧 →</RouterLink>
  </div>
  <p v-if="request.data.value?.ingest_enabled === false" class="notice-bar">
    入库监控已关闭，新入库事件暂不会触发追更通知。
  </p>
  <p v-if="store.uncertain" class="notice-bar">
    保存结果待核实。<button class="text-link" @click="load">重新读取订阅</button>
  </p>
  <LoadState
    :loading="request.loading.value"
    :error="request.error.value"
    :empty="!request.loading.value && !store.items.length"
    message="还没有关注的剧集"
    @retry="load"
  />
  <div v-if="!request.error.value" class="subscription-grid">
    <article v-for="sub in store.items" :key="sub.id" class="subscription-card">
      <img v-if="posterUrl(sub)" :src="posterUrl(sub)" alt="剧集海报" loading="lazy" />
      <div v-else class="subscription-fallback"><Bell :size="25" /></div>
      <div class="subscription-body">
        <span class="small-status" :class="{ paused: !sub.enabled }">{{
          sub.enabled ? '正在追更' : '已暂停'
        }}</span>
        <h2>{{ sub.name }}</h2>
        <p>
          已入库 <strong>{{ sub.have_eps ?? '—' }}</strong> 集 · TMDB
          {{ sub.tmdb_declared ?? '待同步' }}
        </p>
        <small>最近通知 {{ sub.latest_ep || '尚无记录' }}</small>
        <p v-if="sub.check_error" class="inline-error">{{ sub.check_error }}</p>
        <div class="subscription-actions">
          <button
            class="button small"
            :disabled="store.busy || store.uncertain"
            @click="toggle(sub)"
          >
            <Pause v-if="sub.enabled" :size="14" /><Play v-else :size="14" />{{
              sub.enabled ? '暂停' : '恢复'
            }}</button
          ><button
            class="icon-button danger-text"
            :disabled="store.busy || store.uncertain"
            :aria-label="`移除${sub.name}订阅`"
            @click="removing = sub"
          >
            <Trash2 :size="16" />
          </button>
        </div>
      </div>
    </article>
  </div>
  <ModalDialog v-if="settingsOpen" title="追更设置" @close="settingsOpen = false"
    ><form id="subscription-settings" class="form-stack" @submit.prevent="save">
      <label class="checkbox-label"
        ><input v-model="form.enabled" type="checkbox" />启用追更通知</label
      ><label class="checkbox-label"
        ><input v-model="form.check_tmdb" type="checkbox" />使用 TMDB 已播数据核对缺集</label
      ><label
        >校准间隔（小时）<input
          v-model.number="form.calibration_interval_hours"
          type="number"
          min="1"
          max="168"
          required
      /></label>
      <p class="muted-text">新集通知由入库事件触发；这个间隔用于低频校准。</p>
    </form>
    <template #footer
      ><button
        form="subscription-settings"
        class="button primary"
        :disabled="operation.busy.value || operation.uncertain.value"
      >
        {{ operation.busy.value ? '正在保存…' : '保存设置' }}
      </button></template
    ></ModalDialog
  >
  <ModalDialog v-if="removing" title="移除追更订阅" @close="removing = undefined"
    ><p>确定移除《{{ removing.name }}》的追更订阅？</p>
    <p class="muted-text">移除后将停止该作品的追更提醒。</p>
    <template #footer
      ><button class="button" @click="removing = undefined">返回</button
      ><button class="button danger" :disabled="store.busy || store.uncertain" @click="remove">
        确认移除订阅
      </button></template
    ></ModalDialog
  >
  <ConfirmAction
    v-if="checkOpen"
    title="检查追更订阅"
    description="核对已保存订阅的入库与集数；根据当前通知配置可能发送 Telegram 提醒。"
    :busy="operation.busy.value"
    :disabled="operation.uncertain.value"
    @close="checkOpen = false"
    @confirm="check"
  />
</template>
