<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import { useSubscriptions } from '../stores/subscriptions';
import { useRequest } from '../composables/useRequest';
import { useUi } from '../stores/ui';
import { posterUrl, date } from '../composables/format';
import type { Subscription, SubscriptionsResponse } from '../types/api';
import LoadState from '../components/LoadState.vue';
import ModalDialog from '../components/ModalDialog.vue';
import { useOperation } from '../composables/useOperation';
import { useVisiblePoll } from '../composables/useVisiblePoll';
import { readData, writeData, object, records, text, type Data } from '../api/domain';
import OperationState from '../components/OperationState.vue';

const CHECK_LABELS: Record<string, string> = {
  running: '检查中',
  success: '正常',
  partial: '部分失败',
  error: '失败',
  disabled: '已停用',
  idle: '尚未运行',
};

const operation = useOperation();
const store = useSubscriptions();
const ui = useUi();
const request = useRequest<SubscriptionsResponse>();
const status = useRequest<Data>();
const checkResult = ref<Data>();
const removing = ref<Subscription>();

const form = reactive({ enabled: true, check_tmdb: true, calibration_interval_hours: 6 });

const checkData = computed(() => object(object(status.data.value).check));
const checkState = computed(() => String(checkData.value.status || 'idle'));
const checkLabel = computed(() => CHECK_LABELS[checkState.value] || checkState.value);
const checkTime = computed(() => {
  const ts = checkData.value.finished_at ?? checkData.value.ts ?? checkData.value.started_at;
  return ts ? date(Number(ts)) : '—';
});
const checkResultText = computed(() => {
  const st = checkState.value;
  if (st === 'error' || st === 'partial') {
    const failed = checkData.value.failed;
    return (
      '原因：' +
      text(checkData.value.error || '请查看逐部原因') +
      (failed ? ' · ' + text(failed) + ' 部失败/不完整' : '')
    );
  }
  const checked =
    checkData.value.checked != null ? checkData.value.checked : checkData.value.total || 0;
  return (
    '结果：检查 ' +
    text(checked) +
    ' 部 · ' +
    text(checkData.value.updates || 0) +
    ' 部有变化' +
    (checkData.value.paused ? '（另有 ' + text(checkData.value.paused) + ' 部已暂停，未检查）' : '')
  );
});
const triggerHint = computed(() => {
  const parts: string[] = [];
  const q = object(checkData.value.notification);
  if (checkData.value.ingest_enabled === false)
    parts.push('⚠️ 入库监控已关闭，增量不会触发追更通知；可手动检查或开启监控。');
  if (q.pending)
    parts.push(
      '待发送 ' +
        text(q.pending) +
        ' 批通知' +
        (q.last_error ? '：' + text(q.last_error) : '，后台处理中。'),
    );
  if (q.reconcile_required) parts.push('有未确认的入库变化；队列恢复后自动校准补报，原通知保留。');
  if (q.status === 'unavailable')
    parts.push('通知队列状态不可用：' + text(q.error || '请查看日志'));
  return parts.join(' ');
});

const checkUpdates = computed(() => {
  const r = checkResult.value;
  if (!r)
    return {
      updates: [] as {
        name: string;
        refilled: string[];
        new_eps: string[];
        newly_missing: string[];
      }[],
      failed: [] as { name: string; error: string }[],
      count: 0,
    };
  const updates = records(r.updates).map((u) => ({
    name: text(u.name),
    refilled: records(u.refilled).map(text),
    new_eps: records(u.new_eps).map(text),
    newly_missing: records(u.newly_missing).map(text),
  }));
  const failed = records(r.rows)
    .filter((x) => x.status === 'error' || x.status === 'partial')
    .map((x) => ({ name: text(x.name || ''), error: text(x.error || '读取不完整') }));
  return { updates, failed, count: updates.length + failed.length };
});

async function loadStatus() {
  return status.run((signal) => readData('/api/subscriptions/status', signal));
}
async function load() {
  const value = await request.run((signal) => store.load(signal));
  if (value)
    Object.assign(form, {
      enabled: value.enabled,
      check_tmdb: value.check_tmdb,
      calibration_interval_hours: value.calibration_interval_hours,
    });
}
async function check() {
  const r = await operation.run(() => writeData('/api/subscriptions/check'));
  if (r) {
    checkResult.value = r;
    await Promise.allSettled([load(), loadStatus()]);
  }
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
    ui.notify('追更设置已保存', 'success');
    await load();
  }
}
function factsLabel(sub: Subscription): string {
  if (sub.facts_status === 'ambiguous') return '身份冲突，待人工核对';
  if (sub.facts_status === 'unavailable') return '片库事实待同步';
  if (sub.tmdb_status) return 'TMDB ' + sub.tmdb_status;
  return '片库对照正常';
}
useVisiblePoll(loadStatus, 15000);
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
      class="btn"
      :disabled="operation.busy.value || operation.uncertain.value"
      @click="check"
    >
      立即检查
    </button>
  </section>
  <OperationState
    :busy="operation.busy.value"
    :error="operation.error.value || status.error.value"
    :uncertain="operation.uncertain.value"
    :receipt="operation.receipt.value"
  />
  <div class="panel">
    <div class="row-between">
      <h3>追更订阅</h3>
      <span class="status-badge" :class="request.data.value?.enabled ? 'ok' : 'dim'">{{
        request.data.value?.enabled ? '已启用' : '已停用'
      }}</span>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">启用追更通知</div>
        <div class="desc">
          新集由入库事实更新触发；实时模式使用 Emby 回调，只有已订阅作品才发送通知
        </div>
      </div>
      <label class="switch"
        ><input v-model="form.enabled" type="checkbox" @change="save" /><span class="slider"></span
      ></label>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">对照 TMDB 检查缺集</div>
        <div class="desc">同时报告本地比 TMDB 少的集数</div>
      </div>
      <label class="switch"
        ><input v-model="form.check_tmdb" type="checkbox" @change="save" /><span
          class="slider"
        ></span
      ></label>
    </div>
    <div class="toggle-row">
      <div>
        <div class="lbl">异常校准间隔（小时）</div>
        <div class="desc">
          日常通知由增量事件触发；校准用于 TMDB 已播事实和遗漏修复，建议 6 小时
        </div>
      </div>
      <input
        v-model.number="form.calibration_interval_hours"
        class="num-input"
        type="number"
        min="1"
        max="168"
        @change="save"
      />
    </div>
    <div v-if="triggerHint" class="sub-check-status" style="color: #f59e0b">{{ triggerHint }}</div>
    <div class="sub-check-status">
      <span>检查状态：{{ checkLabel }}</span>
      <span>时间：{{ checkTime }}</span>
      <span>{{ checkResultText }}</span>
    </div>
  </div>

  <div class="panel">
    <div class="row-between">
      <h3>
        订阅列表 <span class="sub-count">{{ store.items.length }} 部</span>
      </h3>
      <div class="desc">从「影视探索」点卡片上的「+ 订阅」</div>
    </div>
    <LoadState
      :loading="request.loading.value"
      :error="request.error.value"
      :empty="!request.loading.value && !store.items.length"
      message="还没有关注的剧集"
      @retry="load"
    />
    <div v-if="!request.error.value && store.items.length" class="sub-list">
      <div v-for="sub in store.items" :key="sub.id" class="sub-item">
        <div class="p">
          <img v-if="posterUrl(sub)" :src="posterUrl(sub)" :alt="sub.name" loading="lazy" />
        </div>
        <div class="body">
          <div class="name">{{ sub.name }}</div>
          <div class="meta">
            当前入库:
            {{
              sub.have_eps != null
                ? sub.have_eps + ' 集'
                : sub.facts_status === 'ambiguous'
                  ? '身份冲突'
                  : '待同步'
            }}
            · TMDB {{ sub.tmdb_declared ?? sub.tmdb_total ?? '待同步'
            }}{{ sub.tmdb_status ? ' · ' + sub.tmdb_status : '' }}
          </div>
          <div class="meta">
            已通知至: <code>{{ sub.latest_ep || '尚无记录' }}</code>
          </div>
          <div class="meta">{{ factsLabel(sub) }}</div>
          <div v-if="sub.check_error" class="meta warn">检查失败/不完整：{{ sub.check_error }}</div>
          <div v-if="!sub.enabled" class="meta warn">⏸ 已暂停：不会检查新集，也不会推送</div>
        </div>
        <div class="actions">
          <label class="switch" :title="sub.enabled ? '追更中，点击暂停' : '已暂停，点击恢复'"
            ><input type="checkbox" :checked="sub.enabled" @change="toggle(sub)" /><span
              class="slider"
            ></span
          ></label>
          <button class="btn red sm" @click="removing = sub">删</button>
        </div>
      </div>
    </div>
  </div>

  <div v-if="checkUpdates.count" class="panel">
    <h3>上次检查 · 变化</h3>
    <div v-for="(u, i) in checkUpdates.updates" :key="'u' + i" class="list-item exempt">
      <div class="t">📺 《{{ u.name }}》</div>
      <div class="d">
        <span v-if="u.refilled.length">✅ 已补齐 {{ u.refilled.join(', ') }}</span>
        <span v-if="u.new_eps.length">🆕 新增 {{ u.new_eps.join(', ') }}</span>
        <span v-if="u.newly_missing.length">⚠️ 新缺 {{ u.newly_missing.join(', ') }}</span>
      </div>
    </div>
    <div v-for="(f, i) in checkUpdates.failed" :key="'f' + i" class="list-item">
      <div class="t">⚠️ 《{{ f.name }}》</div>
      <div class="d">{{ f.error }}</div>
    </div>
  </div>

  <ModalDialog v-if="removing" title="移除追更订阅" @close="removing = undefined">
    <p>确定移除《{{ removing.name }}》的追更订阅？</p>
    <p class="muted-text">移除后将停止该作品的追更提醒。</p>
    <template #footer>
      <button class="btn gray" @click="removing = undefined">返回</button>
      <button class="btn red" :disabled="store.busy || store.uncertain" @click="remove">
        确认移除订阅
      </button>
    </template>
  </ModalDialog>
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
.sub-count {
  font-size: 12px;
  font-weight: 400;
  margin-left: 6px;
  color: var(--text-dim);
}
.desc {
  font-size: 11px;
  color: var(--text-dim);
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
.sub-list {
  display: flex;
  flex-direction: column;
}
.sub-item .meta {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 2px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}
.sub-item .meta code {
  font-family: ui-monospace, Menlo, monospace;
  font-size: 10.5px;
}
.sub-item .meta.warn {
  color: var(--warn);
}
.sub-item .actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
  align-items: center;
}
.sub-check-status {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 10px;
  padding: 9px 11px;
  border-radius: 10px;
  background: var(--bg-dim);
  color: var(--text-dim);
  font-size: 11px;
}
.sub-check-status span:first-child {
  font-weight: 600;
  color: var(--text);
}
</style>
