<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { readData, writeData, object, text } from '../api/domain';
import { useRequest } from '../composables/useRequest';
import { useOperation } from '../composables/useOperation';
import { date } from '../composables/format';
import PageHeading from '../components/PageHeading.vue';
import ConfirmAction from '../components/ConfirmAction.vue';
import OperationState from '../components/OperationState.vue';

const settings = useRequest<unknown>(),
  cache = useRequest<unknown>(),
  op = useOperation(),
  resetOpen = ref(false);
const form = reactive({ enabled: true, interval_min: 5, mode: 'poll', reconcile_hours: 24 });

const cfg = computed(() => object((settings.data.value as Record<string, unknown>)?.settings));
const hook = computed(() => object((settings.data.value as Record<string, unknown>)?.webhook));
const hasToken = computed(() => !!cfg.value.webhook_token);
const webhookUrl = computed(() =>
  cfg.value.webhook_token
    ? window.location.origin +
      '/api/emby/webhook?token=' +
      encodeURIComponent(text(cfg.value.webhook_token))
    : '',
);
const webhookStatus = computed(() => {
  const h = hook.value;
  const received = h.received_at || h.probe_at;
  let s = received ? '最近回调：' + date(Number(received)) : '尚未收到 Emby 回调';
  s += ' · 待处理 ' + text(h.pending || 0);
  if (h.error) s += ' · 等待重试：' + text(h.error);
  if (h.processed_at) s += ' · 最近同步：' + date(Number(h.processed_at));
  return s;
});
const cacheStatus = computed(() => {
  const st = cache.data.value as Record<string, unknown> | undefined;
  if (!st) return '';
  if (!object(st).has_cache) return '⚪ 暂无缓存，等待后台首次刷新';
  const age = Number(object(st).age_sec || 0);
  const stats = object(object(st).stats);
  const ageStr =
    age < 60
      ? age + ' 秒前'
      : age < 3600
        ? Math.floor(age / 60) + ' 分钟前'
        : Math.floor(age / 3600) + ' 小时前';
  return (
    '📦 缓存于 ' +
    ageStr +
    ' · 电影 ' +
    text(stats.movies || 0) +
    ' / 剧集 ' +
    text(stats.series || 0) +
    ' 部 / ' +
    text(stats.episodes || 0) +
    ' 集'
  );
});

async function load() {
  await Promise.allSettled([
    settings.run((signal) => readData('/api/ingest/settings', signal)),
    cache.run((signal) => readData('/api/ingest/status', signal)),
  ]);
  const s = cfg.value;
  form.enabled = !!s.enabled;
  form.interval_min = Number(s.interval_min ?? 5);
  form.mode = text(s.mode || 'poll');
  form.reconcile_hours = Number(s.reconcile_hours ?? 24);
}
async function save() {
  await op.run(() => writeData('/api/ingest/settings', { ...form }));
  await load(); // 保存失败时恢复真实同步模式，避免误以为切换成功
}
async function generate() {
  const r = await op.run(() => writeData('/api/ingest/settings', { generate_token: true }));
  if (r) await load();
}
async function resetToken() {
  const r = await op.run(() =>
    writeData('/api/ingest/settings', { reset_token: true, confirm_reset: 'RESET_WEBHOOK_TOKEN' }),
  );
  if (r) {
    resetOpen.value = false;
    await load();
  }
}
onMounted(load);
</script>

<template>
  <PageHeading
    title="Emby 入库监控"
    subtitle="实时回调同步总览、探索入库角标和片库局部对照；全量校准负责补漏。"
  />
  <OperationState
    :busy="op.busy.value"
    :error="op.error.value || settings.error.value || cache.error.value"
    :uncertain="op.uncertain.value"
    :receipt="op.receipt.value"
  />
  <section class="panel">
    <div class="row-between">
      <h3>入库监控</h3>
      <span class="status-badge" :class="form.enabled ? 'ok' : 'dim'">{{
        form.enabled ? '已启用' : '已停用'
      }}</span>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">启用后台入库监控</div>
        <div class="desc">关闭后停止自动接收入库事件及入库检查</div>
      </div>
      <label class="switch"
        ><input v-model="form.enabled" type="checkbox" @change="save" /><span class="slider"></span
      ></label>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">同步方式</div>
        <div class="desc">先配置并测试 Emby 回调，再切换实时模式；切换时初始化一次全量事实</div>
      </div>
      <select v-model="form.mode" class="form-select" @change="save">
        <option value="poll">定时增量（兼容）</option>
        <option value="webhook">Emby 实时回调</option>
      </select>
    </div>

    <div class="webhook-block">
      <label for="ingWebhookUrl">Emby 回调地址</label>
      <input
        id="ingWebhookUrl"
        :value="webhookUrl"
        type="text"
        readonly
        :placeholder="hasToken ? '' : '尚未生成回调令牌'"
        aria-label="Emby 回调地址"
      />
      <div class="btn-row">
        <button v-if="!hasToken" class="btn gray" :disabled="op.busy.value" @click="generate">
          首次生成回调地址
        </button>
        <button
          v-if="hasToken"
          class="btn red"
          :disabled="op.busy.value || op.uncertain.value"
          @click="resetOpen = true"
        >
          重置回调令牌
        </button>
      </div>
      <p class="hint">
        在 Emby Webhooks
        中填写地址并选择媒体新增、删除事件；这里根据当前网页访问域名显示地址，不会自动修改 Emby
        已填写的局域网地址。重置令牌会让旧回调地址立即失效，必须同步更新 Emby。
      </p>
      <p class="hint" role="status">{{ webhookStatus }}</p>
    </div>

    <div v-if="form.mode !== 'webhook'" class="toggle-row">
      <div>
        <div class="lbl">刷新间隔（分钟）</div>
        <div class="desc">建议 5 分钟</div>
      </div>
      <input
        v-model.number="form.interval_min"
        class="num-input"
        type="number"
        min="1"
        max="1440"
        @change="save"
      />
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">全量兜底间隔（小时）</div>
        <div class="desc">实时模式默认 24 小时，校准 STRM 数量、Emby 事实及 TMDB 对照</div>
      </div>
      <input
        v-model.number="form.reconcile_hours"
        class="num-input"
        type="number"
        min="1"
        max="168"
        @change="save"
      />
    </div>
    <div class="ing-status">{{ cacheStatus }}</div>
  </section>

  <ConfirmAction
    v-if="resetOpen"
    title="重置 Emby 回调令牌"
    description="重置后，原令牌和旧回调地址立即失效。Emby 中原有 Webhook 必须同步替换，否则新增、删除等实时入库事件会停止同步。"
    phrase="重置"
    danger
    :busy="op.busy.value"
    :disabled="op.uncertain.value"
    @close="resetOpen = false"
    @confirm="resetToken"
  />
</template>

<style scoped>
.row-between {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.row-between h3 {
  margin: 0;
  font-size: 17px;
  font-weight: 600;
  color: var(--text);
}
.form-select {
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--border-input);
  background: var(--surface-solid);
  color: var(--text);
  font-size: 13px;
}
.num-input {
  width: 100px;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid var(--border-input);
  background: var(--surface-solid);
  color: var(--text);
  font-size: 13px;
  text-align: center;
}
.webhook-block {
  margin: 12px 0;
}
.webhook-block > label {
  font-size: 12px;
  color: var(--text-mid);
  display: block;
  margin-bottom: 6px;
}
.webhook-block > input {
  margin: 4px 0;
  width: 100%;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--border-input);
  background: var(--surface-solid);
  color: var(--text);
  font-size: 12.5px;
}
.btn-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0;
}
.hint {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.7;
  word-break: break-word;
}
.ing-status {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 8px;
}
</style>
